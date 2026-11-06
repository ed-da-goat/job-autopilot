"""httpx TestClient tests for the local FastAPI/HTMX frontend (jobhunt/web).
Every test points app.dependency_overrides[get_db_path] at a tmp_path DB —
never the real data/jobhunt.db.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunt import db as db_module
from jobhunt.rubric import load_rubric
from jobhunt.web.app import app, get_db_path
from jobhunt.web.queries import BOARD_STAGES


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "t.db"
    app.dependency_overrides[get_db_path] = lambda: db_path
    try:
        yield TestClient(app), db_path
    finally:
        app.dependency_overrides.clear()


def _make_posting(
    db_path, spid: str = "p1", company: str = "Acme", title: str = "Backend Engineer"
) -> int:
    conn = db_module.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO postings
               (source, source_posting_id, company, title, status, first_seen_at, last_seen_at)
               VALUES ('greenhouse', ?, ?, ?, 'active', '', '')""",
            (spid, company, title),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM postings WHERE source_posting_id=?", (spid,)
        ).fetchone()["id"]
    finally:
        conn.close()


def test_board_renders_with_empty_funnel(client):
    http, db_path = client
    resp = http.get("/")
    assert resp.status_code == 200
    assert "Job Search OS" in resp.text

    legacy = http.get("/legacy")
    assert legacy.status_code == 200
    assert "Today" in legacy.text
    assert "Eligible queue clear" in legacy.text

    pipeline = http.get("/pipeline")
    assert pipeline.status_code == 200
    assert "Board" in pipeline.text


def test_api_board_empty_funnel(client):
    http, db_path = client
    resp = http.get("/api/board")
    assert resp.status_code == 200
    data = resp.json()
    assert data["stages"] == list(BOARD_STAGES)
    # every stage column present (board stages + collapsed passed/closed), all empty
    assert set(data["columns"]) == {*BOARD_STAGES, "passed", "closed"}
    assert all(cards == [] for cards in data["columns"].values())
    assert data["suggested"] == []


def test_api_today_and_health(client):
    http, db_path = client
    today = http.get("/api/today")
    assert today.status_code == 200
    assert set(today.json()) >= {"eligible", "review", "ready", "touches", "interviews", "offers", "health"}
    health = http.get("/api/health")
    assert health.status_code == 200
    assert isinstance(health.json()["scheduler_installed"], bool)


