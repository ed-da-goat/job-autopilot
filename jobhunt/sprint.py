"""Application sprint tracking (S6): a small named batch of postings the candidate
commits to driving to submission.

A sprint bounds spend-gated drafting (jobhunt/prep.py) to its members, so
LLM spend only goes toward postings actively being worked, and it gives each
member a single next action so the candidate always knows what to do next. There is
meant to be at most one active sprint at a time — start one with
`create_sprint`, work it, then `complete_sprint` before starting the next.
Membership also carries a warm-path readiness check (S7): a member needs
either a linked contact or an explicit no-warm-path note before it's
considered unblocked.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from jobhunt.eligibility import load_preferences
from jobhunt.lifecycle import log_activity

EARLY_STAGES = ("suggested", "qualified", "tailored", "ready")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise ValueError(f"no {table} row with id={row_id}")
    return row


def active_sprint(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The active sprint, if any. There should only ever be one; if somehow
    more than one is marked active, the newest wins."""
    return conn.execute(
        "SELECT * FROM sprints WHERE status='active' ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()


def is_member(conn: sqlite3.Connection, posting_id: int) -> bool:
    """Whether posting_id belongs to the active sprint. False when there is
    no active sprint."""
    sprint = active_sprint(conn)
    if sprint is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM sprint_members WHERE sprint_id=? AND posting_id=?",
        (sprint["id"], posting_id),
    ).fetchone()
    return row is not None


def has_warm_path(conn: sqlite3.Connection, posting_id: int) -> bool:
    """Whether posting_id has a warm path: a touch linking it to a contact,
    or a contact at the same company as the posting (case-insensitive)."""
    touch = conn.execute(
        "SELECT 1 FROM touches WHERE posting_id=? AND contact_id IS NOT NULL",
        (posting_id,),
    ).fetchone()
    if touch is not None:
        return True
    company = conn.execute(
        "SELECT 1 FROM contacts c JOIN postings p ON lower(c.company)=lower(p.company) "
        "WHERE p.id=? AND c.company IS NOT NULL AND c.company != ''",
        (posting_id,),
    ).fetchone()
    return company is not None


