"""jd_summary.py: the JD-summary pre-pass with a write-through cache over
`jd_summaries`, feeding tailor_answers.draft_for_posting a compact
employer-needs summary instead of re-sending raw JD text per question.

The summary runs on the Claude subscription (`claude -p`, haiku): no metered
call is ever made. claude_cli.complete is monkeypatched in every test that could
reach it, openrouter.complete is patched to raise, and JOBHUNT_CLAUDE_BIN points
at a missing binary so a test that forgets a stub cannot shell out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import jobhunt.apply_assist.jd_summary as jd_summary_module
from jobhunt import db as db_module
from jobhunt import spend as spend_module
from jobhunt.apply_assist.jd_summary import summarize_posting
from jobhunt.connectors import claude_cli, openrouter

_NOW = "2026-07-01T00:00:00+00:00"
_GOOD_SUMMARY = {
    "required_skills": ["Python", "SQL"],
    "nice_to_have": ["AWS"],
    "keywords": ["python", "sql", "aws"],
    "seniority_signal": "mid-level",
    "one_line": "Backend engineer building data pipelines.",
}


@pytest.fixture(autouse=True)
def _no_real_calls(monkeypatch) -> None:
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("JOBHUNT_CLAUDE_BIN", "/nonexistent/claude")

    def no_openrouter(*args, **kwargs):
        raise AssertionError("the JD summary must never call OpenRouter")

    monkeypatch.setattr(openrouter, "complete", no_openrouter)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db_module.connect(tmp_path / "jd_summary.db")
    yield connection
    connection.close()


def _insert_posting(
    conn,
    posting_id: int,
    description_html: str = "<p>We need Python and SQL. Nice to have: AWS.</p>",
) -> None:
    raw_json = json.dumps({"content": description_html})
    conn.execute(
        """INSERT INTO postings
               (id, source, source_posting_id, company, title, location, url,
                posted_at, raw_json, first_seen_at, last_seen_at, status)
           VALUES (?, 'greenhouse', ?, 'Acme', 'Engineer', '', 'https://x', ?, ?, ?, ?, 'active')""",
        (posting_id, str(posting_id), _NOW, raw_json, _NOW, _NOW),
    )
    conn.commit()


def _stub_complete(response_text: str):
    """Returns a claude_cli.complete stand-in that always answers
    `response_text` and counts its own calls (and the model asked for) — the
    decisive check for "cache hit made zero LLM calls"."""
    calls = {"n": 0, "models": []}

    def complete(prompt: str, model: str = "sonnet", max_tokens: int = 900, *, system: str | None = None) -> dict:
        calls["n"] += 1
        calls["models"].append(model)
        return {"text": response_text, "cost_usd": 0.0, "model": f"claude-cli:{model}"}

    complete.calls = calls
    return complete


# --- good response: parsed, cached, reused without a second call ---------


def test_good_json_is_parsed_cached_and_reused_without_a_second_call(conn, monkeypatch) -> None:
    _insert_posting(conn, 1)
    stub = _stub_complete(json.dumps(_GOOD_SUMMARY))
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    first = summarize_posting(conn, 1, budget)
    assert first == _GOOD_SUMMARY
    assert stub.calls["n"] == 1

    row = conn.execute(
        "SELECT summary_json FROM jd_summaries WHERE posting_id = ?", (1,)
    ).fetchone()
    assert row is not None
    assert json.loads(row["summary_json"]) == _GOOD_SUMMARY

    second = summarize_posting(conn, 1, budget)
    assert second == _GOOD_SUMMARY
    assert stub.calls["n"] == 1  # decisive: no second network call


# --- defensive parsing: bad responses never raise, never cache -----------


def test_malformed_json_returns_none_and_writes_no_cache_row(conn, monkeypatch) -> None:
    _insert_posting(conn, 1)
    stub = _stub_complete("not json at all")
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    result = summarize_posting(conn, 1, budget)

    assert result is None
    row = conn.execute(
        "SELECT summary_json FROM jd_summaries WHERE posting_id = ?", (1,)
    ).fetchone()
    assert row is None


def test_missing_keys_returns_none_and_writes_no_cache_row(conn, monkeypatch) -> None:
    _insert_posting(conn, 1)
    incomplete = {k: v for k, v in _GOOD_SUMMARY.items() if k != "one_line"}
    stub = _stub_complete(json.dumps(incomplete))
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    result = summarize_posting(conn, 1, budget)

    assert result is None
    row = conn.execute(
        "SELECT summary_json FROM jd_summaries WHERE posting_id = ?", (1,)
    ).fetchone()
    assert row is None


# --- no description / no budget: never even reaches the client -----------


def test_no_description_text_returns_none_without_calling_client(conn, monkeypatch) -> None:
    _insert_posting(conn, 1, description_html="")
    stub = _stub_complete(json.dumps(_GOOD_SUMMARY))
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    result = summarize_posting(conn, 1, budget)

    assert result is None
    assert stub.calls["n"] == 0


def test_budget_none_still_summarizes_on_the_subscription_with_haiku(conn, monkeypatch) -> None:
    """The call is free, so no budget is needed; it runs on haiku through claude -p."""
    _insert_posting(conn, 1)
    stub = _stub_complete(json.dumps(_GOOD_SUMMARY))
    monkeypatch.setattr(claude_cli, "complete", stub)

    result = summarize_posting(conn, 1, None)

    assert result == _GOOD_SUMMARY
    assert stub.calls["n"] == 1 and stub.calls["models"] == ["haiku"]
    row = conn.execute("SELECT model FROM jd_summaries WHERE posting_id = 1").fetchone()
    assert row["model"] == "claude-cli:haiku"


def test_an_unavailable_subscription_returns_none_and_caches_nothing(conn) -> None:
    """No stub: the real connector cannot start (missing binary), and the summary falls
    back to None so the drafter carries on without a JD block."""
    _insert_posting(conn, 1)

    assert summarize_posting(conn, 1, None) is None
    assert conn.execute("SELECT COUNT(*) FROM jd_summaries").fetchone()[0] == 0


def test_a_budget_is_never_charged_for_the_summary(conn, monkeypatch) -> None:
    _insert_posting(conn, 1)
    monkeypatch.setattr(claude_cli, "complete", _stub_complete(json.dumps(_GOOD_SUMMARY)))
    budget = spend_module.RunBudget(conn, job="test")

    assert summarize_posting(conn, 1, budget) == _GOOD_SUMMARY
    assert budget.spent == 0.0


def test_fabricated_keywords_are_dropped(conn, monkeypatch) -> None:
    """The local 7b copied technologies out of the prompt's worked example
    (React/PostgreSQL/Kubernetes/AWS for a posting naming none of them).
    These keywords feed the answer-drafting prompt, so anything not literally
    in the JD must never survive — a fabricated technology here would become
    a skill claimed on a real application."""
    _insert_posting(conn, 1, description_html="<p>We need Python and SQL.</p>")
    payload = dict(_GOOD_SUMMARY)
    payload["keywords"] = ["Python", "Kubernetes", "SQL", "AWS", "React"]
    stub = _stub_complete(json.dumps(payload))
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    result = summarize_posting(conn, 1, budget)

    assert result is not None
    assert result["keywords"] == ["Python", "SQL"]
    # and the filtered form is what gets cached, not the model's raw list
    cached = conn.execute(
        "SELECT summary_json FROM jd_summaries WHERE posting_id = 1"
    ).fetchone()
    assert json.loads(cached["summary_json"])["keywords"] == ["Python", "SQL"]


def test_slash_compound_keyword_keeps_only_present_halves(conn, monkeypatch) -> None:
    _insert_posting(conn, 1, description_html="<p>TypeScript required. Python too.</p>")
    payload = dict(_GOOD_SUMMARY)
    payload["keywords"] = ["TypeScript/JavaScript", "Python"]
    stub = _stub_complete(json.dumps(payload))
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    result = summarize_posting(conn, 1, budget)

    assert result is not None
    assert result["keywords"] == ["TypeScript", "Python"]


def test_all_keywords_fabricated_yields_empty_list_not_none(conn, monkeypatch) -> None:
    """An empty keywords list is a valid, honest summary — the rest of the
    summary is still useful, so this must not collapse to None."""
    _insert_posting(conn, 1, description_html="<p>A role with no named tech.</p>")
    payload = dict(_GOOD_SUMMARY)
    payload["keywords"] = ["Kubernetes", "AWS"]
    stub = _stub_complete(json.dumps(payload))
    monkeypatch.setattr(claude_cli, "complete", stub)
    budget = spend_module.RunBudget(conn, job="test")

    result = summarize_posting(conn, 1, budget)

    assert result is not None
    assert result["keywords"] == []
    assert result["one_line"] == _GOOD_SUMMARY["one_line"]
