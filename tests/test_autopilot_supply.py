from __future__ import annotations

from datetime import UTC, datetime

from jobhunt import autopilot as autopilot_module
from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import lifecycle as lifecycle_module
from tests.test_autopilot import (
    _make_posting,
    _reviewed_variant,
    _seed_answers,
    _seed_eligible_assessment,
)


def _score(conn, posting_id: int, total: int, rubric_version: int = 8, excluded: int = 0) -> None:
    conn.execute(
        "INSERT INTO scores (posting_id, rubric_version, total, excluded) VALUES (?, ?, ?, ?)",
        (posting_id, rubric_version, total, excluded),
    )
    conn.commit()


def _suggested(conn, spid: str, total: int, **score_kwargs) -> int:
    posting_id = _make_posting(conn, spid=spid)
    funnel_module.move(conn, posting_id, "suggested")
    _seed_eligible_assessment(conn, posting_id)
    _score(conn, posting_id, total, **score_kwargs)
    return posting_id


def _stage(conn, posting_id: int) -> str:
    return conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()["stage"]


def test_suggested_high_score_without_blockers_advances(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn)
        _reviewed_variant(conn)
        high = _suggested(conn, "hi", 70)
        low = _suggested(conn, "lo", 65)
        excluded = _suggested(conn, "ex", 90, excluded=1)

        result = autopilot_module.run_autopilot(conn)

        assert result["advanced"] == [high]
        assert _stage(conn, high) == "ready"
        assert _stage(conn, low) == "suggested"
        assert _stage(conn, excluded) == "suggested"
    finally:
        conn.close()


def test_suggested_scored_only_on_old_rubric_does_not_advance(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn)
        _reviewed_variant(conn)
        stale = _suggested(conn, "stale", 90, rubric_version=7)
        fresh = _suggested(conn, "fresh", 40, rubric_version=8)

        result = autopilot_module.run_autopilot(conn)

        assert result["advanced"] == []
        assert _stage(conn, stale) == "suggested"
        assert _stage(conn, fresh) == "suggested"
    finally:
        conn.close()


def test_suggested_high_score_parks_and_digest_counts_every_blocker(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn, skip={"phone"})
        # No reviewed variant -> select_resume_variant; phone answer missing.
        first = _suggested(conn, "a", 80)
        second = _suggested(conn, "b", 75)

        result = autopilot_module.run_autopilot(conn)

        assert result["advanced"] == []
        assert _stage(conn, first) == "suggested"
        assert _stage(conn, second) == "suggested"
        today = datetime.now(UTC).date().isoformat()
        lines = autopilot_module.autopilot_digest_lines(conn, today)
        assert lines[0] == "- advanced to ready: 0"
        assert "- parked: select_resume_variant x2" in lines
        assert "- parked: missing:phone x2" in lines
    finally:
        conn.close()


def _variant_id(conn, posting_id: int):
    row = conn.execute("SELECT resume_variant_id FROM applications WHERE posting_id=?", (posting_id,)).fetchone()
    return None if row is None else row["resume_variant_id"]


def _qualified(conn, spid: str, title: str) -> int:
    posting_id = _make_posting(conn, spid=spid, title=title)
    funnel_module.move(conn, posting_id, "qualified")
    _seed_eligible_assessment(conn, posting_id)
    return posting_id


def test_finance_title_gets_finance_variant_when_both_reviewed(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn)
        founder = _reviewed_variant(conn, "Founder Engineer 2026")
        finance = _reviewed_variant(conn, "Finance x AI 2026")
        fin_post = _qualified(conn, "fin", "Quantitative Analyst")
        eng_post = _qualified(conn, "eng", "Founding Engineer")

        autopilot_module.run_autopilot(conn)

        assert _variant_id(conn, fin_post) == finance
        assert _variant_id(conn, eng_post) == founder
        assert _stage(conn, fin_post) == "ready"
    finally:
        conn.close()


def test_finance_title_with_unreviewed_finance_variant_parks(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn)
        _reviewed_variant(conn, "Founder Engineer 2026")
        lifecycle_module.save_resume_variant(
            conn, label="Finance x AI 2026", path="resume/finance.pdf", reviewed=False
        )
        fin_post = _qualified(conn, "fin", "Investment Analyst")

        result = autopilot_module.run_autopilot(conn)

        assert result["resume_attached"] == []
        assert _variant_id(conn, fin_post) is None
        assert _stage(conn, fin_post) == "qualified"
        today = datetime.now(UTC).date().isoformat()
        lines = autopilot_module.autopilot_digest_lines(conn, today)
        assert "- parked: select_resume_variant x1" in lines
    finally:
        conn.close()


def test_unmapped_title_defaults_to_founder_variant(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn)
        founder = _reviewed_variant(conn, "Founder Engineer 2026")
        _reviewed_variant(conn, "Finance x AI 2026")
        post = _qualified(conn, "log", "Logistics Coordinator")

        autopilot_module.run_autopilot(conn)

        assert _variant_id(conn, post) == founder
    finally:
        conn.close()


def test_attach_to_existing_application_logs_reason(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _seed_answers(conn)
        finance = _reviewed_variant(conn, "Finance x AI 2026")
        post = _qualified(conn, "fin", "Fintech Product Manager")
        lifecycle_module.ensure_application(conn, post)

        autopilot_module.run_autopilot(conn)

        assert _variant_id(conn, post) == finance
        data = conn.execute(
            "SELECT data FROM activity_events WHERE action='autopilot_resume_attached'"
        ).fetchone()["data"]
        assert "title~'fintech'" in data
    finally:
        conn.close()
