"""zapplyjobs New-Grad-Jobs-2027 README source.

GET https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md

Six markdown pipe-tables (Software Engineering; Data, AI & Research; Security;
Hardware & Systems; Business & Operations; Other), all with the same layout:

    | Company | Role | Location | Posted | Visa | **Apply** |

The Apply cell is an image anchor `[<img ...>](https://...)` pointing at the
direct ATS posting (Workday/Greenhouse/etc.), not a tracked redirect. Rows are
parsed best-effort; genuinely unparseable rows are skipped and counted rather
than crashing the sweep. The Visa cell ("✅ Sponsor" / "🏛 H-1B Co." / empty)
is preserved in raw for downstream use.
"""

from __future__ import annotations

import hashlib
import re

from jobhunt.net import FetchError, polite_get

URL = "https://raw.githubusercontent.com/zapplyjobs/New-Grad-Jobs-2027/main/README.md"

_TAG_RE = re.compile(r"<[^>]+>")
_BOLD_RE = re.compile(r"\*\*")
_LINK_URL_RE = re.compile(r'\((https?://[^)\s]+)\)|href="(https?://[^"]+)"')


def _clean_text(cell: str) -> str:
    text = _TAG_RE.sub("", cell)
    text = _BOLD_RE.sub("", text)
    return text.strip()


def _extract_url(cell: str) -> str | None:
    m = _LINK_URL_RE.search(cell)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _split_pipe_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c.replace(" ", "")) <= {"-", ":"} and c.strip() != "" for c in cells)


def parse_zapply_markdown(text: str) -> tuple[list[dict], int]:
    """Parse the README's job tables. Returns (postings, skipped_row_count)."""
    postings: list[dict] = []
    skipped = 0

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _split_pipe_row(stripped)
        if len(cells) < 6:
            continue
        if _is_separator_row(cells):
            continue
        if _clean_text(cells[0]).lower() == "company":
            continue  # header row (repeats once per section table)

        raw_company, raw_role, raw_location, raw_posted, raw_visa, raw_apply = cells[:6]

        company = _clean_text(raw_company)
        role = _clean_text(raw_role)
        location = _clean_text(raw_location)
        posted = _clean_text(raw_posted) or None
        visa = _clean_text(raw_visa) or None
        link = _extract_url(raw_apply)

        if not company or not role or not link:
            skipped += 1
            continue

        digest = hashlib.sha1(f"{company}|{role}|{link}".encode(), usedforsecurity=False)
        postings.append(
            {
                "source_posting_id": digest.hexdigest(),
                "company": company,
                "title": role,
                "location": location or None,
                "url": link,
                "posted_at": posted,
                "raw": {
                    "company": raw_company,
                    "role": raw_role,
                    "location": raw_location,
                    "posted": raw_posted,
                    "visa": visa,
                    "link": raw_apply,
                },
            }
        )

    return postings, skipped


def fetch() -> list[dict]:
    resp = polite_get(URL)
    if resp.status_code != 200:
        raise FetchError(f"zapply: HTTP {resp.status_code}", resp.status_code)
    postings, skipped = parse_zapply_markdown(resp.text)
    if skipped:
        print(f"WARNING: zapply: skipped {skipped} unparseable row(s)")
    return postings
