from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunt import db, funnel
from jobhunt.eligibility import load_preferences
from jobhunt.apply_assist import queue
from jobhunt.apply_assist.schema import FieldSpec, FillOutcome, Resolution, TriageDigest

_NOW = "2026-07-01T00:00:00+00:00"


def _conn(tmp_path, name="queue.db"):
    return db.connect(tmp_path / name)


def _insert_posting(
    conn, posting_id: int, *, url: str, company: str = "Acme", title: str = "Engineer"
) -> None:
    conn.execute(
        """INSERT INTO postings
               (id, source, source_posting_id, company, title, location, url,
                posted_at, raw_json, first_seen_at, last_seen_at, status)
           VALUES (?, 'greenhouse', ?, ?, ?, '', ?, ?, '{}', ?, ?, 'active')""",
        (posting_id, str(posting_id), company, title, url, _NOW, _NOW, _NOW),
    )
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, assessed_at)
           VALUES (?, 1, ?, 'eligible', ?)""",
        (posting_id, load_preferences().version, _NOW),
    )
    conn.commit()


def _insert_score(conn, posting_id: int, total: int, rubric_version: int = 1) -> None:
    conn.execute(
        """INSERT INTO scores
               (posting_id, rubric_version, total, components, missing_evidence, excluded, scored_at)
           VALUES (?, ?, ?, '{}', '[]', 0, ?)""",
        (posting_id, rubric_version, total, _NOW),
    )
    conn.execute(
        """INSERT OR IGNORE INTO eligibility_assessments
           (posting_id, rubric_version, preferences_version, status, assessed_at)
           VALUES (?, ?, ?, 'eligible', ?)""",
        (posting_id, rubric_version, load_preferences().version, _NOW),
    )
    conn.commit()


def _mark_submitted(conn, posting_id: int) -> None:
    conn.execute(
        """INSERT INTO applications (posting_id, submitted_at, channel, created_at, updated_at)
           VALUES (?, ?, 'company careers page', ?, ?)""",
        (posting_id, _NOW, _NOW, _NOW),
    )
    conn.commit()


def _log_fill_activity(conn, posting_id: int, *, at: str) -> None:
    conn.execute(
        """INSERT INTO activity_events (entity_type, entity_id, action, data, at)
           VALUES ('posting', ?, 'apply_assist_fill', '{}', ?)""",
        (posting_id, at),
    )
    conn.commit()


GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/{}"
NON_ATS_URL = "https://acme.com/careers/apply/{}"


# --- select_queue -------------------------------------------------------


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("guard", ["closed", "duplicate", "review", "blocked", "missing", "old_preferences", "old_rubric"])
def test_select_queue_requires_active_current_eligible_assessment(tmp_path, explicit, guard):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")
        if guard in ("closed", "duplicate"):
            conn.execute("UPDATE postings SET status=? WHERE id=1", (guard,))
        elif guard in ("review", "blocked"):
            conn.execute("UPDATE eligibility_assessments SET status=? WHERE posting_id=1", (guard,))
        elif guard == "missing":
            conn.execute("DELETE FROM eligibility_assessments WHERE posting_id=1")
        elif guard == "old_preferences":
            conn.execute("UPDATE eligibility_assessments SET preferences_version=? WHERE posting_id=1", (load_preferences().version - 1,))
        else:
            _insert_score(conn, 1, 90, rubric_version=2)
            conn.execute("DELETE FROM eligibility_assessments WHERE posting_id=1 AND rubric_version=2")
        conn.commit()
        assert queue.select_queue(conn, limit=5, posting_id=1 if explicit else None) == []
    finally:
        conn.close()


def test_select_queue_latest_assessment_cannot_fall_back_to_eligible(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        _insert_score(conn, 1, 90, rubric_version=2)
        funnel.move(conn, 1, "suggested")
        conn.execute("UPDATE eligibility_assessments SET status='blocked' WHERE posting_id=1 AND rubric_version=2")
        conn.commit()
        assert queue.select_queue(conn, limit=5) == []
    finally:
        conn.close()


def test_select_queue_freshness_then_score_with_first_seen_fallback(tmp_path):
    conn = _conn(tmp_path)
    try:
        for pid, score, posted, seen in [
            (1, 99, "2026-06-01", "2026-07-05"),
            (2, 65, "2026-07-04T09:00:00Z", _NOW),
            (3, 80, "2026-07-04T01:00:00Z", _NOW),
            (4, 75, None, "2026-07-05"),
            (5, 60, "2026-07-06", _NOW),
            (6, 70, "", "2026-07-05"),
        ]:
            _insert_posting(conn, pid, url=GREENHOUSE_URL.format(pid))
            _insert_score(conn, pid, score)
            funnel.move(conn, pid, "suggested")
            conn.execute("UPDATE postings SET posted_at=?, first_seen_at=? WHERE id=?", (posted, seen, pid))
        conn.commit()
        assert [r["posting_id"] for r in queue.select_queue(conn, limit=10, min_score=65)] == [4, 6, 3, 2, 1]
        assert [r["posting_id"] for r in queue.select_queue(conn, limit=2, min_score=65)] == [4, 6]
    finally:
        conn.close()


def test_select_queue_tailored_resume_sorts_first(tmp_path):
    """A posting with a tailored resume PDF artifact outranks fresher, higher-scored
    postings without one, so approved tailoring spend is used before it goes stale."""
    conn = _conn(tmp_path)
    try:
        for pid, score, posted in [
            (1, 99, "2026-07-06"),
            (2, 60, "2026-06-01"),
            (3, 80, "2026-07-05"),
        ]:
            _insert_posting(conn, pid, url=GREENHOUSE_URL.format(pid))
            _insert_score(conn, pid, score)
            funnel.move(conn, pid, "suggested")
            conn.execute("UPDATE postings SET posted_at=? WHERE id=?", (posted, pid))
        conn.execute(
            "INSERT INTO artifacts (posting_id, kind, content, created_at, updated_at) "
            "VALUES (2, 'resume_pdf', 'data/apply_packs/resume/2-founder_engineer.pdf', ?, ?)",
            (_NOW, _NOW),
        )
        conn.execute(
            "INSERT INTO artifacts (posting_id, kind, content, created_at, updated_at) "
            "VALUES (3, 'cover_letter', 'not a resume', ?, ?)",
            (_NOW, _NOW),
        )
        conn.commit()
        assert [r["posting_id"] for r in queue.select_queue(conn, limit=10)] == [2, 1, 3]
    finally:
        conn.close()


def test_select_queue_picks_suggested_and_ready(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")

        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        _insert_score(conn, 2, 70)
        funnel.move(conn, 2, "ready")

        _insert_posting(conn, 3, url=GREENHOUSE_URL.format(3))
        _insert_score(conn, 3, 90)
        funnel.move(conn, 3, "harvested")

        rows = queue.select_queue(conn, limit=10)
        ids = [r["posting_id"] for r in rows]
        assert ids == [1, 2]
    finally:
        conn.close()


def test_select_queue_respects_limit_and_ordering(tmp_path):
    conn = _conn(tmp_path)
    try:
        for posting_id, score in [(1, 50), (2, 90), (3, 70)]:
            _insert_posting(conn, posting_id, url=GREENHOUSE_URL.format(posting_id))
            _insert_score(conn, posting_id, score)
            funnel.move(conn, posting_id, "suggested")

        rows = queue.select_queue(conn, limit=2)
        ids = [r["posting_id"] for r in rows]
        assert ids == [2, 3]
    finally:
        conn.close()


def test_select_queue_min_score_filter(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 40)
        funnel.move(conn, 1, "suggested")

        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        _insert_score(conn, 2, 80)
        funnel.move(conn, 2, "suggested")

        rows = queue.select_queue(conn, limit=10, min_score=60)
        ids = [r["posting_id"] for r in rows]
        assert ids == [2]
    finally:
        conn.close()


def test_select_queue_excludes_applied(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "ready")
        _mark_submitted(conn, 1)

        rows = queue.select_queue(conn, limit=10)
        assert rows == []
    finally:
        conn.close()


@pytest.mark.parametrize("action", ["apply_assist_fill", "apply_lite_open"])
def test_select_queue_excludes_recent_fill_activity(tmp_path, action):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")
        recent = datetime.now(UTC).isoformat()
        _log_fill_activity(conn, 1, at=recent)

        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        _insert_score(conn, 2, 70)
        funnel.move(conn, 2, "suggested")
        stale = (
            datetime.now(UTC) - timedelta(days=queue.RECENT_FILL_DAYS + 1)
        ).isoformat()
        _log_fill_activity(conn, 2, at=stale)
        conn.execute("UPDATE activity_events SET action=? WHERE action='apply_assist_fill'", (action,))
        conn.commit()

        rows = queue.select_queue(conn, limit=10)
        ids = [r["posting_id"] for r in rows]
        assert ids == [2]
    finally:
        conn.close()


@pytest.mark.parametrize("action", ["apply_assist_fill", "apply_lite_open"])
def test_select_queue_posting_id_bypasses_cooldown(tmp_path, action):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")
        recent = datetime.now(UTC).isoformat()
        _log_fill_activity(conn, 1, at=recent)

        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        _insert_score(conn, 2, 70)
        funnel.move(conn, 2, "suggested")
        _log_fill_activity(conn, 2, at=recent)
        conn.execute("UPDATE activity_events SET action=? WHERE action='apply_assist_fill'", (action,))
        conn.commit()

        # Normal run: both are inside the cooldown window, so neither is picked.
        assert queue.select_queue(conn, limit=10) == []

        # Posting-id override: only posting 1 is selected, cooldown ignored.
        rows = queue.select_queue(conn, limit=10, posting_id=1)
        ids = [r["posting_id"] for r in rows]
        assert ids == [1]
    finally:
        conn.close()


def test_select_queue_posting_id_list_selects_all_in_one_batch(tmp_path):
    conn = _conn(tmp_path)
    try:
        recent = datetime.now(UTC).isoformat()
        for pid, score in ((1, 80), (2, 70), (3, 60)):
            _insert_posting(conn, pid, url=GREENHOUSE_URL.format(pid))
            _insert_score(conn, pid, score)
            funnel.move(conn, pid, "suggested")
            _log_fill_activity(conn, pid, at=recent)

        # All three are inside the cooldown window; the id list bypasses it
        # for exactly the requested pair, and limit never truncates the set.
        rows = queue.select_queue(conn, limit=1, posting_id=[1, 3])
        ids = [r["posting_id"] for r in rows]
        assert ids == [1, 3]
    finally:
        conn.close()


def test_select_queue_posting_id_empty_list_is_normal_run(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")

        rows = queue.select_queue(conn, limit=10, posting_id=[])
        assert [r["posting_id"] for r in rows] == [1]

        _log_fill_activity(conn, 1, at=datetime.now(UTC).isoformat())
        assert queue.select_queue(conn, limit=10, posting_id=[]) == []
    finally:
        conn.close()


def test_select_queue_posting_id_still_requires_valid_stage(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "harvested")  # not 'suggested' or 'ready'

        rows = queue.select_queue(conn, limit=10, posting_id=1)
        assert rows == []
    finally:
        conn.close()


def test_select_queue_posting_id_still_excludes_submitted(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "ready")
        _mark_submitted(conn, 1)

        rows = queue.select_queue(conn, limit=10, posting_id=1)
        assert rows == []
    finally:
        conn.close()


def test_select_queue_excludes_non_ats_urls(tmp_path):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")

        _insert_posting(conn, 2, url=NON_ATS_URL.format(2))
        _insert_score(conn, 2, 90)
        funnel.move(conn, 2, "suggested")

        rows = queue.select_queue(conn, limit=10)
        ids = [r["posting_id"] for r in rows]
        assert ids == [1]
    finally:
        conn.close()


# --- render_queue_summary -------------------------------------------------------


def _digest(
    posting_id: int, *, filled_n: int = 1, residual_labels=None, errors=None
) -> TriageDigest:
    field = FieldSpec(selector="#f", kind="text", label="First name")
    filled = [
        FillOutcome(
            field=field,
            semantic_key="first_name",
            value="Alex",
            readback="Alex",
            committed=True,
        )
        for _ in range(filled_n)
    ]
    residual = [
        Resolution(
            field=FieldSpec(selector="#r", kind="text", label=label),
            semantic_key=None,
            action="ask",
            reason="test",
        )
        for label in (residual_labels or [])
    ]
    return TriageDigest(
        posting_id=posting_id,
        url=GREENHOUSE_URL.format(posting_id),
        ats="greenhouse",
        filled=filled,
        residual=residual,
        skipped=[],
        errors=errors or [],
    )


def test_render_queue_summary_contains_footer():
    digests = [
        _digest(1, residual_labels=["Veteran status"]),
        _digest(2, errors=["scan failed: timeout"]),
    ]
    out = queue.render_queue_summary(digests)
    assert "Review each tab — YOU click submit. This tool never will." in out
    assert out.rstrip().endswith(
        "Review each tab — YOU click submit. This tool never will."
    )


def test_render_queue_summary_reports_per_posting_counts():
    digests = [_digest(1, filled_n=2, residual_labels=["Veteran status", "Race"])]
    out = queue.render_queue_summary(digests)
    assert "#1" in out
    assert "filled=2" in out
    assert "needs-you=2" in out
    assert "Veteran status" in out


@pytest.mark.parametrize("limit", [0])
def test_select_queue_limit_zero_returns_empty(tmp_path, limit):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_score(conn, 1, 80)
        funnel.move(conn, 1, "suggested")

        rows = queue.select_queue(conn, limit=limit)
        assert rows == []
    finally:
        conn.close()


# --- run_queue tab focus -------------------------------------------------


class _FakePage:
    def __init__(
        self,
        url: str = "about:blank",
        context: "_FakeContext | None" = None,
        raise_on_close: bool = False,
    ) -> None:
        self.url = url
        self.closed = False
        self.fronted = False
        self._context = context
        self._raise_on_close = raise_on_close

    def goto(self, url, **kw):
        self.url = url

    def close(self):
        if self._raise_on_close:
            raise RuntimeError("simulated close failure")
        self.closed = True
        if self._context is not None and self in self._context.pages:
            self._context.pages.remove(self)

    def bring_to_front(self):
        self.fronted = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage(url="about:blank", context=self)
        self.pages.append(page)
        return page


def _queue_rows(conn):
    return conn.execute("SELECT id AS posting_id, url FROM postings").fetchall()


def _fake_run_assist(page, posting_id, url, *args, **kwargs):
    return TriageDigest(
        posting_id=posting_id,
        url=url,
        ats="ashby",
        filled=[],
        residual=[],
        skipped=[],
        errors=[],
    )


def _fake_run_assist_raises(page, posting_id, url, *args, **kwargs):
    raise RuntimeError("simulated fill failure")


def _fake_run_assist_fails_posting_2(page, posting_id, url, *args, **kwargs):
    if posting_id == 2:
        raise RuntimeError("simulated fill failure")
    return TriageDigest(
        posting_id=posting_id,
        url=url,
        ats="ashby",
        filled=[],
        residual=[],
        skipped=[],
        errors=[],
    )


def test_run_queue_focuses_first_posting_tab(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        rows = _queue_rows(conn)

        fake_context = _FakeContext()
        pre_existing = fake_context.new_page()

        monkeypatch.setattr(
            "jobhunt.apply_assist.driver.open_browser",
            lambda: (None, fake_context, False),
        )
        monkeypatch.setattr(
            "jobhunt.apply_assist.fill.run_assist", _fake_run_assist
        )

        queue.run_queue(conn, rows, {}, None)

        assert pre_existing.closed is True
        posting_page_1, posting_page_2 = fake_context.pages[0], fake_context.pages[1]
        assert posting_page_1.fronted is True
        assert posting_page_1.closed is False
        assert posting_page_2.fronted is False
        assert posting_page_2.closed is False
    finally:
        conn.close()


def test_run_queue_cdp_leaves_tabs_alone(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        rows = _queue_rows(conn)

        fake_context = _FakeContext()
        pre_existing = fake_context.new_page()

        monkeypatch.setattr(
            "jobhunt.apply_assist.driver.open_browser",
            lambda: (None, fake_context, True),
        )
        monkeypatch.setattr(
            "jobhunt.apply_assist.fill.run_assist", _fake_run_assist
        )

        queue.run_queue(conn, rows, {}, None)

        assert pre_existing.closed is False
        assert all(not p.fronted and not p.closed for p in fake_context.pages)
    finally:
        conn.close()


def test_run_queue_all_failures_no_focus(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        rows = _queue_rows(conn)

        fake_context = _FakeContext()
        pre_existing = fake_context.new_page()

        monkeypatch.setattr(
            "jobhunt.apply_assist.driver.open_browser",
            lambda: (None, fake_context, False),
        )
        monkeypatch.setattr(
            "jobhunt.apply_assist.fill.run_assist", _fake_run_assist_raises
        )

        queue.run_queue(conn, rows, {}, None)

        assert not any(p.fronted for p in fake_context.pages)
        assert pre_existing.closed is False
    finally:
        conn.close()


def test_run_queue_pages_align_with_digests_none_on_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    try:
        _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1))
        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        rows = _queue_rows(conn)

        fake_context = _FakeContext()

        monkeypatch.setattr(
            "jobhunt.apply_assist.driver.open_browser",
            lambda: (None, fake_context, False),
        )
        monkeypatch.setattr(
            "jobhunt.apply_assist.fill.run_assist", _fake_run_assist_fails_posting_2
        )

        digests, pages, handle, context, is_cdp, mapper_results, tailor_results = (
            queue.run_queue(conn, rows, {}, None)
        )

        assert len(pages) == len(digests) == 2
        assert pages[0] is fake_context.pages[0]
        assert pages[1] is None
    finally:
        conn.close()


# --- sweep_blank_tabs -----------------------------------------------------


def test_sweep_blank_tabs_closes_only_blanks():
    fake_context = _FakeContext()
    blank_1 = fake_context.new_page()
    posting_page = fake_context.new_page()
    posting_page.url = "https://jobs.ashbyhq.com/x"
    blank_2 = fake_context.new_page()

    closed = queue.sweep_blank_tabs(fake_context)

    assert closed == 2
    assert blank_1.closed is True
    assert blank_2.closed is True
    assert posting_page.closed is False
    assert fake_context.pages == [posting_page]


def test_sweep_blank_tabs_empty_context():
    fake_context = _FakeContext()

    closed = queue.sweep_blank_tabs(fake_context)

    assert closed == 0


def test_sweep_blank_tabs_close_failure_swallowed():
    fake_context = _FakeContext()
    ok_blank = fake_context.new_page()
    bad_blank = _FakePage(url="about:blank", context=fake_context, raise_on_close=True)
    fake_context.pages.append(bad_blank)

    closed = queue.sweep_blank_tabs(fake_context)

    assert closed == 1
    assert ok_blank.closed is True


# --- apply-lite -----------------------------------------------------------


@pytest.mark.parametrize("url, expected", [
    (GREENHOUSE_URL.format(1), GREENHOUSE_URL.format(1)),
    ("https://jobs.ashbyhq.com/acme/abc?ref=jobs", "https://jobs.ashbyhq.com/acme/abc/application?ref=jobs"),
    ("https://jobs.lever.co/acme/abc", "https://jobs.lever.co/acme/abc/apply"),
])
def test_apply_lite_opens_queue_in_default_browser(tmp_path, monkeypatch, url, expected):
    from jobhunt import cli

    real_connect = db.connect
    conn = _conn(tmp_path, "lite.db")
    try:
        _insert_posting(conn, 1, url=url)
        funnel.move(conn, 1, "suggested")
        _insert_posting(conn, 2, url=GREENHOUSE_URL.format(2))
        funnel.move(conn, 2, "suggested")
        _mark_submitted(conn, 2)

        monkeypatch.setattr(cli.db_module, "connect", lambda *a, **k: conn)
        opened: list[str] = []
        import webbrowser

        monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)

        cli.apply_lite(limit=10, min_score=None, posting_id=None)

        assert opened == [expected]
    finally:
        # Reopen after the CLI closes the injected connection, keeping the
        # suite's isolated preferences in force.
        conn = real_connect(tmp_path / "lite.db")
    try:
        row = conn.execute(
            """SELECT entity_id, data FROM activity_events
               WHERE action = 'apply_lite_open'"""
        ).fetchone()
        assert row["entity_id"] == 1
        import json
        assert json.loads(row["data"])["url"] == expected
        assert queue.select_queue(conn, limit=10) == []
        assert [r["posting_id"] for r in queue.select_queue(conn, limit=10, posting_id=1)] == [1]
    finally:
        conn.close()


def test_apply_lite_empty_queue_is_healthy(tmp_path, monkeypatch, capsys):
    from jobhunt import cli

    conn = _conn(tmp_path, "lite_empty.db")
    monkeypatch.setattr(cli.db_module, "connect", lambda: conn)

    cli.apply_lite(limit=10, min_score=None, posting_id=None)

    assert "nothing to open" in capsys.readouterr().out


# --- apply-lite tailor-ahead ------------------------------------------------


def _setup_lite_queue(tmp_path, name="lite_tailor.db"):
    conn = _conn(tmp_path, name)
    _insert_posting(conn, 1, url=GREENHOUSE_URL.format(1), company="Acme", title="Engineer")
    funnel.move(conn, 1, "suggested")
    return conn


def test_apply_lite_tailor_prints_drafts_and_opens_tabs(tmp_path, monkeypatch, capsys):
    from jobhunt import cli
    from jobhunt.apply_assist.tailor_answers import TailorResult

    conn = _setup_lite_queue(tmp_path)
    monkeypatch.setattr(cli.db_module, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cli.openrouter_module, "is_local_configured", lambda: True)

    def fake_draft_for_posting(conn, posting_id, fields, budget, **kwargs):
        return TailorResult(
            posting_id=posting_id,
            drafts={"fp1": ("Why do you want to work at Acme?", "Because reasons.")},
            status="ok",
            cache_hits=0,
            misses=1,
            drafted=1,
            cost_usd=0.0,
        )

    monkeypatch.setattr(
        "jobhunt.apply_assist.tailor_answers.draft_for_posting", fake_draft_for_posting
    )

    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)

    cli.apply_lite(limit=10, min_score=None, posting_id=None)

    out = capsys.readouterr().out
    assert opened == [GREENHOUSE_URL.format(1)]
    assert "tailored drafts (paste-only, review before use)" in out
    assert "Why do you want to work at Acme?" in out
    assert "Because reasons." in out


def test_apply_lite_tailor_gate_closed_no_draft_call_tabs_still_open(
    tmp_path, monkeypatch, capsys
):
    from jobhunt import cli

    conn = _setup_lite_queue(tmp_path, "lite_gate_closed.db")
    monkeypatch.setattr(cli.db_module, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cli.openrouter_module, "is_local_configured", lambda: False)
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    called = []
    monkeypatch.setattr(
        "jobhunt.apply_assist.tailor_answers.draft_for_posting",
        lambda *a, **k: called.append(1),
    )

    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)

    cli.apply_lite(limit=10, min_score=None, posting_id=None)

    out = capsys.readouterr().out
    assert called == []
    assert "tailoring skipped" in out
    assert opened == [GREENHOUSE_URL.format(1)]


def test_apply_lite_tailor_raises_never_blocks_tabs(tmp_path, monkeypatch, capsys):
    from jobhunt import cli

    conn = _setup_lite_queue(tmp_path, "lite_tailor_raises.db")
    monkeypatch.setattr(cli.db_module, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cli.openrouter_module, "is_local_configured", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("jobhunt.apply_assist.tailor_answers.draft_for_posting", boom)

    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)

    cli.apply_lite(limit=10, min_score=None, posting_id=None)

    out = capsys.readouterr().out
    assert opened == [GREENHOUSE_URL.format(1)]
    assert "tailoring failed" in out


def test_apply_lite_no_tailor_flag_skips_drafting(tmp_path, monkeypatch, capsys):
    from jobhunt import cli

    conn = _setup_lite_queue(tmp_path, "lite_no_tailor.db")
    monkeypatch.setattr(cli.db_module, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(cli.openrouter_module, "is_local_configured", lambda: True)

    called = []
    monkeypatch.setattr(
        "jobhunt.apply_assist.tailor_answers.draft_for_posting",
        lambda *a, **k: called.append(1),
    )

    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)

    cli.apply_lite(limit=10, min_score=None, posting_id=None, tailor=False)

    out = capsys.readouterr().out
    assert called == []
    assert "tailored drafts" not in out
    assert opened == [GREENHOUSE_URL.format(1)]
