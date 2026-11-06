"""OpenRouter chat-completion connector — the single choke point for real
LLM spend in this repo.

Deliberately NOT routed through `jobhunt.net` (that layer is the polite-GET
harvest path for job boards). This is an authed POST that costs real money,
so it is gated on two explicit env vars and never called implicitly.

Never log the API key or the full prompt at info level — only short,
non-sensitive summaries (model name, token counts, cost) belong in logs.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)


def _base_url() -> str:
    """Chat-completions URL, env-overridable via JOBHUNT_LLM_BASE_URL so the
    draft path can target any OpenAI-compatible endpoint (OpenRouter, a local
    Ollama server, etc.) without code edits. Read at call time, not import
    time, so tests can monkeypatch the env var."""
    # `or` (not a .get default) so a present-but-empty var still falls back
    # instead of producing a bare "/chat/completions" relative URL.
    base = os.environ.get("JOBHUNT_LLM_BASE_URL") or "https://openrouter.ai/api/v1"
    return base.rstrip("/") + "/chat/completions"


_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_local_endpoint(base_url: str) -> bool:
    """True iff base_url's hostname is exactly localhost/127.0.0.1/::1.

    Deliberately narrow: private-range IPs (10.x, 192.168.x, etc.) and
    *.localhost subdomains do NOT count as local — only a call that cannot
    leave the machine skips the spend gate."""
    hostname = urlsplit(base_url).hostname
    return hostname in _LOCAL_HOSTNAMES


def is_local_configured() -> bool:
    """True when JOBHUNT_LLM_BASE_URL points at a local endpoint."""
    return _is_local_endpoint(_base_url())


# Import-time default, kept for backward-compat direct access (e.g.
# tests/test_openrouter.py asserts against this). complete() re-resolves the
# URL at call time via _base_url() so env overrides set after import still
# apply.
OPENROUTER_URL = _base_url()
TIMEOUT_SECONDS = 60

# Default draft model. mistral-small kept pattern-completing false claims
# ("built and sold") against explicit profile instructions; Sonnet follows
# factual constraints reliably at a still-trivial per-draft cost.
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"


def _default_model() -> str:
    """Current default model, env-overridable via JOBHUNT_LLM_MODEL. Read at
    call time so tests can monkeypatch the env var after import."""
    return os.environ.get("JOBHUNT_LLM_MODEL") or DEFAULT_MODEL


# model -> (usd per 1M input tokens, usd per 1M output tokens)
RATE_TABLE: dict[str, tuple[float, float]] = {
    DEFAULT_MODEL: (3.00, 15.00),
    "mistralai/mistral-small-3.2-24b-instruct": (0.15, 0.60),
}

# Conservative fallback for any model not in RATE_TABLE, so an unrecognized
# slug never silently under-reports cost.
DEFAULT_RATES: tuple[float, float] = (0.50, 1.50)


class SpendGateError(Exception):
    """Raised when a real LLM call is attempted without explicit spend
    authorization (SPEND_OK=1) or without an API key configured."""


# Backend switch: JOBHUNT_LLM_BACKEND=claude_cli routes complete() through the
# `claude` CLI on the candidate's Claude subscription (jobhunt/connectors/claude_cli.py).
# That path has no per-token spend, so the SPEND_OK/OPENROUTER_API_KEY gate does
# not apply to it; claude_cli enforces its own guard (apiKeySource must be
# "none"). Every other value or an unset var keeps the OpenRouter path and its
# gate exactly as before. Read at call time so tests can monkeypatch the env.
BACKEND_ENV = "JOBHUNT_LLM_BACKEND"
CLAUDE_CLI_BACKEND = "claude_cli"


def _use_claude_cli() -> bool:
    return os.environ.get(BACKEND_ENV) == CLAUDE_CLI_BACKEND


def _rates_for(model: str) -> tuple[float, float]:
    return RATE_TABLE.get(model, DEFAULT_RATES)


def _cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    rate_in, rate_out = _rates_for(model)
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out


def estimate_max_cost(prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 1400) -> float:
    """Conservative pre-call reservation using 3 chars/input token.

    Returns 0.0 for local endpoints: complete() records $0 for those calls,
    so reserving a nonzero amount here would needlessly shrink the daily
    budget (jobhunt/prep.py's check_budget pre-flight) for spend that will
    never actually post to the ledger. The claude_cli backend is flat-rate
    subscription usage, so it reserves 0.0 as well.
    """
    if _use_claude_cli():
        return 0.0
    if _is_local_endpoint(_base_url()):
        return 0.0
    # Resolve the env override exactly like complete() does, so pre-flight
    # reservations price the same model the call will actually charge.
    resolved_model = _default_model() if model == DEFAULT_MODEL else model
    estimated_input_tokens = max(1, (len(prompt) + 2) // 3)
    return _cost_usd(resolved_model, estimated_input_tokens, max_tokens)


def complete(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1400,
    *,
    system: str | None = None,
) -> dict:
    """Call OpenRouter chat completions with a single user-role prompt.

    Returns a dict: {text, tokens_in, tokens_out, cost_usd, model}.

    Raises SpendGateError unless SPEND_OK=1 and OPENROUTER_API_KEY are both
    set in the environment — this is the hard stop against accidental spend.
    Local endpoints (localhost/127.0.0.1/::1) are exempt from both checks:
    they cost nothing and need no key, so requiring spend authorization for
    them would just block free local inference for no safety benefit.

    With JOBHUNT_LLM_BACKEND=claude_cli the call is delegated to the `claude`
    CLI on the Claude subscription before any of the checks above run; it
    returns the same dict with cost_usd 0.0.
    """
    if _use_claude_cli():
        # Lazy import: claude_cli imports SpendGateError from this module.
        from jobhunt.connectors import claude_cli

        requested = _default_model() if model == DEFAULT_MODEL else model
        return claude_cli.complete(
            prompt,
            model=claude_cli.model_alias(requested),
            max_tokens=max_tokens,
            system=system,
        )

    resolved_base_url = _base_url()
    is_local = _is_local_endpoint(resolved_base_url)

    if not is_local and os.environ.get("SPEND_OK") != "1":
        raise SpendGateError("Refusing OpenRouter call: SPEND_OK=1 not set in environment")

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not is_local and not api_key:
        raise SpendGateError("Refusing OpenRouter call: OPENROUTER_API_KEY not set in environment")
    if not api_key:
        # Local endpoint with no key configured — Ollama and friends ignore
        # the Authorization header, so any placeholder satisfies the
        # `Bearer <token>` shape without implying a real credential exists.
        api_key = "local"

    # Caller passed the (import-time) default explicitly or left it unset —
    # either way, resolve the *current* env override rather than the literal
    # bound at function-definition time.
    resolved_model = _default_model() if model == DEFAULT_MODEL else model

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    response = requests.post(
        resolved_base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    tokens_in = int(usage.get("prompt_tokens", 0))
    tokens_out = int(usage.get("completion_tokens", 0))
    # Local calls cost nothing regardless of reported token counts — pricing
    # keys off the endpoint, never the model name (no local model belongs in
    # RATE_TABLE).
    cost_usd = 0.0 if is_local else _cost_usd(resolved_model, tokens_in, tokens_out)

    logger.info(
        "openrouter complete: model=%s tokens_in=%d tokens_out=%d cost_usd=%.6f",
        resolved_model,
        tokens_in,
        tokens_out,
        cost_usd,
    )

    return {
        "text": text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "model": resolved_model,
    }