def candidates(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Postings eligible to be pulled into a new sprint: active, in an early
    funnel stage, assessed eligible under the current rubric+preferences, and
    whose company is verified. Excludes postings already in the active
    sprint. Ordered by rubric score, highest first."""
    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    preferences = load_preferences()
    placeholders = ",".join("?" for _ in EARLY_STAGES)
    rows = conn.execute(
        f"""SELECT f.posting_id posting_id, p.company company, p.title title,
                   s.total total, f.stage stage
            FROM funnel f
            JOIN postings p ON p.id=f.posting_id
            JOIN scores s ON s.posting_id=p.id AND s.rubric_version=?
            JOIN eligibility_assessments e
              ON e.posting_id=p.id AND e.rubric_version=s.rubric_version
             AND e.preferences_version=?
            JOIN companies c ON c.name=p.company
            WHERE p.status='active'
              AND f.stage IN ({placeholders})
              AND e.status='eligible'
              AND c.verified IN ('yes','auto-yes')
              AND f.posting_id NOT IN (
                  SELECT sm.posting_id FROM sprint_members sm
                  JOIN sprints sp ON sp.id=sm.sprint_id
                  WHERE sp.status='active'
              )
            ORDER BY s.total DESC
            LIMIT ?""",
        (rubric_version, preferences.version, *EARLY_STAGES, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def create_sprint(
    conn: sqlite3.Connection,
    posting_ids: list[int],
    name: str | None = None,
    target_size: int | None = None,
) -> sqlite3.Row:
    """Start a new active sprint with the given postings as members. Raises
    ValueError if a sprint is already active or posting_ids is empty."""
    if active_sprint(conn) is not None:
        raise ValueError("an active sprint already exists; complete it first with `jobhunt sprint complete`")
    if not posting_ids:
        raise ValueError("posting_ids must not be empty")

    now = _now()
    sprint_name = name or f"Sprint {datetime.now(UTC).date().isoformat()}"
    cursor = conn.execute(
        "INSERT INTO sprints (name, status, target_size, started_at) VALUES (?, 'active', ?, ?)",
        (sprint_name, target_size, now),
    )
    sprint_id = cursor.lastrowid
    for posting_id in posting_ids:
        conn.execute(
            "INSERT INTO sprint_members (sprint_id, posting_id, added_at) VALUES (?, ?, ?)",
            (sprint_id, posting_id, now),
        )
        log_activity(conn, "posting", posting_id, "sprint_member_added", {"sprint_id": sprint_id})
    log_activity(conn, "sprint", sprint_id, "created", {"posting_ids": list(posting_ids)})
    conn.commit()
    return _row(conn, "sprints", sprint_id)


def complete_sprint(conn: sqlite3.Connection) -> sqlite3.Row:
    """Mark the active sprint completed. Raises ValueError when none active."""
    sprint = active_sprint(conn)
    if sprint is None:
        raise ValueError("no active sprint")
    now = _now()
    conn.execute(
        "UPDATE sprints SET status='completed', completed_at=? WHERE id=?",
        (now, sprint["id"]),
    )
    log_activity(conn, "sprint", sprint["id"], "completed")
    conn.commit()
    return _row(conn, "sprints", sprint["id"])


def set_no_warm_path_note(conn: sqlite3.Connection, posting_id: int, note: str) -> sqlite3.Row:
    """Record why posting_id has no warm path, clearing its blocker without a
    linked contact. Raises ValueError on an empty note, no active sprint, or
    a posting_id that isn't a member of the active sprint."""
    note = note.strip()
    if not note:
        raise ValueError("note must not be empty")
    sprint = active_sprint(conn)
    if sprint is None:
        raise ValueError("no active sprint")
    member = conn.execute(
        "SELECT 1 FROM sprint_members WHERE sprint_id=? AND posting_id=?",
        (sprint["id"], posting_id),
    ).fetchone()
    if member is None:
        raise ValueError(f"posting {posting_id} is not a member of the active sprint")
    conn.execute(
        "UPDATE sprint_members SET no_warm_path_note=? WHERE sprint_id=? AND posting_id=?",
        (note, sprint["id"], posting_id),
    )
    log_activity(conn, "posting", posting_id, "no_warm_path_recorded", {"note": note})
    conn.commit()
    return conn.execute(
        "SELECT * FROM sprint_members WHERE sprint_id=? AND posting_id=?",
        (sprint["id"], posting_id),
    ).fetchone()


def member_next_action(stage: str, has_planned_touch: bool) -> str:
    """Pure function: the single next action for a sprint member given its
    funnel stage (and, for 'applied', whether a follow-up touch is already
    planned)."""
    if stage in ("suggested", "qualified"):
        return "tailor"
    if stage == "tailored":
        return "apply-assist"
    if stage == "ready":
        return "confirm applied"
    if stage == "applied":
        return "send follow-up touch" if has_planned_touch else "record outcome"
    if stage == "replied":
        return "record outcome"
    if stage == "interviewing":
        return "interview prep"
    if stage == "offer":
        return "decide offer"
    if stage in ("passed", "closed"):
        return "done"
    return "triage"


def sprint_status(conn: sqlite3.Connection) -> dict | None:
    """The active sprint plus one summary row per member. Returns None when
    no sprint is active. `blockers` carries a warm-path blocker when the
    member has neither a linked contact nor a no-warm-path note."""
    sprint = active_sprint(conn)
    if sprint is None:
        return None

    member_rows = conn.execute(
        """SELECT sm.posting_id posting_id, sm.no_warm_path_note no_warm_path_note,
                  p.company company, p.title title, COALESCE(f.stage, 'harvested') stage
           FROM sprint_members sm
           JOIN postings p ON p.id=sm.posting_id
           LEFT JOIN funnel f ON f.posting_id=sm.posting_id
           WHERE sm.sprint_id=?
           ORDER BY sm.added_at, sm.id""",
        (sprint["id"],),
    ).fetchall()

    members = []
    for row in member_rows:
        has_planned_touch = (
            conn.execute(
                "SELECT 1 FROM touches WHERE posting_id=? AND status='planned'",
                (row["posting_id"],),
            ).fetchone()
            is not None
        )
        blockers = []
        if not row["no_warm_path_note"] and not has_warm_path(conn, row["posting_id"]):
            blockers.append("no warm path — link a contact or record a no-warm-path note")
        members.append(
            {
                "posting_id": row["posting_id"],
                "company": row["company"],
                "title": row["title"],
                "stage": row["stage"],
                "next_action": member_next_action(row["stage"], has_planned_touch),
                "blockers": blockers,
                "no_warm_path_note": row["no_warm_path_note"],
            }
        )

    return {
        "id": sprint["id"],
        "name": sprint["name"],
        "started_at": sprint["started_at"],
        "target_size": sprint["target_size"],
        "members": members,
    }
