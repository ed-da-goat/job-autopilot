"""SimplifyJobs New-Grad-Positions README source.

GET https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md

The README's job tables have shipped as either a markdown pipe-table or a
raw HTML `<table>` (the live format as of 2026-07 is HTML). Both are
parsed best-effort; genuinely unparseable rows are skipped and counted
rather than crashing the sweep. Closed/withdrawn postings — marked with a
bare 🔒 in the Application cell instead of a link — are also skipped but
are NOT counted toward the unparseable-row warning, since they parsed
fine and simply aren't open jobs.
"""

from __future__ import annotations

import hashlib
import re

from jobhunt.net import FetchError, polite_get

URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md"

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"</?br\s*/?>", re.IGNORECASE)
_BOLD_RE = re.compile(r"\*\*")
_LINK_URL_RE = re.compile(r'\((https?://[^)\s]+)\)|href="(https?://[^"]+)"')
_CONTINUATION_MARKER = "↳"
_CLOSED_MARKER = "🔒"

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th[^>]*>", re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)


def _clean_text(cell: str) -> str:
    text = _BR_RE.sub(", ", cell)
    text = _TAG_RE.sub("", text)
    text = _BOLD_RE.sub("", text)
    return text.strip()


def _is_closed_application(raw_cell: str) -> bool:
    """The README marks a closed/withdrawn posting by replacing the whole
    Application cell with a single 🔒 (no link at all). That's a legitimate
    "not currently a job" row, not a parse failure — it must not count
    toward the unparseable-row warning."""
    return _CLOSED_MARKER in raw_cell and _extract_url(raw_cell) is None


def _extract_url(cell: str) -> str | None:
    m = _LINK_URL_RE.search(cell)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _resolve_company(raw_company: str, last_company: str | None) -> str | None:
    company_text = _clean_text(raw_company)
    if company_text in ("", _CONTINUATION_MARKER):
        return last_company
    return company_text


def _make_posting(company: str, role: str, location: str, link: str, posted_at: str | None, raw: dict) -> dict:
    digest = hashlib.sha1(f"{company}|{role}|{link}".encode(), usedforsecurity=False)
    return {
        "source_posting_id": digest.hexdigest(),
        "company": company,
        "title": role,
        "location": location or None,
        "url": link,
        "posted_at": posted_at,
        "raw": raw,
    }


def _split_pipe_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c.replace(" ", "")) <= {"-", ":"} and c.strip() != "" for c in cells)


def _parse_markdown_pipe_rows(text: str) -> tuple[list[dict], int]:
    postings: list[dict] = []
    skipped = 0
    last_company: str | None = None
    seen_header = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _split_pipe_row(stripped)
        if len(cells) < 4:
            continue
        if _is_separator_row(cells):
            continue
        if not seen_header and _clean_text(cells[0]).lower() in ("company", ""):
            seen_header = True
            continue

        raw_company, raw_role, raw_location, raw_link = cells[0], cells[1], cells[2], cells[3]
        raw_date = cells[4] if len(cells) > 4 else ""

        company = _resolve_company(raw_company, last_company)
        if company:
            last_company = company
        role = _clean_text(raw_role)
        location = _clean_text(raw_location)
        link = _extract_url(raw_link)
        date_posted = _clean_text(raw_date) or None

        if link is None and _is_closed_application(raw_link):
            continue  # closed posting — not a parse failure

        if not company or not role or not link:
            skipped += 1
            continue

        postings.append(
            _make_posting(
                company,
                role,
                location,
                link,
                date_posted,
                {"company": raw_company, "role": raw_role, "location": raw_location, "link": raw_link},
            )
        )

    return postings, skipped


def _parse_html_table_rows(text: str) -> tuple[list[dict], int]:
    postings: list[dict] = []
    skipped = 0
    last_company: str | None = None

    for tr_match in _TR_RE.finditer(text):
        block = tr_match.group(1)
        if _TH_RE.search(block):
            continue  # header row
        cells = _TD_RE.findall(block)
        if len(cells) < 4:
            continue

        raw_company, raw_role, raw_location, raw_app = cells[0], cells[1], cells[2], cells[3]
        raw_age = cells[4] if len(cells) > 4 else ""

        company = _resolve_company(raw_company, last_company)
        if company:
            last_company = company
        role = _clean_text(raw_role)
        location = _clean_text(raw_location)
        link = _extract_url(raw_app)
        date_posted = _clean_text(raw_age) or None

        if link is None and _is_closed_application(raw_app):
            continue  # closed posting — not a parse failure

        if not company or not role or not link:
            skipped += 1
            continue

        postings.append(
            _make_posting(
                company,
                role,
                location,
                link,
                date_posted,
                {"company": raw_company, "role": raw_role, "location": raw_location, "link": raw_app},
            )
        )

    return postings, skipped


def parse_simplify_markdown(text: str) -> tuple[list[dict], int]:
    """Parse the README's job table(s), tolerating either markdown pipe-tables
    or raw HTML <table> markup (or both in the same document). Returns
    (postings, skipped_row_count)."""
    html_postings, html_skipped = _parse_html_table_rows(text)
    md_postings, md_skipped = _parse_markdown_pipe_rows(text)

    seen_ids: set[str] = set()
    combined: list[dict] = []
    for posting in html_postings + md_postings:
        spid = posting["source_posting_id"]
        if spid in seen_ids:
            continue
        seen_ids.add(spid)
        combined.append(posting)

    return combined, html_skipped + md_skipped


def fetch() -> list[dict]:
    resp = polite_get(URL)
    if resp.status_code != 200:
        raise FetchError(f"simplify: HTTP {resp.status_code}", resp.status_code)
    postings, skipped = parse_simplify_markdown(resp.text)
    if skipped:
        print(f"WARNING: simplify: skipped {skipped} unparseable row(s)")
    return postings
