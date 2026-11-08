"""Build examples/resume.example.docx, the Alex Rivera persona resume template.

The file is a real .docx written with zipfile and raw WordprocessingML (python-docx is
not a dependency). Its body is produced by the same builder the tailoring step uses
(`jobhunt.resume.docx_resume.build_body`) from examples/master.example.rm.json, so it has
the layout the parser requires: a "SKILLS/INTERESTS" heading followed by
"Label:<tab>items" paragraphs, and bullets on numbering ids 21 and 26.

Run from the repo root:  uv run python scripts/make_example_resume.py
Output is byte-stable: the same inputs always produce the same file.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from jobhunt.resume import docx_resume

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "examples" / "master.example.rm.json"
OUT = ROOT / "examples" / "resume.example.docx"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEAD = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>'
# US Letter, 0.5in margins: the 10800-twip text width matches the renderer's right tab stop.
SECT = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
    '<w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720" w:header="360" w:footer="360" w:gutter="0"/>'
    "</w:sectPr>"
)

# (label, items). The tailoring step reorders items inside a category and refuses skills
# that are not listed here, so list every skill the example candidate may claim.
SKILLS = [
    ("Technical", ["Python", "SQL", "Excel (pivot tables, macros)", "TypeScript", "Git"]),
    ("Business", ["Financial reporting", "Account reconciliation", "Process documentation"]),
    ("Honors", ["Dean's List", "Study abroad scholarship"]),
    ("Interests", ["Trail running", "Chess", "Community volunteering"]),
]

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
    "</Relationships>"
)
STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W}">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="21"/><w:szCs w:val="21"/>'
    '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
    '<w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="720"/></w:pPr></w:style>'
    "</w:styles>"
)


def _numbering() -> str:
    """Bullet lists for numIds 21 (education) and 26 (experience), the ids the renderer uses."""
    abstract = "".join(
        f'<w:abstractNum w:abstractNumId="{i}"><w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/>'
        '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
        for i in (21, 26)
    )
    nums = "".join(
        f'<w:num w:numId="{i}"><w:abstractNumId w:val="{i}"/></w:num>' for i in (21, 26)
    )
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="{W}">{abstract}{nums}</w:numbering>'


def document_xml(master: dict) -> str:
    # build_body only needs the skills from the template, so give it a skills-only skeleton.
    skeleton = (
        HEAD
        + docx_resume.section("SKILLS/INTERESTS")
        + "".join(docx_resume.skill(label, ", ".join(items)) for label, items in SKILLS)
        + SECT
        + "</w:body></w:document>"
    )
    body, _texts = docx_resume.build_body(master, skeleton)
    return HEAD + body + SECT + "</w:body></w:document>"


def build(out: Path = OUT, master_path: Path = MASTER) -> Path:
    master = json.loads(master_path.read_text(encoding="utf-8"))
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": ROOT_RELS,
        "word/document.xml": document_xml(master),
        "word/_rels/document.xml.rels": DOC_RELS,
        "word/styles.xml": STYLES,
        "word/numbering.xml": _numbering(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))  # fixed: byte-stable output
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode("utf-8"))
    return out


if __name__ == "__main__":
    print(f"wrote {build()}")
