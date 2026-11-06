from __future__ import annotations

import json

import pytest

from jobhunt import db, funnel, lifecycle
from jobhunt.apply_assist import submitwatch


def _posting(
    conn, spid: str, company: str, title: str, stage: str = "ready", with_assessment: bool = True
) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, status, first_seen_at, last_seen_at)
           VALUES ('ashby', ?, ?, ?, 'Remote', 'active', '', '')""",
        (spid, company, title),
    )
    posting_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if with_assessment:
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


class _FakePage:
    def __init__(self, url: str, text: str = "", raise_on_evaluate: bool = False) -> None:
        self.url = url
        self._text = text
        self._raise_on_evaluate = raise_on_evaluate
        self._closed = False
        self.close_calls = 0

    def evaluate(self, _script: str) -> str:
        if self._raise_on_evaluate:
            raise RuntimeError("simulated navigation mid-evaluate")
        return self._text

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        self.close_calls += 1


# ---------------------------------------------------------------- page_submitted


def test_page_submitted_lever_success_url():
    page = _FakePage("https://jobs.lever.co/acme/abc123/thanks")
    assert submitwatch.page_submitted(page) is True


def test_page_submitted_greenhouse_success_url():
    page = _FakePage("https://boards.greenhouse.io/acme/jobs/1001/confirmation")
    assert submitwatch.page_submitted(page) is True


def test_page_submitted_greenhouse_success_text():
    page = _FakePage(
        "https://boards.greenhouse.io/acme/jobs/1001",
        text="Thank you for applying to Acme! We'll be in touch.",
    )
    assert submitwatch.page_submitted(page) is True


def test_page_submitted_ashby_success_text():
    page = _FakePage(
        "https://jobs.ashbyhq.com/acme/def456",
        text="We have received your application and will review it shortly.",
    )
    assert submitwatch.page_submitted(page) is True


def test_page_submitted_form_page_is_not_success():
    # Real pre-submit form pages carry field labels and a submit button, not
    # confirmation language. No captured ATS form-page fixture exists in
    # tests/fixtures (checked: only job-feed JSON there), so this is a
    # hand-written approximation of that copy.
    page = _FakePage(
        "https://boards.greenhouse.io/acme/jobs/1001",
        text="First Name Last Name Email Resume/CV Cover Letter Submit Application",
    )
    assert submitwatch.page_submitted(page) is False


def test_page_submitted_unknown_host_is_false():
    page = _FakePage("https://example.com/careers/123", text="Thank you for applying!")
    assert submitwatch.page_submitted(page) is False


def test_page_submitted_evaluate_raises_returns_false():
    page = _FakePage("https://jobs.ashbyhq.com/acme/def456", raise_on_evaluate=True)
    assert submitwatch.page_submitted(page) is False


# ---------------------------------------------------------------- record_submission


def test_record_submission_confirms_ready_posting(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "s1", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        status, blockers = submitwatch.record_submission(conn, pid)

        assert status == "confirmed"
        assert blockers == []
        app_row = conn.execute(
            "SELECT submitted_at, confirmation_ref FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row["submitted_at"] is not None
        assert app_row["confirmation_ref"] == f"livewatch:{pid}"
        stage = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (pid,)
        ).fetchone()["stage"]
        assert stage == "applied"
    finally:
        conn.close()


def test_record_submission_resolves_missing_resume_variant(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        _reviewed_resume(conn)
        pid = _posting(conn, "s2", "Sierra", "Agent Engineer", stage="suggested")

        status, blockers = submitwatch.record_submission(conn, pid)

        assert status == "confirmed"
        assert blockers == []
    finally:
        conn.close()


def test_record_submission_reports_blocked_beyond_resume_variant(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        # No reviewed resume variant AND no eligibility assessment: two
        # blockers, so the auto-resolve (which only fires when the resume
        # variant is the *sole* blocker) must not fire.
        pid = _posting(
            conn, "s3", "Anrok", "Product Engineer", stage="suggested", with_assessment=False
        )

        status, blockers = submitwatch.record_submission(conn, pid)

        assert status == "blocked"
        assert "eligibility_not_assessed" in blockers
        stage = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (pid,)
        ).fetchone()["stage"]
        assert stage != "applied"
        events = conn.execute(
            "SELECT action FROM activity_events WHERE entity_type='posting' AND entity_id=?",
            (pid,),
        ).fetchall()
        assert any(e["action"] == "submit_detected" for e in events)
    finally:
        conn.close()


def test_record_submission_idempotent_on_already_submitted(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "s4", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        first = submitwatch.record_submission(conn, pid)
        second = submitwatch.record_submission(conn, pid)

        assert first == ("confirmed", [])
        assert second == ("confirmed", [])
        events = conn.execute(
            "SELECT COUNT(*) AS n FROM activity_events "
            "WHERE entity_type='posting' AND entity_id=? AND action='submit_detected'",
            (pid,),
        ).fetchone()
        assert events["n"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------- sweep_submitted


def test_sweep_submitted_confirms_and_closes_tab(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "s5", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        page = _FakePage(
            "https://boards.greenhouse.io/anrok/jobs/1001/confirmation",
        )
        confirmed, blocked = submitwatch.sweep_submitted(conn, [(pid, page)])

        assert confirmed == [pid]
        assert blocked == []
        assert page.close_calls == 1
        app_row = conn.execute(
            "SELECT submitted_at FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row["submitted_at"] is not None
        stage = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (pid,)
        ).fetchone()["stage"]
        assert stage == "applied"
    finally:
        conn.close()


def test_sweep_submitted_leaves_non_success_tab_open(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "s6", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        page = _FakePage(
            "https://boards.greenhouse.io/anrok/jobs/1001",
            text="First Name Last Name Email Resume/CV Cover Letter Submit Application",
        )
        confirmed, blocked = submitwatch.sweep_submitted(conn, [(pid, page)])

        assert confirmed == []
        assert blocked == []
        assert page.close_calls == 0
        app_row = conn.execute(
            "SELECT submitted_at FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row is None or app_row["submitted_at"] is None
    finally:
        conn.close()


def test_sweep_submitted_reports_blocked(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        pid = _posting(
            conn, "s7", "Anrok", "Product Engineer", stage="suggested", with_assessment=False
        )
        page = _FakePage("https://boards.greenhouse.io/anrok/jobs/1001/confirmation")

        confirmed, blocked = submitwatch.sweep_submitted(conn, [(pid, page)])

        assert confirmed == []
        assert [b[0] for b in blocked] == [pid]
        assert page.close_calls == 1  # a blocked posting still leaves the success page, tab closes
        stage = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (pid,)
        ).fetchone()["stage"]
        assert stage != "applied"
    finally:
        conn.close()


def test_sweep_submitted_skips_already_closed_tab(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "s8", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        page = _FakePage("https://boards.greenhouse.io/anrok/jobs/1001/confirmation")
        page._closed = True

        confirmed, blocked = submitwatch.sweep_submitted(conn, [(pid, page)])

        assert confirmed == []
        assert blocked == []
        assert page.close_calls == 0
        app_row = conn.execute(
            "SELECT submitted_at FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row is None or app_row["submitted_at"] is None
    finally:
        conn.close()


# ------------------------------------------- page_submitted: Ashby confirmation copy


@pytest.mark.parametrize(
    "text",
    [
        "Your application was successfully submitted.",
        "Application successfully submitted",
        "Your application was submitted. We will be in touch.",
        "Application submitted",
    ],
)
def test_page_submitted_ashby_application_submitted_copy(text):
    page = _FakePage("https://jobs.ashbyhq.com/acme/def456/application", text=text)
    assert submitwatch.page_submitted(page) is True


@pytest.mark.parametrize(
    "text",
    [
        "First Name Last Name Email Resume Submit Application",
        "By submitting this application you agree to our privacy policy.",
        "Your application will be submitted to Acme when you press the button.",
        "Missing entry for required field: Email",
        "Your submission was flagged as possible spam.",
    ],
)
def test_page_submitted_ashby_form_and_error_copy_is_not_success(text):
    page = _FakePage("https://jobs.ashbyhq.com/acme/def456/application", text=text)
    assert submitwatch.page_submitted(page) is False


# ----------------------------------------- record_submission: ref / note parameters


def _event_data(conn, pid: int, action: str = "submit_detected") -> list[dict]:
    rows = conn.execute(
        "SELECT data FROM activity_events WHERE entity_type='posting' AND entity_id=? AND action=?"
        " ORDER BY id",
        (pid, action),
    ).fetchall()
    return [json.loads(r["data"]) for r in rows]


@pytest.fixture
def ensure_calls(monkeypatch):
    """Record every kwarg set `lifecycle.ensure_application` is called with."""
    calls: list[dict] = []
    original = lifecycle.ensure_application

    def spy(conn, posting_id, **kwargs):
        calls.append({"posting_id": posting_id, **kwargs})
        return original(conn, posting_id, **kwargs)

    monkeypatch.setattr(lifecycle, "ensure_application", spy)
    return calls


def test_record_submission_default_ref_and_activity_payload_are_unchanged(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "r1", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        assert submitwatch.record_submission(conn, pid) == ("confirmed", [])

        assert _event_data(conn, pid) == [{"ref": f"livewatch:{pid}"}]
        ref = conn.execute(
            "SELECT confirmation_ref FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()["confirmation_ref"]
        assert ref == f"livewatch:{pid}"
    finally:
        conn.close()


def test_record_submission_default_auto_resolve_uses_livewatch_ref_and_note(
    tmp_path, complete_answers, ensure_calls
):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "r2", "Sierra", "Agent Engineer", stage="suggested")

        assert submitwatch.record_submission(conn, pid) == ("confirmed", [])

        linking = [c for c in ensure_calls if c.get("resume_variant_id") is not None]
        assert len(linking) == 1
        assert linking[0]["resume_variant_id"] == variant
        assert linking[0]["confirmation_ref"] == f"livewatch:{pid}"
        assert linking[0]["notes"] == "auto: live success-page watch"
        assert linking[0]["notes"] == submitwatch._LIVE_WATCH_NOTE
    finally:
        conn.close()


def test_record_submission_custom_ref_and_note_on_a_ready_posting(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "r3", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        status = submitwatch.record_submission(
            conn, pid, ref=f"autoapply:{pid}", note="auto-submitted (autoapply) and confirmed"
        )

        assert status == ("confirmed", [])
        app_row = conn.execute(
            "SELECT submitted_at, confirmation_ref FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row["submitted_at"] is not None
        assert app_row["confirmation_ref"] == f"autoapply:{pid}"
        # confirm_applied rewrites applications.notes, so the note lives on the event.
        assert _event_data(conn, pid) == [
            {"ref": f"autoapply:{pid}", "note": "auto-submitted (autoapply) and confirmed"}
        ]
        stage = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (pid,)).fetchone()
        assert stage["stage"] == "applied"
    finally:
        conn.close()


def test_record_submission_custom_ref_and_note_reach_the_auto_resolve_link(
    tmp_path, complete_answers, ensure_calls
):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "r4", "Sierra", "Agent Engineer", stage="suggested")

        status = submitwatch.record_submission(
            conn, pid, ref="autoapply:77", note="custom note"
        )

        assert status == ("confirmed", [])
        linking = [c for c in ensure_calls if c.get("resume_variant_id") is not None]
        assert len(linking) == 1
        assert linking[0]["resume_variant_id"] == variant
        assert linking[0]["confirmation_ref"] == "autoapply:77"
        assert linking[0]["notes"] == "custom note"
        ref = conn.execute(
            "SELECT confirmation_ref FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()["confirmation_ref"]
        assert ref == "autoapply:77"
    finally:
        conn.close()


def test_record_submission_custom_ref_alone_leaves_the_note_out_of_the_event(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "r5", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        submitwatch.record_submission(conn, pid, ref="autoapply:5")

        assert _event_data(conn, pid) == [{"ref": "autoapply:5"}]
    finally:
        conn.close()


def test_record_submission_custom_ref_blocked_still_logs_the_detection(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        pid = _posting(
            conn, "r6", "Anrok", "Product Engineer", stage="suggested", with_assessment=False
        )

        status, blockers = submitwatch.record_submission(
            conn, pid, ref=f"autoapply:{pid}", note="auto-submitted (autoapply) and confirmed"
        )

        assert status == "blocked"
        assert "eligibility_not_assessed" in blockers
        assert _event_data(conn, pid) == [
            {"ref": f"autoapply:{pid}", "note": "auto-submitted (autoapply) and confirmed"}
        ]
        app_row = conn.execute(
            "SELECT submitted_at FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()
        assert app_row is None or app_row["submitted_at"] is None
    finally:
        conn.close()


def test_record_submission_already_submitted_ignores_a_new_ref_and_note(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "r7", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)
        assert submitwatch.record_submission(conn, pid, ref="autoapply:first") == ("confirmed", [])

        again = submitwatch.record_submission(conn, pid, ref="autoapply:second", note="later")

        assert again == ("confirmed", [])
        assert _event_data(conn, pid) == [{"ref": "autoapply:first"}]
        ref = conn.execute(
            "SELECT confirmation_ref FROM applications WHERE posting_id=?", (pid,)
        ).fetchone()["confirmation_ref"]
        assert ref == "autoapply:first"
    finally:
        conn.close()


# ----------------------------------------------------------------- record_blockers


def _counts(conn, pid: int) -> tuple[int, int, str | None]:
    applications = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0]
    stage = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (pid,)).fetchone()
    return applications, events, stage["stage"] if stage else None


def test_record_blockers_empty_for_a_ready_posting(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "b1", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)

        assert submitwatch.record_blockers(conn, pid) == []
    finally:
        conn.close()


def test_record_blockers_sole_resume_variant_blocker_is_cleared_by_a_reviewed_variant(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        _reviewed_resume(conn)
        pid = _posting(conn, "b2", "Sierra", "Agent Engineer", stage="suggested")

        assert submitwatch.record_blockers(conn, pid) == []
    finally:
        conn.close()


def test_record_blockers_sole_resume_variant_blocker_stands_without_a_reviewed_variant(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        pid = _posting(conn, "b3", "Sierra", "Agent Engineer", stage="suggested")

        assert submitwatch.record_blockers(conn, pid) == ["select_resume_variant"]
    finally:
        conn.close()


def test_record_blockers_unreviewed_linked_variant_is_not_auto_resolved(
    tmp_path, complete_answers
):
    conn = db.connect(tmp_path / "t.db")
    try:
        _reviewed_resume(conn)  # a reviewed variant exists, but the application links another
        conn.execute(
            """INSERT INTO resume_variants (label, path, reviewed_at, created_at, updated_at)
               VALUES ('tailored', 'resume/tailored.pdf', NULL, '', '')"""
        )
        conn.commit()
        unreviewed = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        pid = _posting(conn, "b4", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=unreviewed)

        assert submitwatch.record_blockers(conn, pid) == ["resume_not_reviewed"]
    finally:
        conn.close()


def test_record_blockers_returns_every_other_blocker_unchanged(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        _reviewed_resume(conn)  # would clear a sole select_resume_variant, but it is not sole
        pid = _posting(
            conn, "b5", "Anrok", "Product Engineer", stage="suggested", with_assessment=False
        )

        blockers = submitwatch.record_blockers(conn, pid)

        assert blockers == ["select_resume_variant", "eligibility_not_assessed"]
    finally:
        conn.close()


def test_record_blockers_reports_a_closed_posting(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        variant = _reviewed_resume(conn)
        pid = _posting(conn, "b6", "Anrok", "Product Engineer", stage="ready")
        lifecycle.ensure_application(conn, pid, resume_variant_id=variant)
        conn.execute("UPDATE postings SET status='closed' WHERE id=?", (pid,))
        conn.commit()

        assert submitwatch.record_blockers(conn, pid) == ["posting_closed"]
    finally:
        conn.close()


def test_record_blockers_writes_nothing(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        _reviewed_resume(conn)
        pid = _posting(conn, "b7", "Sierra", "Agent Engineer", stage="suggested")
        before = _counts(conn, pid)

        assert submitwatch.record_blockers(conn, pid) == []

        assert _counts(conn, pid) == before
        assert (
            conn.execute("SELECT COUNT(*) FROM applications WHERE posting_id=?", (pid,)).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_record_blockers_unknown_posting_raises(tmp_path, complete_answers):
    conn = db.connect(tmp_path / "t.db")
    try:
        with pytest.raises(ValueError, match="99999"):
            submitwatch.record_blockers(conn, 99999)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "scenario",
    [
        "ready",
        "auto_resolvable",
        "no_reviewed_variant",
        "unreviewed_linked_variant",
        "not_assessed",
        "closed",
    ],
)
def test_record_blockers_predicts_what_record_submission_does(
    tmp_path, complete_answers, scenario
):
    """The autoapply gate trusts record_blockers: [] must mean the post-click
    write is confirmed, and anything else must be exactly what it reports."""
    conn = db.connect(tmp_path / "t.db")
    try:
        pid = _posting(
            conn,
            "p-" + scenario,
            "Anrok",
            "Product Engineer",
            stage="ready",
            with_assessment=scenario != "not_assessed",
        )
        if scenario == "ready":
            lifecycle.ensure_application(conn, pid, resume_variant_id=_reviewed_resume(conn))
        elif scenario == "auto_resolvable":
            _reviewed_resume(conn)
        elif scenario == "unreviewed_linked_variant":
            _reviewed_resume(conn)
            conn.execute(
                """INSERT INTO resume_variants (label, path, reviewed_at, created_at, updated_at)
                   VALUES ('tailored', 'resume/tailored.pdf', NULL, '', '')"""
            )
            conn.commit()
            lifecycle.ensure_application(
                conn, pid, resume_variant_id=conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            )
        elif scenario == "not_assessed":
            lifecycle.ensure_application(conn, pid, resume_variant_id=_reviewed_resume(conn))
        elif scenario == "closed":
            lifecycle.ensure_application(conn, pid, resume_variant_id=_reviewed_resume(conn))
            conn.execute("UPDATE postings SET status='closed' WHERE id=?", (pid,))
            conn.commit()
        # "no_reviewed_variant": nothing linked and nothing reviewed.

        predicted = submitwatch.record_blockers(conn, pid)
        status, blockers = submitwatch.record_submission(conn, pid)

        if predicted == []:
            assert (status, blockers) == ("confirmed", [])
        else:
            assert status == "blocked"
            assert blockers == predicted
    finally:
        conn.close()
