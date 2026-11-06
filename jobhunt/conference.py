"""Parse data/conference_funding.md into the conference_actions table.

Source is the "## Action queue" pipe table — columns
Due | Recur | Action | Program | Notes. Due is an ISO date the action first
surfaces on Today; Recur is an integer number of days between repeats after
marking an action done (blank = one-shot).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

DEFAULT_CONFERENCE_PATH = Path(__file__).resolve().parent.parent / "data" / "conference_funding.md"

_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_BOLD_RE = re.compile(r"^\*\*(.+)\*\*$")


def _split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [_strip_bold(cell.strip()) for cell in inner.split("|")]


def _strip_bold(cell: str) -> str:
    m = _BOLD_RE.match(cell)
    return m.group(1).strip() if m else cell


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(c) <= {"-", ":"} and c != "" for c in cells)


def parse_conference_markdown(path: Path | str) -> list[dict]:
    """Parse the "## Action queue" table into a list of action dicts."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()

    rows: list[dict] = []
    in_section = False
    header: list[str] | None = None
    for line in lines:
        if line.startswith("## "):
            in_section = line.strip().lower() == "## action queue"
            header = None
            continue
        if not in_section:
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
        if len(cells) < 5:
            continue
        due, recur, action, program, notes = cells[:5]
        if not program:
            continue
        rows.append(
            {
                "program": program,
                "action": action,
                "due_at": due or None,
                "recur_days": int(recur) if recur else None,
                "notes": notes or None,
            }
        )

    return rows


def upsert_conference_actions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert new conference actions or update notes/recur_days on existing ones.

    Deliberately never overwrites due_at or status on conflict — re-imports must
    not resurrect a completed action or clobber a recurrence-bumped due date.
    """
    now = datetime.now(UTC).isoformat()
    count = 0
    for row in rows:
        conn.execute(
            """
            INSERT INTO conference_actions (program, action, due_at, recur_days, status, notes, created_at)
            VALUES (:program, :action, :due_at, :recur_days, 'planned', :notes, :created_at)
            ON CONFLICT(program, action) DO UPDATE SET
                notes=excluded.notes,
                recur_days=excluded.recur_days
            """,
            {**row, "created_at": now},
        )
        count += 1
    conn.commit()
    return count


def seed_from_file(conn: sqlite3.Connection, path: Path | str = DEFAULT_CONFERENCE_PATH) -> int:
    rows = parse_conference_markdown(path)
    return upsert_conference_actions(conn, rows)


def complete_conference_action(conn: sqlite3.Connection, action_id: int, final: bool = False) -> dict:
    """Complete a conference action.

    If it recurs and this isn't the final completion, bump due_at forward by
    recur_days (from whichever is later, the existing due_at or today) and
    leave status='planned'. Otherwise mark it completed.
    """
    row = conn.execute("SELECT * FROM conference_actions WHERE id=?", (action_id,)).fetchone()
    if row is None:
        raise ValueError(f"no conference_actions row with id={action_id}")

    now = datetime.now(UTC)
    if row["recur_days"] and not final:
        base = date.fromisoformat(row["due_at"]) if row["due_at"] else now.date()
        base = max(base, now.date())
        next_due = (base + timedelta(days=row["recur_days"])).isoformat()
        conn.execute("UPDATE conference_actions SET due_at=? WHERE id=?", (next_due, action_id))
    else:
        conn.execute(
            "UPDATE conference_actions SET status='completed', completed_at=? WHERE id=?",
            (now.isoformat(), action_id),
        )
    conn.commit()
    return dict(conn.execute("SELECT * FROM conference_actions WHERE id=?", (action_id,)).fetchone())
