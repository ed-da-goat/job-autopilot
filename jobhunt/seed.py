"""Parse data/seed_companies.md into the companies table.

Two sources in the same file:
  1. The main markdown table under "## Company table" — columns
     Company | Category | Platform | Slug | Verified | Why-fit.
  2. The "## ... (manual watchlist)" bullet list at the bottom — companies
     on none of the three platforms. Format: `- **Name** — description`,
     with optional indented continuation lines.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_companies.md"

_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_BULLET_RE = re.compile(r"^-\s+\*\*(?P<name>[^*]+)\*\*\s*(?:—|--|-)?\s*(?P<rest>.*)$")


def _split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c) <= {"-", ":"} and c != "" for c in cells)


def parse_seed_markdown(text: str) -> tuple[list[dict], list[dict]]:
    """Return (table_companies, watchlist_companies) as lists of dicts."""
    lines = text.splitlines()

    table_rows: list[dict] = []
    watchlist_rows: list[dict] = []

    # --- main table: everything between "## Company table" and the next "## " ---
    in_table_section = False
    header: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            in_table_section = line.strip().lower() == "## company table"
            header = None
            continue
        if not in_table_section:
            continue
        m = _TABLE_ROW_RE.match(line)
        if not m:
            continue
        cells = _split_row(line)
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if _is_separator_row(cells):
            continue
        if len(cells) < 6:
            continue
        name, category, platform, slug, verified, why_fit = cells[:6]
        if not name:
            continue
        table_rows.append(
            {
                "name": name,
                "category": category or None,
                "platform": (platform or "").lower() or None,
                "slug": slug or None,
                "verified": verified or None,
                "why_fit": why_fit or None,
                "watchlist": 0,
            }
        )

    # --- watchlist section: bullet list under a "## ... watchlist ..." heading ---
    in_watchlist_section = False
    current: dict | None = None
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                watchlist_rows.append(current)
                current = None
            in_watchlist_section = "watchlist" in line.strip().lower()
            continue
        if not in_watchlist_section:
            continue
        bm = _BULLET_RE.match(line.strip())
        if bm:
            if current is not None:
                watchlist_rows.append(current)
            current = {
                "name": bm.group("name").strip(),
                "category": None,
                "platform": None,
                "slug": None,
                "verified": None,
                "why_fit": bm.group("rest").strip(),
                "watchlist": 1,
            }
        elif current is not None and line.strip():
            # continuation line of a wrapped bullet
            current["why_fit"] = (current["why_fit"] + " " + line.strip()).strip()
    if current is not None:
        watchlist_rows.append(current)

    return table_rows, watchlist_rows


def upsert_companies(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = datetime.now(UTC).isoformat()
    count = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at)
            VALUES (:name, :category, :platform, :slug, :verified, :why_fit, :watchlist, :added_at)
            ON CONFLICT(name) DO UPDATE SET
                category=excluded.category,
                platform=excluded.platform,
                slug=excluded.slug,
                verified=excluded.verified,
                why_fit=excluded.why_fit,
                watchlist=excluded.watchlist
            """,
            {**row, "added_at": now},
        )
        count += 1
    conn.commit()
    return count


def seed_from_file(conn: sqlite3.Connection, path: Path | str = DEFAULT_SEED_PATH) -> int:
    text = Path(path).read_text(encoding="utf-8")
    table_rows, watchlist_rows = parse_seed_markdown(text)
    return upsert_companies(conn, table_rows + watchlist_rows)
