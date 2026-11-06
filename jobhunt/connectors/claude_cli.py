"""Claude-subscription LLM backend: runs one prompt through the `claude` CLI.

The unattended auto-apply lane (specs/2026-10-23-autoapply.md, section B) must
never spend metered API credits. `claude -p` signs in with the candidate's Claude
subscription (OAuth), so this connector shells out to it instead of calling an
HTTP API. It refuses to return anything unless the CLI itself reports that it
did not use an API key.

Guards, in order:
- Only "sonnet" and "haiku" can be requested (ValueError otherwise, before
  anything is spawned): the unattended lane never runs a premium model.
- `--bare` is never passed: it forces ANTHROPIC_API_KEY auth.
- The child env drops ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN and
  ANTHROPIC_BASE_URL (plus the Bedrock/Vertex/Foundry switches), so a key
  exported in the parent shell cannot move the call onto a metered path.
- Child env also drops CLAUDE* vars except CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CONFIG_DIR.
- Hooks are off: --setting-sources project and --settings {"disableAllHooks":true}.
- The stream's `system/init` event must report `apiKeySource == "none"`
  (subscription auth). Any other value, or no init event at all, raises
  `openrouter.SpendGateError`, and the child is killed the moment a bad init
  line arrives (before the model call can finish).
- The CLI's own cost field is ignored; the subscription is flat-rate, so
  `cost_usd` is always 0.0.

Errors: SpendGateError means "cannot prove this ran on the subscription" and
callers should stop the whole lane. Its subclass SubscriptionUnavailableError
means the CLI is signed out or the subscription's usage limit is hit: every
later call would fail the same way, so callers stop the lane instead of marking
each posting failed. RuntimeError means the call itself failed (timeout,
non-zero exit, any other CLI error); no message here contains prompt text.

Never log the prompt or the reply; only model name and token counts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from jobhunt.connectors.openrouter import SpendGateError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_S = 300

# The unattended lane never runs a premium model (the candidate's rule: no unattended
# premium-model loop), so only these two aliases can reach the CLI.
ALLOWED_MODELS = ("sonnet", "haiku")


class SubscriptionUnavailableError(SpendGateError):
    """The CLI cannot serve calls right now: its login is missing or expired, or the
    subscription's usage limit is hit. Every later call in the run fails the same
    way, so this is a SpendGateError (the repo's existing "LLM unavailable, stop or
    degrade" signal) rather than a per-call RuntimeError that would mark each
    posting failed and block it for days."""


# CLI error text (or stderr) that means "not signed in" or "limit hit". Matched
# loosely on purpose: a false positive stops one run, a false negative would burn
# through postings that can never succeed.
_UNAVAILABLE = re.compile(
    r"failed to authenticate|not logged in|/login|oauth|unauthori[sz]ed"
    r"|usage limit|limit reached|hit your .{0,40}limit|rate.?limit",
    re.IGNORECASE,
)
_UNAVAILABLE_STATUS = (401, 403, 429)
_UNAVAILABLE_HINT = " (sign in with `claude auth login --claudeai` or `claude setup-token`, or wait for the limit to reset)"


def _unavailable(text: str, status: object = None) -> bool:
    return status in _UNAVAILABLE_STATUS or bool(_UNAVAILABLE.search(text))


def _failure(message: str, stderr_line: str) -> Exception:
    """RuntimeError, or SubscriptionUnavailableError when stderr says signed out / limit."""
    if _unavailable(stderr_line):
        return SubscriptionUnavailableError(message + _UNAVAILABLE_HINT)
    return RuntimeError(message)


# Env vars that would move the CLI off the subscription onto a metered path.
_STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

# Every other CLAUDE* var is parent-session wiring (host auth refresh, messaging
# socket/token, session id, SDK entrypoint, effort...). The child must behave the
# same when launched from a desktop session and from launchd, so none of it passes
# through. These two stay: the `claude setup-token` credential that the launchd
# path relies on, and a relocated config/credentials dir.
_KEPT_CLAUDE_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")

_ERROR_SNIPPET_CHARS = 200


def model_alias(model: str) -> str:
    """Map an OpenRouter-style slug ("anthropic/claude-sonnet-4.5") to a CLI
    model alias, always one of ALLOWED_MODELS. Anything with "haiku" in the name
    maps to haiku; everything else (Opus included, and slugs the subscription
    cannot run, like a Mistral model) maps to sonnet, so a premium model can
    never be selected through this mapping."""
    if "haiku" in (model or "").lower():
        return "haiku"
    return "sonnet"


def _binary() -> str:
    env_bin = os.environ.get("JOBHUNT_CLAUDE_BIN")
    if env_bin:
        return env_bin
    return shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _child_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _STRIPPED_ENV:
            continue
        if key.startswith("CLAUDE") and key not in _KEPT_CLAUDE_ENV:
            continue
        env[key] = value
    return env


def _argv(binary: str, model: str, system: str | None) -> list[str]:
    argv = [
        binary,
        "-p",
        "--model",
        model,
        "--tools",
        "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--verbose",
        # No user/local settings and no hooks: user SessionStart/UserPromptSubmit
        # hooks would receive the prompt text, and Stop hooks can force extra
        # turns. Separate argv elements (no shell), so the JSON needs no quoting.
        "--setting-sources",
        "project",
        "--settings",
        '{"disableAllHooks":true}',
    ]
    if system:
        argv += ["--system-prompt", system]
    return argv


def _parse_line(line: str) -> dict | None:
    """One stream-json line -> event dict, or None for blank/non-JSON lines."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _kill(proc: subprocess.Popen) -> None:
    """Kill the child and its process group (it runs in its own session).
    No-op once the child has been reaped, so a recycled pid is never signalled."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _feed(proc: subprocess.Popen, prompt: str) -> None:
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except (OSError, ValueError):
        # The child died before reading everything; its exit status and
        # missing events explain why.
        pass


def _stderr_snippet(errfile) -> str:
    try:
        errfile.seek(0)
        for line in errfile.read().splitlines():
            if line.strip():
                return line.strip()[:_ERROR_SNIPPET_CHARS]
    except (OSError, ValueError):
        pass
    return ""


def complete(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1400,
    *,
    system: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """Run `prompt` through `claude -p` on the subscription.

    Returns {text, tokens_in, tokens_out, cost_usd, model} like
    `openrouter.complete`; cost_usd is always 0.0 and model is
    "claude-cli:<model>". `max_tokens` is accepted for signature parity only
    (the CLI has no output cap flag).

    Raises ValueError for any model outside ALLOWED_MODELS ("sonnet", "haiku")
    before anything is spawned, SpendGateError when the CLI does not report
    subscription auth, SubscriptionUnavailableError (a SpendGateError) when it is
    signed out or the usage limit is hit, and RuntimeError on timeout, non-zero
    exit, or any other CLI-reported error.
    """
    del max_tokens  # signature parity with openrouter.complete
    if model not in ALLOWED_MODELS:
        raise ValueError(f"claude CLI backend only runs {' or '.join(ALLOWED_MODELS)}, got {model!r}")
    argv = _argv(_binary(), model, system)
    init: dict | None = None
    result: dict | None = None
    timed_out = threading.Event()

    with tempfile.TemporaryDirectory(prefix="jobhunt-claude-") as workdir, tempfile.TemporaryFile(
        mode="w+", encoding="utf-8"
    ) as errfile:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errfile,
                text=True,
                encoding="utf-8",
                cwd=workdir,
                env=_child_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"claude CLI could not start: {exc.strerror or type(exc).__name__}") from None

        with proc:

            def _expire() -> None:
                timed_out.set()
                _kill(proc)

            timer = threading.Timer(timeout_s, _expire)
            timer.daemon = True
            timer.start()
            try:
                _feed(proc, prompt)
                for line in proc.stdout:
                    event = _parse_line(line)
                    if event is None:
                        continue
                    kind = event.get("type")
                    if kind == "system" and event.get("subtype") == "init":
                        init = event
                        if event.get("apiKeySource") != "none":
                            _kill(proc)
                            raise SpendGateError(
                                "Refusing claude CLI call: apiKeySource="
                                f"{event.get('apiKeySource')!r}, expected 'none' (subscription auth)"
                            )
                    elif kind == "result":
                        result = event
                proc.wait()
            finally:
                timer.cancel()
                _kill(proc)
            stderr_line = _stderr_snippet(errfile)
            returncode = proc.returncode

    if timed_out.is_set():
        raise RuntimeError(f"claude CLI timed out after {timeout_s}s")
    if init is None:
        if returncode:
            raise _failure(f"claude CLI exited {returncode} before starting: {stderr_line}".rstrip(": "), stderr_line)
        raise SpendGateError("Refusing claude CLI call: no system/init event, cannot confirm subscription auth")
    if result is not None and result.get("is_error"):
        detail = str(result.get("result") or result.get("subtype") or "unknown")[:_ERROR_SNIPPET_CHARS]
        if _unavailable(detail, result.get("api_error_status")):
            raise SubscriptionUnavailableError(f"claude CLI cannot use the subscription: {detail}{_UNAVAILABLE_HINT}")
        raise RuntimeError(f"claude CLI reported an error: {detail}")
    if returncode:
        raise _failure(f"claude CLI exited {returncode}: {stderr_line}".rstrip(": "), stderr_line)
    if result is None:
        raise RuntimeError("claude CLI returned no result event")
    text = result.get("result")
    if not isinstance(text, str):
        raise RuntimeError("claude CLI result event has no text")

    usage = result.get("usage") or {}
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    logger.info(
        "claude_cli complete: model=%s tokens_in=%d tokens_out=%d cost_usd=0.000000",
        model,
        tokens_in,
        tokens_out,
    )
    return {
        "text": text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": 0.0,
        "model": f"claude-cli:{model}",
    }
