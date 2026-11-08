"""Deterministic resume .docx renderer.

Takes Resume Matcher (RM) resume JSON and rewrites the body of the master
.docx's ``word/document.xml``; every other zip part is copied unchanged. No LLM
calls. ``fact_gate`` runs before anything is written.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as esc
from xml.sax.saxutils import unescape

from jobhunt.resume.render import OUTPUT_DIR
from jobhunt.resume.validate import scan_banned_phrases

SOFFICE = shutil.which("soffice") or "/opt/homebrew/bin/soffice"
_F = '<w:rFonts w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi" w:cstheme="minorHAnsi"/>'
_RIGHT = (
    '<w:tabs><w:tab w:val="clear" w:pos="1440"/><w:tab w:val="clear" w:pos="6480"/>'
    '<w:tab w:val="right" w:pos="10800"/></w:tabs>'
)
_NUM_RE = re.compile(r"\d[\d,]*\+?%?")


# ---- XML helpers (byte-for-byte port of the reference build script) ----
def rpr(sz: int = 21, b: bool = False) -> str:
    bold = "<w:b/><w:bCs/>" if b else ""
    return f'<w:rPr>{_F}{bold}<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>'


def run(t: str, sz: int = 21, b: bool = False) -> str:
    return f'<w:r>{rpr(sz, b)}<w:t xml:space="preserve">{esc(t)}</w:t></w:r>'


def tab(sz: int = 21, b: bool = False) -> str:
    return f"<w:r>{rpr(sz, b)}<w:tab/></w:r>"


def p(runs: str, ppr: str = "", sz: int = 21, b: bool = False) -> str:
    return f"<w:p><w:pPr>{ppr}{rpr(sz, b)}</w:pPr>{runs}</w:p>"


def name(t: str) -> str:
    return p(run(t, 28, True), '<w:jc w:val="center"/>', 28, True)


def contact(t: str) -> str:
    return p(run(t), '<w:jc w:val="center"/>')


def section(t: str) -> str:
    return p(
        run(t, 26, True),
        '<w:pBdr><w:bottom w:val="single" w:sz="4" w:space="1" w:color="auto"/></w:pBdr>'
        '<w:spacing w:after="80"/>',
        26,
        True,
    )


def two_col(left: str, right: str, b: bool = False, left_runs: str | None = None) -> str:
    return p((left_runs or run(left, b=b)) + tab(b=b) + run(right, b=b), _RIGHT, b=b)


def bullet(t: str, num: int) -> str:
    ind = '<w:ind w:left="360"/>' if num == 21 else ""
    return p(
        run(t),
        f'<w:pStyle w:val="ListParagraph"/><w:numPr><w:ilvl w:val="0"/>'
        f'<w:numId w:val="{num}"/></w:numPr>{ind}',
    )


def blank() -> str:
    return p("")


def skill(label: str, t: str) -> str:
    return p(
        run(label + ":", b=True) + tab(b=True) + run(t),
        '<w:ind w:left="1412" w:hanging="1410"/>',
    )


# ---- template parsing ----
def _paragraph_texts(xml: str) -> list[str]:
    out = []
    for para in re.findall(r"<w:p(?:\s[^>]*)?>.*?</w:p>", xml, re.S):
        parts = []
        for m in re.finditer(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>|<w:tab/>", para, re.S):
            parts.append("\t" if m.group(0) == "<w:tab/>" else unescape(m.group(1)))
        out.append("".join(parts))
    return out


def _read_document_xml(template: Path) -> str:
    with zipfile.ZipFile(template) as z:
        return z.read("word/document.xml").decode("utf-8")


def split_items(s: str) -> list[str]:
    """Split on ', ' that is not inside parentheses."""
    items, depth, cur, i = [], 0, "", 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and s.startswith(", ", i):
            items.append(cur)
            cur, i = "", i + 2
            continue
        cur += ch
        i += 1
    if cur:
        items.append(cur)
    return items


def template_skills(texts: list[str]) -> list[tuple[str, list[str]]]:
    """Ordered (label, items) from 'Label:<tab>items' paragraphs after the SKILLS heading."""
    start = next((i for i, t in enumerate(texts) if t.strip() == "SKILLS/INTERESTS"), None)
    if start is None:
        raise ValueError("template has no SKILLS/INTERESTS heading")
    cats = []
    for t in texts[start + 1:]:
        if "\t" in t and ":" in t.split("\t", 1)[0]:
            label, items = t.split("\t", 1)
            cats.append((label.rstrip(":").strip(), split_items(items)))
    if not cats:
        raise ValueError("template has no skill paragraphs")
    return cats


# ---- content ----
def _d(s: str) -> str:
    return s.replace(" - ", " – ")


def _unwrap(data: dict) -> dict:
    return data["resume_data"] if "resume_data" in data else data


def _reorder(cats, tailored: list[str], awards: list[str]):
    known = {i for _, items in cats for i in items}
    unknown = [s for s in tailored if s not in known]
    if unknown:
        raise ValueError(f"skills not in template categories: {unknown}")
    out = []
    for label, items in cats:
        if label == "Honors" and awards:
            out.append((label, list(awards)))
            continue
        first = [s for s in tailored if s in items]
        out.append((label, first + [s for s in items if s not in first]))
    return out


def build_body(resume: dict, template_xml: str) -> tuple[str, list[str]]:
    """Return (body paragraphs xml, list of paragraph texts as rendered)."""
    r = _unwrap(resume)
    if (r.get("summary") or "").strip():
        raise ValueError("summary is not supported yet (not approved)")
    if r.get("personalProjects"):
        raise ValueError("personalProjects is not supported yet")
    pi = r["personalInfo"]
    loc = pi.get("location", "")
    add = r.get("additional") or {}
    cats = _reorder(
        template_skills(_paragraph_texts(template_xml)),
        add.get("technicalSkills") or [],
        add.get("awards") or [],
    )

    body: list[str] = []
    body.append(name(pi["name"].upper()))
    body.append(contact(" | ".join(x for x in (loc, pi.get("phone"), pi.get("email"),
                                               pi.get("linkedin"), pi.get("website")) if x)))
    body.append(section("EDUCATION"))
    for ed in r.get("education", []):
        inst = ed["institution"]
        head, sep, rest = inst.partition(",")
        body.append(two_col(head.upper() + sep + rest, loc, b=True))
        parts = ed["degree"].split(", ")
        gpa = [x for x in parts[1:] if x.startswith("GPA:")]
        others = [x for x in parts[1:] if not x.startswith("GPA:")]
        lr = run(parts[0], b=True) + "".join(run(", " + g) for g in gpa)
        body.append(two_col("", _d(ed.get("years", "")), left_runs=lr))
        desc = [x for x in (ed.get("description") or "").split("; ") if x]
        for t in others + desc:
            body.append(bullet(_d(t), 21))
        body.append(blank())
    body.append(section("EXPERIENCE"))
    for job in r.get("workExperience", []):
        body.append(two_col(job["company"].upper(), job.get("location", ""), b=True))
        body.append(two_col(job["title"], _d(job.get("years", ""))))
        body += [bullet(_d(t), 26) for t in job.get("description", [])]
        body.append(blank())
    body.append(section("SKILLS/INTERESTS"))
    for label, items in cats:
        body.append(skill(label, ", ".join(items)))
    return "".join(body), _paragraph_texts("".join(body))


# ---- fact gate ----
def _nums(text: str) -> set[str]:
    return {m.rstrip(",") for m in _NUM_RE.findall(text)}


def fact_gate(tailored_text: str, master_text: str, extra_corpus: str = "") -> list[str]:
    """Problems with `tailored_text`. A number is allowed when it appears in
    `master_text` or in `extra_corpus` (e.g. data/facts.local.md)."""
    problems = [f"banned phrase: {h}" for h in scan_banned_phrases(tailored_text)]
    new = sorted(_nums(tailored_text) - _nums(master_text) - _nums(extra_corpus))
    problems += [f"number not in master: {n}" for n in new]
    return problems


def render(resume: dict, template: Path, out: Path, extra_corpus: str = "") -> tuple[Path, list[str]]:
    """Write the docx. Returns (path, texts). Raises ValueError if the fact gate fails."""
    xml = _read_document_xml(template)
    body, texts = build_body(resume, xml)
    master_text = "\n".join(_paragraph_texts(xml))
    problems = fact_gate("\n".join(texts), master_text, extra_corpus)
    if problems:
        raise ValueError("fact gate failed: " + "; ".join(problems))
    head = xml[: xml.find("<w:body>") + 8]
    sect = re.search(r"<w:sectPr.*?</w:sectPr>", xml, re.S).group(0)
    new_xml = head + body + sect + "</w:body></w:document>"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = new_xml.encode("utf-8")
            zout.writestr(item, data)
    return out, texts


# ---- PDF ----
# Word is sandboxed: paths outside its container raise a modal "Grant File Access"
# dialog that blocks automation, so it only ever reads and writes inside this dir.
WORD_TMP = Path.home() / "Library/Containers/com.microsoft.Word/Data/tmp"

_WORD_SCRIPT = """
on run argv
    set src to item 1 of argv
    set dst to item 2 of argv
    set nm to item 3 of argv
    with timeout of 90 seconds
        tell application "Microsoft Word"
            open (POSIX file src)
            set d to document nm
            save as d file name dst file format format PDF
            try
                close d saving no
            end try
        end tell
    end timeout
