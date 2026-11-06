from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.connectors import openrouter


class FakeResponse:
    """Minimal requests.Response stand-in for the openrouter connector."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


def _usage_payload(content: str, prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_complete_refuses_without_spend_ok(monkeypatch):
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError):
            openrouter.complete("hello")
        mock_post.assert_not_called()


def test_complete_refuses_without_api_key(monkeypatch):
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError):
            openrouter.complete("hello")
        mock_post.assert_not_called()


def test_complete_refuses_with_spend_ok_wrong_value(monkeypatch):
    monkeypatch.setenv("SPEND_OK", "true")  # only "1" is accepted
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError):
            openrouter.complete("hello")
        mock_post.assert_not_called()


def test_complete_happy_path_parses_and_computes_cost_exact(monkeypatch):
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    payload = _usage_payload("Here is your draft.", prompt_tokens=1000, completion_tokens=500)
    with patch("jobhunt.connectors.openrouter.requests.post", return_value=FakeResponse(200, payload)) as mock_post:
        result = openrouter.complete("draft me a cover letter", max_tokens=900)

    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    # literal, not openrouter.OPENROUTER_URL: the constant is now derived
    # from the same helper complete() uses, so comparing to it would be a
    # tautology that can't catch a typo in the default endpoint.
    assert call_args[0] == "https://openrouter.ai/api/v1/chat/completions"
    assert call_kwargs["timeout"] == 60
    assert call_kwargs["headers"]["Authorization"] == "Bearer sk-fake"
    assert call_kwargs["json"]["model"] == openrouter.DEFAULT_MODEL
    assert call_kwargs["json"]["max_tokens"] == 900

    assert result["text"] == "Here is your draft."
    assert result["tokens_in"] == 1000
    assert result["tokens_out"] == 500
    assert result["model"] == openrouter.DEFAULT_MODEL
    # DEFAULT_MODEL rate: (3.00, 15.00) usd per 1M tokens
    expected_cost = (1000 / 1_000_000) * 3.00 + (500 / 1_000_000) * 15.00
    assert result["cost_usd"] == pytest.approx(expected_cost)
    assert result["cost_usd"] == pytest.approx(0.0105)


def test_complete_unknown_model_uses_default_rates(monkeypatch):
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    payload = _usage_payload("draft text", prompt_tokens=2000, completion_tokens=1000)
    with patch("jobhunt.connectors.openrouter.requests.post", return_value=FakeResponse(200, payload)):
        result = openrouter.complete("prompt", model="some/unlisted-model")

    expected_cost = (2000 / 1_000_000) * 0.50 + (1000 / 1_000_000) * 1.50
    assert result["cost_usd"] == pytest.approx(expected_cost)
    assert result["model"] == "some/unlisted-model"


def test_complete_separates_system_and_untrusted_user_content(monkeypatch):
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")
    payload = _usage_payload("draft", prompt_tokens=10, completion_tokens=5)
    with patch("jobhunt.connectors.openrouter.requests.post", return_value=FakeResponse(200, payload)) as post:
        openrouter.complete("UNTRUSTED POSTING", system="TRUSTED RULES")
    assert post.call_args.kwargs["json"]["messages"] == [
        {"role": "system", "content": "TRUSTED RULES"},
        {"role": "user", "content": "UNTRUSTED POSTING"},
    ]


def test_complete_raises_on_http_error(monkeypatch):
    monkeypatch.setenv("SPEND_OK", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    with patch("jobhunt.connectors.openrouter.requests.post", return_value=FakeResponse(500, {})):
        with pytest.raises(RuntimeError):
            openrouter.complete("prompt")


def test_complete_local_endpoint_skips_gate_no_spend_ok_no_key(monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    payload = _usage_payload("local draft", prompt_tokens=10, completion_tokens=5)
    with patch("jobhunt.connectors.openrouter.requests.post", return_value=FakeResponse(200, payload)) as mock_post:
        result = openrouter.complete("hello")

    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "http://localhost:11434/v1/chat/completions"
    assert call_kwargs["headers"]["Authorization"] == "Bearer local"
    assert result["text"] == "local draft"


def test_complete_remote_endpoint_still_gated_without_spend_ok(monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError, match="SPEND_OK=1 not set in environment"):
            openrouter.complete("hello")
        mock_post.assert_not_called()


@pytest.mark.parametrize("hostname", ["notlocalhost.evil.com", "10.0.0.5"])
def test_complete_lookalike_hosts_still_gated(monkeypatch, hostname):
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", f"http://{hostname}:11434/v1")
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError):
            openrouter.complete("hello")
        mock_post.assert_not_called()


def test_complete_local_endpoint_cost_usd_is_zero_despite_nonzero_tokens(monkeypatch):
    # Local calls must record cost_usd == 0.0 regardless of reported token
    # usage — pricing keys off the endpoint, never the model name.
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    payload = _usage_payload("local draft", prompt_tokens=5000, completion_tokens=2000)
    with patch("jobhunt.connectors.openrouter.requests.post", return_value=FakeResponse(200, payload)):
        result = openrouter.complete("hello", model="llama3:70b")

    assert result["tokens_in"] == 5000
    assert result["tokens_out"] == 2000
    assert result["cost_usd"] == 0.0


def test_estimate_max_cost_zero_for_local_endpoint(monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "http://localhost:11434/v1")

    assert openrouter.estimate_max_cost("a long prompt " * 500) == 0.0


def test_estimate_max_cost_nonzero_for_remote_endpoint(monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    cost = openrouter.estimate_max_cost("a long prompt " * 500)
    assert cost > 0.0


# --- claude_cli backend (JOBHUNT_LLM_BACKEND=claude_cli) ------------------------


@pytest.fixture
def cli_calls(monkeypatch):
    """Replace claude_cli.complete with a recorder: no `claude` process is spawned."""
    from jobhunt.connectors import claude_cli

    calls = []

    def fake(prompt, model="sonnet", max_tokens=1400, *, system=None, timeout_s=300):
        calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens, "system": system})
        return {"text": "via cli", "tokens_in": 3, "tokens_out": 4, "cost_usd": 0.0, "model": f"claude-cli:{model}"}

    monkeypatch.setattr(claude_cli, "complete", fake)
    monkeypatch.delenv("JOBHUNT_LLM_MODEL", raising=False)
    monkeypatch.delenv("JOBHUNT_LLM_BASE_URL", raising=False)
    return calls


def test_claude_cli_backend_skips_the_spend_gate_and_never_posts(monkeypatch, cli_calls):
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "claude_cli")
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        result = openrouter.complete("hello", system="be brief", max_tokens=500)

    mock_post.assert_not_called()
    assert result["text"] == "via cli" and result["cost_usd"] == 0.0
    assert cli_calls == [{"prompt": "hello", "model": "sonnet", "max_tokens": 500, "system": "be brief"}]


@pytest.mark.parametrize(
    "requested, alias",
    [
        ("anthropic/claude-sonnet-4.5", "sonnet"),
        ("anthropic/claude-haiku-4.5", "haiku"),
        ("anthropic/claude-opus-4", "sonnet"),  # a premium model can never be selected
        ("mistralai/mistral-small-3.2-24b-instruct", "sonnet"),
    ],
)
def test_claude_cli_backend_maps_models_to_the_allowed_aliases(monkeypatch, cli_calls, requested, alias):
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "claude_cli")
    openrouter.complete("hello", model=requested)
    assert cli_calls[0]["model"] == alias


def test_claude_cli_backend_maps_the_env_default_model_too(monkeypatch, cli_calls):
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "claude_cli")
    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "anthropic/claude-opus-4")
    openrouter.complete("hello")
    assert cli_calls[0]["model"] == "sonnet"


def test_claude_cli_backend_propagates_spend_gate_error(monkeypatch):
    from jobhunt.connectors import claude_cli

    def refuse(*args, **kwargs):
        raise openrouter.SpendGateError("cannot prove subscription auth")

    monkeypatch.setattr(claude_cli, "complete", refuse)
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "claude_cli")
    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError, match="subscription auth"):
            openrouter.complete("hello")
    mock_post.assert_not_called()


@pytest.mark.parametrize("backend", [None, "", "openrouter", "CLAUDE_CLI", "claude-cli"])
def test_any_other_backend_value_keeps_the_spend_gate(monkeypatch, cli_calls, backend):
    if backend is None:
        monkeypatch.delenv("JOBHUNT_LLM_BACKEND", raising=False)
    else:
        monkeypatch.setenv("JOBHUNT_LLM_BACKEND", backend)
    monkeypatch.delenv("SPEND_OK", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake")

    with patch("jobhunt.connectors.openrouter.requests.post") as mock_post:
        with pytest.raises(openrouter.SpendGateError, match="SPEND_OK=1 not set in environment"):
            openrouter.complete("hello")
    mock_post.assert_not_called()
    assert cli_calls == []


def test_estimate_max_cost_is_zero_on_the_claude_cli_backend(monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "claude_cli")
    assert openrouter.estimate_max_cost("a long prompt " * 500) == 0.0
    monkeypatch.delenv("JOBHUNT_LLM_BACKEND")
    assert openrouter.estimate_max_cost("a long prompt " * 500) > 0.0
