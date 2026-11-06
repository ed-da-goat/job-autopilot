from __future__ import annotations

from datetime import UTC, datetime, timedelta

from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import report as report_module


def _make_posting(conn, spid: str, company: str = "Acme", title: str = "Backend Engineer", first_seen_at: str = "") -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', ?, '')""",
        (spid, company, title, first_seen_at),
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


def test_conversion_math(tmp_path):
    """qualified -> applied -> replied -> interviewing -> offer, converted =
    ever reached the later stage (not just currently sitting there)."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        # p1: reaches offer (passes through every stage)
        p1 = _make_posting(conn, "p1")
        for stage in ["qualified", "applied", "replied", "interviewing", "offer"]:
            funnel_module.move(conn, p1, stage)

        # p2: reaches applied then passed — still counts as having reached applied
        p2 = _make_posting(conn, "p2")
        funnel_module.move(conn, p2, "qualified")
        funnel_module.move(conn, p2, "applied")
        funnel_module.move(conn, p2, "passed")

        # p3: only reaches qualified
        p3 = _make_posting(conn, "p3")
        funnel_module.move(conn, p3, "qualified")

        data = report_module.build_report(conn)
        assert data.conversion_counts["qualified"] == 3
        assert data.conversion_counts["applied"] == 2
        assert data.conversion_counts["replied"] == 1
        assert data.conversion_counts["interviewing"] == 1
        assert data.conversion_counts["offer"] == 1
    finally:
        conn.close()


def test_top15_excludes_funnel_and_uses_latest_rubric_version(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        in_funnel = _make_posting(conn, "in-funnel", company="InFunnelCo")
        unworked_v1_only = _make_posting(conn, "v1-only", company="StaleCo")
        unworked_v2 = _make_posting(conn, "v2", company="FreshCo")

        funnel_module.ensure_funnel_row(conn, in_funnel)
        _make_score(conn, in_funnel, 1, 90)
        _make_score(conn, in_funnel, 2, 90)

        # scored only at v1 — a real v2 sweep would rescore everything, but a
        # posting missing from v2 must not show up in a v2-scoped top-15
        _make_score(conn, unworked_v1_only, 1, 95)

        _make_score(conn, unworked_v2, 2, 80)

        data = report_module.build_report(conn)
        assert data.rubric_version == 2
        ids = [row["posting_id"] for row in data.top15]
        assert in_funnel not in ids
        assert unworked_v1_only not in ids
        assert unworked_v2 in ids
    finally:
        conn.close()


def test_apply_rate_warning_fires_with_zero_applications_in_7d(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        data = report_module.build_report(conn)
        assert data.applications_7d == 0
        report_text = report_module.format_report(data)
        assert "WARNING: 0 applications in the last 7 days" in report_text
    finally:
        conn.close()


def test_blocked_role_in_active_funnel_is_surfaced(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn, "blocked", company="Scale", title="Director")
        _make_score(conn, posting_id, 6, 65)
        conn.execute(
            """UPDATE eligibility_assessments
               SET status='blocked', blockers='["seniority_title"]',
                   evidence='["Blocked seniority title: director"]'
               WHERE posting_id=? AND rubric_version=6""",
            (posting_id,),
        )
        funnel_module.move(conn, posting_id, "qualified")

        data = report_module.build_report(conn)
        assert [row["posting_id"] for row in data.blocked_funnel] == [posting_id]
        rendered = report_module.format_report(data)
        assert "Blocked roles still in active funnel" in rendered
        assert "seniority_title" in rendered
    finally:
        conn.close()


def test_apply_rate_warning_absent_with_recent_application(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn, "p1")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (posting_id, now, now, now),
        )
        conn.commit()
        data = report_module.build_report(conn)
        assert data.applications_7d == 1
        assert "WARNING: 0 applications in the last 7 days" not in report_module.format_report(data)
    finally:
        conn.close()


def test_warm_touches_and_new_postings_7d(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC).isoformat()
        posting_id = _make_posting(conn, "p1", first_seen_at=now)
        conn.execute(
            """INSERT INTO touches
               (posting_id, kind, status, completed_at, created_at, updated_at)
               VALUES (?, 'intro_request', 'completed', ?, ?, ?)""",
            (posting_id, now, now, now),
        )
        conn.execute(
            "UPDATE postings SET discovered_after_baseline=1 WHERE id=?", (posting_id,)
        )
        conn.commit()

        stale_posting = _make_posting(conn, "p2", first_seen_at="2020-01-01T00:00:00+00:00")

        data = report_module.build_report(conn)
        assert data.warm_touches_7d == 1
        assert data.new_postings_7d == 1  # only p1's first_seen_at is recent
        assert stale_posting  # keep linter happy about unused-looking var
    finally:
        conn.close()


def test_next_actions_due_filters_by_date(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        due = _make_posting(conn, "due")
        not_due = _make_posting(conn, "not-due")

        yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        next_week = (datetime.now(UTC) + timedelta(days=7)).date().isoformat()

        funnel_module.set_field(conn, due, "next_action_date", yesterday)
        funnel_module.set_field(conn, not_due, "next_action_date", next_week)

        data = report_module.build_report(conn)
        ids = [row["posting_id"] for row in data.next_actions]
        assert due in ids
        assert not_due not in ids
    finally:
        conn.close()


def test_terminal_roles_do_not_remain_overdue(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _make_posting(conn, "closed-due")
        yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        funnel_module.set_field(conn, posting_id, "next_action_date", yesterday)
        funnel_module.move(conn, posting_id, "closed")
        assert report_module.build_report(conn).next_actions == []
    finally:
        conn.close()


def test_response_rate_counts_any_human_reply_after_submission(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC).isoformat()

        # p1: submitted and replied — counts toward numerator and denominator
        p1 = _make_posting(conn, "p1")
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (p1, now, now, now),
        )
        conn.commit()
        funnel_module.move(conn, p1, "applied")
        funnel_module.move(conn, p1, "replied")

        # p2: submitted, never replied — denominator only
        p2 = _make_posting(conn, "p2")
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (p2, now, now, now),
        )
        conn.commit()
        funnel_module.move(conn, p2, "applied")

        # p3: reached 'replied' without ever submitting an application — must
        # not count in the numerator
        p3 = _make_posting(conn, "p3")
        funnel_module.move(conn, p3, "replied")

        data = report_module.build_report(conn)
        assert data.submitted_total == 2
        assert data.replied_total == 1
        assert "response rate: 1/2 (50%)" in report_module.format_report(data)
    finally:
        conn.close()


def test_response_rate_shows_na_with_zero_submitted(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        data = report_module.build_report(conn)
        assert data.submitted_total == 0
        assert data.replied_total == 0
        assert "response rate: 0/0 (n/a)" in report_module.format_report(data)
    finally:
        conn.close()


def test_tier1_supply_apps7d_and_response_rate_by_tier(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC).isoformat()

        # tier-1 supply: not-yet-suggested, eligible, active, score >= 70
        supply1 = _make_posting(conn, "supply1", company="SupplyOne")
        _make_score(conn, supply1, 1, 75)

        # tier-1 supply: sitting in 'suggested' stage, score >= 70
        supply2 = _make_posting(conn, "supply2", company="SupplyTwo")
        _make_score(conn, supply2, 1, 80)
        funnel_module.move(conn, supply2, "suggested", note="auto-suggested (score 80)")

        # below threshold — not tier-1 supply
        below = _make_posting(conn, "below", company="BelowCo")
        _make_score(conn, below, 1, 65)

        # already past 'suggested' — not counted as supply
        past = _make_posting(conn, "past", company="PastCo")
        _make_score(conn, past, 1, 90)
        funnel_module.move(conn, past, "suggested", note="auto-suggested (score 90)")
        funnel_module.move(conn, past, "qualified")

        # tier-1 applicant: submitted + replied, suggested without fallback note
        t1 = _make_posting(conn, "t1", company="Tier1Co")
        _make_score(conn, t1, 1, 85)
        funnel_module.move(conn, t1, "suggested", note="auto-suggested (score 85)")
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (t1, now, now, now),
        )
        conn.commit()
        funnel_module.move(conn, t1, "applied")
        funnel_module.move(conn, t1, "replied")

        # fallback applicant: submitted, not replied, suggested via fallback-fill
        fb = _make_posting(conn, "fb", company="FallbackCo")
        _make_score(conn, fb, 1, 62)
        funnel_module.move(conn, fb, "suggested", note="fallback-fill (score 62)")
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (fb, now, now, now),
        )
        conn.commit()
        funnel_module.move(conn, fb, "applied")

        data = report_module.build_report(conn)

        assert data.tier1_supply == 2
        assert data.applications_7d_by_tier == {"tier1": 1, "fallback": 1}
        assert data.submitted_by_tier == {"tier1": 1, "fallback": 1}
        assert data.replied_by_tier == {"tier1": 1, "fallback": 0}

        rendered = report_module.format_report(data)
        assert "tier-1 supply: 2 appliable (gate: 10-12 apps/week)" in rendered
        assert "applications 7d: 2 (tier-1 1 / fallback 1)" in rendered
        assert "WARNING: below the 10-12 apps/week gate" in rendered
        assert "response rate by tier: tier-1 1/1 (100%) · fallback 0/1 (0%)" in rendered
    finally:
        conn.close()


def test_response_rate_by_resume_variant(tmp_path):
    """A 'Tailored' resume variant gets its own callback rate next to the
    generic variants; applications with no variant show as 'none'."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO resume_variants (label, path, created_at, updated_at)
               VALUES ('Tailored (RM)', 'out/', '', '')"""
        )
        tailored_id = conn.execute("SELECT id FROM resume_variants WHERE label='Tailored (RM)'").fetchone()["id"]

        def apply(spid: str, variant_id: int | None, replied: bool, submitted: bool = True) -> None:
            pid = _make_posting(conn, spid)
            conn.execute(
                """INSERT INTO applications (posting_id, submitted_at, resume_variant_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (pid, now if submitted else None, variant_id, now, now),
            )
            conn.commit()
            funnel_module.move(conn, pid, "applied")
            if replied:
                funnel_module.move(conn, pid, "replied")
                funnel_module.move(conn, pid, "interviewing")

        apply("t1", tailored_id, replied=True)
        apply("t2", tailored_id, replied=False)
        apply("t3", tailored_id, replied=False, submitted=False)
        apply("n1", None, replied=False)
        apply("n2", None, replied=False)
        apply("n3", None, replied=True)

        data = report_module.build_report(conn)
        assert data.response_by_variant == {"Tailored (RM)": (1, 2), "none": (1, 3)}

        rendered = report_module.format_report(data)
        assert "  response rate by resume: none 1/3 (33%) · Tailored (RM) 1/2 (50%)" in rendered
    finally:
        conn.close()


