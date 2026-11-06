"""Tests for jobhunt.prep (draft generation engine) and the /prep, /draft
web routes. Every OpenRouter call is mocked via monkeypatch — no real spend,
per repo convention (see tests/test_openrouter.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobhunt import db as db_module
from jobhunt import prep as prep_module
from jobhunt import spend as spend_module
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.web.app import app, get_db_path


def _fake_result(text: str = "draft text", cost_usd: float = 0.001) -> dict:
    return {
        "text": text,
        "tokens_in": 500,
        "tokens_out": 200,
        "cost_usd": cost_usd,
        "model": "mistralai/mistral-small-3.2-24b-instruct",
    }


def _insert_posting(
    conn, spid: str = "p1", company: str = "Acme", title: str = "Backend Engineer", source: str = "greenhouse"
) -> int:
    raw = json.dumps({"content": "We need a backend engineer with Python and Postgres experience."})
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, status, raw_json, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, 'active', ?, '', '')""",
        (source, spid, company, title, raw),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def _insert_score(conn, posting_id: int, rubric_version: int = 1, total: int = 80) -> None:
    conn.execute(
        """INSERT INTO scores
           (posting_id, rubric_version, total, components, missing_evidence, excluded, scored_at)
           VALUES (?, ?, ?, ?, '[]', 0, '')""",
        (posting_id, rubric_version, total, json.dumps({"skill_match": 25, "degree_fit": 12})),
    )
    conn.commit()


def _insert_draft(conn, posting_id: int, kind: str, edited: int = 0, created_at: str | None = None) -> int:
    created_at = created_at or datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """INSERT INTO drafts (posting_id, kind, content, model, tokens_in, tokens_out, cost_usd, edited, created_at)
           VALUES (?, ?, 'existing', 'm', 10, 10, 0.001, ?, ?)""",
        (posting_id, kind, edited, created_at),
    )
    conn.commit()
    return cursor.lastrowid


# ------------------------------------------------------------- build_prompt


def test_build_prompt_substitutes_all_placeholders_no_leftover_braces(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, company="Acme", title="Backend Engineer")
        posting_row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
        _insert_score(conn, posting_id)
        score_row = conn.execute("SELECT * FROM scores WHERE posting_id=?", (posting_id,)).fetchone()

        for kind in prep_module.KINDS:
            prompt = prep_module.build_prompt(kind, posting_row, score_row, "PROFILE TEXT HERE")
            assert "{" not in prompt and "}" not in prompt
            assert "PROFILE TEXT HERE" in prompt
            assert "acme" in prompt.lower() or "Acme" in prompt
            assert "backend engineer" in prompt.lower()
            assert "skill_match: 25" in prompt
    finally:
        conn.close()