end run
"""


def _soffice_to_pdf(docx: Path, out_dir: Path) -> Path:
    subprocess.run(
        [SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx)],
        check=True, capture_output=True, timeout=180,
    )
    return out_dir / (docx.stem + ".pdf")


def _word_to_pdf(docx: Path, out_dir: Path) -> Path:
    WORD_TMP.mkdir(parents=True, exist_ok=True)
    nm = f"docx_resume-{uuid.uuid4().hex[:12]}.docx"
    src, dst = WORD_TMP / nm, WORD_TMP / (Path(nm).stem + ".pdf")
    shutil.copyfile(docx, src)
    try:
        subprocess.run(["osascript", "-e", _WORD_SCRIPT, str(src), str(dst), nm],
                       check=True, capture_output=True, timeout=120)
        if not dst.exists():
            raise RuntimeError(f"Word produced no {dst}")
        pdf = out_dir / (docx.stem + ".pdf")
        shutil.copyfile(dst, pdf)
        return pdf
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


WORD_APP = Path("/Applications/Microsoft Word.app")
NO_CONVERTER_HELP = (
    "no .docx to PDF converter works here: install LibreOffice "
    "(macOS: `brew install --cask libreoffice`; Linux: your package manager) or Microsoft Word"
)


class PdfConverterMissing(RuntimeError):
    """Neither LibreOffice nor Word can convert here; no tailored resume PDF can be made."""


def pdf_converter() -> str | None:
    """Name of a converter that exists ("libreoffice" or "word"), else None. Probes
    `soffice --version` rather than trusting `which`: a Homebrew cask whose app was deleted
    leaves a symlink that is on PATH and still fails on use."""
    soffice = shutil.which("soffice") or SOFFICE
    if Path(soffice).exists():
        try:
            probe = subprocess.run([soffice, "--version"], capture_output=True, timeout=60)
            if probe.returncode == 0:
                return "libreoffice"
        except (OSError, subprocess.TimeoutExpired):
            pass
    if WORD_APP.exists():
        return "word"
    return None


def require_pdf_converter() -> str:
    found = pdf_converter()
    if found is None:
        raise PdfConverterMissing(NO_CONVERTER_HELP)
    return found


def docx_to_pdf(docx: Path, out_dir: Path) -> Path:
    """LibreOffice if it works, else Microsoft Word via AppleScript."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pdf = _soffice_to_pdf(docx, out_dir)
        if pdf.exists():
            return pdf
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return _word_to_pdf(docx, out_dir)


def pdf_page_count(path: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", Path(path).read_bytes()))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "resume"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render RM resume JSON to .docx")
    ap.add_argument("--resume-json", required=True, type=Path)
    ap.add_argument("--template", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--pdf", action="store_true")
    a = ap.parse_args(argv)
    out = a.out or OUTPUT_DIR / f"docx_resume-{_slug(a.resume_json.stem)}.docx"
    try:
        resume = json.loads(a.resume_json.read_text(encoding="utf-8"))
        out, texts = render(resume, a.template, out)
    except (ValueError, KeyError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"wrote {out} ({len(texts)} paragraphs)")
    if a.pdf:
        try:
            pdf = docx_to_pdf(out, out.parent)
        except (subprocess.CalledProcessError, FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"refused: PDF conversion failed ({type(e).__name__}); needs LibreOffice or Microsoft Word", file=sys.stderr)
            return 3
        pages = pdf_page_count(pdf)
        print(f"pdf {pdf} pages={pages}")
        if pages != 1:
            print("refused: resume is not exactly 1 page", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
