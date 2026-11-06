"""jobhunt.screen_check: the Haiku screen run over postings Sonnet already decided.

Fakes only: no test calls a model, and none touches the real DB or data directory.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunt import autotailor as A
from jobhunt import db as db_module
from jobhunt import finai
from jobhunt import screen_check as S
from jobhunt.connectors import claude_cli, openrouter
from jobhunt.connectors.claude_cli import SubscriptionUnavailableError
from jobhunt.connectors.openrouter import SpendGateError

REPO = Path(__file__).resolve().parent.parent

JD_BODY = (
    "We are hiring a finance operations analyst to reconcile ledgers, prepare month end reports, "
    "and support audit requests for our wealth management clients. The role works with Python and "
    "spreadsheets every day and uses machine learning and Python automation to clear repetitive "
    "back office work across the team."
)
YEARS = "Candidates must bring 10 or more years of fund accounting experience."
YEARS_QUOTE = "10 or more years of fund accounting experience"

MASTER = {
    "personalInfo": {"name": "Pat Doe", "email": "p@x.edu", "phone": "555-0100",
                     "location": "Town, State", "website": "", "linkedin": "in/pat"},
    "summary": "",
    "workExperience": [
        {"title": "Lead", "company": "Acme Co", "location": "City, ST", "years": "March 2026 - Present",
         "description": ["Built a ledger tool used by 12 people", "Ran 3,000+ checks"]},
        {"title": "Analyst", "company": "Beta LLC", "location": "City, ST", "years": "May 2025 - August 2025",
         "description": ["Reviewed 40 filings", "Drafted client memos"]},
    ],
    "education": [{"institution": "Big University", "degree": "Bachelor of Arts", "years": "May 2026",
                   "description": "Scholar"}],
    "personalProjects": [],
    "additional": {"technicalSkills": [], "awards": []},
}

DIMS = {"keyword_match": 55, "skills_overlap": 60, "experience_proof": 70,
        "education_fit": 65, "role_alignment": 60}

TAILORED = "tailored by autotailor (sonnet, 2 model calls)"
SONNET_DROP = "dropped: the role needs a license the candidate does not hold"
HAIKU_DROP = "dropped (haiku screen): screen score 12 is below 27"


def screen_reply(score=62, **over):
    """A Haiku screen reply: the given score, no hard mismatch."""
    reply = {"match_before": score, "dimensions": dict(DIMS), "missing_keywords": ["reconciliation"],
             "red_flags": ["thin quantified outcomes"], "hard_mismatch": None}
    reply.update(over)
    return reply


class Haiku:
    """A fake `complete`. Replies are chosen by the posting named in the prompt (its url,
    https://x/<spid>); every call is recorded. An exception is raised, a str is the reply
    text, a dict is JSON."""

    def __init__(self, **by_posting):
        self.by_posting = by_posting
        self.calls: list[dict] = []

    def __call__(self, prompt, model="sonnet", max_tokens=1400, *, system=None, timeout_s=300):
        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens, "system": system})
        for spid, reply in self.by_posting.items():
            if re.search(re.escape(f"https://x/{spid}") + r"(?!\w)", prompt):
                break
        else:
            raise AssertionError("no canned reply for this prompt")
        if isinstance(reply, BaseException):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return {"text": text, "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "model": "claude-cli:haiku"}


@pytest.fixture
def conn(tmp_path):
    connection = db_module.connect(tmp_path / "check.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp finai dir holding a master resume, in place of the real one."""
    finai_dir = tmp_path / "finai"
    finai_dir.mkdir()
    master = finai_dir / "master.rm.json"
    master.write_text(json.dumps(MASTER), encoding="utf-8")
    monkeypatch.setattr(A, "FINAI_DIR", finai_dir)
    monkeypatch.setattr(A, "MASTER_PATH", master)
    for var in ("SPEND_OK", "OPENROUTER_API_KEY", "JOBHUNT_LLM_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    return SimpleNamespace(finai_dir=finai_dir, master=master)


def add(conn, spid, desc=JD_BODY):
    conn.execute(
        """INSERT INTO postings (source, source_posting_id, company, title, location, url,
               posted_at, raw_json, status, first_seen_at, last_seen_at)
           VALUES ('ashby', ?, 'Acme', 'Financial Analyst, Operations', 'New York, NY', 'https://x/' || ?,
                   '2026-10-01', ?, 'active', '2026-10-01', '2026-10-01')""",
        (spid, spid, json.dumps({"descriptionPlain": desc})),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def write_report(env, posting_id, notes, *, score=62, source=A.SOURCE):
    report = {"match_before": score, "dimensions": dict(DIMS), "missing_keywords": [], "red_flags": [],
              "notes": notes, "source": source}
    (env.finai_dir / f"{posting_id}.report.json").write_text(json.dumps(report), encoding="utf-8")


def seed(conn, env, spid, notes, score, *, sentence=""):
    """A posting in the DB and the report file a Sonnet run left for it."""
    posting_id = add(conn, spid, JD_BODY + (" " + sentence if sentence else ""))
    assert "GUARD:" not in finai.jd_text(conn, posting_id), "fixture posting must not trip posting_guard"
    write_report(env, posting_id, notes, score=score)
    return posting_id


def check(conn, *args, haiku):
    out = io.StringIO()
    code = S.main(list(args), complete=haiku, conn=conn, out=out)
    return code, out.getvalue().splitlines()


# --- which reports are checked ---------------------------------------------------


def test_only_sonnet_made_autotailor_reports_are_checked(env):
    write_report(env, 12, TAILORED, score=70)
    write_report(env, 3, SONNET_DROP, score=20)
    skipped = {
        7: lambda: write_report(env, 7, HAIKU_DROP, score=12),  # the screen's own drop
        8: lambda: write_report(env, 8, TAILORED, source="finai"),  # not autotailor's
        9: lambda: write_report(env, 9, TAILORED, source=None),
        10: lambda: (env.finai_dir / "10.report.json").write_text(json.dumps({"source": "autotailor"})),
        11: lambda: write_report(env, 11, "   "),  # nothing to read a decision from
        13: lambda: (env.finai_dir / "13.report.json").write_text("{not json"),
        14: lambda: (env.finai_dir / "14.report.json").write_text("[1, 2]"),
    }
    for write in skipped.values():
        write()
    for name in ("abc.report.json", "5.report.json.bak", "5.json", "report.json", "master.rm.json.report.json"):
        (env.finai_dir / name).write_text(json.dumps({"source": "autotailor", "notes": TAILORED}))

    priors, left_out = S.load_priors(env.finai_dir)

    assert priors == [S.Prior(3, "drop", 20), S.Prior(12, "keep", 70)]
    assert left_out == len(skipped), "files not named like a posting's report are not counted"


def test_a_missing_finai_dir_has_nothing_to_check(tmp_path):
    assert S.load_priors(tmp_path / "absent") == ([], 0)


@pytest.mark.parametrize(("notes", "decision"), [
    ("dropped: the role needs a license the candidate does not hold", "drop"),
    ("  Dropped: capitalised and indented", "drop"),
    ("dropped (older tag): still Sonnet's", "drop"),
    ("tailored by autotailor (sonnet, 2 model calls)", "keep"),
    ("tailored by autotailor (sonnet, 3 model calls)", "keep"),
])
def test_sonnets_decision_is_read_from_the_notes(env, notes, decision):
    write_report(env, 1, notes)
    assert S.load_priors(env.finai_dir) == ([S.Prior(1, decision, 62)], 0)


@pytest.mark.parametrize(("score", "expected"), [
    (0, 0), (62, 62), (100, 100), (None, None), (True, None), ("62", None), (62.0, None), ([62], None),
])
def test_sonnets_score_is_the_reports_integer_match_before(env, score, expected):
    write_report(env, 1, TAILORED, score=score)
    assert S.load_priors(env.finai_dir) == ([S.Prior(1, "keep", expected)], 0)


def test_a_report_without_match_before_still_counts(env):
    report = {"source": "autotailor", "notes": SONNET_DROP}
    (env.finai_dir / "4.report.json").write_text(json.dumps(report), encoding="utf-8")
    assert S.load_priors(env.finai_dir) == ([S.Prior(4, "drop", None)], 0)


# --- the report ------------------------------------------------------------------


def test_one_line_per_posting_then_the_summary(conn, env):
    k1 = seed(conn, env, "k1", TAILORED, 70)
    k2 = seed(conn, env, "k2", TAILORED, 64)
    d1 = seed(conn, env, "d1", SONNET_DROP, 20)
    d2 = seed(conn, env, "d2", SONNET_DROP, 28, sentence=YEARS)
    d3 = seed(conn, env, "d3", SONNET_DROP, 25)
    haiku = Haiku(
        k1=screen_reply(66),
        k2=screen_reply(12),
        d1=screen_reply(15),
        d2=screen_reply(55, hard_mismatch={"kind": "years", "quote": YEARS_QUOTE}),
        d3=screen_reply(58),
    )

    code, lines = check(conn, haiku=haiku)

    assert code == 0 and len(haiku.calls) == 5
    assert lines == [
        f"pid={k1} sonnet=keep(70) haiku=keep(66) mismatch=- agree",
        f"pid={k2} sonnet=keep(64) haiku=drop(12) mismatch=- LOST",
        "    why: screen score 12 is below 27",
        f"pid={d1} sonnet=drop(20) haiku=drop(15) mismatch=- caught",
        f"pid={d2} sonnet=drop(28) haiku=drop(55) mismatch=years caught",
        f"pid={d3} sonnet=drop(25) haiku=keep(58) mismatch=- missed",
        "",
        "checked 5 of 5 reports",
        "kept postings Haiku would drop: 1 of 2 (must be 0): NOT MET",
        "Sonnet drops Haiku catches: 2 of 3 (target at least 2): ok",
        "model calls: 5; failures: 0",
    ]


def test_a_kept_posting_lost_to_a_hard_mismatch_says_what_the_quote_was(conn, env):
    pid = seed(conn, env, "k1", TAILORED, 70, sentence=YEARS)
    reply = screen_reply(55, hard_mismatch={"kind": "years", "quote": YEARS_QUOTE})

    code, lines = check(conn, haiku=Haiku(k1=reply))

    assert code == 0
    assert lines[:2] == [
        f"pid={pid} sonnet=keep(70) haiku=drop(55) mismatch=years LOST",
        f'    why: requires 8 or more years of experience: "{YEARS_QUOTE}"',
    ]


def test_a_run_that_meets_both_targets_says_ok(conn, env):
    k1 = seed(conn, env, "k1", TAILORED, 70)
    d1 = seed(conn, env, "d1", SONNET_DROP, 20)
    d2 = seed(conn, env, "d2", SONNET_DROP, 22)

    _, lines = check(conn, haiku=Haiku(k1=screen_reply(66), d1=screen_reply(10), d2=screen_reply(60)))

    assert lines == [
        f"pid={k1} sonnet=keep(70) haiku=keep(66) mismatch=- agree",
        f"pid={d1} sonnet=drop(20) haiku=drop(10) mismatch=- caught",
        f"pid={d2} sonnet=drop(22) haiku=keep(60) mismatch=- missed",
        "",
        "checked 3 of 3 reports",
        "kept postings Haiku would drop: 0 of 1 (must be 0): ok",
        "Sonnet drops Haiku catches: 1 of 2 (target at least 1): ok",
        "model calls: 3; failures: 0",
    ]


@pytest.mark.parametrize(("dropped", "target"), [(23, 12), (22, 11), (3, 2), (2, 1), (1, 1)])
def test_the_target_is_at_least_half_of_the_sonnet_drops(dropped, target):
    def summary(caught):
        lines = []
        tally = S._Tally(checked=dropped, dropped=dropped, dropped_caught=caught)
        S._summary(tally, dropped, 0, lambda line="": lines.append(line))
        return lines

    assert f"Sonnet drops Haiku catches: {target} of {dropped} (target at least {target}): ok" in summary(target)
    assert (
        f"Sonnet drops Haiku catches: {target - 1} of {dropped} (target at least {target}): NOT MET"
        in summary(target - 1)
    )


def test_an_empty_side_of_the_comparison_is_no_data_not_ok(conn, env):
    seed(conn, env, "k1", TAILORED, 70)

    _, lines = check(conn, haiku=Haiku(k1=screen_reply(66)))

    assert lines[-4:] == [
        "checked 1 of 1 reports",
        "kept postings Haiku would drop: 0 of 1 (must be 0): ok",
        "Sonnet drops Haiku catches: 0 of 0 (target at least 0): no data",
        "model calls: 1; failures: 0",
    ]


def test_the_summary_counts_the_report_files_it_left_out(conn, env):
    seed(conn, env, "k1", TAILORED, 70)
    write_report(env, 500, HAIKU_DROP, score=12)
    write_report(env, 501, TAILORED, source="finai")

    _, lines = check(conn, haiku=Haiku(k1=screen_reply(66)))

    assert "checked 1 of 1 reports (2 other report files skipped)" in lines


# --- which postings, and what it never does ----------------------------------------


def test_limit_checks_the_first_reports_by_posting_id(conn, env):
    pids = [seed(conn, env, f"k{n}", TAILORED, 70) for n in range(1, 5)]
    haiku = Haiku(**{f"k{n}": screen_reply(66) for n in range(1, 5)})

    code, lines = check(conn, "--limit", "2", haiku=haiku)

    assert code == 0 and len(haiku.calls) == 2
    assert lines[:2] == [f"pid={pid} sonnet=keep(70) haiku=keep(66) mismatch=- agree" for pid in pids[:2]]
    assert "checked 2 of 2 reports" in lines


def test_a_limit_beyond_the_reports_checks_them_all(conn, env):
    for n in range(1, 4):
        seed(conn, env, f"k{n}", TAILORED, 70)
    haiku = Haiku(**{f"k{n}": screen_reply(66) for n in range(1, 4)})

    _, lines = check(conn, "--limit", "99", haiku=haiku)

    assert len(haiku.calls) == 3 and "checked 3 of 3 reports" in lines


def test_pid_checks_one_posting(conn, env):
    pids = [seed(conn, env, f"k{n}", TAILORED, 70) for n in range(1, 4)]
    haiku = Haiku(**{f"k{n}": screen_reply(66) for n in range(1, 4)})

    code, lines = check(conn, "--pid", str(pids[1]), haiku=haiku)

    assert code == 0 and len(haiku.calls) == 1 and "https://x/k2" in haiku.calls[0]["prompt"]
    assert lines[0] == f"pid={pids[1]} sonnet=keep(70) haiku=keep(66) mismatch=- agree"
    assert "checked 1 of 1 reports" in lines


@pytest.mark.parametrize("pid_arg", ["9999", "2"], ids=["no report", "the screen's own drop"])
def test_pid_without_a_sonnet_made_report_has_nothing_to_check(conn, env, pid_arg):
    seed(conn, env, "k1", TAILORED, 70)
    write_report(env, 2, HAIKU_DROP, score=12)
    haiku = Haiku()

    code, lines = check(conn, "--pid", pid_arg, haiku=haiku)

    assert (code, lines) == (0, ["nothing to check: no Sonnet-made autotailor reports for that posting"])
    assert haiku.calls == []


def test_no_reports_at_all_has_nothing_to_check(conn, env):
    haiku = Haiku()

    code, lines = check(conn, haiku=haiku)

    assert (code, lines) == (0, ["nothing to check: no Sonnet-made autotailor reports"])
    assert haiku.calls == []


def test_check_writes_nothing(conn, env, tmp_path):
    seed(conn, env, "k1", TAILORED, 70)
    seed(conn, env, "d1", SONNET_DROP, 20)
    haiku = Haiku(k1=screen_reply(12), d1=screen_reply(10))  # the lane would record both as drops

    def state():
        return (
            {path.name: path.read_bytes() for path in env.finai_dir.iterdir()},
            sorted(path.name for path in tmp_path.iterdir() if not path.name.startswith("check.db")),
            list(conn.iterdump()),
            conn.total_changes,
            dict(os.environ),
        )

    before = state()
    code, lines = check(conn, haiku=haiku)

    assert code == 0 and len(haiku.calls) == 2 and "LOST" in lines[0] and "caught" in lines[2]
    assert state() == before, "no report, file, DB row or environment variable may change"


def test_no_statement_can_write_while_the_check_runs(conn, env):
    seed(conn, env, "k1", TAILORED, 70)
    seen = []

    def probe(prompt, **kwargs):
        try:
            conn.execute("DELETE FROM postings")
        except sqlite3.OperationalError as exc:
            seen.append(str(exc))
        return {"text": json.dumps(screen_reply(66))}

    code, _ = check(conn, haiku=probe)

    assert code == 0 and seen == ["attempt to write a readonly database"]
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    add(conn, "later")  # the caller's connection can write again afterwards


@pytest.mark.parametrize("initial", [0, 1])
def test_the_callers_query_only_setting_comes_back(conn, env, initial):
    seed(conn, env, "k1", TAILORED, 70)
    conn.execute(f"PRAGMA query_only={initial}")

    check(conn, haiku=Haiku(k1=screen_reply(66)))

    assert conn.execute("PRAGMA query_only").fetchone()[0] == initial


def test_the_default_database_is_opened_read_only(tmp_path, env, monkeypatch):
    path = tmp_path / "default.db"
    setup = db_module.connect(path)
    pid = seed(setup, env, "k1", TAILORED, 70)
    setup.close()
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", path)
    out = io.StringIO()

    code = S.main([], complete=Haiku(k1=screen_reply(66)), out=out)

    assert code == 0
    assert out.getvalue().splitlines()[0] == f"pid={pid} sonnet=keep(70) haiku=keep(66) mismatch=- agree"
    readonly = S._open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            readonly.execute("DELETE FROM postings")
        assert readonly.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 1
    finally:
        readonly.close()


def test_a_missing_database_stops_the_check_and_is_not_created(tmp_path, env, monkeypatch):
    missing = tmp_path / "nowhere" / "jobhunt.db"
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", missing)
    write_report(env, 1, TAILORED)
    haiku = Haiku()
    out = io.StringIO()

    code = S.main([], complete=haiku, out=out)

    assert code == 0
    assert out.getvalue().splitlines() == ["stopped: cannot open the job-hunt database read-only (OperationalError)"]
    assert haiku.calls == [] and not missing.parent.exists()


# --- failures ----------------------------------------------------------------------


def test_a_posting_without_a_usable_answer_is_a_failure_and_the_run_goes_on(conn, env):
    ok = seed(conn, env, "k1", TAILORED, 70)
    garbled = seed(conn, env, "k2", TAILORED, 70)
    broken = seed(conn, env, "k3", TAILORED, 70)
    write_report(env, 9999, TAILORED, score=70)  # its posting is gone from the DB
    haiku = Haiku(k1=screen_reply(66), k2="not json at all", k3=RuntimeError("cli exploded"))

    code, lines = check(conn, haiku=haiku)

    assert code == 0
    assert lines == [
        f"pid={ok} sonnet=keep(70) haiku=keep(66) mismatch=- agree",
        f"pid={garbled} sonnet=keep(70) haiku=error reason=screen reply unusable: the reply was not a single JSON object",
        f"pid={broken} sonnet=keep(70) haiku=error reason=screen call failed (RuntimeError: cli exploded)",
        "pid=9999 sonnet=keep(70) haiku=error reason=posting not found",
        "",
        "checked 4 of 4 reports",
        "kept postings Haiku would drop: 0 of 1 (must be 0): ok",
        "Sonnet drops Haiku catches: 0 of 0 (target at least 0): no data",
        "model calls: 3; failures: 3 (no usable answer: left out of the counts above)",
    ]


def test_a_bug_in_one_posting_is_a_failure_not_a_crash(conn, env, monkeypatch):
    k1 = seed(conn, env, "k1", TAILORED, 70)
    k2 = seed(conn, env, "k2", TAILORED, 70)
    real = A.screen_posting

    def flaky(connection, posting_id, **kwargs):
        if posting_id == k1:
            raise ValueError("boom\nsecond line")
        return real(connection, posting_id, **kwargs)

    monkeypatch.setattr(A, "screen_posting", flaky)

    code, lines = check(conn, haiku=Haiku(k2=screen_reply(66)))

    assert code == 0
    assert lines[:2] == [
        f"pid={k1} sonnet=keep(70) haiku=error reason=unexpected ValueError: boom second line",
        f"pid={k2} sonnet=keep(70) haiku=keep(66) mismatch=- agree",
    ]
    assert lines[-1] == "model calls: 1; failures: 1 (no usable answer: left out of the counts above)"


@pytest.mark.parametrize("error", [SpendGateError("gate"), SubscriptionUnavailableError("signed out")],
                         ids=["spend-gate", "subscription-unavailable"])
def test_the_spend_gate_stops_the_run_with_a_partial_report_and_exit_0(conn, env, error):
    k1 = seed(conn, env, "k1", TAILORED, 70)
    seed(conn, env, "k2", TAILORED, 70)
    seed(conn, env, "k3", TAILORED, 70)
    haiku = Haiku(k1=screen_reply(66), k2=error, k3=screen_reply(66))

    code, lines = check(conn, haiku=haiku)

    assert code == 0 and len(haiku.calls) == 2, "nothing is called after the gate refuses"
    assert lines == [
        f"pid={k1} sonnet=keep(70) haiku=keep(66) mismatch=- agree",
        "",
        "checked 1 of 3 reports",
        "kept postings Haiku would drop: 0 of 1 (must be 0): ok",
        "Sonnet drops Haiku catches: 0 of 0 (target at least 0): no data",
        "model calls: 1; failures: 0",
        f"stopped early, after 1 of 3 reports: {type(error).__name__}: {error}",
    ]


@pytest.mark.parametrize(("master_text", "message"), [
    (None, "master resume missing or unreadable"),
    ("{not json", "master resume missing or unreadable"),
    (json.dumps({"workExperience": []}), "master resume has an unexpected shape"),
], ids=["missing", "garbled", "odd-shape"])
def test_a_master_resume_that_cannot_be_used_stops_the_check(conn, env, master_text, message):
    seed(conn, env, "k1", TAILORED, 70)
    if master_text is None:
        env.master.unlink()
    else:
        env.master.write_text(master_text, encoding="utf-8")
    haiku = Haiku()

    code, lines = check(conn, haiku=haiku)

    assert (code, lines) == (0, [f"stopped: {message}"])
    assert haiku.calls == []


# --- arguments, backend, entry point -----------------------------------------------


@pytest.mark.parametrize("args", [
    ["--limit", "0"], ["--limit", "-2"], ["--limit", "x"], ["--limit"],
    ["--pid", "0"], ["--pid", "abc"], ["--bogus"], ["extra"],
])
def test_bad_arguments_exit_2_before_any_work(conn, env, capsys, args):
    seed(conn, env, "k1", TAILORED, 70)
    haiku = Haiku()
    out = io.StringIO()

    assert S.main(args, complete=haiku, conn=conn, out=out) == 2

    assert haiku.calls == [] and out.getvalue() == ""
    assert "usage: python -m jobhunt.screen_check" in capsys.readouterr().err


def test_help_exits_0(capsys):
    assert S.main(["--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: python -m jobhunt.screen_check [-h] [--limit N] [--pid ID]")


def test_default_backend_is_claude_cli_whatever_the_env_says(conn, env, monkeypatch):
    seed(conn, env, "k1", TAILORED, 70)
    haiku = Haiku(k1=screen_reply(66))
    monkeypatch.setattr(claude_cli, "complete", haiku)

    def trap(*args, **kwargs):
        raise AssertionError("screen_check must never reach openrouter")

    monkeypatch.setattr(openrouter, "complete", trap)
    monkeypatch.setattr(openrouter.requests, "post", trap)
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "openrouter")
    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "anthropic/claude-opus-4")
    out = io.StringIO()

    assert S.main([], conn=conn, out=out) == 0

    assert len(haiku.calls) == 1 and out.getvalue().splitlines()[0].endswith("haiku=keep(66) mismatch=- agree")


def test_each_call_is_the_autotailor_screen_call(conn, env):
    seed(conn, env, "k1", TAILORED, 70)
    haiku = Haiku(k1=screen_reply(66))

    check(conn, haiku=haiku)

    (call,) = haiku.calls
    assert (call["model"], call["max_tokens"], call["system"]) == (A.SCREEN_MODEL, A.SCREEN_MAX_TOKENS, A.SYSTEM)
    assert "Acme Co" in call["prompt"], "the master's roles go in"
    assert "Pat Doe" not in call["prompt"] and "p@x.edu" not in call["prompt"], "its personal info does not"


def test_output_goes_to_stdout_by_default(conn, env, capsys):
    pid = seed(conn, env, "k1", TAILORED, 70)

    assert S.main([], complete=Haiku(k1=screen_reply(66)), conn=conn) == 0

    assert capsys.readouterr().out.splitlines()[0] == f"pid={pid} sonnet=keep(70) haiku=keep(66) mismatch=- agree"


def test_runs_as_a_module():
    """--help and a bad flag end before the DB or any model is touched."""
    ran = subprocess.run(
        [sys.executable, "-m", "jobhunt.screen_check", "--help"],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )
    assert ran.returncode == 0 and ran.stdout.startswith("usage: python -m jobhunt.screen_check")
    bad = subprocess.run(
        [sys.executable, "-m", "jobhunt.screen_check", "--limit", "0"],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )
    assert bad.returncode == 2 and "'0' is not a positive whole number" in bad.stderr