def test_build_prompt_handles_missing_score(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        posting_row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
        prompt = prep_module.build_prompt("cover", posting_row, None, "PROFILE")
        assert "not scored yet" in prompt
    finally:
        conn.close()


def test_build_prompt_invalid_kind_raises(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        posting_row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
        with pytest.raises(ValueError):
            prep_module.build_prompt("bogus", posting_row, None, "PROFILE")
    finally:
        conn.close()


# ------------------------------------------------- prompt placeholder hygiene


def test_build_prompt_leaves_no_unfilled_placeholders(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        posting_row = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()

        for kind in prep_module.KINDS:
            prompt = prep_module.build_prompt(kind, posting_row, None, "PROFILE")
            assert "Career-services guidance" not in prompt
            assert "Reference: generic career-services" not in prompt
            assert "{" not in prompt and "}" not in prompt
    finally:
        conn.close()


def test_load_profile_falls_back_to_example_when_profile_missing(tmp_path):
    (tmp_path / "PROFILE.example.md").write_text("EXAMPLE PROFILE", encoding="utf-8")
    assert prep_module._load_profile(tmp_path / "PROFILE.md") == "EXAMPLE PROFILE"
    (tmp_path / "PROFILE.md").write_text("REAL PROFILE", encoding="utf-8")
    assert prep_module._load_profile(tmp_path / "PROFILE.md") == "REAL PROFILE"


# ------------------------------------------------------------ generate_draft


def test_generate_draft_calls_check_budget_before_complete(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        call_order: list[str] = []

        real_check_budget = spend_module.check_budget

        def spy_check_budget(c, anticipated_cost=0.0):
            call_order.append("check_budget")
            return real_check_budget(c, anticipated_cost)

        def spy_complete(prompt, model=openrouter_module.DEFAULT_MODEL, max_tokens=900, **kwargs):
            call_order.append("complete")
            return _fake_result()

        monkeypatch.setattr(spend_module, "check_budget", spy_check_budget)
        monkeypatch.setattr(openrouter_module, "complete", spy_complete)

        prep_module.generate_draft(conn, posting_id, "cover")

        assert call_order == ["check_budget", "complete"]
    finally:
        conn.close()


def test_generate_draft_writes_draft_and_ledger_rows(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result("hello draft", 0.0042))

        draft = prep_module.generate_draft(conn, posting_id, "outreach")

        assert draft["posting_id"] == posting_id
        assert draft["kind"] == "outreach"
        assert draft["content"] == "hello draft"
        assert draft["cost_usd"] == pytest.approx(0.0042)
        assert draft["edited"] == 0

        ledger_row = conn.execute("SELECT * FROM spend_ledger WHERE draft_id=?", (draft["id"],)).fetchone()
        assert ledger_row is not None
        assert ledger_row["cost_usd"] == pytest.approx(0.0042)
        assert spend_module.today_spend(conn) == pytest.approx(0.0042)
    finally:
        conn.close()


def test_generate_draft_raises_spend_cap_error_without_calling_complete(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        spend_module.record_spend(conn, None, spend_module.DAILY_CAP_USD)

        called = {"n": 0}

        def spy_complete(*a, **k):
            called["n"] += 1
            return _fake_result()

        monkeypatch.setattr(openrouter_module, "complete", spy_complete)

        with pytest.raises(spend_module.SpendCapError):
            prep_module.generate_draft(conn, posting_id, "cover")

        assert called["n"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"] == 0
    finally:
        conn.close()


def test_generate_draft_unknown_kind_raises(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        with pytest.raises(ValueError):
            prep_module.generate_draft(conn, posting_id, "bogus")
    finally:
        conn.close()


# ----------------------------------------------------------- generate_packet


def test_generate_packet_generates_all_three_kinds(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

        result = prep_module.generate_packet(conn, posting_id)

        assert sorted(result["generated"]) == sorted(prep_module.KINDS)
        assert result["skipped"] == []
        assert result["warning"] is None
        assert conn.execute("SELECT COUNT(*) c FROM drafts WHERE posting_id=?", (posting_id,)).fetchone()["c"] == 3
    finally:
        conn.close()


def test_generate_packet_skips_fresh_unedited_duplicate(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_draft(conn, posting_id, "cover", edited=0)  # fresh, unedited

        call_kinds: list[str] = []

        def spy_complete(prompt, model=openrouter_module.DEFAULT_MODEL, max_tokens=900, **kwargs):
            call_kinds.append("called")
            return _fake_result()

        monkeypatch.setattr(openrouter_module, "complete", spy_complete)

        result = prep_module.generate_packet(conn, posting_id)

        assert "cover" in result["skipped"]
        assert sorted(result["generated"]) == sorted(k for k in prep_module.KINDS if k != "cover")
        # only 2 real calls made (tailoring, outreach) — cover skipped
        assert len(call_kinds) == 2
        assert conn.execute(
            "SELECT COUNT(*) c FROM drafts WHERE posting_id=? AND kind='cover'", (posting_id,)
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_generate_packet_does_not_skip_edited_or_stale_drafts(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn)
        _insert_draft(conn, posting_id, "cover", edited=1)  # edited -> not "fresh unedited"
        stale = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        _insert_draft(conn, posting_id, "outreach", edited=0, created_at=stale)  # stale -> not fresh

        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

        result = prep_module.generate_packet(conn, posting_id)

        assert sorted(result["generated"]) == sorted(prep_module.KINDS)
        assert result["skipped"] == []
    finally:
        conn.close()


def test_generate_packet_warns_on_simplify_source_and_does_not_generate(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, source="simplify")

        called = {"n": 0}
        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _fake_result())

        result = prep_module.generate_packet(conn, posting_id)

        assert result["warning"] is not None
        assert "simplify" in result["warning"]
        assert result["generated"] == []
        assert result["skipped"] == list(prep_module.KINDS)
        assert called["n"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"] == 0
    finally:
        conn.close()


def test_generate_packet_simplify_source_proceeds_when_forced(tmp_path, monkeypatch):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        posting_id = _insert_posting(conn, source="simplify")
        monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

        result = prep_module.generate_packet(conn, posting_id, force=True)

        assert result["warning"] is not None
        assert sorted(result["generated"]) == sorted(prep_module.KINDS)
        assert conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"] == 3
    finally:
        conn.close()


# ---------------------------------------------------------------- web routes


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "t.db"
    app.dependency_overrides[get_db_path] = lambda: db_path
    try:
        yield TestClient(app), db_path
    finally:
        app.dependency_overrides.clear()


def test_prep_route_creates_draft_with_mocked_connector(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    conn.close()

    monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result("web draft", 0.002))

    resp = http.post(f"/prep/{posting_id}/tailoring")
    assert resp.status_code == 200
    assert resp.headers.get("hx-refresh") == "true"

    conn = db_module.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM drafts WHERE posting_id=? AND kind='tailoring'", (posting_id,)
        ).fetchone()
        assert row is not None
        assert row["content"] == "web draft"
    finally:
        conn.close()


def test_prep_route_rejects_unknown_kind(client):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    conn.close()

    resp = http.post(f"/prep/{posting_id}/bogus")
    assert resp.status_code == 400


def test_prep_packet_route_creates_all_three(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    conn.close()

    monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result())

    resp = http.post(f"/prep/{posting_id}")
    assert resp.status_code == 200
    assert resp.headers.get("hx-refresh") == "true"

    conn = db_module.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) c FROM drafts WHERE posting_id=?", (posting_id,)).fetchone()["c"]
        assert count == 3
    finally:
        conn.close()


def test_save_route_sets_content_and_edited_flag(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    draft_id = _insert_draft(conn, posting_id, "cover", edited=0)
    conn.close()

    resp = http.post(f"/draft/{draft_id}/save", data={"content": "edited by hand"})
    assert resp.status_code == 200
    assert resp.headers.get("hx-refresh") == "true"

    conn = db_module.connect(db_path)
    try:
        row = conn.execute("SELECT content, edited FROM drafts WHERE id=?", (draft_id,)).fetchone()
        assert row["content"] == "edited by hand"
        assert row["edited"] == 1
    finally:
        conn.close()


def test_save_route_404_on_unknown_draft(client):
    http, db_path = client
    resp = http.post("/draft/999999/save", data={"content": "x"})
    assert resp.status_code == 404


def test_regenerate_route_creates_new_draft_row(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    draft_id = _insert_draft(conn, posting_id, "outreach", edited=1)
    conn.close()

    monkeypatch.setattr(openrouter_module, "complete", lambda *a, **k: _fake_result("regenerated", 0.003))

    resp = http.post(f"/draft/{draft_id}/regenerate")
    assert resp.status_code == 200
    assert resp.headers.get("hx-refresh") == "true"

    conn = db_module.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) c FROM drafts WHERE posting_id=? AND kind='outreach'", (posting_id,)
        ).fetchone()["c"]
        assert count == 2  # original + regenerated
    finally:
        conn.close()


def test_prep_route_gate_error_renders_banner_not_500(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    conn.close()

    def raise_cap(*a, **k):
        raise spend_module.SpendCapError("Daily spend cap reached: $1.00 spent of $1.00 cap")

    monkeypatch.setattr(spend_module, "check_budget", raise_cap)

    resp = http.post(f"/prep/{posting_id}/cover")
    assert resp.status_code == 200
    assert "hx-refresh" not in resp.headers
    assert "banner-error" in resp.text
    assert "spend cap" in resp.text.lower()

    conn = db_module.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM drafts").fetchone()["c"] == 0
    finally:
        conn.close()


def test_prep_route_spend_gate_error_renders_banner_not_500(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    conn.close()

    def raise_gate(*a, **k):
        raise openrouter_module.SpendGateError("Refusing OpenRouter call: SPEND_OK=1 not set in environment")

    monkeypatch.setattr(openrouter_module, "complete", raise_gate)

    resp = http.post(f"/prep/{posting_id}/cover")
    assert resp.status_code == 200
    assert "hx-refresh" not in resp.headers
    assert "banner-error" in resp.text


def test_detail_page_renders_drafts_section(client, monkeypatch):
    http, db_path = client
    conn = db_module.connect(db_path)
    posting_id = _insert_posting(conn)
    _insert_draft(conn, posting_id, "cover", edited=0)
    conn.close()

    resp = http.get(f"/posting/{posting_id}")
    assert resp.status_code == 200
    assert "existing" in resp.text  # draft content rendered in textarea
    assert "Prep" in resp.text  # missing-draft kinds show a Prep button

def test_strip_ai_dashes() -> None:
    assert prep_module.strip_ai_dashes("built end-to-end — then sold it") == "built end-to-end, then sold it"
    assert prep_module.strip_ai_dashes("2024–2026") == "2024-2026"


# ------------------------------------------------------- LLM endpoint/model env flex


def _llm_usage_payload(content: str = "draft text", prompt_tokens: int = 10, completion_tokens: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class _FakeLLMResponse:
    def __init__(self, json_data: dict):
        self._json_data = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


def test_llm_base_url_env_override(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1")

    with patch(
        "jobhunt.connectors.openrouter.requests.post", return_value=_FakeLLMResponse(_llm_usage_payload())
    ) as mock_post:
        openrouter_module.complete("prompt")
    assert mock_post.call_args[0][0] == "http://localhost:11434/v1/chat/completions"

    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1/")  # trailing slash
    with patch(
        "jobhunt.connectors.openrouter.requests.post", return_value=_FakeLLMResponse(_llm_usage_payload())
    ) as mock_post:
        openrouter_module.complete("prompt")
    assert mock_post.call_args[0][0] == "http://localhost:11434/v1/chat/completions"


def test_llm_model_env_override(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "dummy/local-model")

    with patch(
        "jobhunt.connectors.openrouter.requests.post", return_value=_FakeLLMResponse(_llm_usage_payload())
    ) as mock_post:
        result = openrouter_module.complete("prompt")

    assert mock_post.call_args.kwargs["json"]["model"] == "dummy/local-model"
    assert result["model"] == "dummy/local-model"
    # unrecognized slug -> DEFAULT_RATES (0.50, 1.50) usd per 1M tokens
    expected_cost = (10 / 1_000_000) * 0.50 + (5 / 1_000_000) * 1.50
    assert result["cost_usd"] == pytest.approx(expected_cost)


def test_llm_env_overrides_empty_string_falls_back(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "")
    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "")

    with patch(
        "jobhunt.connectors.openrouter.requests.post", return_value=_FakeLLMResponse(_llm_usage_payload())
    ) as mock_post:
        openrouter_module.complete("prompt")
    # present-but-empty vars must behave exactly like unset: real default
    # endpoint and default model, not "/chat/completions" or model "".
    assert mock_post.call_args[0][0] == "https://openrouter.ai/api/v1/chat/completions"
    assert mock_post.call_args.kwargs["json"]["model"] == openrouter_module.DEFAULT_MODEL


def test_estimate_max_cost_prices_env_override_model(monkeypatch):
    # pre-flight reservation must price the model complete() will actually
    # use: override to an unknown slug -> DEFAULT_RATES, not sonnet rates.
    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "dummy/local-model")
    prompt = "x" * 3000  # ~1000 input tokens at 3 chars/token
    cost = openrouter_module.estimate_max_cost(prompt, max_tokens=900)
    tokens_in = (3000 + 2) // 3
    expected = (tokens_in / 1_000_000) * 0.50 + (900 / 1_000_000) * 1.50
    assert cost == pytest.approx(expected)
