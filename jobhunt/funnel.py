"""Funnel stage tracking: an append-only event log layered over a mutable
current-state row per posting.

Real job searches are messy — a recruiter reopens a "passed" role, a reply
lands after `applied`, an application skips `tailored` entirely — so `move()`
never blocks a transition. Any stage to any stage is allowed; it just always
logs the move as a funnel_events row so history is reconstructable.
"""

from __future__ import annotations

import sqlite3
import json
from datetime import UTC, datetime, timedelta

STAGES = ["harvested", "qualified", "tailored", "ready", "applied", "replied", "interviewing", "offer"]
SIDE_STAGES = ["suggested", "passed", "closed"]
ALL_STAGES = STAGES + SIDE_STAGES

FOLLOW_UP_DAYS = 7

SETTABLE_FIELDS = {"tier", "referral_path", "next_action", "next_action_date", "notes"}
PASS_REASONS = {
    "seniority",
    "geography",
    "sponsorship",
    "skills",
    "timing",
    "duplicate",
    "not_interested",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _log_activity(
    conn: sqlite3.Connection, posting_id: int, action: str, data: dict
) -> None:
    conn.execute(
        "INSERT INTO activity_events (entity_type, entity_id, action, data, at) VALUES ('posting', ?, ?, ?, ?)",
        (posting_id, action, json.dumps(data), _now()),
    )


def ensure_funnel_row(conn: sqlite3.Connection, posting_id: int) -> sqlite3.Row:
    """Create the funnel row at 'harvested' if one doesn't exist yet.
    Idempotent — returns the (possibly pre-existing) row either way."""
    existing = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
    if existing is not None:
        return existing

    now = _now()
    conn.execute(
        "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, 'harvested', ?, ?)",
        (posting_id, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()


def move(conn: sqlite3.Connection, posting_id: int, stage: str, note: str | None = None) -> tuple[str, str]:
    """Move a posting to `stage`, logging a funnel_events row regardless of
    direction. Stamps applied_at the first time (and only the first time) a
    posting reaches 'applied' — harvested -> applied direct is fine, 'tailored'
    is optional. On that same first move to 'applied', if next_action is not
    already set, auto-sets next_action='follow up' and next_action_date to
    today + FOLLOW_UP_DAYS — a manually-set next_action is never overwritten.
    Returns (from_stage, to_stage)."""
    if stage not in ALL_STAGES:
        raise ValueError(f"unknown stage {stage!r}. Valid stages: {', '.join(ALL_STAGES)}")

    row = ensure_funnel_row(conn, posting_id)
    from_stage = row["stage"]
    now = _now()

    applied_at = row["applied_at"]
    first_applied = stage == "applied" and applied_at is None
    if first_applied:
        applied_at = now

    conn.execute(
        "UPDATE funnel SET stage=?, applied_at=?, updated_at=? WHERE posting_id=?",
        (stage, applied_at, now, posting_id),
    )
    conn.execute(
        "INSERT INTO funnel_events (posting_id, from_stage, to_stage, note, at) VALUES (?, ?, ?, ?, ?)",
        (posting_id, from_stage, stage, note, now),
    )
    _log_activity(
        conn,
        posting_id,
        "stage_changed",
        {"from_stage": from_stage, "to_stage": stage, "note": note},
    )

    if first_applied and not row["next_action"]:
        next_action_date = (datetime.now(UTC) + timedelta(days=FOLLOW_UP_DAYS)).date().isoformat()
        conn.execute(
            "UPDATE funnel SET next_action=?, next_action_date=?, updated_at=? WHERE posting_id=?",
            ("follow up", next_action_date, now, posting_id),
        )

    conn.commit()
    return from_stage, stage


def set_field(conn: sqlite3.Connection, posting_id: int, field: str, value: str) -> None:
    """Set one allowlisted mutable field on the funnel row. Creates the row
    (at 'harvested') first if it doesn't exist yet."""
    if field not in SETTABLE_FIELDS:
        raise ValueError(f"unknown field {field!r}. Settable: {', '.join(sorted(SETTABLE_FIELDS))}")

    ensure_funnel_row(conn, posting_id)
    now = _now()
    conn.execute(f"UPDATE funnel SET {field}=?, updated_at=? WHERE posting_id=?", (value, now, posting_id))
    _log_activity(conn, posting_id, "field_set", {"field": field, "value": value})
    conn.commit()