def test_api_posting_detail_is_json(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    response = http.get(f"/api/posting/{posting_id}")
    assert response.status_code == 200
    assert response.json()["posting"]["id"] == posting_id
    assert set(response.json()) >= {"readiness", "application", "resume_variants", "artifacts"}


def test_api_search_filters_jobs_and_includes_contacts(client):
    http, db_path = client
    _make_posting(db_path, "open", company="OpenAI", title="Software Engineer")
    _make_posting(db_path, "other", company="Acme", title="Backend Engineer")
    conn = db_module.connect(db_path)
    try:
        from jobhunt import lifecycle

        lifecycle.create_contact(conn, name="Ada", company="OpenAI")
    finally:
        conn.close()
    rows = http.get("/api/search?q=OpenAI").json()
    assert {(row["type"], row["primary"]) for row in rows} == {
        ("posting", "OpenAI"),
        ("contact", "Ada"),
    }


def test_api_prep_remains_spend_gated(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    response = http.post(f"/api/posting/{posting_id}/prep/outreach")
    assert response.status_code == 422
    assert "SPEND_OK" in response.json()["detail"]


def test_api_board_reflects_funnel_moves(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    http.post(f"/qualify/{posting_id}", data={"tier": "top20"})

    data = http.get("/api/board").json()
    (card,) = data["columns"]["qualified"]
    assert card["posting_id"] == posting_id
    assert card["company"] == "Acme"
    assert card["title"] == "Backend Engineer"
    assert card["posting_status"] == "active"
    assert card["tier"] == "top20"
    assert card["days_in_stage"] == 0


def test_qualify_accepts_stretch_tier(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    response = http.post(f"/qualify/{posting_id}", data={"tier": "stretch"})
    assert response.status_code == 200
    conn = db_module.connect(db_path)
    try:
        row = conn.execute(
            "SELECT stage, tier FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert tuple(row) == ("qualified", "stretch")
    finally:
        conn.close()


def test_shortlist_renders_with_no_scores(client):
    http, db_path = client
    resp = http.get("/shortlist")
    assert resp.status_code == 200
    assert "Shortlist" in resp.text


def test_postings_renders_with_no_postings(client):
    http, db_path = client
    resp = http.get("/postings")
    assert resp.status_code == 200
    assert "0 matches" in resp.text


def test_postings_out_of_range_page_preserves_total(client):
    http, db_path = client
    _make_posting(db_path, "p1")
    _make_posting(db_path, "p2")
    resp = http.get("/postings?page=999")
    assert resp.status_code == 200
    assert "2 matches" in resp.text


def test_report_renders(client):
    http, db_path = client
    resp = http.get("/report")
    assert resp.status_code == 200
    assert "Report" in resp.text


def test_qualify_creates_funnel_row_and_event(client):
    http, db_path = client
    posting_id = _make_posting(db_path)

    resp = http.post(f"/qualify/{posting_id}", data={"tier": "top20"})
    assert resp.status_code == 200
    assert resp.headers.get("hx-refresh") == "true"

    conn = db_module.connect(db_path)
    try:
        row = conn.execute(
            "SELECT stage, tier FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert row["stage"] == "qualified"
        assert row["tier"] == "top20"
        events = conn.execute(
            "SELECT from_stage, to_stage FROM funnel_events WHERE posting_id=?",
            (posting_id,),
        ).fetchall()
        assert [(e["from_stage"], e["to_stage"]) for e in events] == [
            ("harvested", "qualified")
        ]
    finally:
        conn.close()


def test_move_appends_event_and_stamps_applied_at_once(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    http.post(f"/qualify/{posting_id}")

    # The web route intentionally rejects an unconfirmed external submission.
    resp = http.post(f"/move/{posting_id}/applied", data={"note": "same-day apply"})
    assert resp.status_code == 400

    conn = db_module.connect(db_path)
    try:
        # The funnel domain still owns transition/event semantics. The guarded
        # lifecycle confirmation route is the only UI/API path that invokes this.
        from jobhunt import funnel as funnel_module

        funnel_module.move(conn, posting_id, "applied", note="same-day apply")
        row = conn.execute(
            "SELECT stage, applied_at FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert row["stage"] == "applied"
        first_applied_at = row["applied_at"]
        assert first_applied_at is not None

        events = conn.execute(
            "SELECT from_stage, to_stage, note FROM funnel_events WHERE posting_id=? ORDER BY id",
            (posting_id,),
        ).fetchall()
        assert [(e["from_stage"], e["to_stage"]) for e in events] == [
            ("harvested", "qualified"),
            ("qualified", "applied"),
        ]
        assert events[-1]["note"] == "same-day apply"
    finally:
        conn.close()

    # move away and back to applied — applied_at must not be overwritten
    conn = db_module.connect(db_path)
    try:
        from jobhunt import funnel as funnel_module

        funnel_module.move(conn, posting_id, "replied")
        funnel_module.move(conn, posting_id, "applied")
        row = conn.execute(
            "SELECT applied_at FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert row["applied_at"] == first_applied_at
    finally:
        conn.close()


def test_move_rejects_unknown_stage(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    resp = http.post(f"/move/{posting_id}/ghosted")
    assert resp.status_code == 400


def test_set_field_rejects_non_allowlisted_field(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    resp = http.post(f"/set/{posting_id}/salary", data={"value": "200k"})
    assert resp.status_code == 400

    conn = db_module.connect(db_path)
    try:
        # no funnel row should have been created by the rejected call
        row = conn.execute(
            "SELECT * FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_set_field_allowlisted_persists(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    resp = http.post(
        f"/set/{posting_id}/referral_path", data={"value": "GT alum intro"}
    )
    assert resp.status_code == 200

    conn = db_module.connect(db_path)
    try:
        row = conn.execute(
            "SELECT referral_path FROM funnel WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert row["referral_path"] == "GT alum intro"
    finally:
        conn.close()


def test_detail_404_on_unknown_id(client):
    http, db_path = client
    resp = http.get("/posting/999999")
    assert resp.status_code == 404


def test_detail_renders_for_known_posting(client):
    http, db_path = client
    posting_id = _make_posting(db_path, company="Acme", title="Backend Engineer")
    resp = http.get(f"/posting/{posting_id}")
    assert resp.status_code == 200
    assert "Acme" in resp.text
    assert "Backend Engineer" in resp.text


def test_api_outcome_replied_moves_to_replied_and_logs_note(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    conn = db_module.connect(db_path)
    try:
        from jobhunt import funnel as funnel_module

        funnel_module.move(conn, posting_id, "applied")
    finally:
        conn.close()

    resp = http.post(f"/api/posting/{posting_id}/outcome", json={"outcome": "replied"})
    assert resp.status_code == 200

    conn = db_module.connect(db_path)
    try:
        row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["stage"] == "replied"
        event = conn.execute(
            "SELECT note FROM funnel_events WHERE posting_id=? ORDER BY id DESC LIMIT 1",
            (posting_id,),
        ).fetchone()
        assert event["note"] == "human reply received"
    finally:
        conn.close()


def test_api_outcome_screen_creates_interview_and_moves_to_interviewing(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    conn = db_module.connect(db_path)
    try:
        from jobhunt import funnel as funnel_module

        funnel_module.move(conn, posting_id, "applied")
    finally:
        conn.close()

    resp = http.post(f"/api/posting/{posting_id}/outcome", json={"outcome": "screen"})
    assert resp.status_code == 200

    conn = db_module.connect(db_path)
    try:
        row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
        assert row["stage"] == "interviewing"
        interview = conn.execute(
            "SELECT round_name FROM interviews WHERE posting_id=?", (posting_id,)
        ).fetchone()
        assert interview["round_name"] == "Recruiter screen"
    finally:
        conn.close()


def test_api_outcome_rejected_and_ghosted_close_with_distinct_notes(client):
    http, db_path = client
    rejected_id = _make_posting(db_path, spid="rejected")
    ghosted_id = _make_posting(db_path, spid="ghosted")
    conn = db_module.connect(db_path)
    try:
        from jobhunt import funnel as funnel_module

        funnel_module.move(conn, rejected_id, "applied")
        funnel_module.move(conn, ghosted_id, "applied")
    finally:
        conn.close()

    resp = http.post(f"/api/posting/{rejected_id}/outcome", json={"outcome": "rejected"})
    assert resp.status_code == 200
    resp = http.post(f"/api/posting/{ghosted_id}/outcome", json={"outcome": "ghosted"})
    assert resp.status_code == 200

    conn = db_module.connect(db_path)
    try:
        rejected_row = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (rejected_id,)
        ).fetchone()
        ghosted_row = conn.execute(
            "SELECT stage FROM funnel WHERE posting_id=?", (ghosted_id,)
        ).fetchone()
        assert rejected_row["stage"] == "closed"
        assert ghosted_row["stage"] == "closed"
        rejected_note = conn.execute(
            "SELECT note FROM funnel_events WHERE posting_id=? ORDER BY id DESC LIMIT 1",
            (rejected_id,),
        ).fetchone()["note"]
        ghosted_note = conn.execute(
            "SELECT note FROM funnel_events WHERE posting_id=? ORDER BY id DESC LIMIT 1",
            (ghosted_id,),
        ).fetchone()["note"]
        assert rejected_note == "rejected"
        assert ghosted_note == "ghosted after no reply"
    finally:
        conn.close()


def test_api_outcome_rejects_unknown_outcome(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    conn = db_module.connect(db_path)
    try:
        from jobhunt import funnel as funnel_module

        funnel_module.move(conn, posting_id, "applied")
    finally:
        conn.close()

    resp = http.post(f"/api/posting/{posting_id}/outcome", json={"outcome": "bogus"})
    assert resp.status_code == 400


def test_api_outcome_rejects_when_stage_not_eligible(client):
    http, db_path = client
    posting_id = _make_posting(db_path)
    # no funnel row created yet — current stage is effectively "none"
    resp = http.post(f"/api/posting/{posting_id}/outcome", json={"outcome": "replied"})
    assert resp.status_code == 400
    assert "current stage" in resp.json()["detail"]


def test_api_today_includes_ghost_check_for_stale_applications(client):
    http, db_path = client
    posting_id = _make_posting(db_path, company="Stale", title="Old Apply")
    conn = db_module.connect(db_path)
    try:
        from jobhunt import funnel as funnel_module

        stale = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        conn.execute(
            """INSERT INTO applications (posting_id, submitted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?)""",
            (posting_id, stale, stale, stale),
        )
        conn.commit()
        funnel_module.move(conn, posting_id, "applied")
    finally:
        conn.close()

    resp = http.get("/api/today")
    assert resp.status_code == 200
    ghost = resp.json()["ghost_check"]
    assert [row["posting_id"] for row in ghost] == [posting_id]
    assert ghost[0]["company"] == "Stale"


def test_api_sprint_no_warm_path_clears_blocker_and_422_on_non_member(client):
    http, db_path = client
    member_id = _make_posting(db_path, "p1", company="Acme")
    outsider_id = _make_posting(db_path, "p2", company="Other Co")
    conn = db_module.connect(db_path)
    try:
        from jobhunt import sprint as sprint_module

        sprint_module.create_sprint(conn, [member_id])
    finally:
        conn.close()

    resp = http.post(
        f"/api/sprint/members/{member_id}/no-warm-path", json={"note": "cold apply"}
    )
    assert resp.status_code == 200
    assert resp.json()["no_warm_path_note"] == "cold apply"

    status = http.get("/api/today").json()["sprint"]
    assert status["members"][0]["blockers"] == []

    resp = http.post(
        f"/api/sprint/members/{outsider_id}/no-warm-path", json={"note": "cold apply"}
    )
    assert resp.status_code == 422


def _make_score(conn, posting_id: int, rubric_version: int, total: int) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, exclude_reason, scored_at)
           VALUES (?, ?, ?, '{"skill_match": 1}', '[]', 0, NULL, '')""",
        (posting_id, rubric_version, total),
    )
    conn.commit()


def _make_eligibility(conn, posting_id: int, rubric_version: int, status: str) -> None:
    conn.execute(
        """INSERT INTO eligibility_assessments
           (posting_id, preferences_version, rubric_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, 1, ?, ?, '[]', '[]', '[]', ?)""",
        (posting_id, rubric_version, status, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def test_postings_max_score_combines_with_min_score_for_triage_band(client):
    http, db_path = client
    low_id = _make_posting(db_path, "p1", company="Low")
    band_id = _make_posting(db_path, "p2", company="Band")
    high_id = _make_posting(db_path, "p3", company="High")
    conn = db_module.connect(db_path)
    try:
        _make_score(conn, low_id, 1, 40)
        _make_score(conn, band_id, 1, 60)
        _make_score(conn, high_id, 1, 80)
    finally:
        conn.close()

    resp = http.get("/postings?min_score=50&max_score=69")
    assert resp.status_code == 200
    assert "1 match" in resp.text
    assert "Band" in resp.text
    assert "Low" not in resp.text
    assert "High" not in resp.text


def test_postings_eligible_filter_excludes_non_eligible_rows(client):
    http, db_path = client
    eligible_id = _make_posting(db_path, "p1", company="Eligible Co")
    blocked_id = _make_posting(db_path, "p2", company="Blocked Co")
    conn = db_module.connect(db_path)
    try:
        _make_score(conn, eligible_id, 1, 60)
        _make_eligibility(conn, eligible_id, 1, "eligible")
        _make_score(conn, blocked_id, 1, 60)
        _make_eligibility(conn, blocked_id, 1, "blocked")
    finally:
        conn.close()

    all_resp = http.get("/postings")
    assert "Eligible Co" in all_resp.text
    assert "Blocked Co" in all_resp.text

    filtered = http.get("/postings?eligible=y")
    assert "Eligible Co" in filtered.text
    assert "Blocked Co" not in filtered.text


def test_shortlist_excludes_passed_and_closed_postings(client):
    http, db_path = client
    rubric_version = load_rubric()["version"]
    passed_id = _make_posting(db_path, "p1", company="Passed Co")
    qualified_id = _make_posting(db_path, "p2", company="Qualified Co")
    conn = db_module.connect(db_path)
    try:
        _make_score(conn, passed_id, rubric_version, 90)
        _make_eligibility(conn, passed_id, rubric_version, "eligible")
        _make_score(conn, qualified_id, rubric_version, 80)
        _make_eligibility(conn, qualified_id, rubric_version, "eligible")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, 'passed', ?, ?)",
            (passed_id, now, now),
        )
        conn.execute(
            "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, 'qualified', ?, ?)",
            (qualified_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    resp = http.get("/shortlist")
    assert resp.status_code == 200
    assert "Qualified Co" in resp.text
    assert "Passed Co" not in resp.text
