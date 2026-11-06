"""Tests for jobhunt.sprint (S6: application sprint object). Every OpenRouter
call in the spend-bound section is mocked via monkeypatch — no real spend,
per repo convention (see tests/test_prep.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jobhunt import db as db_module
from jobhunt import funnel as funnel_module
from jobhunt import lifecycle as lifecycle_module
from jobhunt import prep as prep_module
from jobhunt import sprint as sprint_module
from jobhunt import today as today_module
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.eligibility import TEMPLATE_PREFERENCES_PATH, load_preferences

# Explicit template path: module-level load runs before the conftest isolation
# fixture, and the suite must never depend on the operator's local toml.
_PREFS_VERSION = load_preferences(TEMPLATE_PREFERENCES_PATH).version
_RUBRIC_VERSION = 3


def _fake_result(text: str = "draft text", cost_usd: float = 0.001) -> dict:
    return {
        "text": text,
        "tokens_in": 500,
        "tokens_out": 200,
        "cost_usd": cost_usd,
        "model": "mistralai/mistral-small-3.2-24b-instruct",
    }


def _insert_company(conn, name: str = "Acme", verified: str = "yes") -> None:
    conn.execute(
        "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
        "VALUES (?, 'startup', 'greenhouse', ?, ?, '', 0, '')",
        (name, name.lower(), verified),
    )
    conn.commit()


def _insert_posting(conn, spid: str, company: str = "Acme", title: str = "Backend Engineer") -> int:
    raw = json.dumps({"content": "We need a backend engineer."})
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, raw_json, first_seen_at, last_seen_at)
           VALUES ('greenhouse', ?, ?, ?, 'active', ?, '', '')""",
        (spid, company, title, raw),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def _insert_score(conn, posting_id: int, total: int = 80, rubric_version: int = _RUBRIC_VERSION) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, scored_at)
           VALUES (?, ?, ?, '{}', '[]', 0, '')""",
        (posting_id, rubric_version, total),
    )
    conn.commit()


def _insert_eligibility(conn, posting_id: int, status: str = "eligible", rubric_version: int = _RUBRIC_VERSION) -> None:
    conn.execute(
        """INSERT INTO eligibility_assessments
               (posting_id, rubric_version, preferences_version, status, blockers, warnings, evidence, assessed_at)
           VALUES (?, ?, ?, ?, '[]', '[]', '[]', '')""",
        (posting_id, rubric_version, _PREFS_VERSION, status),
    )
    conn.commit()


def _insert_funnel(conn, posting_id: int, stage: str) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO funnel (posting_id, stage, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (posting_id, stage, now, now),
    )
    conn.commit()


def _full_candidate(conn, spid: str, company: str = "Acme", stage: str = "suggested", total: int = 80) -> int:
    """A posting that satisfies every candidates() filter: verified company,
    early stage, eligible assessment, scored under the current rubric."""
    _insert_company(conn, company)
    posting_id = _insert_posting(conn, spid, company=company)
    _insert_score(conn, posting_id, total=total)
    _insert_eligibility(conn, posting_id)
    _insert_funnel(conn, posting_id, stage)
    return posting_id


# ------------------------------------------------------------- candidates()


def test_candidates_returns_eligible_verified_early_stage_posting(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        rows = sprint_module.candidates(conn)
        assert [r["posting_id"] for r in rows] == [posting_id]
    finally:
        conn.close()


def test_candidates_excludes_unverified_company(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_company(conn, "Shady Co", verified="no")
        posting_id = _insert_posting(conn, "p1", company="Shady Co")
        _insert_score(conn, posting_id)
        _insert_eligibility(conn, posting_id)
        _insert_funnel(conn, posting_id, "suggested")

        assert sprint_module.candidates(conn) == []
    finally:
        conn.close()


def test_candidates_excludes_applied_stage(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _full_candidate(conn, "p1", stage="applied")
        assert sprint_module.candidates(conn) == []
    finally:
        conn.close()


def test_candidates_excludes_blocked_eligibility(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        _insert_company(conn, "Acme")
        posting_id = _insert_posting(conn, "p1")
        _insert_score(conn, posting_id)
        _insert_eligibility(conn, posting_id, status="blocked")
        _insert_funnel(conn, posting_id, "suggested")

        assert sprint_module.candidates(conn) == []
    finally:
        conn.close()


def test_candidates_ordered_by_score_descending(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        low = _full_candidate(conn, "p1", company="Low Co", stage="qualified", total=40)
        high = _full_candidate(conn, "p2", company="High Co", stage="tailored", total=95)
        rows = sprint_module.candidates(conn)
        assert [r["posting_id"] for r in rows] == [high, low]
    finally:
        conn.close()


def test_candidates_excludes_postings_already_in_active_sprint(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [posting_id])
        assert sprint_module.candidates(conn) == []
    finally:
        conn.close()


# ------------------------------------------------ create/active/is_member/complete


def test_create_sprint_active_sprint_is_member_round_trip(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        p1 = _full_candidate(conn, "p1")
        p2 = _full_candidate(conn, "p2", company="Other Co")

        sprint = sprint_module.create_sprint(conn, [p1], name="Test Sprint", target_size=1)
        assert sprint["name"] == "Test Sprint"
        assert sprint["status"] == "active"

        active = sprint_module.active_sprint(conn)
        assert active["id"] == sprint["id"]

        assert sprint_module.is_member(conn, p1) is True
        assert sprint_module.is_member(conn, p2) is False
    finally:
        conn.close()


def test_create_sprint_default_name_is_dated(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint = sprint_module.create_sprint(conn, [posting_id])
        assert sprint["name"] == f"Sprint {datetime.now(UTC).date().isoformat()}"
    finally:
        conn.close()


def test_create_sprint_raises_when_one_already_active(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        p1 = _full_candidate(conn, "p1")
        p2 = _full_candidate(conn, "p2", company="Other Co")
        sprint_module.create_sprint(conn, [p1])

        with pytest.raises(ValueError):
            sprint_module.create_sprint(conn, [p2])
    finally:
        conn.close()


def test_create_sprint_raises_on_empty_posting_ids(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        with pytest.raises(ValueError):
            sprint_module.create_sprint(conn, [])
    finally:
        conn.close()


def test_complete_sprint_clears_active_and_allows_new_one(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        p1 = _full_candidate(conn, "p1")
        p2 = _full_candidate(conn, "p2", company="Other Co")
        first = sprint_module.create_sprint(conn, [p1])

        completed = sprint_module.complete_sprint(conn)
        assert completed["id"] == first["id"]
        assert completed["status"] == "completed"
        assert sprint_module.active_sprint(conn) is None

        second = sprint_module.create_sprint(conn, [p2])
        assert second["id"] != first["id"]
        assert sprint_module.active_sprint(conn)["id"] == second["id"]
    finally:
        conn.close()


def test_complete_sprint_raises_when_none_active(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        with pytest.raises(ValueError):
            sprint_module.complete_sprint(conn)
    finally:
        conn.close()


# ------------------------------------------------------------ member_next_action


def test_member_next_action_across_all_stages():
    assert sprint_module.member_next_action("suggested", False) == "tailor"
    assert sprint_module.member_next_action("qualified", False) == "tailor"
    assert sprint_module.member_next_action("tailored", False) == "apply-assist"
    assert sprint_module.member_next_action("ready", False) == "confirm applied"
    assert sprint_module.member_next_action("applied", False) == "record outcome"
    assert sprint_module.member_next_action("applied", True) == "send follow-up touch"
    assert sprint_module.member_next_action("replied", False) == "record outcome"
    assert sprint_module.member_next_action("interviewing", False) == "interview prep"
    assert sprint_module.member_next_action("offer", False) == "decide offer"
    assert sprint_module.member_next_action("passed", False) == "done"
    assert sprint_module.member_next_action("closed", False) == "done"
    assert sprint_module.member_next_action("harvested", False) == "triage"


# ------------------------------------------------------------------ sprint_status


def test_sprint_status_none_when_no_active_sprint(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        assert sprint_module.sprint_status(conn) is None
    finally:
        conn.close()


def test_sprint_status_member_shape_and_blockers_empty_with_warm_path(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1", stage="tailored")
        sprint_module.create_sprint(conn, [posting_id], name="S1", target_size=1)
        lifecycle_module.create_contact(conn, name="Ada", company="Acme")

        status = sprint_module.sprint_status(conn)
        assert status["name"] == "S1"
        assert status["target_size"] == 1
        assert len(status["members"]) == 1
        member = status["members"][0]
        assert member["posting_id"] == posting_id
        assert member["stage"] == "tailored"
        assert member["next_action"] == "apply-assist"
        assert member["blockers"] == []
    finally:
        conn.close()


def test_sprint_status_applied_member_with_planned_touch(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1", stage="suggested")
        sprint_module.create_sprint(conn, [posting_id])

        funnel_module.move(conn, posting_id, "applied")
        lifecycle_module.create_touch(conn, kind="follow_up", posting_id=posting_id)

        status = sprint_module.sprint_status(conn)
        member = status["members"][0]
        assert member["stage"] == "applied"
        assert member["next_action"] == "send follow-up touch"
    finally:
        conn.close()


def test_sprint_status_applied_member_without_planned_touch(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1", stage="suggested")
        sprint_module.create_sprint(conn, [posting_id])

        funnel_module.move(conn, posting_id, "applied")

        status = sprint_module.sprint_status(conn)
        member = status["members"][0]
        assert member["stage"] == "applied"
        assert member["next_action"] == "record outcome"
    finally:
        conn.close()


# --------------------------------------------------------------- warm path (S7)


def test_sprint_status_blocker_when_no_contact(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [posting_id])

        status = sprint_module.sprint_status(conn)
        member = status["members"][0]
        assert member["blockers"] == [
            "no warm path — link a contact or record a no-warm-path note"
        ]
    finally:
        conn.close()


def test_sprint_status_blocker_clears_with_linked_touch(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [posting_id])

        contact = lifecycle_module.create_contact(conn, name="Ada", company="Other Co")
        lifecycle_module.create_touch(
            conn, kind="outreach", contact_id=contact["id"], posting_id=posting_id
        )

        status = sprint_module.sprint_status(conn)
        assert status["members"][0]["blockers"] == []
        assert sprint_module.has_warm_path(conn, posting_id) is True
    finally:
        conn.close()


def test_sprint_status_blocker_clears_with_same_company_contact(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1", company="Acme")
        sprint_module.create_sprint(conn, [posting_id])

        lifecycle_module.create_contact(conn, name="Ada", company="Acme")

        status = sprint_module.sprint_status(conn)
        assert status["members"][0]["blockers"] == []
        assert sprint_module.has_warm_path(conn, posting_id) is True
    finally:
        conn.close()


def test_set_no_warm_path_note_clears_blocker_and_round_trips(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [posting_id])

        row = sprint_module.set_no_warm_path_note(conn, posting_id, "  cold apply, no contact yet  ")
        assert row["no_warm_path_note"] == "cold apply, no contact yet"

        status = sprint_module.sprint_status(conn)
        member = status["members"][0]
        assert member["blockers"] == []
        assert member["no_warm_path_note"] == "cold apply, no contact yet"
    finally:
        conn.close()


def test_set_no_warm_path_note_raises_on_empty_note(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [posting_id])

        with pytest.raises(ValueError):
            sprint_module.set_no_warm_path_note(conn, posting_id, "   ")
    finally:
        conn.close()


def test_set_no_warm_path_note_raises_when_no_active_sprint(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        with pytest.raises(ValueError):
            sprint_module.set_no_warm_path_note(conn, posting_id, "no contact")
    finally:
        conn.close()


def test_set_no_warm_path_note_raises_on_non_member_posting(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        member_id = _full_candidate(conn, "p1")
        outsider_id = _insert_posting(conn, "p2", company="Other Co")
        sprint_module.create_sprint(conn, [member_id])

        with pytest.raises(ValueError):
            sprint_module.set_no_warm_path_note(conn, outsider_id, "no contact")
    finally:
        conn.close()


# ---------------------------------------------------------------- build_today


def test_build_today_includes_sprint_panel(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [posting_id], name="Today Sprint")

        result = today_module.build_today(conn)
        assert result["sprint"] is not None
        assert result["sprint"]["name"] == "Today Sprint"
    finally:
        conn.close()


def test_build_today_sprint_panel_none_without_active_sprint(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        result = today_module.build_today(conn)
        assert result["sprint"] is None
    finally:
        conn.close()


# --------------------------------------------------------- schema (v8 tables)


def test_v8_sprint_tables_exist_after_connect(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('sprints','sprint_members')"
            ).fetchall()
        }
        assert names == {"sprints", "sprint_members"}
    finally:
        conn.close()


# --------------------------------------------------- spend-bound (prep.py gate)


def test_generate_draft_non_member_raises_before_any_model_call(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        member_id = _full_candidate(conn, "p1")
        outsider_id = _insert_posting(conn, "p2", company="Other Co")
        sprint_module.create_sprint(conn, [member_id])

        def fail_if_called(*a, **k):
            pytest.fail("openrouter.complete should not be called for a non-sprint posting")

        monkeypatch.setattr(openrouter_module, "complete", fail_if_called)

        with pytest.raises(prep_module.SprintScopeError):
            prep_module.generate_draft(conn, outsider_id, "cover")

        assert conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"] == 0
    finally:
        conn.close()


def test_generate_draft_non_member_proceeds_with_allow_non_sprint(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        member_id = _full_candidate(conn, "p1")
        outsider_id = _insert_posting(conn, "p2", company="Other Co")
        sprint_module.create_sprint(conn, [member_id])

        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

        draft = prep_module.generate_draft(conn, outsider_id, "cover", allow_non_sprint=True)
        assert draft["posting_id"] == outsider_id
    finally:
        conn.close()


def test_generate_draft_member_proceeds_without_flag(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        member_id = _full_candidate(conn, "p1")
        sprint_module.create_sprint(conn, [member_id])

        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

        draft = prep_module.generate_draft(conn, member_id, "cover")
        assert draft["posting_id"] == member_id
    finally:
        conn.close()


def test_generate_draft_proceeds_when_no_active_sprint(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, "p1")
        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

        draft = prep_module.generate_draft(conn, posting_id, "cover")
        assert draft["posting_id"] == posting_id
    finally:
        conn.close()
