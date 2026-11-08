"""Deterministic pipeline autopilot: advances every funnel member to its
human gate and guarantees every promise stage carries a next action.

No LLM, no network, no spend — pure local SQLite reads/writes. Meant to run
nightly (via daily.py) or on demand (`uv run python -m jobhunt autopilot`).
A manually-set next_action is a promise the candidate made to themselves and is never
overwritten here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from jobhunt import funnel, lifecycle
from jobhunt.resume.select import _components, select_variant

READY_ACTION = "review + submit (apply session)"

_ATTACH_STAGES = ("qualified", "tailored")

# 'suggested' rows are also advanced, but only at or above this latest-rubric
# score; lower-scoring suggestions stay put for human triage.
SUGGESTED_PROMOTE_MIN_SCORE = 70


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _choose_variant(conn: sqlite3.Connection, posting_id: int) -> tuple[int, str] | None:
    """Pick the resume variant for a posting by title/score components.

    Returns (variant_id, reason) only when the chosen variant exists and is
    reviewed. Never falls back to another variant: a wrong-fit resume must not
    be attached silently; the row parks at select_resume_variant instead.
    """
    posting = conn.execute("SELECT title FROM postings WHERE id=?", (posting_id,)).fetchone()
    if posting is None:
        return None
    score = conn.execute(
        """SELECT components FROM scores
           WHERE posting_id=? AND excluded=0
             AND rubric_version=(SELECT MAX(rubric_version) FROM scores)""",
        (posting_id,),
    ).fetchone()
    sel = select_variant(posting["title"], _components(score["components"] if score else None))
    variant = conn.execute(
        "SELECT id, reviewed_at FROM resume_variants WHERE label=?", (sel.db_label,)
    ).fetchone()
    if variant is None or _blank(variant["reviewed_at"]):
        return None
    return variant["id"], sel.reason


def _needs_action(row: sqlite3.Row) -> bool:
    return _blank(row["next_action"]) or _blank(row["next_action_date"])


def _fill_if_blank(conn: sqlite3.Connection, posting_id: int, row: sqlite3.Row, value_for: dict[str, str]) -> None:
    if _blank(row["next_action"]) and "next_action" in value_for:
        funnel.set_field(conn, posting_id, "next_action", value_for["next_action"])
    if _blank(row["next_action_date"]) and "next_action_date" in value_for:
        funnel.set_field(conn, posting_id, "next_action_date", value_for["next_action_date"])


def _attach_stage_postings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in _ATTACH_STAGES)
    rows = conn.execute(
        f"""SELECT f.* FROM funnel f
            JOIN postings p ON p.id = f.posting_id
            WHERE p.status = 'active' AND f.stage IN ({placeholders})""",
        _ATTACH_STAGES,
    ).fetchall()
    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    if rubric_version is None:
        return rows
    suggested = conn.execute(
        """SELECT f.* FROM funnel f
           JOIN postings p ON p.id = f.posting_id
           JOIN scores s ON s.posting_id = f.posting_id AND s.rubric_version = ?
           WHERE p.status = 'active' AND f.stage = 'suggested'
             AND s.excluded = 0 AND s.total >= ?
           ORDER BY s.total DESC, f.posting_id""",
        (rubric_version, SUGGESTED_PROMOTE_MIN_SCORE),
    ).fetchall()
    return rows + suggested


def run_autopilot(conn: sqlite3.Connection) -> dict:
    today = _today_iso()
    advanced: list[int] = []
    parked: dict[str, int] = {}
    resume_attached: list[int] = []
    next_actions_set = 0

    candidates = _attach_stage_postings(conn)

    # (a) Resume auto-attach.
    for row in candidates:
        posting_id = row["posting_id"]
        application = conn.execute(
            "SELECT * FROM applications WHERE posting_id=?", (posting_id,)
        ).fetchone()
        if application is None:
            choice = _choose_variant(conn, posting_id)
            if choice is not None:
                vid, _reason = choice
                lifecycle.ensure_application(conn, posting_id, resume_variant_id=vid)
                resume_attached.append(posting_id)
        elif application["resume_variant_id"] is None:
            choice = _choose_variant(conn, posting_id)
            if choice is not None:
                vid, reason = choice
                now = datetime.now(UTC).isoformat()
                conn.execute(
                    "UPDATE applications SET resume_variant_id=?, updated_at=? "
                    "WHERE posting_id=? AND resume_variant_id IS NULL",
                    (vid, now, posting_id),
                )
                lifecycle.log_activity(
                    conn,
                    "application",
                    application["id"],
                    "autopilot_resume_attached",
                    {"posting_id": posting_id, "variant_id": vid, "reason": reason},
                )
                resume_attached.append(posting_id)

    # (b) Advance/park.
    for row in candidates:
        posting_id = row["posting_id"]
        readiness = lifecycle.application_readiness(conn, posting_id)
        current = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        if readiness["ready"]:
            lifecycle.mark_ready(conn, posting_id)
            if _needs_action(current):
                _fill_if_blank(
                    conn,
                    posting_id,
                    current,
                    {"next_action": READY_ACTION, "next_action_date": today},
                )
            lifecycle.log_activity(conn, "posting", posting_id, "autopilot_advanced", {"to": "ready"})
            advanced.append(posting_id)
        else:
            blockers = readiness["blockers"]
            first = blockers[0]
            parked[first] = parked.get(first, 0) + 1
            if _blank(current["next_action"]):
                funnel.set_field(conn, posting_id, "next_action", f"unblock: {first}")
            if _blank(current["next_action_date"]):
                funnel.set_field(conn, posting_id, "next_action_date", today)
            lifecycle.log_activity(
                conn, "posting", posting_id, "autopilot_parked", {"blockers": blockers}
            )

    # (c) Fill blank next actions for 'ready' postings.
    ready_rows = conn.execute(
        "SELECT f.* FROM funnel f JOIN postings p ON p.id = f.posting_id "
        "WHERE p.status = 'active' AND f.stage = 'ready'"
    ).fetchall()
    for row in ready_rows:
        if _needs_action(row):
            _fill_if_blank(
                conn,
                row["posting_id"],
                row,
                {"next_action": READY_ACTION, "next_action_date": today},
            )
            next_actions_set += 1

    # (d) Fill blank next actions for 'applied' postings (follow-up cadence).
    applied_rows = conn.execute(
        "SELECT f.* FROM funnel f JOIN postings p ON p.id = f.posting_id "
        "WHERE p.status = 'active' AND f.stage = 'applied'"
    ).fetchall()
    follow_up_date = (datetime.now(UTC) + timedelta(days=funnel.FOLLOW_UP_DAYS)).date().isoformat()
    for row in applied_rows:
        if _blank(row["next_action"]):
            _fill_if_blank(
                conn,
                row["posting_id"],
                row,
                {"next_action": "follow up", "next_action_date": follow_up_date},
            )
            next_actions_set += 1

    conn.commit()
    return {
        "advanced": advanced,
        "parked": parked,
        "resume_attached": resume_attached,
        "next_actions_set": next_actions_set,
    }


def autopilot_digest_lines(conn: sqlite3.Connection, date_str: str) -> list[str]:
    rows = conn.execute(
        "SELECT action, data FROM activity_events "
        "WHERE action IN ('autopilot_advanced', 'autopilot_parked') AND at LIKE ?",
        (date_str + "%",),
    ).fetchall()
    if not rows:
        return []

    advanced_count = 0
    blocker_counts: dict[str, int] = {}
    for row in rows:
        if row["action"] == "autopilot_advanced":
            advanced_count += 1
            continue
        data = json.loads(row["data"] or "{}")
        for blocker in data.get("blockers") or []:
            if blocker.startswith("missing_required_answer:"):
                blocker = "missing:" + blocker.split(":", 1)[1]
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1

    lines = [f"- advanced to ready: {advanced_count}"]
    top_blockers = sorted(blocker_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    for blocker, count in top_blockers:
        lines.append(f"- parked: {blocker} x{count}")
    return lines
