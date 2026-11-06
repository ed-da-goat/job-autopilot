"""The daily operating view: triage, readiness, follow-ups, interviews, offers."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from jobhunt import sprint as sprint_module
from jobhunt.eligibility import load_preferences
from jobhunt.health import build_health


def _group_role_variants(rows: list[sqlite3.Row]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        item = dict(row)
        key = ((item.get("company") or "").strip().lower(), (item.get("title") or "").strip().lower())
        if key not in grouped:
            item["variants"] = []
            grouped[key] = item
        grouped[key]["variants"].append(
            {
                "posting_id": item["posting_id"],
                "location": item.get("location"),
                "url": item.get("url"),
            }
        )
    return list(grouped.values())


PROMISE_STAGES = ("qualified", "tailored", "ready", "applied", "replied", "interviewing", "offer")


def invariant_scan(conn: sqlite3.Connection) -> dict:
    """No-dropped-balls check: postings that promised follow-up (via stage)
    but have no next action, and 'applied' postings with no submitted
    application on record."""
    no_next_action = conn.execute(
        f"""SELECT f.posting_id, p.company, p.title, f.stage
            FROM funnel f JOIN postings p ON p.id=f.posting_id
            WHERE f.stage IN {PROMISE_STAGES}
              AND (f.next_action IS NULL OR TRIM(f.next_action)='' OR f.next_action_date IS NULL)
            ORDER BY f.updated_at LIMIT 25"""
    ).fetchall()
    no_next_action_count = conn.execute(
        f"""SELECT COUNT(*) c FROM funnel f
            WHERE f.stage IN {PROMISE_STAGES}
              AND (f.next_action IS NULL OR TRIM(f.next_action)='' OR f.next_action_date IS NULL)"""
    ).fetchone()["c"]

    applied_missing_submitted = conn.execute(
        """SELECT f.posting_id, p.company, p.title
           FROM funnel f
           JOIN postings p ON p.id=f.posting_id
           LEFT JOIN applications a ON a.posting_id=f.posting_id
           WHERE f.stage='applied' AND (a.id IS NULL OR a.submitted_at IS NULL)
           ORDER BY f.updated_at LIMIT 25"""
    ).fetchall()
    applied_missing_submitted_count = conn.execute(
        """SELECT COUNT(*) c
           FROM funnel f
           LEFT JOIN applications a ON a.posting_id=f.posting_id
           WHERE f.stage='applied' AND (a.id IS NULL OR a.submitted_at IS NULL)"""
    ).fetchone()["c"]

    return {
        "no_next_action": [dict(row) for row in no_next_action],
        "no_next_action_count": no_next_action_count,
        "applied_missing_submitted": [dict(row) for row in applied_missing_submitted],
        "applied_missing_submitted_count": applied_missing_submitted_count,
    }


def build_today(conn: sqlite3.Connection) -> dict:
    rubric_version = conn.execute("SELECT MAX(rubric_version) v FROM scores").fetchone()["v"]
    preferences = load_preferences()
    now = datetime.now(UTC)
    today = now.date().isoformat()
    next_week = (now + timedelta(days=7)).isoformat()

    eligible = conn.execute(
        """SELECT f.posting_id, p.company, p.title, p.location, p.url, s.total,
                  e.evidence, f.updated_at
           FROM funnel f
           JOIN postings p ON p.id=f.posting_id
           JOIN scores s ON s.posting_id=p.id AND s.rubric_version=?
           JOIN eligibility_assessments e
             ON e.posting_id=p.id AND e.rubric_version=s.rubric_version
            AND e.preferences_version=?
           WHERE f.stage='suggested' AND p.status='active' AND e.status='eligible'
           ORDER BY s.total DESC, f.updated_at DESC""",
        (rubric_version, preferences.version),
    ).fetchall()
    review = conn.execute(
        """SELECT p.id posting_id, p.company, p.title, p.location, p.url, s.total,
                  e.warnings, e.evidence
           FROM postings p
           JOIN scores s ON s.posting_id=p.id AND s.rubric_version=?
           JOIN eligibility_assessments e
             ON e.posting_id=p.id AND e.rubric_version=s.rubric_version
            AND e.preferences_version=?
           LEFT JOIN funnel f ON f.posting_id=p.id
           WHERE p.status='active' AND s.excluded=0 AND s.total>=55
             AND e.status='review' AND f.posting_id IS NULL
           ORDER BY s.total DESC, p.id LIMIT 30""",
        (rubric_version, preferences.version),
    ).fetchall()
    ready = conn.execute(
        """SELECT f.posting_id, p.company, p.title, p.url,
                  p.status posting_status, a.updated_at
           FROM funnel f JOIN postings p ON p.id=f.posting_id
           LEFT JOIN applications a ON a.posting_id=f.posting_id
           WHERE f.stage='ready' ORDER BY a.updated_at"""
    ).fetchall()
    blocked_funnel = conn.execute(
        """SELECT f.posting_id, f.stage, p.company, p.title, p.url,
                  e.blockers, e.evidence
           FROM funnel f
           JOIN postings p ON p.id=f.posting_id
           JOIN eligibility_assessments e
             ON e.posting_id=p.id AND e.rubric_version=?
            AND e.preferences_version=?
           WHERE f.stage NOT IN ('passed','closed','offer') AND e.status='blocked'
           ORDER BY f.updated_at DESC, f.posting_id""",
        (rubric_version, preferences.version),
    ).fetchall()
    touches = conn.execute(
        """SELECT t.*, c.name contact_name, c.company contact_company,
                  p.company posting_company, p.title posting_title
           FROM touches t
           LEFT JOIN contacts c ON c.id=t.contact_id
           LEFT JOIN postings p ON p.id=t.posting_id
           WHERE t.status='planned' AND t.due_at IS NOT NULL AND t.due_at<=?
           ORDER BY t.due_at""",
        (today + "T23:59:59",),
    ).fetchall()
    interviews = conn.execute(
        """SELECT i.*, p.company, p.title FROM interviews i
           JOIN postings p ON p.id=i.posting_id
           WHERE (i.scheduled_at IS NOT NULL AND i.scheduled_at<=? AND COALESCE(i.outcome,'')='')
              OR (i.thank_you_due_at IS NOT NULL AND i.thank_you_due_at<=?
                  AND i.thank_you_completed_at IS NULL)
           ORDER BY COALESCE(i.scheduled_at, i.thank_you_due_at)""",
        (next_week, today + "T23:59:59"),
    ).fetchall()
    offers = conn.execute(
        """SELECT o.*, p.company, p.title FROM offers o
           JOIN postings p ON p.id=o.posting_id
           WHERE o.deadline IS NOT NULL AND COALESCE(o.decision,'')=''
           ORDER BY o.deadline"""
    ).fetchall()
    conference_actions = conn.execute(
        """SELECT * FROM conference_actions
           WHERE status='planned' AND due_at IS NOT NULL AND due_at<=?
           ORDER BY due_at""",
        (today,),
    ).fetchall()
    ghost_cutoff = (now - timedelta(days=14)).isoformat()
    ghost_check = conn.execute(
        """SELECT f.posting_id, p.company, p.title, p.url, a.submitted_at
           FROM funnel f
           JOIN postings p ON p.id=f.posting_id
           JOIN applications a ON a.posting_id=f.posting_id
           WHERE f.stage='applied' AND a.submitted_at IS NOT NULL AND a.submitted_at<=?
           ORDER BY a.submitted_at""",
        (ghost_cutoff,),
    ).fetchall()
    return {
        "rubric_version": rubric_version,
        "eligible": _group_role_variants(eligible),
        "review": _group_role_variants(review),
        "ready": [dict(row) for row in ready],
        "blocked_funnel": [dict(row) for row in blocked_funnel],
        "touches": [dict(row) for row in touches],
        "interviews": [dict(row) for row in interviews],
        "offers": [dict(row) for row in offers],
        "conference_actions": [dict(row) for row in conference_actions],
        "ghost_check": [dict(row) for row in ghost_check],
        "health": build_health(conn),
        "sprint": sprint_module.sprint_status(conn),
        "invariants": invariant_scan(conn),
    }
