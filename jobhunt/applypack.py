"""Apply Pack: one page per posting with everything an ATS application form
asks for, assembled from data already in the DB/prompts — no LLM calls.

Sources: the posting row + latest score (for the "why you" angle, via the
same `shortlist._why_you` helper the shortlist page uses), the latest
tailoring/cover/outreach drafts (if `prep` has been run), the standard
SQLite-backed confirmed application answers (the same authoritative store
used by browser-assisted form filling), and PROFILE.md's
Education / work-history sections.

Web (`GET /pack/{posting_id}`) renders sections in-memory only — no file
write. The CLI (`pack` command) is the only thing that writes
`data/apply_packs/{posting_id}-{company-slug}.md`.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from jobhunt import eligibility
from jobhunt.apply_assist import profile_store
from jobhunt.apply_assist.keys import SENSITIVE_KEYS
from jobhunt.rubric import load_rubric
from jobhunt.scoring import load_companies_by_name
from jobhunt.shortlist import _why_you

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ANSWERS_TEMPLATE_PATH = DATA_DIR / "answers.local.template.md"
ANSWERS_FILLED_PATH = DATA_DIR / "answers.local.md"
APPLY_PACKS_DIR = DATA_DIR / "apply_packs"
PROFILE_PATH = Path(__file__).resolve().parent.parent / "PROFILE.md"

RESUME_PATH = f"resume/{eligibility.resume_basename()}_2026_Founder_Engineer.pdf"
"""Fallback resume, used when a posting has no tailored render yet."""


def resume_path_for(
    conn: sqlite3.Connection, posting_id: int, fallback: str | None = RESUME_PATH
) -> str | None:
    """The resume to attach for this posting.

    Prefers the PDF rendered by `jobhunt resume tailor` for this exact posting;
    falls back to the standing resume when there is none (or the recorded file
    has since been removed), so a pack is never left pointing at nothing.

    `fallback` exists for the batch queue, which resolves its own standing resume
    from `resume_variants` and may legitimately have none — passing it through
    keeps per-posting tailoring from silently reintroducing a path the caller
    already decided against.
    """
    from jobhunt.resume.tailor import latest_resume_artifact

    recorded = latest_resume_artifact(conn, posting_id)
    if not recorded:
        return fallback
    candidate = Path(recorded)
    if not candidate.is_absolute():
        candidate = DATA_DIR.parent / recorded
    return recorded if candidate.exists() else fallback


FILL_MARKER = "[FILL"

ANSWER_LABELS: dict[str, str] = {
    "full_name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "location": "Location",
    "linkedin": "LinkedIn",
    "github": "GitHub",
    "website": "Portfolio / website",
    "work_authorization": "Work authorization",
    "start_date": "Earliest start date",
    "salary_expectation": "Salary expectation",
    "willing_to_relocate": "Willing to relocate",
    "degree": "Degree",
    "how_heard": "How did you hear about us",
}


# --------------------------------------------------------------- answers


def _parse_answers_file(path: Path) -> dict[str, str]:
    answers: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        answers[key.strip()] = value.strip()
    return answers


def load_answers(
    filled_path: Path = ANSWERS_FILLED_PATH,
    template_path: Path = ANSWERS_TEMPLATE_PATH,
) -> dict:
    """Load standard application answers. Prefers the filled copy
    (`data/answers.local.md`, gitignored) if it exists; otherwise falls
    back to the committed template and flags every still-[FILL] entry.

    Returns {"answers": {key: value}, "source": "filled" | "template",
    "unfilled": [key, ...]}.
    """
    if filled_path.exists():
        source = "filled"
        answers = _parse_answers_file(filled_path)
    else:
        source = "template"
        answers = _parse_answers_file(template_path)
    unfilled = [key for key, value in answers.items() if FILL_MARKER in value]
    return {"answers": answers, "source": source, "unfilled": unfilled}


def load_stored_answers(conn: sqlite3.Connection) -> dict:
    """Load the single operational answer source used by apply packs and forms.

    ``load_answers`` remains only as a one-way legacy import/parser surface for
    existing local files and its compatibility tests.
    """
    answers = profile_store.get_answers(conn)
    return {"answers": answers, "source": "database", "unfilled": []}


def _render_answers_block(answers_result: dict) -> str:
    answers = answers_result["answers"]
    lines = []
    for key, label in ANSWER_LABELS.items():
        value = answers.get(key, FILL_MARKER + "]")
        lines.append(f"- {label}: {value}")
    # Any keys in the file that aren't in ANSWER_LABELS still get surfaced,
    # so a hand-edited answers file never silently drops a field.
    for key, value in answers.items():
        if key not in ANSWER_LABELS and key not in SENSITIVE_KEYS:
            lines.append(f"- {key}: {value}")
    if answers_result["source"] == "template":
        lines.insert(
            0,
            "_No data/answers.local.md yet — showing the template. "
            "Copy data/answers.local.template.md to data/answers.local.md and fill it in._",
        )
        lines.insert(1, "")
    return "\n".join(lines)


# --------------------------------------------------------------- DB reads


def _fetch_posting(conn: sqlite3.Connection, posting_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if row is None:
        raise ValueError(f"no posting with id={posting_id}")
    return row


def _fetch_latest_score(conn: sqlite3.Connection, posting_id: int) -> sqlite3.Row | None:
    """Score row (joined with the posting fields `_why_you` needs) for the
    most recent rubric version, or None if never scored."""
    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    if rubric_version is None:
        return None
    return conn.execute(
        """
        SELECT s.*, p.company, p.title, p.source, p.raw_json
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        WHERE s.posting_id = ? AND s.rubric_version = ?
        """,
        (posting_id, rubric_version),
    ).fetchone()


def _latest_draft(conn: sqlite3.Connection, posting_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM drafts WHERE posting_id=? AND kind=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (posting_id, kind),
    ).fetchone()


def _why_you_angle(conn: sqlite3.Connection, posting_row: sqlite3.Row, posting_id: int) -> str:
    score_row = _fetch_latest_score(conn, posting_id)
    if score_row is None or score_row["excluded"]:
        return "(not scored yet)"
    rubric = load_rubric()
    companies = load_companies_by_name(conn)
    company_row = companies.get(posting_row["company"]) if posting_row["company"] else None
    return _why_you(score_row, company_row, rubric)


# --------------------------------------------------------------- extraction helpers


# A paragraph that is nothing but a greeting line ("Dear Hiring Manager," /
# "Hi Anna,"). Local models often emit the salutation as its own paragraph.
_SALUTATION_ONLY_RE = re.compile(
    r"^(?:dear|hi|hello|hey|greetings|to whom|to the)\b[\w\s.'&-]*[,:]?$",
    re.IGNORECASE,
)


def _first_paragraph(content: str) -> str:
    paragraphs = [p.strip() for p in content.strip().split("\n\n") if p.strip()]
    for para in paragraphs:
        if "\n" not in para and _SALUTATION_ONLY_RE.match(para):
            continue
        return para
    return paragraphs[0] if paragraphs else ""


_POSITIONING_ANGLE_RE = re.compile(r"positioning angle:?\s*(.+)", re.IGNORECASE)

# Captures that carry no content: punctuation/markdown residue like ":" or "**"
# left over when the header and its content sit on different lines.
_JUNK_CAPTURE_RE = re.compile(r"^[\s:;#*_`>.=\-]*$")


def _positioning_angle_from_tailoring(content: str) -> str | None:
    """Pull the "Positioning angle: ..." line out of a tailoring draft
    (section 4 of jobhunt/prompts/tailoring.md's output format).

    Local models sometimes render it as a markdown header ("## Positioning
    angle:") with the content on the following line, so a junk-only capture
    falls through to the next substantive line before the next section."""
    lines = content.splitlines()
    for i, line in enumerate(lines):
        match = _POSITIONING_ANGLE_RE.search(line)
        if not match:
            continue
        captured = match.group(1).strip()
        if captured and not _JUNK_CAPTURE_RE.match(captured):
            return captured
        for next_line in lines[i + 1 :]:
            candidate = next_line.strip()
            if candidate.startswith("#"):
                break
            if candidate and not _JUNK_CAPTURE_RE.match(candidate):
                return candidate
    return None


_PROFILE_SECTION_RE_TEMPLATE = r"^## {header}\s*\n(.*?)(?=\n## |\Z)"


def _extract_profile_section(profile_text: str, header: str) -> str:
    pattern = re.compile(
        _PROFILE_SECTION_RE_TEMPLATE.format(header=re.escape(header)),
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(profile_text)
    return match.group(1).strip() if match else ""


def _clean_profile_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[-*]\s*", "", line)
    line = line.replace("**", "")
    return line


def _profile_positioning_one_liner(profile_text: str) -> str:
    section = _extract_profile_section(profile_text, "Positioning (the one-liner)")
    if not section:
        return "(see PROFILE.md — Positioning section)"
    first_line = section.splitlines()[0]
    return _clean_profile_line(first_line)


def _education_block(profile_text: str) -> str:
    section = _extract_profile_section(profile_text, "Education")
    lines = [_clean_profile_line(line) for line in section.splitlines() if line.strip()]
    if not lines:
        return "(no Education section found in PROFILE.md)"
    return "\n".join(f"- {line}" for line in lines)


def _extract_profile_section_by_prefix(profile_text: str, prefix: str) -> tuple[str, str]:
    """(header, body) of the first `## ` section whose header starts with `prefix`
    (case-insensitive); ("", "") when none does."""
    for match in re.finditer(r"^## (.+?)\s*$", profile_text, re.MULTILINE):
        header = match.group(1).strip()
        if header.casefold().startswith(prefix.casefold()):
            return header, _extract_profile_section(profile_text, header)
    return "", ""


def _work_history_block(profile_text: str) -> str:
    # The headline venture/role section ("## The differentiator ...") and the remaining work
    # history ("## Work history ...") are matched by header prefix, so the profile owner names them.
    venture_header, venture_section = _extract_profile_section_by_prefix(
        profile_text, "The differentiator"
    )
    _, beyond_section = _extract_profile_section_by_prefix(profile_text, "Work history")

    lines = [venture_header or "Founder"]
    lines.extend(
        f"  {_clean_profile_line(line)}" for line in venture_section.splitlines() if line.strip()
    )
    lines.append("")
    lines.extend(_clean_profile_line(line) for line in beyond_section.splitlines() if line.strip())
    if not venture_section and not beyond_section:
        return "(no work-history sections found in PROFILE.md)"
    return "\n".join(lines).strip()


# --------------------------------------------------------------- assembly


def _slugify(company: str | None) -> str:
    text = (company or "unknown").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def _posting_block(posting_row: sqlite3.Row, angle: str) -> str:
    return (
        f"- Company: {posting_row['company'] or '(unknown)'}\n"
        f"- Title: {posting_row['title'] or '(untitled)'}\n"
        f"- Location: {posting_row['location'] or '(n/a)'}\n"
        f"- URL: {posting_row['url'] or '(n/a)'}\n"
        f"- Why this one: {angle}"
    )


def _attachments_block(cover_draft: sqlite3.Row | None, resume_path: str = RESUME_PATH) -> str:
    lines = [f"- Resume: {resume_path}"]
    if cover_draft is not None:
        lines.append(
            "- Cover letter: a cover draft exists below (Short Answers) — "
            "save it as a file if the ATS wants a cover-letter upload rather than a text field."
        )
    else:
        lines.append("- Cover letter: no cover draft yet — none to attach.")
    return "\n".join(lines)


def _short_answers_block(
    posting_row: sqlite3.Row,
    posting_id: int,
    cover_draft: sqlite3.Row | None,
    tailoring_draft: sqlite3.Row | None,
    outreach_draft: sqlite3.Row | None,
    profile_text: str,
) -> str:
    company = posting_row["company"] or "the company"

    if cover_draft is not None:
        why_company = _first_paragraph(cover_draft["content"])
    else:
        why_company = f"[no cover draft yet — run `prep {posting_id}`]"

    positioning = None
    if tailoring_draft is not None:
        positioning = _positioning_angle_from_tailoring(tailoring_draft["content"])
    if not positioning:
        positioning = _profile_positioning_one_liner(profile_text)

    if outreach_draft is not None:
        outreach_note = (
            "Draft available (outreach) — reuse for any optional "
            f'"message to the recruiter/hiring manager" field:\n\n  {outreach_draft["content"].strip()}'
        )
    else:
        outreach_note = f"[no outreach draft yet — run `prep {posting_id}`]"

    return (
        f"**Why {company}?**\n{why_company}\n\n"
        f"**Tell us about yourself**\n{positioning}\n\n"
        f"**Optional message / how did you hear (if the form asks)**\n{outreach_note}"
    )


def _checklist_block(posting_id: int, resume_path: str = RESUME_PATH) -> str:
    return (
        f"- [ ] Attach resume ({resume_path})\n"
        "- [ ] Paste Standard Answers above into the matching form fields\n"
        "- [ ] Fill any remaining [FILL] placeholders before submitting\n"
        "- [ ] Check for ATS auto-fill leftovers (LinkedIn/resume-parse can mis-fill fields)\n"
        f"- [ ] After submitting: `uv run python -m jobhunt move {posting_id} applied`"
    )


def build_pack_sections(conn: sqlite3.Connection, posting_id: int) -> tuple[sqlite3.Row, list[dict]]:
    """Assemble the pack as (posting_row, [{"title": ..., "content": ...}]) —
    shared by build_pack() (joins to markdown) and the web route (renders
    per-section with Copy buttons). Raises ValueError if the posting
    doesn't exist."""
    posting_row = _fetch_posting(conn, posting_id)
    profile_path = PROFILE_PATH if PROFILE_PATH.exists() else PROFILE_PATH.with_name("PROFILE.example.md")
    profile_text = profile_path.read_text(encoding="utf-8")

    cover_draft = _latest_draft(conn, posting_id, "cover")
    tailoring_draft = _latest_draft(conn, posting_id, "tailoring")
    outreach_draft = _latest_draft(conn, posting_id, "outreach")

    angle = _why_you_angle(conn, posting_row, posting_id)
    answers_result = load_stored_answers(conn)
    resume_path = resume_path_for(conn, posting_id)

    sections = [
        {"key": "posting", "title": "Posting", "content": _posting_block(posting_row, angle)},
        {
            "key": "attachments",
            "title": "Attachments",
            "content": _attachments_block(cover_draft, resume_path),
        },
        {
            "key": "standard_answers",
            "title": "Standard Answers",
            "content": _render_answers_block(answers_result),
        },
        {
            "key": "short_answers",
            "title": "Short Answers",
            "content": _short_answers_block(
                posting_row, posting_id, cover_draft, tailoring_draft, outreach_draft, profile_text
            ),
        },
        {"key": "education", "title": "Education", "content": _education_block(profile_text)},
        {"key": "work_history", "title": "Work History", "content": _work_history_block(profile_text)},
        {
            "key": "checklist",
            "title": "Checklist",
            "content": _checklist_block(posting_id, resume_path),
        },
    ]
    return posting_row, sections


def build_pack(conn: sqlite3.Connection, posting_id: int) -> str:
    """The Apply Pack as a single markdown document."""
    posting_row, sections = build_pack_sections(conn, posting_id)
    header = (
        f"# Apply Pack — [{posting_id}] {posting_row['company'] or '(unknown)'} "
        f"— {posting_row['title'] or '(untitled)'}\n"
    )
    body = "\n\n".join(f"## {s['title']}\n\n{s['content']}" for s in sections)
    return header + "\n" + body + "\n"


def fill_markers(markdown: str) -> list[str]:
    """Every line in the assembled pack that still contains a `[FILL`
    marker — backs the web page's red "still unfilled" banner."""
    return [line.strip() for line in markdown.splitlines() if FILL_MARKER in line]


def write_pack(conn: sqlite3.Connection, posting_id: int) -> Path:
    """Build the pack and write it to
    data/apply_packs/{posting_id}-{company-slug}.md. CLI-only side effect —
    the web route never writes files (see module docstring)."""
    posting_row = _fetch_posting(conn, posting_id)
    markdown = build_pack(conn, posting_id)
    APPLY_PACKS_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(posting_row["company"])
    path = APPLY_PACKS_DIR / f"{posting_id}-{slug}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
