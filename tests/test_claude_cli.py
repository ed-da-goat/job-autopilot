"""Tests for the claude CLI (subscription) LLM backend.

No test here calls the real `claude` binary: JOBHUNT_CLAUDE_BIN points at a
tiny fake executable that records how it was invoked and prints canned
stream-json lines, so argv, env, cwd, stdin, timeouts and kill behavior are all
exercised through the real subprocess path.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
from jobhunt.connectors import claude_cli, openrouter

FAKE_CLAUDE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys, time
    scn = json.load(open(os.environ["FAKE_CLAUDE_SCENARIO"]))
    rec = {{
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "stdin": sys.stdin.read(),
        "env_keys": sorted(os.environ),
        "pid": os.getpid(),
    }}
    json.dump(rec, open(scn["record"], "w"))
    if scn.get("stderr"):
        print(scn["stderr"], file=sys.stderr, flush=True)
    for ev in scn["events"]:
        if ev == "SLEEP":
            time.sleep(scn.get("sleep", 20))
            continue
        print(json.dumps(ev) if isinstance(ev, dict) else ev, flush=True)
    sys.exit(scn.get("exit", 0))
    """
)

INIT_OK = {"type": "system", "subtype": "init", "apiKeySource": "none", "model": "claude-sonnet"}
RESULT_OK = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "ok",
    "total_cost_usd": 0.42,
    "usage": {"input_tokens": 11, "output_tokens": 3},
}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Install a fake `claude` binary; returns run(events, **scenario) -> record path."""
    binary = tmp_path / "fake-claude"
    binary.write_text(FAKE_CLAUDE.format(python=sys.executable))
    binary.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("JOBHUNT_CLAUDE_BIN", str(binary))

    def arm(events, **scenario):
        scn = tmp_path / "scenario.json"
        scn.write_text(json.dumps({"events": events, "record": str(record), **scenario}))
        monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(scn))
        return record

    arm.record = record
    return arm


def _recorded(record: Path) -> dict:
    return json.loads(record.read_text())


def test_complete_happy_path_shape_and_zero_cost(fake_claude):
    fake_claude([INIT_OK, {"type": "assistant", "message": {}}, RESULT_OK])

    out = claude_cli.complete("Reply with the single word ok.")

    assert out == {
        "text": "ok",
        "tokens_in": 11,
        "tokens_out": 3,
        "cost_usd": 0.0,  # total_cost_usd (0.42) is ignored: flat-rate subscription
        "model": "claude-cli:sonnet",
    }


def test_argv_is_exact_and_never_bare(fake_claude):
    record = fake_claude([INIT_OK, RESULT_OK])

    claude_cli.complete("hello", model="haiku", max_tokens=5, system="You are terse.")

    argv = _recorded(record)["argv"]
    assert "--bare" not in argv
    assert argv == [
        "-p",
        "--model",
        "haiku",
        "--tools",
        "",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "project",
        "--settings",
        '{"disableAllHooks":true}',
        "--system-prompt",
        "You are terse.",
    ]


@pytest.mark.parametrize("system", [None, "You are terse."])
def test_argv_turns_off_user_settings_and_hooks(fake_claude, system):
    record = fake_claude([INIT_OK, RESULT_OK])

    claude_cli.complete("hello", system=system)

    argv = _recorded(record)["argv"]
    # Each flag appears once, as its own element, followed by exactly its value.
    assert argv.count("--setting-sources") == 1
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert argv.count("--settings") == 1
    settings = argv[argv.index("--settings") + 1]
    # The JSON is a single raw argv element: no shell quoting around it.
    assert settings == '{"disableAllHooks":true}'
    assert json.loads(settings) == {"disableAllHooks": True}
    assert "--bare" not in argv


def test_argv_omits_system_prompt_flag_when_not_given(fake_claude):
    record = fake_claude([INIT_OK, RESULT_OK])

    claude_cli.complete("hello")

    argv = _recorded(record)["argv"]
    assert "--system-prompt" not in argv
    assert "--bare" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_prompt_goes_on_stdin_not_argv(fake_claude):
    record = fake_claude([INIT_OK, RESULT_OK])
    prompt = "line one\nline two with 'quotes' and “curly” text"

    claude_cli.complete(prompt)

    rec = _recorded(record)
    assert rec["stdin"] == prompt
    assert all(prompt not in arg for arg in rec["argv"])


@pytest.mark.parametrize(
    "var",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ],
)
def test_metered_env_vars_are_stripped_from_the_child(fake_claude, monkeypatch, var):
    record = fake_claude([INIT_OK, RESULT_OK])
    monkeypatch.setenv(var, "dummy-not-a-real-value")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-subscription-token")

    claude_cli.complete("hello")

    env_keys = _recorded(record)["env_keys"]
    assert var not in env_keys
    # The subscription credential and the rest of the environment pass through.
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env_keys
    assert "PATH" in env_keys
    # The parent environment itself is untouched.
    assert os.environ[var] == "dummy-not-a-real-value"


# Host/session wiring a desktop-app (or SDK) parent leaks into its children.
PARENT_SESSION_VARS = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
]
KEPT_CLAUDE_VARS = ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"]


def test_child_env_drops_claude_session_vars_and_keeps_the_allowlist(monkeypatch):
    parent = {
        **{var: "dummy-session-wiring" for var in PARENT_SESSION_VARS},
        **{var: "dummy-kept" for var in KEPT_CLAUDE_VARS},
        "ANTHROPIC_API_KEY": "dummy-not-a-real-value",
        "ANTHROPIC_AUTH_TOKEN": "dummy-not-a-real-value",
        "ANTHROPIC_BASE_URL": "https://dummy.invalid",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/dummy",
        "SOME_UNRELATED_TOOL_VAR": "kept",
        # Only a CLAUDE *prefix* is dropped, not a name that merely contains it.
        "MY_CLAUDE_NOTE": "kept",
    }
    monkeypatch.setattr(os, "environ", dict(parent))

    assert claude_cli._child_env() == {
        "CLAUDE_CODE_OAUTH_TOKEN": "dummy-kept",
        "CLAUDE_CONFIG_DIR": "dummy-kept",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/dummy",
        "SOME_UNRELATED_TOOL_VAR": "kept",
        "MY_CLAUDE_NOTE": "kept",
    }
    # Building the child env never mutates the parent's.
    assert os.environ == parent


@pytest.mark.parametrize("var", PARENT_SESSION_VARS)
def test_parent_session_vars_never_reach_the_child(fake_claude, monkeypatch, var):
    record = fake_claude([INIT_OK, RESULT_OK])
    monkeypatch.setenv(var, "dummy-session-wiring")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "dummy-subscription-token")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/dummy/claude-config")
    monkeypatch.setenv("SOME_UNRELATED_TOOL_VAR", "kept")

    claude_cli.complete("hello")

    env_keys = _recorded(record)["env_keys"]
    assert var not in env_keys
    # Of everything CLAUDE*, exactly the allowlist survives (the setup-token
    # credential and the config dir), whatever else the parent process carried.
    claude_vars = sorted(k for k in env_keys if k.startswith("CLAUDE"))
    assert claude_vars == sorted(KEPT_CLAUDE_VARS)
    # Unrelated vars pass through.
    assert "SOME_UNRELATED_TOOL_VAR" in env_keys
    assert "PATH" in env_keys
    # The parent environment itself is untouched.
    assert os.environ[var] == "dummy-session-wiring"


def test_child_runs_in_a_throwaway_directory(fake_claude):
    record = fake_claude([INIT_OK, RESULT_OK])

    claude_cli.complete("hello")

    cwd = Path(_recorded(record)["cwd"])
    assert cwd.resolve() != Path.cwd().resolve()
    assert not cwd.exists()  # removed once the call returned


@pytest.mark.parametrize("source", ["user", "project", "org", "temporary", "ANTHROPIC_API_KEY", ""])
def test_api_key_source_other_than_none_raises_spend_gate_error(fake_claude, source):
    fake_claude([{**INIT_OK, "apiKeySource": source}, RESULT_OK])

    with pytest.raises(openrouter.SpendGateError):
        claude_cli.complete("hello")


def test_missing_api_key_source_field_raises_spend_gate_error(fake_claude):
    init = {k: v for k, v in INIT_OK.items() if k != "apiKeySource"}
    fake_claude([init, RESULT_OK])

    with pytest.raises(openrouter.SpendGateError):
        claude_cli.complete("hello")


def test_no_init_event_raises_spend_gate_error(fake_claude):
    fake_claude([RESULT_OK])

    with pytest.raises(openrouter.SpendGateError):
        claude_cli.complete("hello")


def test_bad_init_aborts_before_the_rest_of_the_stream(fake_claude):
    """The child is killed as soon as the bad init line arrives, not after the
    model call would have finished (the fake would otherwise sleep 20s)."""
    fake_claude([{**INIT_OK, "apiKeySource": "user"}, "SLEEP", RESULT_OK], sleep=20)

    started = time.monotonic()
    with pytest.raises(openrouter.SpendGateError):
        claude_cli.complete("hello")

    assert time.monotonic() - started < 10


def _error_result(subtype, message, **extra):
    return {
        **RESULT_OK,
        "is_error": True,
        "subtype": subtype,
        "result": message,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        **extra,
    }


@pytest.mark.parametrize(
    ("subtype", "message", "exit_code"),
    [
        ("error_during_execution", "Tool crashed unexpectedly", 1),
        ("error_max_turns", "Reached the maximum number of turns", 1),
        ("success", "API Error: 529 overloaded_error", 0),
    ],
)
def test_other_cli_errors_raise_plain_runtime_error_without_prompt_text(fake_claude, subtype, message, exit_code):
    fake_claude([INIT_OK, _error_result(subtype, message)], exit=exit_code)

    with pytest.raises(RuntimeError) as err:
        claude_cli.complete("SECRET-PROMPT-TEXT")

    assert not isinstance(err.value, openrouter.SpendGateError)
    assert message in str(err.value)
    assert "SECRET-PROMPT-TEXT" not in str(err.value)


@pytest.mark.parametrize(
    ("subtype", "message", "exit_code"),
    [
        ("error_during_execution", "Not logged in", 1),
        # Shape captured from the one live smoke call (2026-10-23): an expired
        # login arrives as subtype "success" with is_error true and zero usage.
        ("success", "Failed to authenticate: OAuth session expired and could not be refreshed", 0),
        ("success", "Failed to authenticate: OAuth session expired and could not be refreshed", 1),
        ("success", "Claude AI usage limit reached|1761253200", 1),
        ("success", "You've hit your session limit · resets 3pm", 0),
        ("success", "5-hour limit reached ∙ resets 2pm", 1),
        ("success", "API Error: Rate limit reached", 1),
        ("success", "API Error: 401 Unauthorized", 1),
        ("success", "Failed to authenticate: token revoked", 1),
        ("success", "Not logged in · Please run /login", 1),  # the CLI's own words may name /login
    ],
)
def test_signed_out_or_limit_hit_raises_subscription_unavailable_without_prompt_text(
    fake_claude, subtype, message, exit_code
):
    """Not a per-call failure: every later call fails the same way, so it is a
    SpendGateError subclass and the unattended lane stops instead of marking
    each posting failed."""
    fake_claude([INIT_OK, _error_result(subtype, message)], exit=exit_code)

    with pytest.raises(claude_cli.SubscriptionUnavailableError) as err:
        claude_cli.complete("SECRET-PROMPT-TEXT")

    assert isinstance(err.value, openrouter.SpendGateError)
    # The CLI's own words survive whole (a message that names /login keeps saying so), then
    # the hint says what to do: `claude auth login --claudeai`, not the interactive /login.
    assert message in str(err.value)
    assert str(err.value).endswith(
        " (sign in with `claude auth login --claudeai` or `claude setup-token`, or wait for the limit to reset)"
    )
    assert "SECRET-PROMPT-TEXT" not in str(err.value)


@pytest.mark.parametrize(("status", "unavailable"), [(401, True), (403, True), (429, True), (500, False), (None, False)])
def test_api_error_status_field_decides_when_the_text_is_unrecognised(fake_claude, status, unavailable):
    fake_claude([INIT_OK, _error_result("success", "Something odd happened", api_error_status=status)], exit=1)

    with pytest.raises(claude_cli.SubscriptionUnavailableError if unavailable else RuntimeError) as err:
        claude_cli.complete("hello")

    assert isinstance(err.value, openrouter.SpendGateError) is unavailable


@pytest.mark.parametrize(
    ("events", "exit_code", "stderr", "unavailable"),
    [
        ([], 1, "Not logged in · Please run /login", True),  # before init
        ([INIT_OK], 1, "Invalid session: OAuth token expired", True),  # after init, no result
        ([], 2, "segfault in native module", False),
        ([INIT_OK], 3, "something else broke", False),
    ],
)
def test_stderr_login_text_on_a_failed_exit_is_subscription_unavailable(
    fake_claude, events, exit_code, stderr, unavailable
):
    fake_claude(events, exit=exit_code, stderr=stderr)

    with pytest.raises(claude_cli.SubscriptionUnavailableError if unavailable else RuntimeError) as err:
        claude_cli.complete("SECRET-PROMPT-TEXT")

    assert isinstance(err.value, openrouter.SpendGateError) is unavailable
    assert "SECRET-PROMPT-TEXT" not in str(err.value)


def test_hook_events_before_init_are_ignored(fake_claude):
    """The real stream opens with system/hook_started and hook_response events
    (the user's SessionStart hooks run in the child) before system/init."""
    hook_started = {"type": "system", "subtype": "hook_started", "hook_event": "SessionStart"}
    hook_response = {"type": "system", "subtype": "hook_response", "exit_code": 0, "stdout": "noise"}
    fake_claude([hook_started, hook_response, INIT_OK, {"type": "assistant", "message": {}}, RESULT_OK])

    assert claude_cli.complete("hello")["text"] == "ok"


def test_nonzero_exit_raises_runtime_error_without_prompt_text(fake_claude):
    fake_claude([INIT_OK], exit=3)

    with pytest.raises(RuntimeError) as err:
        claude_cli.complete("SECRET-PROMPT-TEXT")

    assert "exited 3" in str(err.value)
    assert "SECRET-PROMPT-TEXT" not in str(err.value)


def test_exit_before_init_is_a_runtime_error_not_a_spend_gate_error(fake_claude):
    fake_claude([], exit=2)

    with pytest.raises(RuntimeError) as err:
        claude_cli.complete("hello")

    assert not isinstance(err.value, openrouter.SpendGateError)


def test_missing_result_event_raises_runtime_error(fake_claude):
    fake_claude([INIT_OK])

    with pytest.raises(RuntimeError, match="no result"):
        claude_cli.complete("hello")


def test_non_string_result_text_raises_runtime_error(fake_claude):
    fake_claude([INIT_OK, {**RESULT_OK, "result": None}])

    with pytest.raises(RuntimeError):
        claude_cli.complete("hello")


def test_garbage_lines_are_ignored(fake_claude):
    fake_claude(["not json at all", "", "[1, 2]", INIT_OK, "{broken", RESULT_OK])

    assert claude_cli.complete("hello")["text"] == "ok"


def test_timeout_kills_the_child_and_raises_runtime_error(fake_claude):
    record = fake_claude([INIT_OK, "SLEEP", RESULT_OK], sleep=30)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out") as err:
        claude_cli.complete("SECRET-PROMPT-TEXT", timeout_s=1)

    assert time.monotonic() - started < 15
    assert "SECRET-PROMPT-TEXT" not in str(err.value)
    pid = _recorded(record)["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_missing_binary_raises_runtime_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_CLAUDE_BIN", str(tmp_path / "does-not-exist"))

    with pytest.raises(RuntimeError, match="could not start"):
        claude_cli.complete("hello")


def test_binary_resolution_order(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNT_CLAUDE_BIN", "/custom/claude")
    assert claude_cli._binary() == "/custom/claude"

    monkeypatch.delenv("JOBHUNT_CLAUDE_BIN")
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert claude_cli._binary() == "/usr/local/bin/claude"

    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(claude_cli.Path, "home", classmethod(lambda cls: tmp_path))
    assert claude_cli._binary() == str(tmp_path / ".local" / "bin" / "claude")


@pytest.mark.parametrize(
    ("slug", "alias"),
    [
        ("anthropic/claude-sonnet-4.5", "sonnet"),
        # No premium model in the unattended lane: Opus maps down to sonnet.
        ("anthropic/claude-opus-4.1", "sonnet"),
        ("opus", "sonnet"),
        ("OPUS", "sonnet"),
        ("anthropic/claude-3.5-haiku", "haiku"),
        ("Haiku", "haiku"),
        ("sonnet", "sonnet"),
        ("mistralai/mistral-small-3.2-24b-instruct", "sonnet"),
        ("", "sonnet"),
        (None, "sonnet"),
    ],
)
def test_model_alias_only_ever_returns_sonnet_or_haiku(slug, alias):
    assert claude_cli.model_alias(slug) == alias
    assert claude_cli.model_alias(slug) in claude_cli.ALLOWED_MODELS


def test_allowed_models_are_exactly_sonnet_and_haiku():
    assert claude_cli.ALLOWED_MODELS == ("sonnet", "haiku")


@pytest.mark.parametrize(
    "model",
    ["opus", "Opus", "OPUS", "fable", "claude-opus-4-1", "anthropic/claude-sonnet-4.5", "Sonnet", "", "sonnet "],
)
def test_disallowed_model_raises_value_error_before_spawning_anything(fake_claude, model):
    record = fake_claude([INIT_OK, RESULT_OK])

    with pytest.raises(ValueError, match="sonnet or haiku"):
        claude_cli.complete("SECRET-PROMPT-TEXT", model=model)

    assert not record.exists()  # the fake binary never ran


def test_value_error_message_has_no_prompt_text(fake_claude):
    fake_claude([INIT_OK, RESULT_OK])

    with pytest.raises(ValueError) as err:
        claude_cli.complete("SECRET-PROMPT-TEXT", model="opus")

    assert "SECRET-PROMPT-TEXT" not in str(err.value)


@pytest.mark.parametrize("model", ["sonnet", "haiku"])
def test_allowed_models_reach_the_cli(fake_claude, model):
    record = fake_claude([INIT_OK, RESULT_OK])

    out = claude_cli.complete("hello", model=model)

    argv = _recorded(record)["argv"]
    assert argv[argv.index("--model") + 1] == model
    assert out["model"] == f"claude-cli:{model}"