def test_fetch_pipeline_rows_filters_by_stage(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        p1 = _make_posting(conn, "p1")
        p2 = _make_posting(conn, "p2")
        funnel_module.move(conn, p1, "qualified")
        funnel_module.move(conn, p2, "applied")

        rows = report_module.fetch_pipeline_rows(conn, "qualified")
        assert [r["posting_id"] for r in rows] == [p1]

        all_rows = report_module.fetch_pipeline_rows(conn)
        assert {r["posting_id"] for r in all_rows} == {p1, p2}
    finally:
        conn.close()


def _make_submitted(conn, posting_id: int, submitted: datetime) -> None:
    ts = submitted.isoformat()
    conn.execute(
        "INSERT INTO applications (posting_id, submitted_at, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (posting_id, ts, ts, ts),
    )


def _make_open(conn, posting_id: int, at: datetime) -> None:
    conn.execute(
        "INSERT INTO activity_events (entity_type, entity_id, action, data, at) VALUES ('posting', ?, 'apply_lite_open', '{}', ?)",
        (posting_id, at.isoformat()),
    )


def test_takt_median_minutes_from_open_to_submit(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC)
        p1, p2, p3, p4 = (_make_posting(conn, f"t{i}") for i in range(4))
        _make_submitted(conn, p1, now)
        _make_open(conn, p1, now - timedelta(minutes=5))
        _make_submitted(conn, p2, now)
        _make_open(conn, p2, now - timedelta(minutes=15))
        # no open event -> excluded
        _make_submitted(conn, p3, now)
        # open event after submit -> excluded
        _make_submitted(conn, p4, now - timedelta(minutes=30))
        _make_open(conn, p4, now)
        conn.commit()
        data = report_module.build_report(conn)
        assert data.takt_samples == 2
        assert data.takt_median_minutes == 10
        assert data.takt_samples_7d == 2
        assert (
            "Minutes per application (open→submit, median of 2): 10.0 (2 in last 7d)"
            in report_module.format_report(data)
        )
    finally:
        conn.close()


def test_takt_ignores_stale_open_and_uses_latest_open(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        now = datetime.now(UTC)
        p1, p2 = _make_posting(conn, "s1"), _make_posting(conn, "s2")
        _make_submitted(conn, p1, now)
        _make_open(conn, p1, now - timedelta(days=3))  # stale tab -> excluded
        _make_submitted(conn, p2, now)
        _make_open(conn, p2, now - timedelta(minutes=90))
        _make_open(conn, p2, now - timedelta(minutes=8))  # latest wins
        conn.commit()
        data = report_module.build_report(conn)
        assert data.takt_samples == 1
        assert data.takt_median_minutes == 8
    finally:
        conn.close()


def test_takt_no_data_line(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        data = report_module.build_report(conn)
        assert data.takt_samples == 0
        assert data.takt_median_minutes is None
        assert (
            "Minutes per application: no data yet (apply-lite open events: 0)"
            in report_module.format_report(data)
        )
    finally:
        conn.close()
