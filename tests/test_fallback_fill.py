from __future__ import annotations

import json
from datetime import UTC, datetime

from jobhunt import db as db_module
from jobhunt import suggest as suggest_module


def _make_posting(conn, spid: str, company: str = "Acme", title: str = "Backend Engineer") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', '', '')""",
        (spid, company, title),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def _make_score(
    conn,
    posting_id: int,
    rubric_version: int,
    total: int,
    excluded: int = 0,
    founder_leverage: int = 0,
    eligible: bool = True,
) -> None:
    components = json.dumps({"founder_leverage": founder_leverage})
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, exclude_reason, scored_at)
           VALUES (?, ?, ?, ?, '[]', ?, NULL, '')""",
        (posting_id, rubric_version, total, components, excluded),
    )
    status = "eligible" if eligible else "blocked"
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, ?, 1, ?, '[]', '[]', '[]', '')""",
        (posting_id, rubric_version, status),
    )
    conn.commit()


def _log_suggested_event(conn, posting_id: int, when: datetime | None = None) -> None:
    at = (when or datetime.now(UTC)).isoformat()
    conn.execute(
        "INSERT INTO funnel_events (posting_id, from_stage, to_stage, note, at) VALUES (?, 'harvested', 'suggested', '', ?)",
        (posting_id, at),
    )
    conn.commit()


def test_quota_already_met_returns_empty(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        for i in range(3):
            pid = _make_posting(conn, f"t1-{i}")
            _make_score(conn, pid, 1, 80)
            _log_suggested_event(conn, pid)

        gap_fillers = _make_posting(conn, "gap-candidate")
        _make_score(conn, gap_fillers, 1, 60)

        result = suggest_module.run_weekly_fallback_fill(conn, quota=3)
        assert result == []
        row = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (gap_fillers,)).fetchone()
        assert row is None
    finally:
        conn.close()


def test_gap_fills_with_eligible_non_discriminator_band_ordered_by_total(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        tier1 = _make_posting(conn, "t1")
        _make_score(conn, tier1, 1, 80)
        _log_suggested_event(conn, tier1)

        low = _make_posting(conn, "low", company="LowCo")
        _make_score(conn, low, 1, 55)
        high = _make_posting(conn, "high", company="HighCo")
        _make_score(conn, high, 1, 65)

        result = suggest_module.run_weekly_fallback_fill(conn, quota=3)
        assert result == [high, low]

        for pid in (high, low):
            row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (pid,)).fetchone()
            assert row["stage"] == "suggested"
    finally:
        conn.close()


def test_discriminator_exclusion_founder_leverage_and_category(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        conn.execute(
            "INSERT INTO companies (name, category) VALUES ('FinCo', 'fintech')"
        )
        conn.commit()

        high_leverage = _make_posting(conn, "hl", company="Acme")
        _make_score(conn, high_leverage, 1, 60, founder_leverage=6)

        fintech = _make_posting(conn, "fc", company="FinCo")
        _make_score(conn, fintech, 1, 62)

        clean = _make_posting(conn, "clean", company="CleanCo")
        _make_score(conn, clean, 1, 58)

        result = suggest_module.run_weekly_fallback_fill(conn, quota=5)
        assert result == [clean]
    finally:
        conn.close()


def test_eligibility_gate_excludes_ineligible_excluded_inactive_and_in_funnel(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        ineligible = _make_posting(conn, "ineligible")
        _make_score(conn, ineligible, 1, 60, eligible=False)

        excluded = _make_posting(conn, "excluded")
        _make_score(conn, excluded, 1, 60, excluded=1)

        inactive = _make_posting(conn, "inactive")
        _make_score(conn, inactive, 1, 60)
        conn.execute("UPDATE postings SET status='closed' WHERE id=?", (inactive,))
        conn.commit()

        already_funnel = _make_posting(conn, "already")
        _make_score(conn, already_funnel, 1, 60)
        conn.execute(
            "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, 'qualified', '', '')",
            (already_funnel,),
        )
        conn.commit()

        result = suggest_module.run_weekly_fallback_fill(conn, quota=5)
        assert result == []
    finally:
        conn.close()


def test_postings_scoring_ge_70_never_pulled_by_fallback(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        top = _make_posting(conn, "top")
        _make_score(conn, top, 1, 90)

        result = suggest_module.run_weekly_fallback_fill(conn, quota=5)
        assert result == []
        row = conn.execute("SELECT * FROM funnel WHERE posting_id=?", (top,)).fetchone()
        assert row is None
    finally:
        conn.close()
