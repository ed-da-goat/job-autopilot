from __future__ import annotations

from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import report as report_module
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


def _make_score(conn, posting_id: int, rubric_version: int, total: int, excluded: int = 0) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, exclude_reason, scored_at)
           VALUES (?, ?, ?, '{}', '[]', ?, NULL, '')""",
        (posting_id, rubric_version, total, excluded),
    )
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, ?, 1, 'eligible', '[]', '[]', '[]', '')""",
        (posting_id, rubric_version),
    )
    conn.commit()


def test_threshold_respected(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        above = _make_posting(conn, "above")
        below = _make_posting(conn, "below")
        _make_score(conn, above, 1, 70)
        _make_score(conn, below, 1, 60)

        ids = suggest_module.run_suggest(conn, threshold=65, limit=20)
        assert ids == [above]

        row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (above,)).fetchone()
        assert row["stage"] == "suggested"
        assert conn.execute("SELECT * FROM funnel WHERE posting_id=?", (below,)).fetchone() is None
    finally:
        conn.close()


def test_backfill_limit_respected(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        ids_in_order = []
        for i in range(5):
            pid = _make_posting(conn, f"p{i}")
            _make_score(conn, pid, 1, 90 - i)  # descending scores
            ids_in_order.append(pid)

        suggested = suggest_module.run_suggest(conn, threshold=65, limit=3)
        assert len(suggested) == 3
        # top-3 by score, i.e. the first three inserted (descending score)
        assert suggested == ids_in_order[:3]
    finally:
        conn.close()


def test_daily_run_path_new_ids_uncapped_stragglers_capped(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        new_ids = []
        for i in range(4):
            pid = _make_posting(conn, f"new{i}")
            _make_score(conn, pid, 1, 80)
            new_ids.append(pid)
        # qualifying postings NOT in new_posting_ids (harvested outside a
        # daily run) — swept as stragglers, best-score-first, capped at limit
        stragglers = []
        for i in range(3):
            pid = _make_posting(conn, f"straggler{i}")
            _make_score(conn, pid, 1, 99 - i)
            stragglers.append(pid)

        suggested = suggest_module.run_suggest(conn, new_posting_ids=new_ids, threshold=65, limit=2)
        # all 4 new ids despite limit=2 (no cap on the new-id pass), plus
        # the top-2 stragglers under the cap
        assert set(suggested) == set(new_ids) | set(stragglers[:2])
        assert stragglers[2] not in suggested
    finally:
        conn.close()


def test_taktile_gap_regression_stragglers_swept_even_with_no_new_ids(tmp_path):
    """2026-08-05 Taktile gap: postings harvested+scored outside a daily run
    (manual CLI harvest, ingester, a daily that died between harvest and
    suggest) are never in a later run's new-id set, so the daily-run path
    must still sweep them in — including when the run harvested nothing."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        straggler = _make_posting(conn, "taktile", company="Taktile")
        _make_score(conn, straggler, 1, 81)
        assert suggest_module.run_suggest(conn, new_posting_ids=[]) == [straggler]
        row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (straggler,)).fetchone()
        assert row["stage"] == "suggested"
    finally:
        conn.close()


def test_never_resuggests_any_existing_funnel_row(tmp_path):
    """Includes 'passed' — a dismissed suggestion stays dismissed."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        already_qualified = _make_posting(conn, "qualified")
        _make_score(conn, already_qualified, 1, 90)
        funnel_module.move(conn, already_qualified, "qualified")

        already_passed = _make_posting(conn, "passed")
        _make_score(conn, already_passed, 1, 90)
        funnel_module.move(conn, already_passed, "passed")

        fresh = _make_posting(conn, "fresh")
        _make_score(conn, fresh, 1, 90)

        ids = suggest_module.run_suggest(conn, threshold=65, limit=20)
        assert ids == [fresh]
    finally:
        conn.close()


def test_excludes_excluded_scores_and_inactive_postings(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        excluded = _make_posting(conn, "excl")
        _make_score(conn, excluded, 1, 90, excluded=1)

        inactive = _make_posting(conn, "inactive")
        _make_score(conn, inactive, 1, 90)
        conn.execute("UPDATE postings SET status='closed' WHERE id=?", (inactive,))
        conn.commit()

        ids = suggest_module.run_suggest(conn, threshold=65, limit=20)
        assert ids == []
    finally:
        conn.close()


def test_suggested_excluded_from_conversion_counts(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        pid = _make_posting(conn, "p1")
        _make_score(conn, pid, 1, 90)
        suggest_module.run_suggest(conn, threshold=65, limit=20)

        data = report_module.build_report(conn)
        assert data.stage_counts["suggested"] == 1
        # CONVERSION_STAGES is qualified..offer only — a suggested-only
        # posting must not count toward any of them
        assert all(count == 0 for count in data.conversion_counts.values())
    finally:
        conn.close()


def test_applied_follow_up_hook_sets_next_action_once(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        pid = _make_posting(conn, "p1")
        funnel_module.move(conn, pid, "applied")
        row = conn.execute("SELECT next_action, next_action_date FROM funnel WHERE posting_id=?", (pid,)).fetchone()
        assert row["next_action"] == "follow up"
        assert row["next_action_date"] is not None

        # moving away and re-applying must not clobber a next_action that
        # was already set (either by the hook or manually)
        funnel_module.set_field(conn, pid, "next_action", "custom follow-up")
        funnel_module.move(conn, pid, "replied")
        funnel_module.move(conn, pid, "applied")
        row2 = conn.execute("SELECT next_action FROM funnel WHERE posting_id=?", (pid,)).fetchone()
        assert row2["next_action"] == "custom follow-up"
    finally:
        conn.close()


def test_applied_follow_up_hook_skips_when_next_action_already_set(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        pid = _make_posting(conn, "p1")
        funnel_module.set_field(conn, pid, "next_action", "ping recruiter")
        funnel_module.move(conn, pid, "applied")
        row = conn.execute("SELECT next_action FROM funnel WHERE posting_id=?", (pid,)).fetchone()
        assert row["next_action"] == "ping recruiter"
    finally:
        conn.close()


def test_surge_id_list_exceeding_sqlite_variable_limit(tmp_path):
    """Regression: 2026-08-06 daily crash — a 33K-id surge harvest passed one
    IN(...) placeholder per id and blew SQLite's max-variable limit. The id
    filter must be chunked so an arbitrarily large new_posting_ids list works."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        real = _make_posting(conn, "surge-real")
        _make_score(conn, real, 1, 80)

        huge = [real] + list(range(1_000_000, 1_040_000))  # 40,001 ids
        ids = suggest_module.run_suggest(conn, new_posting_ids=huge, threshold=65, limit=20)
        assert real in ids

        row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (real,)).fetchone()
        assert row["stage"] == "suggested"
    finally:
        conn.close()
