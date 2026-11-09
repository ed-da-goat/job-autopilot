"""Full-lifecycle domain operations for the local job-search OS.

All external-world actions remain manual. These functions only record local
state after the candidate acts, or prepare copy-ready text for them to use elsewhere.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from jobhunt.apply_assist import profile_store
from jobhunt.eligibility import latest_assessment, load_preferences, user_preferences
from jobhunt.funnel import PASS_REASONS, ensure_funnel_row, move

TOUCH_KINDS = {"intro_request", "outreach", "follow_up", "reply", "thank_you"}
ARTIFACT_KINDS = {"follow_up", "interview_prep", "thank_you", "offer_questions"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise ValueError(f"no {table} row with id={row_id}")
    return row


def log_activity(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    action: str,
    data: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO activity_events (entity_type, entity_id, action, data, at) VALUES (?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, json.dumps(data or {}), _now()),
    )


def pass_posting(conn: sqlite3.Connection, posting_id: int, reason: str, detail: str = "") -> None:
    if reason not in PASS_REASONS:
        raise ValueError(f"unknown pass reason {reason!r}. Valid: {', '.join(sorted(PASS_REASONS))}")
    ensure_funnel_row(conn, posting_id)
    conn.execute(
        "UPDATE funnel SET pass_reason=?, pass_detail=? WHERE posting_id=?",
        (reason, detail, posting_id),
    )
    move(conn, posting_id, "passed", note=f"pass:{reason}" + (f" — {detail}" if detail else ""))
    log_activity(conn, "posting", posting_id, "passed", {"reason": reason, "detail": detail})
    conn.commit()


def save_resume_variant(
    conn: sqlite3.Connection,
    *,
    label: str,
    path: str,
    purpose: str = "",
    reviewed: bool = False,
) -> sqlite3.Row:
    now = _now()
    reviewed_at = now if reviewed else None
    conn.execute(
        """INSERT INTO resume_variants
               (label, path, purpose, reviewed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(label) DO UPDATE SET path=excluded.path,
             purpose=excluded.purpose,
             reviewed_at=CASE WHEN excluded.reviewed_at IS NOT NULL
                              THEN excluded.reviewed_at ELSE resume_variants.reviewed_at END,
             updated_at=excluded.updated_at""",
        (label, path, purpose, reviewed_at, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM resume_variants WHERE label=?", (label,)).fetchone()


def ensure_application(
    conn: sqlite3.Connection,
    posting_id: int,
    *,
    resume_variant_id: int | None = None,
    channel: str = "company careers page",
    confirmation_ref: str = "",
    notes: str = "",
) -> sqlite3.Row:
    now = _now()
    conn.execute(
        """INSERT INTO applications
               (posting_id, channel, resume_variant_id, confirmation_ref, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(posting_id) DO UPDATE SET
             channel=excluded.channel,
             resume_variant_id=COALESCE(excluded.resume_variant_id, applications.resume_variant_id),
             confirmation_ref=excluded.confirmation_ref,
             notes=excluded.notes,
             updated_at=excluded.updated_at""",
        (posting_id, channel, resume_variant_id, confirmation_ref, notes, now, now),
    )
    application = conn.execute(
        "SELECT * FROM applications WHERE posting_id=?", (posting_id,)
    ).fetchone()
    log_activity(conn, "application", application["id"], "saved", {"posting_id": posting_id})
    conn.commit()
    return application


def application_readiness(conn: sqlite3.Connection, posting_id: int) -> dict:
    blockers: list[str] = []
    application = conn.execute(
        "SELECT * FROM applications WHERE posting_id=?", (posting_id,)
    ).fetchone()
    if application is None or application["resume_variant_id"] is None:
        blockers.append("select_resume_variant")
    else:
        resume = conn.execute(
            "SELECT * FROM resume_variants WHERE id=?", (application["resume_variant_id"],)
        ).fetchone()
        if resume is None or not resume["reviewed_at"]:
            blockers.append("resume_not_reviewed")

    assessment = latest_assessment(conn, posting_id)
    if assessment is None:
        blockers.append("eligibility_not_assessed")
    elif assessment["status"] == "blocked":
        blockers.append("eligibility_blocked")

    answers = profile_store.get_answers(conn)
    preferences = load_preferences()
    for field in preferences.required_application_fields:
        value = profile_store.answer_for_preference(answers, field)
        if not value:
            blockers.append(f"missing_required_answer:{field}")

    posting = conn.execute("SELECT status FROM postings WHERE id=?", (posting_id,)).fetchone()
    if posting is None:
        raise ValueError(f"no posting with id={posting_id}")
    if posting["status"] != "active":
        blockers.append("posting_closed")
    return {"ready": not blockers, "blockers": blockers}


def mark_ready(conn: sqlite3.Connection, posting_id: int) -> dict:
    readiness = application_readiness(conn, posting_id)
    if not readiness["ready"]:
        raise ValueError("application is not ready: " + ", ".join(readiness["blockers"]))
    move(conn, posting_id, "ready", note="readiness gate passed")
    log_activity(conn, "posting", posting_id, "marked_ready")
    conn.commit()
    return readiness


def confirm_applied(
    conn: sqlite3.Connection,
    posting_id: int,
    *,
    confirmed: bool,
    submitted_at: str | None = None,
    confirmation_ref: str = "",
) -> sqlite3.Row:
    if not confirmed:
        raise ValueError("manual submission confirmation is required")
    readiness = application_readiness(conn, posting_id)
    if not readiness["ready"]:
        raise ValueError("application is not ready: " + ", ".join(readiness["blockers"]))
    application = ensure_application(conn, posting_id, confirmation_ref=confirmation_ref)
    stamp = submitted_at or _now()
    conn.execute(
        "UPDATE applications SET submitted_at=?, confirmation_ref=?, updated_at=? WHERE id=?",
        (stamp, confirmation_ref, _now(), application["id"]),
    )
    move(conn, posting_id, "applied", note="manually submitted and confirmed")
    log_activity(conn, "application", application["id"], "submitted", {"submitted_at": stamp})
    conn.commit()
    # skip if follow-up touches were already scheduled for this posting (avoid duplicates on re-confirm)
    existing = conn.execute(
        "SELECT 1 FROM touches WHERE posting_id=? AND kind='follow_up' AND status='planned'",
        (posting_id,),
    ).fetchone()
    if existing is None:
        try:
            submitted_dt = datetime.fromisoformat(stamp)
        except ValueError:
            submitted_dt = datetime.now(UTC)
        create_touch(
            conn,
            kind="follow_up",
            posting_id=posting_id,
            notes="T+5 application nudge",
            due_at=(submitted_dt + timedelta(days=5)).isoformat(),
        )
        create_touch(
            conn,
            kind="follow_up",
            posting_id=posting_id,
            notes="T+10 recruiter follow-up",
            due_at=(submitted_dt + timedelta(days=10)).isoformat(),
        )
    return _row(conn, "applications", application["id"])


def create_contact(conn: sqlite3.Connection, **fields: str) -> sqlite3.Row:
    now = _now()
    cursor = conn.execute(
        """INSERT INTO contacts
               (name, company, title, relationship, source, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fields.get("name", "").strip(), fields.get("company", ""), fields.get("title", ""),
            fields.get("relationship", ""), fields.get("source", ""), fields.get("notes", ""), now, now,
        ),
    )
    if not fields.get("name", "").strip():
        conn.rollback()
        raise ValueError("contact name is required")
    log_activity(conn, "contact", cursor.lastrowid, "created")
    conn.commit()
    return _row(conn, "contacts", cursor.lastrowid)


def create_touch(
    conn: sqlite3.Connection,
    *,
    kind: str,
    contact_id: int | None = None,
    posting_id: int | None = None,
    due_at: str | None = None,
    completed: bool = False,
    notes: str = "",
) -> sqlite3.Row:
    if kind not in TOUCH_KINDS:
        raise ValueError(f"unknown touch kind {kind!r}")
    now = _now()
    status = "completed" if completed else "planned"
    cursor = conn.execute(
        """INSERT INTO touches
               (contact_id, posting_id, kind, status, due_at, completed_at, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contact_id, posting_id, kind, status, due_at, now if completed else None, notes, now, now),
    )
    log_activity(conn, "touch", cursor.lastrowid, "created", {"status": status})
    conn.commit()
    return _row(conn, "touches", cursor.lastrowid)


def complete_touch(conn: sqlite3.Connection, touch_id: int) -> sqlite3.Row:
    _row(conn, "touches", touch_id)
    now = _now()
    conn.execute(
        "UPDATE touches SET status='completed', completed_at=?, updated_at=? WHERE id=?",
        (now, now, touch_id),
    )
    log_activity(conn, "touch", touch_id, "completed")
    conn.commit()
    return _row(conn, "touches", touch_id)


def create_interview(conn: sqlite3.Connection, posting_id: int, **fields: str) -> sqlite3.Row:
    now = _now()
    cursor = conn.execute(
        """INSERT INTO interviews
               (posting_id, round_name, scheduled_at, interviewers, prep_notes, outcome,
                thank_you_due_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            posting_id, fields.get("round_name", "Interview"), fields.get("scheduled_at"),
            fields.get("interviewers", ""), fields.get("prep_notes", ""), fields.get("outcome", ""),
            fields.get("thank_you_due_at"), now, now,
        ),
    )
    move(conn, posting_id, "interviewing", note=f"interview: {fields.get('round_name', 'Interview')}")
    log_activity(conn, "interview", cursor.lastrowid, "created")
    conn.commit()
    return _row(conn, "interviews", cursor.lastrowid)


def update_interview(
    conn: sqlite3.Connection,
    interview_id: int,
    *,
    outcome: str | None = None,
    thank_you_completed: bool = False,
) -> sqlite3.Row:
    _row(conn, "interviews", interview_id)
    now = _now()
    if outcome is not None:
        conn.execute(
            "UPDATE interviews SET outcome=?, updated_at=? WHERE id=?",
            (outcome, now, interview_id),
        )
    if thank_you_completed:
        conn.execute(
            "UPDATE interviews SET thank_you_completed_at=?, updated_at=? WHERE id=?",
            (now, now, interview_id),
        )
    log_activity(
        conn,
        "interview",
        interview_id,
        "updated",
        {"outcome": outcome, "thank_you_completed": thank_you_completed},
    )
    conn.commit()
    return _row(conn, "interviews", interview_id)


def create_offer(conn: sqlite3.Connection, posting_id: int, **fields) -> sqlite3.Row:
    now = _now()
    conn.execute(
        """INSERT INTO offers
               (posting_id, received_at, deadline, base_salary, bonus, equity, location,
                notes, decision, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(posting_id) DO UPDATE SET
             received_at=excluded.received_at, deadline=excluded.deadline,
             base_salary=excluded.base_salary, bonus=excluded.bonus, equity=excluded.equity,
             location=excluded.location, notes=excluded.notes, decision=excluded.decision,
             updated_at=excluded.updated_at""",
        (
            posting_id, fields.get("received_at") or now, fields.get("deadline"),
            fields.get("base_salary"), fields.get("bonus"), fields.get("equity", ""),
            fields.get("location", ""), fields.get("notes", ""), fields.get("decision", ""), now, now,
        ),
    )
    offer = conn.execute("SELECT * FROM offers WHERE posting_id=?", (posting_id,)).fetchone()
    move(conn, posting_id, "offer", note="offer recorded")
    log_activity(conn, "offer", offer["id"], "saved")
    conn.commit()
    return offer


def create_artifact(conn: sqlite3.Connection, posting_id: int, kind: str) -> sqlite3.Row:
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact kind {kind!r}")
    posting = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if posting is None:
        raise ValueError(f"no posting with id={posting_id}")
    company = posting["company"] or "the company"
    title = posting["title"] or "the role"
    sign_off = (user_preferences().candidate_name or "").strip() or "[YOUR NAME]"
    templates = {
        "follow_up": f"Hi [NAME],\n\nI wanted to follow up on my application for {title} at {company}. I believe my background is a strong fit for the role, and I would be glad to share any additional context.\n\nBest,\n{sign_off}",
        "interview_prep": f"Role: {title} at {company}\n\n1. Why this role?\n2. Three profile-backed stories to use:\n3. Requirements to validate:\n4. Questions for the interviewer:\n5. Risks or gaps to address:",
        "thank_you": f"Hi [NAME],\n\nThank you for the conversation about the {title} role. I appreciated learning more about [SPECIFIC TOPIC]. [PROFILE-BACKED CONNECTION].\n\nBest,\n{sign_off}",
        "offer_questions": f"Offer review for {title} at {company}\n\n- Decision deadline:\n- Base, bonus, and equity details:\n- Location and start-date expectations:\n- Benefits and time off:\n- Role scope and first-90-day expectations:\n- Questions or terms to clarify:",
    }
    now = _now()
    cursor = conn.execute(
        "INSERT INTO artifacts (posting_id, kind, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (posting_id, kind, templates[kind], now, now),
    )
    log_activity(conn, "artifact", cursor.lastrowid, "created", {"kind": kind})
    conn.commit()
    return _row(conn, "artifacts", cursor.lastrowid)
