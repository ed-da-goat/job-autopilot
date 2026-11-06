from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunt import db, funnel, lifecycle


def _posting(conn, spid: str = "p1") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, 'Acme', 'New Grad Engineer', 'New York, NY', 'active', '', '')""",
        (spid,),
    )
    posting_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, 3, 1, 'eligible', '[]', '[]', '[]', '')""",
        (posting_id,),
    )
    conn.commit()
    return posting_id


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


def test_full_manual_lifecycle(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        posting_id = _posting(conn)
        funnel.move(conn, posting_id, "suggested")
        funnel.move(conn, posting_id, "qualified")
        funnel.move(conn, posting_id, "tailored")
        resume = lifecycle.save_resume_variant(
            conn, label="Founder", path="resume/founder.pdf", reviewed=True
        )
        lifecycle.ensure_application(conn, posting_id, resume_variant_id=resume["id"])
        assert lifecycle.mark_ready(conn, posting_id)["ready"] is True
        application = lifecycle.confirm_applied(conn, posting_id, confirmed=True)
        assert application["submitted_at"]

        contact = lifecycle.create_contact(conn, name="Alex", company="Acme")
        touch = lifecycle.create_touch(
            conn,
            kind="intro_request",
            contact_id=contact["id"],
            posting_id=posting_id,
        )
        assert lifecycle.complete_touch(conn, touch["id"])["status"] == "completed"
        interview = lifecycle.create_interview(
            conn,
            posting_id,
            round_name="Technical",
            scheduled_at=datetime.now(UTC).isoformat(),
        )
        lifecycle.update_interview(conn, interview["id"], thank_you_completed=True)
        lifecycle.create_offer(conn, posting_id, deadline="2026-08-01")

        stage = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()["stage"]
        assert stage == "offer"
        assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] >= 10
    finally:
        conn.close()


def test_readiness_allows_drafting_but_blocks_unreviewed_resume(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        posting_id = _posting(conn)
        resume = lifecycle.save_resume_variant(
            conn, label="Draft", path="resume/draft.pdf", reviewed=False
        )
        lifecycle.ensure_application(conn, posting_id, resume_variant_id=resume["id"])
        readiness = lifecycle.application_readiness(conn, posting_id)
        assert readiness["ready"] is False
        assert "resume_not_reviewed" in readiness["blockers"]
        assert lifecycle.create_artifact(conn, posting_id, "follow_up")["content"]
        with pytest.raises(ValueError, match="not ready"):
            lifecycle.mark_ready(conn, posting_id)
    finally:
        conn.close()


def test_mark_applied_requires_explicit_confirmation(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        posting_id = _posting(conn)
        with pytest.raises(ValueError, match="manual submission confirmation"):
            lifecycle.confirm_applied(conn, posting_id, confirmed=False)
    finally:
        conn.close()


def test_confirm_applied_schedules_two_follow_up_touches(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        posting_id = _posting(conn)
        resume = lifecycle.save_resume_variant(
            conn, label="Founder", path="resume/founder.pdf", reviewed=True
        )
        lifecycle.ensure_application(conn, posting_id, resume_variant_id=resume["id"])
        submitted_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        lifecycle.confirm_applied(conn, posting_id, confirmed=True, submitted_at=submitted_at)

        touches = conn.execute(
            "SELECT * FROM touches WHERE posting_id=? AND kind='follow_up' ORDER BY due_at",
            (posting_id,),
        ).fetchall()
        assert len(touches) == 2
        assert all(t["status"] == "planned" for t in touches)
        expected_5 = (datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC) + timedelta(days=5)).isoformat()
        expected_10 = (datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC) + timedelta(days=10)).isoformat()
        assert touches[0]["due_at"] == expected_5
        assert touches[1]["due_at"] == expected_10
    finally:
        conn.close()


def test_confirm_applied_does_not_duplicate_touches_on_rerun(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        posting_id = _posting(conn)
        resume = lifecycle.save_resume_variant(
            conn, label="Founder", path="resume/founder.pdf", reviewed=True
        )
        lifecycle.ensure_application(conn, posting_id, resume_variant_id=resume["id"])
        submitted_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC).isoformat()
        lifecycle.confirm_applied(conn, posting_id, confirmed=True, submitted_at=submitted_at)
        lifecycle.confirm_applied(conn, posting_id, confirmed=True, submitted_at=submitted_at)

        touches = conn.execute(
            "SELECT COUNT(*) FROM touches WHERE posting_id=? AND kind='follow_up'",
            (posting_id,),
        ).fetchone()[0]
        assert touches == 2
    finally:
        conn.close()


def test_structured_pass_reason(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    try:
        posting_id = _posting(conn)
        lifecycle.pass_posting(conn, posting_id, "geography", "onsite outside US")
        row = conn.execute(
            "SELECT stage, pass_reason, pass_detail FROM funnel WHERE posting_id=?",
            (posting_id,),
        ).fetchone()
        assert tuple(row) == ("passed", "geography", "onsite outside US")
    finally:
        conn.close()
