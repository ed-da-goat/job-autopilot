from __future__ import annotations

import base64

import pytest

from jobhunt import confirmations, db, funnel, lifecycle


def _posting(conn, spid: str, company: str, title: str, stage: str = "ready") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, status, first_seen_at, last_seen_at)
           VALUES ('ashby', ?, ?, ?, 'Remote', 'active', '', '')""",
        (spid, company, title),
    )
    posting_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, 3, 1, 'eligible', '[]', '[]', '[]', '')""",
        (posting_id,),
    )
    conn.commit()
    funnel.move(conn, posting_id, stage, note="test setup")
    return posting_id


def _reviewed_resume(conn) -> int:
    conn.execute(
        """INSERT INTO resume_variants (label, path, reviewed_at, created_at, updated_at)
           VALUES ('base', 'resume/base.pdf', '2026-08-01T00:00:00', '', '')"""
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.fixture
def complete_answers(monkeypatch):
    monkeypatch.setattr(
        lifecycle.profile_store,
        "get_answers",
        lambda conn: {
            "full_name": "Alex Rivera",
            "email": "ed@example.com",
            "phone": "555-0100",
            "work_authorization": "authorized",
            "start_date": "December 2026",
        },
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(
    msg_id: str,
    subject: str,
    from_header: str,
    body: str = "",
    date: str = "Sun, 3 Aug 2026 21:30:00 -0400",
) -> dict:
    return {
        "id": msg_id,
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_header},
                {"name": "Date", "value": date},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64(body)},
        },
    }


# ---------------------------------------------------------------- matching


def test_match_single_company_candidate():
    candidates = [
        {"posting_id": 1, "company": "Anrok", "title": "Product Engineer"},
        {"posting_id": 2, "company": "Sierra", "title": "Agent Engineer"},
    ]
    match, reason = confirmations.match_message(
        "Thank you for applying to Anrok!", "Anrok <no-reply@ashbyhq.com>", "", candidates
    )
    assert match["posting_id"] == 1
    assert reason == "company"


def test_match_title_disambiguates_same_company():
    candidates = [
        {"posting_id": 1, "company": "Clera", "title": "Founding Engineer - Full Stack"},
        {"posting_id": 2, "company": "Clera", "title": "AI Engineer (Mid-Level)"},
    ]
    match, reason = confirmations.match_message(
        "Thank you for applying to Clera",
        "Clera <no-reply@ashbyhq.com>",
        "We received your application for the AI Engineer (Mid-Level) role.",
        candidates,
    )
    assert match["posting_id"] == 2
    assert reason == "title"


def test_match_ambiguous_when_title_absent():
    candidates = [
        {"posting_id": 1, "company": "Clera", "title": "Founding Engineer"},
        {"posting_id": 2, "company": "Clera", "title": "AI Engineer"},
    ]
    match, reason = confirmations.match_message(
        "Thank you for applying to Clera", "Clera <no-reply@ashbyhq.com>", "no role named", candidates
    )
    assert match is None
    assert reason.startswith("ambiguous:")


def test_match_no_company():
    candidates = [{"posting_id": 1, "company": "Anrok", "title": "Product Engineer"}]
    match, reason = confirmations.match_message(
        "Thank you for applying to Stripe", "Stripe <no-reply@greenhouse.io>", "", candidates
    )
    assert match is None
    assert reason == "no_company_match"


def test_match_company_word_boundary():
    # 'Ramp' must not match inside 'Trampoline'.
    candidates = [{"posting_id": 1, "company": "Ramp", "title": "Engineer"}]
    match, reason = confirmations.match_message(
        "Thanks for applying to Trampoline", "Trampoline <no-reply@ashbyhq.com>", "", candidates
    )
    assert match is None


# ---------------------------------------------------------------- run_scan


def test_scan_confirms_ready_posting(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "a1", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        messages = [
            _message("m1", "Thank you for applying to Anrok!", "Anrok <no-reply@ashbyhq.com>")
        ]
        summary = confirmations.run_scan(conn, messages=messages)

        assert [c["posting_id"] for c in summary["confirmed"]] == [pid]
        app_row = conn.execute(
            "SELECT submitted_at, confirmation_ref FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row["submitted_at"] is not None
        assert app_row["confirmation_ref"] == "gmail:m1"
        stage = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (pid,)
        ).fetchone()["stage"]
        assert stage == "applied"
    finally:
        conn.close()


def test_scan_fills_resume_variant_for_suggested_posting(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        _reviewed_resume(conn)
        pid = _posting(conn, "a2", "Sierra", "Agent Engineer", stage="suggested")

        messages = [
            _message("m2", "Thanks for applying to Sierra", "Sierra <no-reply@ashbyhq.com>")
        ]
        summary = confirmations.run_scan(conn, messages=messages)

        assert [c["posting_id"] for c in summary["confirmed"]] == [pid]
    finally:
        conn.close()


def test_scan_is_idempotent_on_rerun(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "a3", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        messages = [
            _message("m3", "Thank you for applying to Anrok!", "Anrok <no-reply@ashbyhq.com>")
        ]
        first = confirmations.run_scan(conn, messages=messages)
        second = confirmations.run_scan(conn, messages=messages)

        assert len(first["confirmed"]) == 1
        assert second["confirmed"] == []
        assert second["skipped"] == 1
        touches = conn.execute(
            "SELECT COUNT(*) FROM touches WHERE posting_id=?", (pid,)
        ).fetchone()[0]
        assert touches == 2  # the two follow-ups from the single confirm
    finally:
        conn.close()


def test_scan_ignores_non_confirmation_subjects(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    try:
        _posting(conn, "a4", "Anrok", "Product Engineer", stage="ready")
        messages = [
            _message("m4", "New roles at Anrok this week", "Anrok <digest@ashbyhq.com>", body="x"),
            _message("m5", "Anrok order shipped", "orders@example.com"),
        ]
        summary = confirmations.run_scan(conn, messages=messages)
        assert summary["confirmed"] == []
        assert summary["skipped"] == 2
    finally:
        conn.close()


def test_scan_dry_run_marks_nothing(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "a5", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        messages = [
            _message("m6", "Thank you for applying to Anrok!", "Anrok <no-reply@ashbyhq.com>")
        ]
        summary = confirmations.run_scan(conn, dry_run=True, messages=messages)

        assert summary["confirmed"] == [{"posting_id": pid, "dry_run": True}]
        app_row = conn.execute(
            "SELECT submitted_at FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row["submitted_at"] is None
    finally:
        conn.close()


def test_scan_reports_blocked_instead_of_forcing(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        # No reviewed resume variant exists at all -> blocker can't be
        # auto-resolved and the posting must be reported, not confirmed.
        pid = _posting(conn, "a6", "Anrok", "Product Engineer", stage="suggested")
        messages = [
            _message("m7", "Thank you for applying to Anrok!", "Anrok <no-reply@ashbyhq.com>")
        ]
        summary = confirmations.run_scan(conn, messages=messages)

        assert summary["confirmed"] == []
        assert [b["posting_id"] for b in summary["blocked"]] == [pid]
    finally:
        conn.close()
