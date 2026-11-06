from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobhunt import autopilot as autopilot_module
from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import lifecycle as lifecycle_module
from jobhunt.apply_assist import profile_store as profile_store_module

REQUIRED_ANSWERS = {
    "full_name": "Alex Rivera",
    "email": "alex.rivera@example.com",
    "phone": "555-555-5555",
    "work_authorization": "US citizen",
    "start_date": "2026-12-01",  # earliest_start preference field aliases to start_date
}


def _make_posting(conn, spid: str = "p1", company: str = "Acme", title: str = "Backend Engineer") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', '', '')""",
        (spid, company, title),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def _seed_answers(conn, skip: set[str] | None = None) -> None:
    skip = skip or set()
    for key, value in REQUIRED_ANSWERS.items():
        if key in skip:
            continue
        profile_store_module.set_answer(conn, key, value)


def _seed_eligible_assessment(conn, posting_id: int, status: str = "eligible") -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO eligibility_assessments
               (posting_id, rubric_version, preferences_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, 1, 1, ?, '[]', '[]', '[]', ?)""",
        (posting_id, status, now),
    )
    conn.commit()


def _reviewed_variant(conn, label: str = "Founder Engineer 2026") -> int:
    row = lifecycle_module.save_resume_variant(
        conn, label=label, path=f"resume/{label}.pdf", reviewed=True
    )
    return row["id"]


def test_fully_ready_qualified_posting_advances_to_ready(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "qualified")
        _seed_answers(conn)
        _seed_eligible_assessment(conn, posting_id)
        _reviewed_variant(conn)

        result = autopilot_module.run_autopilot(conn)

        assert result["advanced"] == [posting_id]
        row = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["stage"] == "ready"
        assert row["next_action"] == autopilot_module.READY_ACTION
        assert row["next_action_date"] == datetime.now(UTC).date().isoformat()
    finally:
        conn.close()


def test_missing_answer_posting_parks_and_preserves_manual_next_action(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "qualified")
        _seed_answers(conn, skip={"work_authorization"})
        _seed_eligible_assessment(conn, posting_id)
        _reviewed_variant(conn)

        result = autopilot_module.run_autopilot(conn)

        row = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["stage"] == "qualified"
        assert row["next_action"] == "unblock: missing_required_answer:work_authorization"
        assert result["parked"] == {"missing_required_answer:work_authorization": 1}

        # The candidate overwrites the auto-set next_action with a manual promise.
        funnel_module.set_field(conn, posting_id, "next_action", "waiting on visa answer from the candidate")

        autopilot_module.run_autopilot(conn)

        row2 = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row2["next_action"] == "waiting on visa answer from the candidate"
    finally:
        conn.close()


def test_existing_application_targeted_update_preserves_notes(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "qualified")
        lifecycle_module.ensure_application(
            conn, posting_id, channel="referral", notes="warm intro via Jane"
        )
        vid = _reviewed_variant(conn)

        result = autopilot_module.run_autopilot(conn)

        assert result["resume_attached"] == [posting_id]
        app_row = conn.execute("SELECT * FROM applications WHERE posting_id=?", (posting_id,)).fetchone()
        assert app_row["resume_variant_id"] == vid
        assert app_row["notes"] == "warm intro via Jane"
        assert app_row["channel"] == "referral"
    finally:
        conn.close()


def test_unrelated_reviewed_variants_no_auto_attach(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "qualified")
        _reviewed_variant(conn, label="Variant A")
        _reviewed_variant(conn, label="Variant B")
        # Neither label is the one the selector wants -> no silent fallback.

        result = autopilot_module.run_autopilot(conn)

        assert result["resume_attached"] == []
        app_row = conn.execute("SELECT * FROM applications WHERE posting_id=?", (posting_id,)).fetchone()
        assert app_row is None
    finally:
        conn.close()


def test_applied_row_with_blank_next_action_gets_follow_up(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn)
        funnel_module.move(conn, posting_id, "applied")
        # move() to 'applied' auto-sets a follow-up next_action; blank it out
        # to exercise autopilot's own blank-fill path.
        conn.execute(
            "UPDATE funnel SET next_action=NULL, next_action_date=NULL WHERE posting_id=?",
            (posting_id,),
        )
        conn.commit()

        result = autopilot_module.run_autopilot(conn)

        row = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["next_action"] == "follow up"
        expected_date = (datetime.now(UTC) + timedelta(days=funnel_module.FOLLOW_UP_DAYS)).date().isoformat()
        assert row["next_action_date"] == expected_date
        assert result["next_actions_set"] == 1
    finally:
        conn.close()


def test_autopilot_digest_lines_after_run(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        ready_posting = _make_posting(conn, spid="ready1")
        funnel_module.move(conn, ready_posting, "qualified")
        _seed_answers(conn)
        _seed_eligible_assessment(conn, ready_posting)
        _reviewed_variant(conn)

        parked_posting = _make_posting(conn, spid="parked1")
        funnel_module.move(conn, parked_posting, "qualified")
        # No eligibility assessment -> blocked on eligibility_not_assessed
        # (application/resume already satisfied by the single reviewed variant above).

        autopilot_module.run_autopilot(conn)

        today = datetime.now(UTC).date().isoformat()
        lines = autopilot_module.autopilot_digest_lines(conn, today)

        assert lines[0] == "- advanced to ready: 1"
        assert any(line.startswith("- parked: eligibility_not_assessed x") for line in lines[1:])
    finally:
        conn.close()


def test_autopilot_digest_lines_empty_when_no_events(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        assert autopilot_module.autopilot_digest_lines(conn, "2020-01-01") == []
    finally:
        conn.close()
