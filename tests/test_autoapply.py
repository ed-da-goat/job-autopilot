"""Autoapply orchestrator: dry quota, caps, skips, readiness, KILL, tailor handling, state files,
arm, CLI.

Every seam is injected: fake tailor/assist/submit/browser/posting-answers/guard/guard-reader
functions, a no-op sleep, a tmp root and a tmp DB, with the cwd moved under tmp_path. No browser, no network,
no model call, and nothing under the real data/ directory (data/jobhunt.db, data/autoapply/,
data/apply_packs/).
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import json
import plistlib
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

from jobhunt import apply_assist as apply_assist_package
from jobhunt import applylock, eligibility
from jobhunt import autoapply as autoapply_module
from jobhunt import cli as cli_module
from jobhunt import db as db_module
from jobhunt import finai as finai_module
from jobhunt import lifecycle
from jobhunt.apply_assist import submitwatch
from jobhunt.apply_assist.schema import FieldSpec, FillOutcome, Resolution
from jobhunt.autoapply import AutoApplyConfig
from jobhunt.connectors.openrouter import SpendGateError
from jobhunt.eligibility import LaneConfig, SearchPreferences

REAL_CONNECT = db_module.connect
REAL_RECORD_BLOCKERS = submitwatch.record_blockers
REPO = Path(__file__).resolve().parents[1]
PREFS = SearchPreferences(
    version=1, allowed_countries=("US",), allow_us_remote=True, max_eligible_years=3,
    review_years_max=5, blocked_title_terms=("senior", "sr", "director", "head of"),
    review_title_terms=(), target_roles=(), target_start="", required_application_fields=(),
    lane=LaneConfig(preset="finance_ai"),
)
AI_DESC = "We use machine learning and Python automation for our desks."
BASE_TIME = datetime(2026, 10, 28, 12, 0)
URLS = {
    "greenhouse": "https://boards.greenhouse.io/{slug}/jobs/{key}",
    "lever": "https://jobs.lever.co/{slug}/{key}",
    "ashby": "https://jobs.ashbyhq.com/{slug}/{key}",
    "workday": "https://{slug}.wd1.myworkdayjobs.com/en-US/careers/job/{key}",
}
SEQ = itertools.count(1)
KILL_MESSAGE = "KILL file present"
RUN_STOPPED_TODO = "Fix the cause; the next scheduled run retries."


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(finai_module.eligibility, "load_preferences", lambda: PREFS)
    # AutoApplyConfig reads its caps from the preferences: pin them to the code defaults.
    monkeypatch.setattr(eligibility, "user_preferences", lambda: PREFS)
    # Whatever the CLI or a helper asks for, the only DB these tests can open is the tmp one.
    monkeypatch.setattr(db_module, "connect", lambda *a, **k: REAL_CONNECT(tmp_path / "t.db"))
    # data/apply.lock, data/apply_packs/... and data/autoapply resolve under tmp_path.
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def readiness(monkeypatch):
    """The submit gate's readiness check (submitwatch.record_blockers), stubbed: every posting is
    ready unless a test lists blockers for it. `real_readiness` swaps the real one back in."""
    stub = Readiness()
    monkeypatch.setattr(submitwatch, "record_blockers", stub)
    return stub


@pytest.fixture
def conn(tmp_path):
    connection = REAL_CONNECT(tmp_path / "t.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def real_readiness(readiness, monkeypatch, conn):
    """The real record_blockers over the tmp DB, so readiness comes from actual rows. As in the
    live DB, a reviewed default resume variant exists, so a lone select_resume_variant resolves."""
    monkeypatch.setattr(submitwatch, "record_blockers", REAL_RECORD_BLOCKERS)
    monkeypatch.setattr(lifecycle, "load_preferences", lambda: PREFS)
    review_default_variant(conn)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "data" / "autoapply"


# --- data helpers --------------------------------------------------------------------


def iso(days: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def add(conn, *, ats="greenhouse", company=None, title="Financial Analyst",
        location="New York, NY", status="active", desc=AI_DESC):
    """One finance x AI lane posting. Later inserts rank lower, so insertion order is lane order."""
    seq = next(SEQ)
    company = company if company is not None else f"Firm {seq}"
    url = URLS[ats].format(slug=f"firm{seq}", key=seq)
    posted = (BASE_TIME - timedelta(minutes=seq)).strftime("%Y-%m-%d %H:%M")
    conn.execute(
        """INSERT INTO postings (source, source_posting_id, company, title, location, url,
               posted_at, raw_json, status, first_seen_at, last_seen_at)
           VALUES ('ashby', ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-01', '2026-10-01')""",
        (f"p{seq}", company, title, location, url, posted,
         json.dumps({"descriptionPlain": desc}), status),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM postings WHERE source_posting_id = ?", (f"p{seq}",))
    return row.fetchone()["id"]


def add_many(conn, count, **kwargs):
    return [add(conn, **kwargs) for _ in range(count)]


def posting_url(conn, posting_id):
    return conn.execute("SELECT url FROM postings WHERE id = ?", (posting_id,)).fetchone()["url"]


def write_pdf(posting_id):
    """Where B's tailor leaves the PDF (relative to the cwd, which is tmp_path here)."""
    path = Path("data/apply_packs/resume/tailored") / str(posting_id) / "Alex_Rivera_Resume.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 fake resume")
    return path


def add_report(conn, posting_id):
    stamp = iso()
    conn.execute(
        "INSERT INTO artifacts (posting_id, kind, content, created_at, updated_at) "
        "VALUES (?, ?, '{}', ?, ?)",
        (posting_id, finai_module.REPORT_KIND, stamp, stamp),
    )
    conn.commit()


def set_ready(conn, posting_id, stamp="2026-10-20T00:00:00"):
    conn.execute(
        "INSERT OR REPLACE INTO funnel (posting_id, stage, created_at, updated_at) "
        "VALUES (?, 'ready', ?, ?)",
        (posting_id, stamp, stamp),
    )
    conn.commit()


def make_tailored(conn, posting_id, stamp="2026-10-20T00:00:00"):
    write_pdf(posting_id)
    add_report(conn, posting_id)
    set_ready(conn, posting_id, stamp)


def record_application(conn, posting_id, ref=None, days=0):
    stamp = iso(days)
    conn.execute(
        "INSERT INTO applications (posting_id, submitted_at, channel, confirmation_ref, "
        "created_at, updated_at) VALUES (?, ?, 'ats', ?, ?, ?)",
        (posting_id, stamp, ref, stamp, stamp),
    )
    conn.commit()


def add_activity(conn, posting_id, action, days_ago, entity_type="posting"):
    conn.execute(
        "INSERT INTO activity_events (entity_type, entity_id, action, at) VALUES (?, ?, ?, ?)",
        (entity_type, posting_id, action, iso(days_ago)),
    )
    conn.commit()


def assess(conn, posting_id, status="eligible"):
    """A stored eligibility assessment: the row score_sweep writes and the readiness gate reads."""
    conn.execute(
        "INSERT INTO eligibility_assessments (posting_id, rubric_version, preferences_version, "
        "status, assessed_at) VALUES (?, 1, 1, ?, ?)",
        (posting_id, status, iso()),
    )
    conn.commit()


def review_default_variant(conn, reviewed=True):
    conn.execute("UPDATE resume_variants SET reviewed_at = ?", (iso() if reviewed else None,))
    conn.commit()


def write_marker(root, posting_id, status="confirmed", at=None):
    """A submit marker in the shape apply_assist/submit.py writes."""
    path = Path(root) / "submitted" / f"{posting_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = at or iso()
    path.write_text(json.dumps(
        {"posting_id": posting_id, "status": status, "at": stamp, "updated_at": stamp}
    ))
    return path


def write_state(root, dry_runs=(), armed_at=None, armed_note=""):
    root.mkdir(parents=True, exist_ok=True)
    (root / "state.json").write_text(
        json.dumps({"dry_runs": list(dry_runs), "armed_at": armed_at, "armed_note": armed_note})
    )


def dry_entry(posting_id, status="dry_run_ok", ats="greenhouse"):
    return {"posting_id": posting_id, "at": iso(), "status": status, "ats": ats}


def arm_now(root, dry_runs=()):
    write_state(root, dry_runs, armed_at=iso(), armed_note="test arm")


def write_blocked(root, ages):
    """blocked.json from {posting_id: days_old}."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "blocked.json").write_text(json.dumps(
        {str(pid): {"reason": "earlier block", "at": iso(days)} for pid, days in ages.items()}
    ))


def read_json(path):
    return json.loads(Path(path).read_text())


def dry_runs_of(root):
    return read_json(root / "state.json")["dry_runs"]


def statuses(summary):
    return [outcome["status"] for outcome in summary["outcomes"]]


def reasons(summary):
    return {outcome["posting_id"]: outcome["reason"] for outcome in summary["outcomes"]}


def digest_text(root):
    files = sorted(root.glob("????-??-??.md"))
    assert len(files) == 1, files
    return files[0].read_text()


def needs_you_text(root):
    return (root / "needs-you.md").read_text()


def needs_you_headings(root):
    """The `## ` lines of needs-you.md, which is what `autoapply status` counts."""
    path = root / "needs-you.md"
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.startswith("## ")]


def run_entries(root):
    """Each `Run stopped` entry of needs-you.md as its lines, the heading without its `## `."""
    path = root / "needs-you.md"
    if not path.exists():
        return []
    parts = path.read_text().split("\n## ")[1:]
    return [part.rstrip("\n").splitlines() for part in parts if part.startswith("Run stopped ")]


def assert_run_entry(root, reason, mode="dry"):
    """Exactly one `Run stopped` entry, stamped just now, with `reason` and the fixed to-do line."""
    [entry] = run_entries(root)
    stamp = datetime.strptime(entry[0], f"Run stopped %Y-%m-%d %H:%M ({mode})")
    assert abs(datetime.now() - stamp) < timedelta(minutes=2)
    assert entry[1:] == [f"- Reason: {reason}", f"- What you do: {RUN_STOPPED_TODO}"]


# --- fakes ---------------------------------------------------------------------------


class Tailor:
    """autotailor.tailor_posting stand-in. plan: posting_id -> mode (default 'tailored').

    Modes: tailored, tailored_no_path, dropped, failed, no_pdf, weird, or an Exception to raise.
    Like contract B, a tailored result leaves the PDF, a match_report and stage `ready`, and a
    drop leaves a match_report (which takes the posting out of the finai lane).
    """

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []

    def __call__(self, conn, posting_id):
        self.calls.append(posting_id)
        mode = self.plan.get(posting_id, "tailored")
        if isinstance(mode, Exception):
            raise mode
        if mode == "dropped":
            add_report(conn, posting_id)
            return SimpleNamespace(posting_id=posting_id, status="dropped", resume_pdf=None,
                                   reason="match 61 below 70")
        if mode == "failed":
            return SimpleNamespace(posting_id=posting_id, status="failed", resume_pdf=None,
                                   reason="model returned no JSON")
        if mode == "weird":
            return SimpleNamespace(posting_id=posting_id, status="queued", resume_pdf=None,
                                   reason="")
        if mode == "no_pdf":
            return SimpleNamespace(posting_id=posting_id, status="tailored", reason="",
                                   resume_pdf=Path("data/apply_packs/missing/nothing.pdf"))
        pdf = write_pdf(posting_id)
        add_report(conn, posting_id)
        set_ready(conn, posting_id)
        return SimpleNamespace(
            posting_id=posting_id, status="tailored", reason="",
            resume_pdf=None if mode == "tailored_no_path" else pdf,
        )


class Assist:
    """fill.run_assist stand-in: records the call and hands back a digest-like object."""

    def __init__(self, fail=None):
        self.fail = fail or {}
        self.calls = []
        self.hook = None
        # posting_id -> [(selector, question, answer)]: AI drafts the digest carries, each one
        # typed into the form (a draft Resolution plus a clean FillOutcome, like fill.py's).
        self.drafts = {}

    def __call__(self, page, posting_id, url, answers, resume, **kwargs):
        self.calls.append({"page": page, "posting_id": posting_id, "url": url,
                           "answers": answers, "resume": resume, "kwargs": kwargs})
        if self.hook:
            self.hook(posting_id)
        if posting_id in self.fail:
            raise self.fail[posting_id]
        plan = self.drafts.get(posting_id)
        if not plan:
            return SimpleNamespace(posting_id=posting_id)
        fields = [FieldSpec(selector=sel, kind="textarea", label=question) for sel, question, _ in plan]
        return SimpleNamespace(
            posting_id=posting_id,
            drafts=[Resolution(field=f, semantic_key=None, action="fill", value=answer, draft=True)
                    for f, (_, _, answer) in zip(fields, plan)],
            filled=[FillOutcome(field=f, semantic_key=None, value=answer, readback=answer,
                                committed=True)
                    for f, (_, _, answer) in zip(fields, plan)],
        )


class PostingAnswers:
    """posting_answers.posting_answers stand-in. plan: posting_id -> the answers that come from
    that posting (default none) or an Exception to raise. Like the real one it returns a new
    dict on every call."""

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []

    def __call__(self, conn, posting_id):
        self.calls.append((conn, posting_id))
        step = self.plan.get(posting_id, {})
        if isinstance(step, Exception):
            raise step
        return dict(step)


class Guard:
    """submit_guard.install stand-in. Records each page it was put on and how many times that page
    had navigated by then; error: an Exception to raise instead, on every call."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def __call__(self, page):
        self.calls.append({"page": page, "gotos_before": len(page.goto_calls),
                           "closed_before": page.closed})
        if self.error:
            raise self.error


class GuardReads:
    """submit_guard.blockers stand-in, asked when an attempt raised. lines: what the guard holds
    for a page (a list, or a function of the page), or an Exception to raise. Records each page
    it was asked about and whether that page was still open then."""

    def __init__(self, lines=None):
        self.lines = [] if lines is None else lines
        self.calls = []

    def __call__(self, page):
        self.calls.append({"page": page, "closed_before": page.closed})
        if isinstance(self.lines, Exception):
            raise self.lines
        if callable(self.lines):
            return self.lines(page)
        return list(self.lines)


class Submit:
    """submit_application stand-in. plan: posting_id -> status | (status, blockers) | Exception
    | 'marker_then_error'. The default is dry_run_ok in a dry run and submitted when live.

    Like the real one, a click writes the marker first, and a clean confirmation records the
    application row with an `autoapply:<id>` ref.
    """

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []
        self.hook = None
        self.final = {}  # deferred handoffs: posting_id -> (status, blockers) once Await runs

    def __call__(self, page, conn, digest, *, resume_path, ats, live, audit_root, **extra):
        pid = digest.posting_id
        self.calls.append({"posting_id": pid, "live": live, "ats": ats, "page": page,
                           "resume_path": resume_path, "audit_root": audit_root, "extra": extra})
        if self.hook:
            self.hook(pid)
        step = self.plan.get(pid)
        if isinstance(step, Exception):
            raise step
        if step == "marker_then_error":
            write_marker(audit_root, pid, "clicking")
            step = "error"
        status, blockers = step if isinstance(step, tuple) else (step, [])
        status = status or ("submitted" if live else "dry_run_ok")
        if extra.get("handoff_defer") and status in ("submitted", "handed_off"):
            # Like the real one: the marker first, then the page is left open, untouched.
            write_marker(audit_root, pid, "handed_off")
            self.final[pid] = (status, list(blockers))
            return SimpleNamespace(posting_id=pid, status="handoff_pending", blockers=[],
                                   audit_dir=Path(audit_root) / "runs" / str(pid),
                                   confirmation_ref=None)
        return self.finish(conn, audit_root, pid, status, blockers)

    def finish(self, conn, audit_root, pid, status, blockers):
        ref = None
        if status in ("submitted", "unconfirmed", "not_submitted", "handed_off"):
            write_marker(audit_root, pid, "confirmed" if status == "submitted" else status)
        if status == "submitted" and not blockers:
            ref = f"autoapply:{pid}"
            record_application(conn, pid, ref=ref)
        return SimpleNamespace(posting_id=pid, status=status, blockers=list(blockers),
                               audit_dir=Path(audit_root) / "runs" / str(pid),
                               confirmation_ref=ref)


class Await:
    """await_handoffs stand-in. Resolves each item to what the Submit plan said its final status
    is (`override`: posting_id -> status | (status, blockers) | Exception to raise for the whole
    stack). Records each stack it was asked for and the state of every page at that moment."""

    def __init__(self, submit):
        self.submit = submit
        self.stacks = []
        self.override = {}
        self.raises = None
        self.on_call = None

    def __call__(self, items, conn, *, timeout_s):
        self.stacks.append({
            "ids": [item.posting_id for item in items], "timeout_s": timeout_s,
            "closed": [item.page.closed for item in items],
            "labels": [item.label for item in items],
            "drafted": [item.drafted for item in items],
        })
        if self.on_call:
            self.on_call()
        if self.raises:
            raise self.raises
        out = {}
        for item in items:
            pid = item.posting_id
            if pid in self.override:
                step = self.override[pid]
                if step is None:
                    continue  # no outcome for this one
                status, blockers = step if isinstance(step, tuple) else (step, [])
            else:
                status, blockers = self.submit.final[pid]
            out[pid] = self.submit.finish(conn, item_root(item), pid, status, blockers)
        return out


def item_root(item):
    return item.audit_root


class Readiness:
    """submitwatch.record_blockers stand-in. blockers: posting_id -> list of blocker names, or
    an Exception to raise. A posting that is not listed is ready."""

    def __init__(self):
        self.blockers = {}
        self.calls = []

    def __call__(self, conn, posting_id):
        self.calls.append(posting_id)
        listed = self.blockers.get(posting_id) or []
        if isinstance(listed, Exception):
            raise listed
        return list(listed)


class Page:
    def __init__(self, code):
        self.code = code
        self.closed = False
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        return SimpleNamespace(status=self.code)

    def close(self):
        self.closed = True


class Browser:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Handle:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class Context:
    def __init__(self, codes=(), new_page_error=None, close_error=None):
        self.pages = []
        self.codes = list(codes)  # HTTP status per page, in order; 200 once they run out
        self.new_page_error = new_page_error
        self.close_error = close_error
        self.closed = False
        self.browser = Browser()

    def new_page(self):
        if self.new_page_error:
            raise self.new_page_error
        page = Page(self.codes.pop(0) if self.codes else 200)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakeBrowser:
    """browser_fn stand-in: counts opens and returns (handle, context, is_cdp)."""

    def __init__(self, *, cdp=False, open_error=None, **context_kwargs):
        self.cdp = cdp
        self.open_error = open_error
        self.opened = 0
        self.handle = Handle()
        self.context = Context(**context_kwargs)

    def __call__(self):
        self.opened += 1
        if self.open_error:
            raise self.open_error
        return self.handle, self.context, self.cdp


class Rig:
    """One run's fakes plus a recorder for the pause between postings."""

    def __init__(self, conn, root, cfg=None, *, tailor=None, submit=None, assist_fail=None,
                 browser=None, posting_answers=None, guard_error=None, guard_reads=None,
                 handoff=False):
        self.conn = conn
        self.root = root
        # handoff=False: these tests cover the click path (kept in the code, unused by default).
        # Handoff has its own tests below.
        self.cfg = dataclasses.replace(cfg or AutoApplyConfig(), handoff=handoff)
        self.tailor = Tailor(tailor)
        self.submit = Submit(submit)
        self.assist = Assist(assist_fail)
        self.browser = browser or FakeBrowser()
        self.posting_answers = PostingAnswers(posting_answers)
        self.guard = Guard(guard_error)
        self.guard_reads = GuardReads(guard_reads)
        self.await_ = Await(self.submit)
        self.sleeps = []
        self.on_sleep = None

    def _sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep()

    def run(self, **overrides):
        kwargs = {
            "cfg": self.cfg, "root": self.root, "tailor_fn": self.tailor,
            "submit_fn": self.submit, "assist_fn": self.assist, "browser_fn": self.browser,
            "posting_answers_fn": self.posting_answers, "guard_fn": self.guard,
            "guard_blockers_fn": self.guard_reads, "sleep_fn": self._sleep,
            "await_fn": self.await_,
        }
        kwargs.update(overrides)
        return autoapply_module.run_autoapply(self.conn, **kwargs)


# --- config and dry mode ---------------------------------------------------------------


def counts_of(summary):
    return {key: value for key, value in summary["counts"].items() if value}


def test_config_defaults_match_the_spec():
    cfg = AutoApplyConfig()
    assert (cfg.daily_cap, cfg.company_cap, cfg.company_window_days) == (25, 3, 30)
    assert (cfg.dry_run_quota, cfg.dry_run_per_ats, cfg.dry_company_cap) == (10, 4, 1)
    assert (cfg.pause_s, cfg.max_tier, cfg.max_consecutive_failures) == (20.0, 4, 5)
    assert cfg.handoff_pause_s == 5.0
    assert (cfg.hard_req_filter, cfg.hard_req_min_years) == (True, 5)
    assert cfg.hard_req_rules == {"jd", "phd", "masters", "license", "domain_years", "years"}
    assert cfg.max_tailors_per_run is None
    assert (cfg.handoff, cfg.handoff_timeout_min, cfg.handoff_stack) == (True, 60.0, 10)


def test_dry_run_stops_at_the_quota_and_never_clicks(conn, root):
    ids = (add_many(conn, 4, ats="greenhouse") + add_many(conn, 4, ats="lever")
           + add_many(conn, 4, ats="ashby"))
    stamp = iso()
    conn.execute(
        "INSERT INTO application_answers (semantic_key, value, created_at, updated_at) "
        "VALUES ('first_name', 'Alex', ?, ?)", (stamp, stamp),
    )
    conn.commit()
    rig = Rig(conn, root)
    summary = rig.run()

    assert summary["mode"] == "dry"
    assert summary["processed"] == ids[:10]
    assert summary["stop_reason"] == autoapply_module.DRY_DONE_REASON
    assert summary["aborted"] is False
    assert counts_of(summary) == {"dry_run_ok": 10}
    assert summary["tailor_calls"] == 10
    assert rig.tailor.calls == ids[:10]
    assert [call["live"] for call in rig.submit.calls] == [False] * 10
    assert not (root / "submitted").exists()
    assert rig.sleeps == [20.0] * 9
    entries = dry_runs_of(root)
    assert [entry["posting_id"] for entry in entries] == ids[:10]
    assert {entry["status"] for entry in entries} == {"dry_run_ok"}
    assert Counter(entry["ats"] for entry in entries) == {"greenhouse": 4, "lever": 4, "ashby": 2}
    first = rig.assist.calls[0]
    first_company = conn.execute(
        "SELECT company FROM postings WHERE id = ?", (ids[0],)
    ).fetchone()["company"]
    assert first["kwargs"] == {
        "conn": conn, "tailor": False, "company": first_company, "autoapply": True,
    }
    assert first["answers"] == {"first_name": "Alex"}
    tailored = Path("data/apply_packs/resume/tailored") / str(ids[0]) / "Alex_Rivera_Resume.pdf"
    assert Path(first["resume"]) == tailored.resolve()
    assert Path(first["resume"]).is_absolute() and Path(first["resume"]).is_file()
    assert rig.submit.calls[0]["resume_path"] == tailored.resolve()
    assert rig.submit.calls[0]["audit_root"] == root
    footer = "Stopped: dry run complete: review, then arm. Attempted 10. Tailor calls 10."
    assert footer in digest_text(root)


def test_the_assist_is_told_which_company_each_posting_is_for(conn, root):
    # A how-heard question that names the company ("How did you first learn about
    # Veeva?") can only be read with it. A posting with no company passes None.
    named = add(conn, ats="lever", company="Veeva Systems")
    unnamed = add(conn, ats="greenhouse", company="")
    rig = Rig(conn, root)
    summary = rig.run()

    assert summary["processed"] == [named, unnamed]
    assert [call["posting_id"] for call in rig.assist.calls] == [named, unnamed]
    assert [call["kwargs"]["company"] for call in rig.assist.calls] == ["Veeva Systems", None]


def test_dry_run_holds_each_ats_to_its_limit(conn, root):
    ids = add_many(conn, 6)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == ids[:4]
    assert summary["skipped"] == {"dry_ats_limit": 2}
    assert summary["stop_reason"] == (
        "pool exhausted (2 postings left over at the per-ATS dry-run limit of 4)"
    )
    assert rig.tailor.calls == ids[:4]  # a skipped posting costs no tailor call


def test_dry_run_counts_blocked_but_not_errors(conn, root):
    a, b, c = add_many(conn, 3)
    rig = Rig(conn, root, submit={a: ("blocked", ["salary_required"])},
              assist_fail={b: RuntimeError("boom")})
    summary = rig.run()
    assert statuses(summary) == ["blocked", "error", "dry_run_ok"]
    assert counts_of(summary) == {"blocked": 1, "error": 1, "dry_run_ok": 1}
    recorded = [(entry["posting_id"], entry["status"]) for entry in dry_runs_of(root)]
    assert recorded == [(a, "blocked"), (c, "dry_run_ok")]
    assert set(autoapply_module.load_blocked(root)) == {str(a), str(b)}


def test_a_second_dry_run_skips_what_the_first_one_did(conn, root):
    ids = add_many(conn, 4)
    rig = Rig(conn, root)
    first = rig.run(limit=2)
    assert first["processed"] == ids[:2]
    assert first["stop_reason"] == "limit reached (2 postings)"
    second = rig.run()
    assert second["skipped"] == {"already_dry_run": 2}
    assert second["processed"] == ids[2:]
    assert rig.tailor.calls == ids  # the first two were tailored once, never again
    assert [entry["posting_id"] for entry in dry_runs_of(root)] == ids


def test_dry_run_takes_one_posting_per_company(conn, root):
    h1 = add(conn, company="Harvey")
    add(conn, company="Harvey, Inc.")
    add(conn, company="HARVEY INC")
    o1 = add(conn, company="Other One")
    o2 = add(conn, company="Other Two")
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == [h1, o1, o2]
    assert summary["skipped"] == {"dry_company_cap": 2}
    assert rig.tailor.calls == [h1, o1, o2]


@pytest.mark.parametrize("status", ["dry_run_ok", "blocked"])
def test_an_earlier_dry_attempt_counts_toward_the_dry_company_cap(conn, root, status):
    earlier = add(conn, company="Harvey")
    later = add(conn, company="Harvey Inc")
    other = add(conn, company="Other")
    write_state(root, [dry_entry(earlier, status)])
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"already_dry_run": 1, "dry_company_cap": 1}
    assert summary["processed"] == [other]
    assert later not in rig.tailor.calls


def test_an_earlier_dry_error_does_not_count_toward_the_company_cap(conn, root):
    earlier = add(conn, company="Harvey", status="closed")
    first = add(conn, company="Harvey")
    second = add(conn, company="Harvey")
    write_state(root, [dry_entry(earlier, "error")])
    summary = Rig(conn, root).run()
    assert summary["processed"] == [first]
    assert summary["skipped"] == {"dry_company_cap": 1}
    assert second not in summary["processed"]


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_live_company_cap_follows_the_config(conn, root, cap):
    ids = add_many(conn, 4, company="Acme")
    arm_now(root)
    summary = Rig(conn, root, AutoApplyConfig(company_cap=cap)).run()
    assert summary["mode"] == "live"
    assert summary["processed"] == ids[:cap]
    assert summary["skipped"] == {"company_cap": 4 - cap}


def test_real_submissions_cap_a_company_even_in_a_dry_run(conn, root):
    done = add_many(conn, 3, company="Acme")
    for posting_id in done:
        record_application(conn, posting_id, days=5)
    add(conn, company="Acme")
    other = add(conn, company="Other")
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"already_submitted": 3, "company_cap": 1}
    assert summary["processed"] == [other]


def test_the_pool_is_not_sliced_before_the_skip_rules(conn, root):
    greenhouse = add_many(conn, 12)
    lever = add(conn, ats="lever")
    cfg = AutoApplyConfig(daily_cap=2, dry_run_per_ats=1)
    rig = Rig(conn, root, cfg)
    summary = rig.run()
    assert summary["processed"] == [greenhouse[0], lever]
    assert summary["skipped"] == {"dry_ats_limit": 11}
    assert summary["stop_reason"] == (
        "pool exhausted (11 postings left over at the per-ATS dry-run limit of 1)"
    )
    assert rig.tailor.calls == [greenhouse[0], lever]


def test_workday_postings_never_reach_the_pool(conn, root):
    add_many(conn, 3, ats="workday")
    greenhouse = add_many(conn, 2)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == greenhouse
    assert summary["skipped"] == {}
    assert summary["stop_reason"] == "pool exhausted"


# --- armed mode ------------------------------------------------------------------------


def test_an_armed_run_submits_and_records_each_posting(conn, root):
    ids = add_many(conn, 3)
    arm_now(root)
    rig = Rig(conn, root)
    summary = rig.run()

    assert summary["mode"] == "live"
    assert summary["processed"] == ids
    assert counts_of(summary) == {"submitted": 3}
    assert summary["stop_reason"] == "pool exhausted"
    assert [call["live"] for call in rig.submit.calls] == [True] * 3
    assert rig.sleeps == [20.0, 20.0]
    for posting_id in ids:
        assert (root / "submitted" / f"{posting_id}.json").is_file()
        row = conn.execute(
            "SELECT confirmation_ref FROM applications WHERE posting_id = ?", (posting_id,)
        ).fetchone()
        assert row["confirmation_ref"] == f"autoapply:{posting_id}"
    assert dry_runs_of(root) == []
    assert not (root / "needs-you.md").exists()


def test_an_armed_run_stops_at_the_daily_cap(conn, root):
    add_many(conn, 5)
    arm_now(root)
    summary = Rig(conn, root, AutoApplyConfig(daily_cap=2)).run()
    assert counts_of(summary) == {"submitted": 2}
    assert summary["stop_reason"] == "daily cap reached (2 submissions today)"
    assert summary["tailor_calls"] == 2
    assert summary["aborted"] is False


def test_the_daily_cap_counts_only_todays_autoapply_submissions(conn, root):
    marked_today, marked_old, by_hand, db_auto, fresh = add_many(conn, 5)
    write_marker(root, marked_today)
    write_marker(root, marked_old, at=iso(2))
    record_application(conn, by_hand)
    record_application(conn, db_auto, ref=f"autoapply:{db_auto}")
    arm_now(root)
    summary = Rig(conn, root, AutoApplyConfig(daily_cap=3)).run()
    assert summary["processed"] == [fresh]
    assert summary["stop_reason"] == "daily cap reached (3 submissions today)"
    assert summary["skipped"] == {"submitted_marker": 2, "already_submitted": 2}


def test_the_company_cap_uses_a_thirty_day_window_and_normalized_names(conn, root):
    for name in ("Acme, Inc.", "ACME INC", "Acme"):
        record_application(conn, add(conn, company=name), days=5)
    capped = add(conn, company="Acme Inc.")
    for _ in range(3):
        record_application(conn, add(conn, company="Oldco"), days=40)
    fresh = add(conn, company="Oldco")
    arm_now(root)
    summary = Rig(conn, root).run()
    assert summary["skipped"] == {"already_submitted": 6, "company_cap": 1}
    assert summary["processed"] == [fresh]
    assert capped not in summary["processed"]


# --- skip rules -------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["clicking", "confirmed", "unconfirmed", "weird"])
def test_a_submit_marker_of_any_status_skips_the_posting(conn, root, status):
    marked = add(conn)
    other = add(conn)
    write_marker(root, marked, status)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"submitted_marker": 1}
    assert summary["processed"] == [other]
    assert rig.tailor.calls == [other]


def test_skip_reasons_are_counted_per_posting(conn, root):
    marked, applied, fresh_block, old_block, free = add_many(conn, 5)
    write_marker(root, marked)
    record_application(conn, applied)
    write_blocked(root, {fresh_block: 2, old_block: 8})
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {
        "submitted_marker": 1, "already_submitted": 1, "blocked_recently": 1,
    }
    assert summary["processed"] == [old_block, free]
    assert rig.tailor.calls == [old_block, free]


def test_a_blocked_entry_without_a_readable_time_stays_blocked(conn, root):
    stuck = add(conn)
    free = add(conn)
    root.mkdir(parents=True)
    (root / "blocked.json").write_text(
        json.dumps({str(stuck): {"reason": "earlier", "at": "not a time"}})
    )
    summary = Rig(conn, root).run()
    assert summary["skipped"] == {"blocked_recently": 1}
    assert summary["processed"] == [free]


def test_stale_pool_entries_are_skipped_before_any_work(conn, root, monkeypatch):
    closed = add(conn, status="closed")
    live = add(conn)
    candidate = autoapply_module._Candidate
    pool = [
        candidate(closed, "Firm A", "Financial Analyst", posting_url(conn, closed), "greenhouse"),
        candidate(9999, "Ghost", "Financial Analyst", "https://boards.greenhouse.io/x/jobs/1",
                  "greenhouse"),
        candidate(live, "Firm B", "Financial Analyst", posting_url(conn, live), "workday"),
    ]
    monkeypatch.setattr(autoapply_module, "_candidate_pool", lambda conn, cfg: pool)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"not_active": 2, "ats_not_allowed": 1}
    assert summary["processed"] == []
    assert summary["stop_reason"] == "pool exhausted"
    assert rig.browser.opened == 0
    assert rig.tailor.calls == []


def test_an_empty_pool_never_opens_the_browser(conn, root):
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == []
    assert summary["stop_reason"] == "pool exhausted"
    assert summary["aborted"] is False
    assert rig.browser.opened == 0


@pytest.mark.parametrize(
    ("action", "days_ago", "entity_type", "skipped"),
    [
        ("apply_lite_open", 5, "posting", True),
        ("apply_assist_fill", 29, "posting", True),
        ("apply_lite_open", 31, "posting", False),
        ("apply_assist_scan", 1, "posting", False),
        ("apply_lite_open", 1, "application", False),
    ],
)
def test_hand_worked_postings_are_left_alone(conn, root, action, days_ago, entity_type, skipped):
    worked = add(conn)
    other = add(conn)
    add_activity(conn, worked, action, days_ago, entity_type)
    rig = Rig(conn, root)
    summary = rig.run()
    if skipped:
        assert summary["skipped"] == {"hand_worked": 1}
        assert summary["processed"] == [other]
        assert rig.tailor.calls == [other]
    else:
        assert summary["skipped"] == {}
        assert summary["processed"] == [worked, other]


def test_a_marker_or_application_is_reported_before_hand_worked(conn, root):
    marked, applied = add_many(conn, 2)
    for posting_id in (marked, applied):
        add_activity(conn, posting_id, "apply_lite_open", 1)
    write_marker(root, marked)
    record_application(conn, applied)
    summary = Rig(conn, root).run()
    assert summary["skipped"] == {"submitted_marker": 1, "already_submitted": 1}


# --- handoff ----------------------------------------------------------------------------


def test_an_armed_handoff_run_asks_the_submitter_for_a_handoff_never_a_click(conn, root):
    pid = add(conn, company="Globex", title="Financial Analyst")
    arm_now(root)
    rig = Rig(conn, root, handoff=True)
    summary = rig.run()

    assert statuses(summary) == ["submitted"]
    call = rig.submit.calls[0]
    assert call["live"] is True
    assert call["extra"] == {
        "handoff": True, "handoff_timeout_s": 60 * 60.0,
        "handoff_label": f"#{pid} Globex | Financial Analyst", "handoff_defer": True,
    }


def test_the_handoff_timeout_follows_the_config(conn, root):
    add(conn)
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(handoff_timeout_min=2.5), handoff=True)
    rig.run()
    assert rig.submit.calls[0]["extra"]["handoff_timeout_s"] == 150.0


def test_a_dry_handoff_run_only_tells_the_gate_it_rehearses_a_handoff_and_a_click_run_passes_nothing(
    conn, root
):
    first, second = add_many(conn, 2)
    dry = Rig(conn, root, handoff=True)
    dry.run(limit=1)
    assert dry.submit.calls[0]["live"] is False
    # Drafts are not blockers in a handoff, so the rehearsal's gate must judge it as one;
    # nothing else (no timeout, no label, no deferral) goes with it, and nothing is handed off.
    assert dry.submit.calls[0]["extra"] == {"handoff": True}
    assert dry.await_.stacks == []
    arm_now(root)
    click = Rig(conn, root, handoff=False)
    click.run(limit=1)
    assert click.submit.calls[0]["live"] is True
    assert click.submit.calls[0]["extra"] == {}


# --- essay and cover-letter drafting (handoff mode only) ---------------------------------


def drafts_files(root):
    return sorted(Path(root).glob("runs/*/drafts.md"))


def test_a_handoff_run_asks_the_filler_to_draft_and_type_drafts_and_a_click_run_does_not(conn, root):
    first = add(conn, company="Globex")
    arm_now(root)
    handoff = Rig(conn, root, handoff=True)
    handoff.run()
    kwargs = handoff.assist.calls[0]["kwargs"]
    assert kwargs == {"conn": conn, "tailor": True, "type_drafts": True, "company": "Globex",
                      "autoapply": True, "audit_root": root}  # the cover letter lands in drafts.md

    other = add(conn, company="Initech")
    click = Rig(conn, root, handoff=False)
    click.run()
    kwargs = next(c["kwargs"] for c in click.assist.calls if c["posting_id"] == other)
    assert kwargs == {"conn": conn, "tailor": False, "company": "Initech", "autoapply": True}
    assert "type_drafts" not in kwargs
    assert "audit_root" not in kwargs  # click mode's call is unchanged


def test_drafted_answers_reach_the_handoff_stack_and_the_drafts_file(conn, root):
    pid = add(conn, company="Globex", title="Financial Analyst")
    arm_now(root)
    rig = Rig(conn, root, handoff=True)
    rig.assist.drafts[pid] = [
        ("#q1", "What have you built that you are most proud of?", "I built Example Ventures tools."),
        ("#q2", "Why Globex?", "I want finance work with a real AI angle."),
    ]
    summary = rig.run()

    assert statuses(summary) == ["submitted"]
    assert rig.await_.stacks[0]["drafted"] == [
        ["What have you built that you are most proud of?", "Why Globex?"]
    ]
    (path,) = drafts_files(root)
    text = path.read_text()
    assert f"## #{pid} Globex" in text
    assert "Question: What have you built that you are most proud of?" in text
    assert "I built Example Ventures tools." in text
    assert "Question: Why Globex?" in text
    assert "I want finance work with a real AI angle." in text
    assert text.count("Status: typed into the form") == 2


def test_a_form_with_no_drafts_writes_no_drafts_file_and_passes_no_questions(conn, root):
    add(conn)
    arm_now(root)
    rig = Rig(conn, root, handoff=True)
    rig.run()

    assert drafts_files(root) == []
    assert rig.await_.stacks[0]["drafted"] == [[]]


def test_a_dry_handoff_rehearsal_writes_the_drafts_file_but_hands_nothing_off(conn, root):
    pid = add(conn, company="Globex")
    rig = Rig(conn, root, handoff=True)
    rig.assist.drafts[pid] = [("#q1", "Why Globex?", "Because of the work.")]
    summary = rig.run()

    assert summary["mode"] == "dry"
    assert rig.await_.stacks == []
    assert "Because of the work." in drafts_files(root)[0].read_text()


def test_a_click_run_never_collects_drafts(conn, root):
    pid = add(conn, company="Globex")
    arm_now(root)
    rig = Rig(conn, root, handoff=False)
    rig.assist.drafts[pid] = [("#q1", "Why Globex?", "Because of the work.")]
    rig.run()

    assert drafts_files(root) == []


def test_a_drafts_file_that_cannot_be_written_never_costs_the_posting_its_handoff(
    conn, root, monkeypatch, capsys
):
    from jobhunt.apply_assist import submit as submit_module

    pid = add(conn, company="Globex")
    arm_now(root)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(submit_module, "write_drafts", broken)
    rig = Rig(conn, root, handoff=True)
    rig.assist.drafts[pid] = [("#q1", "Why Globex?", "Because of the work.")]
    summary = rig.run()

    assert statuses(summary) == ["submitted"]
    assert rig.await_.stacks[0]["drafted"] == [["Why Globex?"]]
    assert "could not write drafts.md: OSError: disk full" in capsys.readouterr().out


def test_a_handed_off_posting_goes_to_needs_you_counts_toward_the_caps_and_is_not_reopened(
    conn, root
):
    pid = add(conn)
    arm_now(root)
    rig = Rig(conn, root, handoff=True, submit={pid: ("handed_off", ["no confirmation page seen"])})
    summary = rig.run()

    assert statuses(summary) == ["handed_off"]
    assert reasons(summary) == {pid: "handed to you; no confirmation page seen; no confirmation page seen"}
    assert summary["aborted"] is False
    text = needs_you_text(root)
    assert "- Status: handed_off" in text
    assert f"- What you do: {HANDED_OFF_TODO}" in text
    assert autoapply_module.status_summary(root)["submitted_today"] == 1
    assert autoapply_module.load_blocked(root) == {}
    assert rig.run()["skipped"] == {"submitted_marker": 1}
    assert len(rig.submit.calls) == 1


def test_a_handed_off_posting_uses_a_company_slot(conn, root):
    first, second = add_many(conn, 2, company="Globex")
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(company_cap=1), handoff=True, submit={first: "handed_off"})
    summary = rig.run()
    assert statuses(summary) == ["handed_off"]
    assert summary["skipped"] == {"company_cap": 1}


def test_a_confirmed_handoff_is_a_plain_submission_with_no_todo(conn, root):
    pid = add(conn)
    arm_now(root)
    rig = Rig(conn, root, handoff=True)
    summary = rig.run()
    assert statuses(summary) == ["submitted"]
    assert reasons(summary) == {pid: f"confirmed (autoapply:{pid})"}
    assert not (root / "needs-you.md").exists()


def rig_company(conn, pid):
    return conn.execute("SELECT company FROM postings WHERE id = ?", (pid,)).fetchone()["company"]


def marker_json(root, pid):
    return json.loads((root / "submitted" / f"{pid}.json").read_text())


def test_a_stack_of_three_is_filled_first_then_handed_off_once_and_recorded_after(conn, root):
    first, second, third = add_many(conn, 3, company="Globex")
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(company_cap=3), handoff=True, submit={
        first: "submitted",
        second: ("handed_off", ["page closed before any confirmation page was seen"]),
        third: ("handed_off", ["no confirmation page seen within 20 min of the last activity"]),
    })
    seen = {}
    rig.await_.on_call = lambda: seen.update(
        notes=list(rig.await_.stacks), digest=digest_text(root),
        needs=(root / "needs-you.md").exists(), stopped=rig.browser.handle.stopped,
    )
    summary = rig.run()

    assert rig.await_.stacks[0]["ids"] == [first, second, third]
    assert len(rig.await_.stacks) == 1  # one handoff for the whole stack
    assert rig.await_.stacks[0]["timeout_s"] == 60 * 60
    assert rig.await_.stacks[0]["labels"][0] == f"#{first} Globex | Financial Analyst"
    assert rig.await_.stacks[0]["closed"] == [False, False, False]  # all three tabs still open
    assert [call["extra"]["handoff_defer"] for call in rig.submit.calls] == [True] * 3
    # Nothing was noted or listed before the wait, and the browser was still up.
    assert f"| {first} |" not in seen["digest"] and seen["needs"] is False
    assert seen["stopped"] is False
    assert statuses(summary) == ["submitted", "handed_off", "handed_off"]
    assert summary["processed"] == [first, second, third]
    assert summary["aborted"] is False
    assert reasons(summary)[first] == f"confirmed (autoapply:{first})"
    assert "page closed before any confirmation page was seen" in reasons(summary)[second]
    assert "of the last activity" in reasons(summary)[third]
    assert [m["status"] for m in (marker_json(root, pid) for pid in (first, second, third))] == [
        "confirmed", "handed_off", "handed_off",
    ]
    assert needs_you_headings(root) == [
        f"## #{second} Globex | Financial Analyst", f"## #{third} Globex | Financial Analyst",
    ]
    assert all(page.closed for page in rig.browser.context.pages)  # closed after the wait
    assert rig.browser.handle.stopped is True
    assert autoapply_module.status_summary(root)["submitted_today"] == 3


def test_caps_are_counted_once_when_a_form_is_filled_not_again_after_the_wait(conn, root):
    ids = add_many(conn, 3, company="Globex")
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(company_cap=3, handoff_stack=2), handoff=True)
    summary = rig.run()
    # Counted twice, Globex would sit at 4 >= 3 after the first stack and the third would skip.
    assert summary["processed"] == ids
    assert summary["skipped"] == {}
    assert statuses(summary) == ["submitted"] * 3
    assert [stack["ids"] for stack in rig.await_.stacks] == [ids[:2], ids[2:]]


def test_the_daily_cap_holds_while_the_stack_is_filling(conn, root):
    ids = add_many(conn, 4)
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(daily_cap=2), handoff=True)
    summary = rig.run()
    assert summary["processed"] == ids[:2]
    assert "daily cap reached" in summary["stop_reason"]
    assert [stack["ids"] for stack in rig.await_.stacks] == [ids[:2]]  # flushed at the stop
    assert statuses(summary) == ["submitted", "submitted"]


def test_the_stack_flushes_at_handoff_stack_and_again_when_the_queue_runs_out(conn, root):
    ids = add_many(conn, 5)
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(handoff_stack=2), handoff=True)
    summary = rig.run()
    assert [stack["ids"] for stack in rig.await_.stacks] == [ids[:2], ids[2:4], ids[4:]]
    assert summary["stop_reason"].startswith("pool exhausted")
    assert statuses(summary) == ["submitted"] * 5
    assert all(page.closed for page in rig.browser.context.pages)


def test_the_stack_flushes_when_the_limit_ends_the_loop(conn, root):
    ids = add_many(conn, 5)
    arm_now(root)
    rig = Rig(conn, root, handoff=True)  # handoff_stack defaults to 10
    summary = rig.run(limit=3)
    assert [stack["ids"] for stack in rig.await_.stacks] == [ids[:3]]
    assert "limit reached" in summary["stop_reason"]


def test_a_dry_run_and_the_click_path_close_each_page_and_never_wait(conn, root):
    add_many(conn, 2)
    dry = Rig(conn, root, handoff=True)
    dry.run(limit=2)
    assert dry.await_.stacks == []
    assert all(page.closed for page in dry.browser.context.pages)
    arm_now(root)
    click = Rig(conn, root, handoff=False)
    click.run(limit=1)
    assert click.await_.stacks == []
    assert all(page.closed for page in click.browser.context.pages)
    assert "handoff_defer" not in click.submit.calls[0]["extra"]


def test_a_blocked_or_errored_form_in_handoff_mode_closes_its_page_and_stays_out_of_the_stack(
    conn, root
):
    blocked, errored, good = add_many(conn, 3)
    arm_now(root)
    rig = Rig(conn, root, handoff=True, submit={
        blocked: ("blocked", ["resume missing"]), errored: ("error", ["boom"]),
    })
    summary = rig.run()
    assert statuses(summary) == ["blocked", "error", "submitted"]
    assert [stack["ids"] for stack in rig.await_.stacks] == [[good]]
    assert all(page.closed for page in rig.browser.context.pages)


def test_a_lost_browser_records_every_pending_form_as_handed_off(conn, root):
    first, second, third = add_many(conn, 3)
    arm_now(root)
    rig = Rig(conn, root, handoff=True)
    original = rig.browser.context.new_page
    opened = []

    def dying_new_page():
        if len(opened) == 2:
            raise RuntimeError("Target closed")
        opened.append(original())
        return opened[-1]

    rig.browser.context.new_page = dying_new_page
    summary = rig.run()

    assert rig.await_.stacks == []  # a dead browser is not waited on
    assert summary["aborted"] is True
    assert "browser unavailable" in summary["stop_reason"]
    assert summary["processed"] == [first, second]
    assert statuses(summary) == ["handed_off", "handed_off"]
    for pid in (first, second):
        marker = marker_json(root, pid)
        assert marker["status"] == "handed_off"
        assert "browser lost before the handoff" in marker["reason"]
    headings = needs_you_headings(root)
    assert headings[:2] == [f"## #{first} {rig_company(conn, first)} | Financial Analyst",
                            f"## #{second} {rig_company(conn, second)} | Financial Analyst"]
    text = needs_you_text(root)
    assert "browser lost before the handoff" in text and "Run stopped" in text
    assert all(page.closed for page in opened)
    assert rig.browser.handle.stopped is True
    assert third not in summary["processed"]


def test_a_run_that_crashes_with_forms_pending_still_records_them(conn, root):
    first, second = add_many(conn, 2)
    arm_now(root)
    rig = Rig(conn, root, handoff=True)

    def crash():
        raise RuntimeError("disk on fire")

    rig.on_sleep = crash  # the pause before the second posting
    with pytest.raises(RuntimeError, match="disk on fire"):
        rig.run()
    assert rig.await_.stacks == []
    assert marker_json(root, first)["status"] == "handed_off"
    assert "run crashed before the handoff" in marker_json(root, first)["reason"]
    assert f"## #{first} " in needs_you_text(root)
    assert all(page.closed for page in rig.browser.context.pages)
    assert rig.browser.handle.stopped is True


def test_an_await_that_fails_or_forgets_an_item_still_records_every_form(conn, root):
    first, second = add_many(conn, 2)
    arm_now(root)
    rig = Rig(conn, root, handoff=True)
    rig.await_.override = {second: None}  # no outcome for the second form
    summary = rig.run()
    assert statuses(summary) == ["submitted", "handed_off"]
    assert "no outcome came back" in reasons(summary)[second]
    assert "no outcome came back" in marker_json(root, second)["reason"]

    third, fourth = add_many(conn, 2)
    broken = Rig(conn, root, handoff=True)
    broken.await_.raises = RuntimeError("wait blew up")
    again = broken.run()
    assert statuses(again) == ["handed_off", "handed_off"]
    assert all("the handoff wait failed: RuntimeError: wait blew up" in r for r in reasons(again).values())
    assert again["aborted"] is False
    assert all(page.closed for page in broken.browser.context.pages)


def test_a_kill_file_does_not_stop_a_handoff_run_and_the_run_says_so(conn, root, capsys):
    first, second = add_many(conn, 2)
    arm_now(root)
    (root / "KILL").write_text("")
    rig = Rig(conn, root, handoff=True)
    summary = rig.run()
    assert summary["processed"] == [first, second]
    assert summary["stop_reason"] != KILL_MESSAGE
    out = capsys.readouterr().out
    assert "KILL file present but ignored: this is a handoff run" in out


def test_a_kill_file_still_stops_a_click_path_run(conn, root, capsys):
    add_many(conn, 2)
    arm_now(root)
    (root / "KILL").write_text("")
    rig = Rig(conn, root, handoff=False)
    summary = rig.run()
    assert summary["processed"] == []
    assert summary["stop_reason"] == KILL_MESSAGE
    assert "ignored" not in capsys.readouterr().out


def test_format_status_says_what_kill_means_in_each_mode(root):
    root.mkdir(parents=True)
    (root / "KILL").touch()
    handoff = autoapply_module.format_status(autoapply_module.status_summary(root))
    assert "KILL file: PRESENT, but ignored: handoff runs never click Submit" in handoff
    click = autoapply_module.format_status(
        autoapply_module.status_summary(root, cfg=AutoApplyConfig(handoff=False))
    )
    assert "KILL file: PRESENT, runs stop before their next posting" in click


# --- KILL file --------------------------------------------------------------------------


def test_a_kill_file_stops_a_run_before_it_starts(conn, root):
    add_many(conn, 3)
    root.mkdir(parents=True)
    (root / "KILL").write_text("")
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == []
    assert summary["stop_reason"] == KILL_MESSAGE
    assert summary["aborted"] is False
    assert rig.tailor.calls == []
    assert rig.browser.opened == 0


def test_kill_during_a_posting_stops_before_the_next_one(conn, root):
    first, _second, _third = add_many(conn, 3)
    rig = Rig(conn, root)
    rig.assist.hook = lambda posting_id: (root / "KILL").write_text("")
    summary = rig.run()
    assert summary["processed"] == [first]
    assert summary["stop_reason"] == KILL_MESSAGE
    assert rig.tailor.calls == [first]


def test_kill_during_the_pause_stops_before_the_attempt(conn, root):
    first, second, _third = add_many(conn, 3)
    rig = Rig(conn, root)
    rig.on_sleep = lambda: (root / "KILL").write_text("")
    summary = rig.run()
    assert summary["processed"] == [first]
    assert rig.tailor.calls == [first, second]
    assert summary["stop_reason"] == KILL_MESSAGE


# --- tailor handling --------------------------------------------------------------------


def test_every_tailor_outcome_is_handled_before_the_form(conn, root):
    dropped, failed, raised, no_pdf, weird, good = add_many(conn, 6)
    plan = {
        dropped: "dropped", failed: "failed", raised: RuntimeError("claude CLI is logged out"),
        no_pdf: "no_pdf", weird: "weird",
    }
    rig = Rig(conn, root, tailor=plan)
    summary = rig.run(limit=1)

    assert summary["processed"] == [good]
    assert counts_of(summary) == {"tailor_dropped": 1, "tailor_failed": 4, "dry_run_ok": 1}
    assert rig.sleeps == []
    assert summary["stop_reason"] == "limit reached (1 postings)"
    assert reasons(summary) == {
        dropped: "match 61 below 70",
        failed: "model returned no JSON",
        raised: "RuntimeError: claude CLI is logged out",
        no_pdf: "tailor reported success but data/apply_packs/missing/nothing.pdf is missing",
        weird: "tailor returned status 'queued'",
        good: "gate passed; nothing clicked (dry run)",
    }
    blocked = set(autoapply_module.load_blocked(root))
    assert blocked == {str(failed), str(raised), str(no_pdf), str(weird)}


def test_a_tailored_result_without_a_path_uses_the_conventional_one(conn, root):
    pid = add(conn)
    rig = Rig(conn, root, tailor={pid: "tailored_no_path"})
    rig.run()
    expected = Path("data/apply_packs/resume/tailored") / str(pid) / "Alex_Rivera_Resume.pdf"
    assert Path(rig.assist.calls[0]["resume"]) == expected.resolve()


def test_an_already_tailored_posting_is_not_tailored_again(conn, root):
    done = add(conn)
    make_tailored(conn, done)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == [done]
    assert summary["tailor_calls"] == 0
    assert rig.tailor.calls == []


def test_a_pdf_without_a_match_report_is_tailored(conn, root):
    pid = add(conn)
    write_pdf(pid)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == [pid]
    assert rig.tailor.calls == [pid]


def test_five_failures_in_a_row_abort_the_run(conn, root):
    ids = add_many(conn, 7)
    rig = Rig(conn, root, tailor={pid: "failed" for pid in ids})
    summary = rig.run()
    assert summary["aborted"] is True
    assert summary["stop_reason"] == "5 failures in a row, last: model returned no JSON"
    assert summary["tailor_calls"] == 5
    assert summary["processed"] == []
    assert rig.browser.opened == 1
    assert rig.browser.handle.stopped
    assert set(autoapply_module.load_blocked(root)) == {str(pid) for pid in ids[:5]}


def test_a_good_posting_resets_the_failure_count(conn, root):
    first_fail, first_good, second_fail, second_good = add_many(conn, 4)
    cfg = AutoApplyConfig(max_consecutive_failures=2)
    plan = {first_fail: "failed", second_fail: "failed"}
    summary = Rig(conn, root, cfg, tailor=plan).run()
    assert summary["aborted"] is False
    assert summary["processed"] == [first_good, second_good]
    assert summary["stop_reason"] == "pool exhausted"


def test_submit_errors_feed_the_failure_breaker(conn, root):
    ids = add_many(conn, 5)
    plan = {pid: RuntimeError("boom") for pid in ids[:3]}
    summary = Rig(conn, root, AutoApplyConfig(max_consecutive_failures=3), submit=plan).run()
    assert summary["aborted"] is True
    assert summary["stop_reason"] == "3 failures in a row, last: RuntimeError: boom"
    assert summary["processed"] == ids[:3]


def test_the_tailor_cap_bounds_model_calls_per_run(conn, root):
    ids = add_many(conn, 6)
    cfg = AutoApplyConfig(max_tailors_per_run=3)
    rig = Rig(conn, root, cfg, tailor={pid: "dropped" for pid in ids})
    summary = rig.run()
    assert rig.tailor.calls == ids[:3]
    assert summary["tailor_calls"] == 3
    assert summary["stop_reason"] == "tailor cap reached (3 tailor calls)"
    assert summary["aborted"] is False
    assert counts_of(summary) == {"tailor_dropped": 3}


def test_the_tailor_cap_defaults_to_the_daily_cap(conn, root):
    ids = add_many(conn, 5)
    cfg = AutoApplyConfig(daily_cap=2, dry_run_quota=50, dry_run_per_ats=50)
    rig = Rig(conn, root, cfg, tailor={pid: "dropped" for pid in ids})
    summary = rig.run()
    assert rig.tailor.calls == ids[:2]
    assert summary["stop_reason"] == "tailor cap reached (2 tailor calls)"


def test_already_tailored_postings_go_past_the_tailor_cap(conn, root):
    fresh_one, fresh_two = add_many(conn, 2)
    ready_one, ready_two = add_many(conn, 2)
    make_tailored(conn, ready_one, "2026-10-20T00:00:00")
    make_tailored(conn, ready_two, "2026-10-20T00:00:01")
    rig = Rig(conn, root, AutoApplyConfig(max_tailors_per_run=1))
    summary = rig.run()
    assert summary["processed"] == [ready_one, ready_two, fresh_one]
    assert rig.tailor.calls == [fresh_one]
    assert summary["stop_reason"] == "tailor cap reached (1 tailor calls)"
    assert fresh_two not in summary["processed"]


# --- the seven-day block list -------------------------------------------------------------


def test_a_failed_tailor_is_blocked_for_seven_days(conn, root):
    pid = add(conn)
    rig = Rig(conn, root, tailor={pid: "failed"})
    first = rig.run()
    assert reasons(first) == {pid: "model returned no JSON"}
    assert autoapply_module.load_blocked(root)[str(pid)]["reason"] == "model returned no JSON"

    second = rig.run()
    assert second["skipped"] == {"blocked_recently": 1}
    assert rig.tailor.calls == [pid]  # not asked again

    write_blocked(root, {pid: 8})
    rig.tailor.plan[pid] = "tailored"
    third = rig.run()
    assert third["processed"] == [pid]
    assert rig.tailor.calls == [pid, pid]


def test_a_tailor_exception_is_blocked_like_a_failure(conn, root):
    pid = add(conn)
    rig = Rig(conn, root, tailor={pid: RuntimeError("claude CLI is logged out")})
    summary = rig.run()
    assert summary["aborted"] is False
    blocked = autoapply_module.load_blocked(root)
    assert blocked[str(pid)]["reason"] == "RuntimeError: claude CLI is logged out"


def test_a_dropped_posting_is_not_blocked_and_not_retried(conn, root):
    pid = add(conn)
    rig = Rig(conn, root, tailor={pid: "dropped"})
    rig.run()
    assert autoapply_module.load_blocked(root) == {}
    second = rig.run()
    assert rig.tailor.calls == [pid]
    assert second["skipped"] == {}
    assert second["stop_reason"] == "pool exhausted"


def test_an_error_without_a_marker_is_blocked_and_the_run_goes_on(conn, root):
    bad, good = add_many(conn, 2)
    rig = Rig(conn, root, assist_fail={bad: TimeoutError("form never rendered")})
    summary = rig.run()
    assert statuses(summary) == ["error", "dry_run_ok"]
    assert reasons(summary)[bad] == "TimeoutError: form never rendered"
    blocked = autoapply_module.load_blocked(root)
    assert blocked[str(bad)]["reason"] == "TimeoutError: form never rendered"
    assert not (root / "needs-you.md").exists()
    assert all(page.closed for page in rig.browser.context.pages)
    second = rig.run()
    assert second["skipped"] == {"blocked_recently": 1, "already_dry_run": 1}


def test_an_http_error_page_is_an_error_not_a_dry_run(conn, root):
    pid = add(conn)
    browser = FakeBrowser(codes=[404])
    rig = Rig(conn, root, browser=browser)
    summary = rig.run()
    assert statuses(summary) == ["error"]
    assert reasons(summary) == {pid: "HTTP 404 on the application page; the posting may be closed"}
    assert rig.assist.calls == []
    assert str(pid) in autoapply_module.load_blocked(root)
    assert autoapply_module.load_state(root)["dry_runs"] == []
    assert browser.context.pages[0].closed


def test_an_error_after_the_click_goes_to_needs_you_not_the_block_list(conn, root):
    pid = add(conn)
    arm_now(root)
    rig = Rig(conn, root, submit={pid: "marker_then_error"})
    summary = rig.run()
    assert statuses(summary) == ["error"]
    text = needs_you_text(root)
    assert f"## #{pid} " in text
    assert "- Status: error" in text
    assert "A submit marker exists" in text
    assert autoapply_module.load_blocked(root) == {}
    second = rig.run()
    assert second["skipped"] == {"submitted_marker": 1}


def test_an_unknown_submit_status_is_an_error(conn, root):
    pid = add(conn)
    summary = Rig(conn, root, submit={pid: "weird"}).run()
    assert statuses(summary) == ["error"]
    assert reasons(summary)[pid] == "unexpected submit status 'weird'"


# --- the spend gate ---------------------------------------------------------------------------


def test_a_spend_gate_refusal_stops_the_run_and_blocks_nothing(conn, root):
    first, second, third = add_many(conn, 3)
    gate = SpendGateError("OPENROUTER_API_KEY needs SPEND_OK=1")
    rig = Rig(conn, root, tailor={second: gate})
    summary = rig.run()

    assert summary["aborted"] is True
    assert summary["stop_reason"] == (
        "LLM unavailable or refused (OPENROUTER_API_KEY needs SPEND_OK=1); run stopped, "
        f"posting #{second} not blocked"
    )
    assert summary["processed"] == [first]
    assert summary["counts"]["tailor_failed"] == 0
    assert autoapply_module.load_blocked(root) == {}
    assert rig.tailor.calls == [first, second]  # never reached the third
    assert third not in rig.tailor.calls
    assert summary["tailor_calls"] == 2
    assert rig.browser.handle.stopped
    assert summary["stop_reason"] in digest_text(root)


# --- a run that stops on a failure --------------------------------------------------------------
# Under launchd nobody reads the log: needs-you.md is what the candidate checks. Every abnormal stop
# leaves one `Run stopped` entry there, and no normal stop does.


def signed_out_cli():
    from jobhunt.connectors.claude_cli import SubscriptionUnavailableError

    return SubscriptionUnavailableError(
        "claude CLI cannot use the subscription: OAuth session expired (sign in with "
        "`claude auth login --claudeai` or `claude setup-token`, or wait for the limit to reset)"
    )


def spend_gate():
    return SpendGateError("OPENROUTER_API_KEY needs SPEND_OK=1")


@pytest.mark.parametrize(
    "make_error",
    [pytest.param(signed_out_cli, id="claude-cli-signed-out"),
     pytest.param(spend_gate, id="spend-gate")],
)
def test_an_unavailable_model_stops_the_run_with_one_run_entry(conn, root, make_error):
    first, second, _third = add_many(conn, 3)
    error = make_error()
    rig = Rig(conn, root, tailor={second: error})
    summary = rig.run()

    reason = f"LLM unavailable or refused ({error}); run stopped, posting #{second} not blocked"
    assert summary["aborted"] is True
    assert summary["stop_reason"] == reason
    assert "spend gate" not in reason  # a signed-out CLI is not a spend-gate refusal
    assert summary["processed"] == [first]
    assert rig.tailor.calls == [first, second]  # never reached the third
    assert counts_of(summary) == {"dry_run_ok": 1}
    assert autoapply_module.load_blocked(root) == {}  # the posting did nothing wrong
    assert_run_entry(root, reason)
    assert len(needs_you_headings(root)) == 1  # no posting entry beside it
    assert needs_you_text(root).startswith("# Needs you (autoapply)\n\n")  # the file header too


def stop_by_pool_exhausted(conn, root):
    add_many(conn, 2)
    return Rig(conn, root).run(), "pool exhausted"


def stop_after_a_failed_posting(conn, root):
    first, _second = add_many(conn, 2)
    return Rig(conn, root, tailor={first: RuntimeError("boom")}).run(), "pool exhausted"


def stop_by_limit(conn, root):
    add_many(conn, 3)
    return Rig(conn, root).run(limit=1), "limit reached (1 postings)"


def stop_by_daily_cap(conn, root):
    add_many(conn, 3)
    arm_now(root)
    summary = Rig(conn, root, AutoApplyConfig(daily_cap=1)).run()
    return summary, "daily cap reached (1 submissions today)"


def stop_by_tailor_cap(conn, root):
    ids = add_many(conn, 3)
    cfg = AutoApplyConfig(max_tailors_per_run=1)
    rig = Rig(conn, root, cfg, tailor={pid: "dropped" for pid in ids})
    return rig.run(), "tailor cap reached (1 tailor calls)"


def stop_by_kill(conn, root):
    add_many(conn, 2)
    root.mkdir(parents=True)
    (root / "KILL").write_text("")
    return Rig(conn, root).run(), KILL_MESSAGE


def stop_by_dry_quota(conn, root):
    add_many(conn, 3)
    summary = Rig(conn, root, AutoApplyConfig(dry_run_quota=2)).run()
    return summary, autoapply_module.DRY_DONE_REASON


def stop_by_arm_change(conn, root):
    add_many(conn, 2)
    rig = Rig(conn, root)
    rig.assist.hook = lambda posting_id: arm_now(root)
    return rig.run(), "armed state changed during the run"


NORMAL_STOPS = [
    stop_by_pool_exhausted, stop_after_a_failed_posting, stop_by_limit, stop_by_daily_cap,
    stop_by_tailor_cap, stop_by_kill, stop_by_dry_quota, stop_by_arm_change,
]


@pytest.mark.parametrize("scenario", NORMAL_STOPS, ids=lambda scenario: scenario.__name__)
def test_a_normal_stop_leaves_no_run_entry(conn, root, scenario):
    summary, reason = scenario(conn, root)
    assert summary["stop_reason"] == reason
    assert summary["aborted"] is False
    assert not (root / "needs-you.md").exists()


def test_a_crash_leaves_one_run_entry_and_still_raises(conn, root):
    add_many(conn, 3)

    def boom(seconds):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        Rig(conn, root).run(sleep_fn=boom)
    assert_run_entry(root, "crashed: RuntimeError: boom")


def test_a_crash_before_the_first_posting_leaves_a_run_entry(conn, root, monkeypatch):
    add_many(conn, 2)

    def locked(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(autoapply_module, "_submissions", locked)
    rig = Rig(conn, root)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        rig.run()
    assert_run_entry(root, "crashed: OperationalError: database is locked")
    digest = digest_text(root)
    assert len([line for line in digest.splitlines() if line.startswith("## Run ")]) == 1
    assert "Stopped: crashed: OperationalError: database is locked" in digest
    assert rig.tailor.calls == []
    assert rig.browser.opened == 0


def test_the_failures_halt_leaves_a_run_entry_beside_the_blocks(conn, root):
    ids = add_many(conn, 7)
    summary = Rig(conn, root, tailor={pid: "failed" for pid in ids}).run()
    assert summary["aborted"] is True
    assert_run_entry(root, "5 failures in a row, last: model returned no JSON")
    # This stop does block postings, so the entry makes no claim that nothing was blocked.
    assert set(autoapply_module.load_blocked(root)) == {str(pid) for pid in ids[:5]}
    assert "not blocked" not in needs_you_text(root)


@pytest.mark.parametrize(
    "browser_kwargs, reason",
    [
        pytest.param({"open_error": RuntimeError("no display")},
                     "browser could not be opened: no display", id="will-not-open"),
        pytest.param({"new_page_error": RuntimeError("target closed")},
                     "browser unavailable: target closed", id="dies-mid-run"),
    ],
)
def test_a_browser_failure_leaves_a_run_entry(conn, root, browser_kwargs, reason):
    add_many(conn, 2)
    summary = Rig(conn, root, browser=FakeBrowser(**browser_kwargs)).run()
    assert summary["aborted"] is True
    assert summary["stop_reason"] == reason
    assert_run_entry(root, reason)


def test_a_click_in_dry_mode_leaves_the_posting_entry_and_a_run_entry(conn, root):
    first, _second = add_many(conn, 2)
    Rig(conn, root, submit={first: "submitted"}).run()
    assert_run_entry(root, "submitted reported in dry mode; check apply_assist/submit.py")
    headings = needs_you_headings(root)
    assert len(headings) == 2
    assert headings[0].startswith(f"## #{first} ")
    assert headings[1].startswith("## Run stopped ")


def test_each_stopped_run_adds_an_entry_under_one_file_header(conn, root):
    add_many(conn, 2)
    for _ in range(2):
        browser = FakeBrowser(open_error=RuntimeError("no display"))
        Rig(conn, root, browser=browser).run()
    assert needs_you_text(root).count("# Needs you (autoapply)") == 1
    reason = "- Reason: browser could not be opened: no display"
    assert [entry[1] for entry in run_entries(root)] == [reason, reason]
    assert autoapply_module.status_summary(root)["needs_you"] == 2


def test_a_multi_line_reason_stays_on_one_line_and_cannot_forge_an_entry(conn, root):
    first, _second = add_many(conn, 2)
    error = SpendGateError("gate said no\n## #99 Fake | entry")
    Rig(conn, root, tailor={first: error}).run()
    assert_run_entry(
        root,
        "LLM unavailable or refused (gate said no ## #99 Fake | entry); run stopped, "
        f"posting #{first} not blocked",
    )
    assert len(needs_you_headings(root)) == 1


# --- pacing ---------------------------------------------------------------------------------------


def test_the_pause_comes_between_postings_only(conn, root):
    add_many(conn, 3)
    rig = Rig(conn, root, AutoApplyConfig(pause_s=7.5, handoff_pause_s=1.0))  # click path
    rig.run()
    assert rig.sleeps == [7.5, 7.5]


def test_a_handoff_run_sleeps_the_short_handoff_pause(conn, root):
    add_many(conn, 3)
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(handoff_pause_s=2.5), handoff=True)
    rig.run()
    assert rig.sleeps == [2.5, 2.5]


def test_a_single_posting_run_never_pauses(conn, root):
    add_many(conn, 3)
    rig = Rig(conn, root)
    summary = rig.run(limit=1)
    assert rig.sleeps == []
    assert summary["stop_reason"] == "limit reached (1 postings)"


# --- the browser ------------------------------------------------------------------------------


def test_one_browser_serves_the_whole_run_and_every_page_is_closed(conn, root):
    gh, lever, ashby = (add(conn, ats=ats) for ats in ("greenhouse", "lever", "ashby"))
    browser = FakeBrowser()
    rig = Rig(conn, root, browser=browser)
    summary = rig.run()

    assert sorted(summary["processed"]) == sorted([gh, lever, ashby])
    assert browser.opened == 1
    assert len(browser.context.pages) == 3
    assert all(page.closed for page in browser.context.pages)

    base = {pid: posting_url(conn, pid) for pid in (gh, lever, ashby)}
    gotos = {call["posting_id"]: call["page"].goto_calls for call in rig.assist.calls}
    # Lever and Ashby open the application form itself; Greenhouse's posting URL already is it.
    assert [url for url, _ in gotos[gh]] == [base[gh]]
    assert [url for url, _ in gotos[lever]] == [base[lever] + "/apply"]
    assert [url for url, _ in gotos[ashby]] == [base[ashby] + "/application"]
    expected = {"wait_until": "domcontentloaded", "timeout": autoapply_module.GOTO_TIMEOUT_MS}
    assert all(kwargs == expected for calls in gotos.values() for _, kwargs in calls)
    # The filler is given the posting URL as stored, and builds its own form URL.
    assert {call["posting_id"]: call["url"] for call in rig.assist.calls} == base


def test_a_persistent_profile_is_closed_by_closing_its_context(conn, root):
    add(conn)
    browser = FakeBrowser(cdp=False)
    Rig(conn, root, browser=browser).run()
    assert browser.context.closed is True
    assert browser.handle.stopped is True
    assert browser.context.browser.closed is False


def test_a_cdp_session_only_disconnects_and_never_closes_the_context(conn, root):
    add(conn)
    browser = FakeBrowser(cdp=True)
    Rig(conn, root, browser=browser).run()
    assert browser.context.closed is False  # the candidate's own Chrome windows stay open
    assert browser.context.browser.closed is True  # the CDP disconnect
    assert browser.handle.stopped is True


def test_a_browser_that_will_not_open_aborts_before_any_tailoring(conn, root):
    add_many(conn, 2)
    rig = Rig(conn, root, browser=FakeBrowser(open_error=RuntimeError("no display")))
    summary = rig.run()
    assert summary["aborted"] is True
    assert summary["stop_reason"] == "browser could not be opened: no display"
    assert summary["processed"] == []
    assert rig.tailor.calls == []
    assert summary["tailor_calls"] == 0
    assert "browser could not be opened: no display" in digest_text(root)


def test_a_browser_that_dies_mid_run_aborts_and_still_stops_playwright(conn, root):
    first, _second = add_many(conn, 2)
    browser = FakeBrowser(new_page_error=RuntimeError("target closed"))
    rig = Rig(conn, root, browser=browser)
    summary = rig.run()
    assert summary["aborted"] is True
    assert summary["stop_reason"] == "browser unavailable: target closed"
    assert summary["processed"] == []
    assert rig.tailor.calls == [first]
    assert browser.handle.stopped is True
    assert autoapply_module.load_blocked(root) == {}  # the posting did nothing wrong


def test_a_crash_reaches_the_digest_and_still_releases_the_browser_and_the_lock(conn, root):
    add_many(conn, 3)
    browser = FakeBrowser()
    rig = Rig(conn, root, browser=browser)

    def boom(seconds):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        rig.run(sleep_fn=boom)
    assert browser.handle.stopped is True
    assert "Stopped: crashed: RuntimeError: boom" in digest_text(root)
    # the apply-session lock came back with it
    assert Rig(conn, root).run(limit=1)["processed"]


def test_a_failing_browser_close_is_reported_and_swallowed(conn, root, capsys):
    add(conn)
    browser = FakeBrowser(close_error=RuntimeError("already closed"))
    summary = Rig(conn, root, browser=browser).run()
    assert summary["stop_reason"] == "pool exhausted"
    assert summary["aborted"] is False
    assert browser.handle.stopped is True
    assert "autoapply: browser close failed: already closed" in capsys.readouterr().out


# --- the files a run leaves behind ---------------------------------------------------------------


def test_a_blocked_posting_reaches_every_file_you_read(conn, root):
    pid = add(conn, company="Globex")
    reason = "required_consent_checkbox; salary_required"
    rig = Rig(conn, root, submit={pid: ("blocked", ["required_consent_checkbox", "salary_required"])})
    summary = rig.run()

    assert statuses(summary) == ["blocked"]
    assert reasons(summary) == {pid: reason}
    assert autoapply_module.load_blocked(root)[str(pid)]["reason"] == reason
    [entry] = dry_runs_of(root)
    assert (entry["posting_id"], entry["status"], entry["ats"]) == (pid, "blocked", "greenhouse")
    text = needs_you_text(root)
    assert text.startswith("# Needs you (autoapply)")
    assert f"## #{pid} Globex | Financial Analyst" in text
    assert "- Status: blocked" in text
    assert f"- URL: {posting_url(conn, pid)}" in text
    assert f"- Reason: {reason}" in text
    assert "Apply by hand at the URL" in text
    row = f"| {pid} | Globex | Financial Analyst | greenhouse | blocked | {reason} |"
    assert row in digest_text(root)


def test_an_unconfirmed_click_is_counted_reported_and_never_retried(conn, root):
    pid = add(conn)
    arm_now(root)
    rig = Rig(conn, root, submit={pid: "unconfirmed"})
    summary = rig.run()

    assert statuses(summary) == ["unconfirmed"]
    assert reasons(summary) == {pid: "submit click sent, no success page seen"}
    assert "A submit click went out but no success page was seen" in needs_you_text(root)
    assert autoapply_module.status_summary(root)["submitted_today"] == 1
    assert autoapply_module.load_blocked(root) == {}
    assert rig.run()["skipped"] == {"submitted_marker": 1}


NOT_SUBMITTED_TODO = (
    "Submit was clicked, but the site put a human check after the click (see the reason), so the "
    "application was NOT sent. Apply by hand at the URL. Auto-apply will not retry it."
)
HANDED_OFF_TODO = (
    "Auto-apply filled this form and handed it to you, but no confirmation page was seen. If you "
    "sent it, `uv run python -m jobhunt confirm-scan` records it from the confirmation email; if "
    "not, apply by hand at the URL. Auto-apply will not reopen it."
)


def test_a_post_click_human_check_is_not_submitted_uncounted_and_never_retried(conn, root):
    pid = add(conn)
    arm_now(root)
    rig = Rig(conn, root, submit={pid: ("not_submitted", ["human check after the click"])})
    summary = rig.run()

    assert statuses(summary) == ["not_submitted"]
    assert reasons(summary) == {
        pid: "submit clicked, but a human check stood after the click; nothing was sent; "
             "human check after the click"
    }
    text = needs_you_text(root)
    assert "- Status: not_submitted" in text
    assert f"- What you do: {NOT_SUBMITTED_TODO}" in text
    assert summary["aborted"] is False
    # Not a submission: not in submitted-today, no company slot used, not blocked for 7 days ...
    assert autoapply_module.status_summary(root)["submitted_today"] == 0
    assert autoapply_module.status_summary(root, conn=conn)["submitted_today"] == 0
    assert autoapply_module.load_blocked(root) == {}
    # ... but the marker still keeps the lane from reopening (and re-clicking) the posting.
    assert rig.run()["skipped"] == {"submitted_marker": 1}
    assert len(rig.submit.calls) == 1
    assert "not_submitted 1" in autoapply_module.format_summary(summary, root)


def test_a_not_submitted_click_takes_no_company_or_daily_slot(conn, root):
    first = add(conn, company="Globex", title="Financial Analyst")
    second = add(conn, company="Globex", title="Finance Data Analyst")
    arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(company_cap=1, daily_cap=1, max_tailors_per_run=5),
              submit={first: "not_submitted"})
    summary = rig.run()
    assert statuses(summary) == ["not_submitted", "submitted"]
    assert [call["posting_id"] for call in rig.submit.calls] == [first, second]


def test_a_not_submitted_click_in_a_dry_run_stops_the_run(conn, root):
    pid = add(conn)
    rig = Rig(conn, root, submit={pid: "not_submitted"})
    summary = rig.run()
    assert summary["aborted"] is True
    assert "reported in dry mode" in summary["stop_reason"]


def test_a_confirmed_submission_with_an_incomplete_record_goes_to_needs_you(conn, root):
    pid = add(conn)
    arm_now(root)
    rig = Rig(conn, root, submit={pid: ("submitted", ["db_row_missing"])})
    summary = rig.run()

    assert statuses(summary) == ["submitted"]
    assert reasons(summary) == {pid: "submitted; record incomplete: db_row_missing"}
    assert summary["aborted"] is False
    text = needs_you_text(root)
    assert "The site confirmed the application, but the database record is incomplete" in text
    assert autoapply_module.status_summary(root)["submitted_today"] == 1


def test_a_click_reported_in_dry_mode_stops_the_run_and_says_so(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: "submitted"})
    summary = rig.run()

    assert summary["aborted"] is True
    assert summary["stop_reason"] == "submitted reported in dry mode; check apply_assist/submit.py"
    assert summary["processed"] == [first]
    assert second not in rig.tailor.calls
    assert "A click went out during a DRY run" in needs_you_text(root)
    assert rig.browser.handle.stopped is True


# --- the digest ---------------------------------------------------------------------------------


def test_the_digest_appends_one_section_per_run_under_one_heading(conn, root):
    add_many(conn, 2)
    rig = Rig(conn, root)
    rig.run(limit=1)
    rig.run(limit=1)

    text = digest_text(root)
    assert text.count("# Autoapply digest") == 1
    runs = [line for line in text.splitlines() if line.startswith("## Run ")]
    assert len(runs) == 2
    assert all(line.endswith("(dry)") for line in runs)
    assert text.count("| id | company | title | ATS | status | reason |") == 2
    assert text.count("Stopped: limit reached (1 postings). Attempted 1. Tailor calls 1.") == 2
    # The second run found the first run's posting already dry-run.
    assert text.count("Skipped: already_dry_run 1.") == 1


# --- the apply-session lock --------------------------------------------------------------------


def test_a_second_apply_session_is_refused_before_anything_happens(conn, root):
    add(conn)
    rig = Rig(conn, root)
    with applylock.held():
        with pytest.raises(applylock.ApplySessionActiveError):
            rig.run()
    assert rig.browser.opened == 0
    assert rig.tailor.calls == []
    assert not root.exists()
    assert rig.run()["processed"]  # once the other session is gone, the run goes ahead


# --- arming while a run is in flight ---------------------------------------------------------------


def test_arming_during_a_dry_run_stops_it_before_the_next_posting(conn, root):
    first, _second = add_many(conn, 2)
    rig = Rig(conn, root)
    rig.assist.hook = lambda pid: arm_now(root)
    summary = rig.run()

    assert summary["mode"] == "dry"
    assert summary["stop_reason"] == "armed state changed during the run"
    assert summary["aborted"] is False
    assert summary["processed"] == [first]
    assert autoapply_module.load_state(root)["armed_at"] is not None
    assert len(dry_runs_of(root)) == 1  # the in-flight dry attempt is still recorded


def test_disarming_during_a_live_run_stops_it_before_the_next_posting(conn, root):
    first, _second = add_many(conn, 2)
    arm_now(root)
    rig = Rig(conn, root)
    rig.assist.hook = lambda pid: autoapply_module.disarm(root)
    summary = rig.run()

    assert summary["mode"] == "live"
    assert summary["stop_reason"] == "armed state changed during the run"
    assert summary["aborted"] is False
    assert summary["processed"] == [first]
    assert autoapply_module.load_state(root)["armed_at"] is None


# --- arm, disarm and the state file ---------------------------------------------------------------


def quota_runs(count=10):
    """`count` recorded dry runs. The ids need not exist: arm and status read the file only."""
    return [dry_entry(pid) for pid in range(1, count + 1)]


def test_arm_refuses_until_the_dry_quota_is_met(root):
    runs = [
        dry_entry(1), dry_entry(2), dry_entry(3), dry_entry(4),
        dry_entry(5, "blocked"), dry_entry(6, "blocked"), dry_entry(7, "blocked"),
        dry_entry(8, "error"), dry_entry(9, "submitted"), "junk",
    ]
    write_state(root, runs)
    before = (root / "state.json").read_text()
    with pytest.raises(autoapply_module.ArmError, match="only 7 of 10 dry runs recorded"):
        autoapply_module.arm(root, approved="reviewed the digest", cfg=AutoApplyConfig())
    assert (root / "state.json").read_text() == before
    assert autoapply_module.load_state(root)["armed_at"] is None


def test_arm_refuses_a_blank_approval_even_when_the_quota_is_met(root):
    write_state(root, quota_runs())
    with pytest.raises(autoapply_module.ArmError, match="--approved needs your approval text"):
        autoapply_module.arm(root, approved="   ", cfg=AutoApplyConfig())
    assert autoapply_module.load_state(root)["armed_at"] is None


def test_arm_records_the_time_and_the_note_and_keeps_the_dry_runs(root):
    write_state(root, quota_runs())
    state = autoapply_module.arm(root, approved="  reviewed the digest  ", cfg=AutoApplyConfig())
    assert datetime.fromisoformat(state["armed_at"])
    assert state["armed_note"] == "reviewed the digest"
    saved = autoapply_module.load_state(root)
    assert saved["armed_at"] == state["armed_at"]
    assert saved["armed_note"] == "reviewed the digest"
    assert len(saved["dry_runs"]) == 10


def test_disarm_clears_the_time_keeps_the_history_and_the_next_run_is_dry(conn, root):
    add(conn)
    write_state(root, [dry_entry(900)], armed_at=iso(), armed_note="go")
    state = autoapply_module.disarm(root)
    assert state["armed_at"] is None
    assert [entry["posting_id"] for entry in autoapply_module.load_state(root)["dry_runs"]] == [900]

    rig = Rig(conn, root)
    summary = rig.run(limit=1)
    assert summary["mode"] == "dry"
    assert [call["live"] for call in rig.submit.calls] == [False]


def test_a_missing_state_file_reads_as_disarmed_with_no_dry_runs(root):
    assert autoapply_module.load_state(root) == {"dry_runs": [], "armed_at": None, "armed_note": ""}


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        '["x"]',
        '{"armed_at": "yes"}',
        '{"armed_at": true}',
        '{"armed_at": 1761200000}',
    ],
)
def test_a_state_file_that_is_not_a_real_arm_record_reads_as_disarmed(conn, root, text):
    add(conn)
    root.mkdir(parents=True)
    (root / "state.json").write_text(text)
    assert autoapply_module.load_state(root)["armed_at"] is None
    assert autoapply_module.status_summary(root)["armed"] is False

    rig = Rig(conn, root)
    summary = rig.run(limit=1)
    assert summary["mode"] == "dry"
    assert [call["live"] for call in rig.submit.calls] == [False]


def test_status_counts_dry_runs_by_ats_and_finds_a_missing_ats_in_the_db(conn, root):
    lever = add(conn, ats="lever")
    write_state(root, [
        dry_entry(1, ats="greenhouse"),
        dry_entry(2, "blocked", ats="greenhouse"),
        dry_entry(3, ats="ashby"),
        {"posting_id": lever, "at": iso(), "status": "dry_run_ok"},  # no ats recorded
        dry_entry(4, "error", ats="ashby"),  # errors never count
    ])
    with_db = autoapply_module.status_summary(root, conn=conn)
    assert with_db["dry_runs"] == 4
    assert with_db["dry_runs_by_ats"] == {"greenhouse": 2, "lever": 1, "ashby": 1}
    without_db = autoapply_module.status_summary(root)
    assert without_db["dry_runs_by_ats"] == {"greenhouse": 2, "lever": 0, "ashby": 1, "unknown": 1}


def test_status_counts_only_todays_autoapply_submissions(conn, root):
    marker_pid, auto_pid, manual_pid, old_pid = add_many(conn, 4)
    write_marker(root, marker_pid)
    record_application(conn, auto_pid, ref=f"autoapply:{auto_pid}")
    record_application(conn, manual_pid, ref="by-hand")
    record_application(conn, old_pid, ref=f"autoapply:{old_pid}", days=3)
    assert autoapply_module.status_summary(root, conn=conn)["submitted_today"] == 2
    assert autoapply_module.status_summary(root)["submitted_today"] == 1  # markers only, no DB


def test_status_reports_the_kill_file_and_the_needs_you_entries(root):
    info = autoapply_module.status_summary(root)
    assert info["kill"] is False
    assert info["needs_you"] == 0
    root.mkdir(parents=True)
    (root / "KILL").touch()
    (root / "needs-you.md").write_text("# Needs you (autoapply)\n\n## #1 A | x\n\n## #2 B | y\n")
    info = autoapply_module.status_summary(root)
    assert info["kill"] is True
    assert info["needs_you"] == 2


def test_format_status_lays_out_mode_quota_and_counters(root):
    write_state(root, [dry_entry(1), dry_entry(2, ats="lever")], armed_at=iso(),
                armed_note="reviewed")
    lines = autoapply_module.format_status(autoapply_module.status_summary(root)).splitlines()
    assert lines[0].startswith("mode: live, armed ")
    assert lines[0].endswith("(reviewed)")
    assert lines[1:] == [
        "dry runs: 2/10 (greenhouse 1/4, lever 1/4, ashby 0/4)",
        "submitted today: 0/25",
        "KILL file: absent",
        "needs-you entries: 0",
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme, Inc.", "acme"),
        ("ACME INC", "acme"),
        ("Smith & Wesson Co.", "smith and wesson"),
        ("Foo-Bar LLC", "foo bar"),
        ("  ", ""),
        (None, ""),
        ("Inc.", "inc"),
    ],
)
def test_company_names_are_normalized_for_the_caps(name, expected):
    assert autoapply_module.normalize_company(name) == expected


# --- the ready pool -----------------------------------------------------------------------------------


def test_tailored_postings_come_first_and_the_lane_follows(conn, root, tmp_path):
    ready_off = add(conn, title="Warehouse Associate")
    make_tailored(conn, ready_off, "2026-10-19T00:00:00")
    ready_nopdf = add(conn, title="Warehouse Associate")
    set_ready(conn, ready_nopdf)  # stage ready, but no PDF on disk: not usable
    both = add(conn)
    write_pdf(both)
    set_ready(conn, both, "2026-10-20T00:00:00")  # a PDF but no match report: tailored again
    lane = add(conn)
    rig = Rig(conn, root)
    summary = rig.run()

    assert summary["processed"] == [ready_off, both, lane]
    assert rig.tailor.calls == [both, lane]
    assert ready_nopdf not in summary["processed"]
    folder = (tmp_path / "data" / "apply_packs" / "resume" / "tailored").resolve()
    for call in rig.assist.calls:
        assert call["resume"] == str(folder / str(call["posting_id"]) / "Alex_Rivera_Resume.pdf")
    assert all(call["resume_path"].is_absolute() for call in rig.submit.calls)


# --- the CLI ------------------------------------------------------------------------------------------------

runner = CliRunner()


def invoke(*args):
    return runner.invoke(cli_module.app, ["autoapply", *args])


def fake_summary(**overrides):
    summary = {
        "mode": "dry", "counts": {"dry_run_ok": 2}, "processed": [1, 2], "outcomes": [],
        "skipped": {}, "tailor_calls": 2, "stop_reason": "limit reached (3 postings)",
        "aborted": False,
    }
    summary.update(overrides)
    return summary


def forbid_real_seams(monkeypatch):
    """The real browser, tailor, filler and submitter are what these tests must never reach."""

    def tripwire(name):
        def fail(*args, **kwargs):
            raise AssertionError(f"{name} must not run in a test")

        return fail

    for name in ("_default_browser", "_default_tailor", "_default_assist", "_default_submit",
                 "_default_posting_answers", "_default_guard", "_default_guard_blockers"):
        monkeypatch.setattr(autoapply_module, name, tripwire(name))


def test_cli_status_prints_the_dry_mode_summary(root):
    result = invoke("status")
    assert result.exit_code == 0
    for line in ("mode: dry (not armed)", "dry runs: 0/10", "submitted today: 0/25",
                 "KILL file: absent", "needs-you entries: 0"):
        assert line in result.output
    write_marker(root, 5)
    assert "submitted today: 1/25" in invoke("status").output


def test_cli_status_require_armed_gates_on_the_arm_state(root):
    refused = invoke("status", "--require-armed")
    assert refused.exit_code == 1
    assert "not armed" in refused.output
    arm_now(root)
    allowed = invoke("status", "--require-armed")
    assert allowed.exit_code == 0
    assert "mode: live, armed" in allowed.output


def test_cli_arm_needs_approval_text_and_the_dry_quota():
    assert invoke("arm").exit_code == 2  # --approved is required
    refused = invoke("arm", "--approved", "reviewed digest")
    assert refused.exit_code == 1
    assert "only 0 of 10 dry runs" in refused.output


def test_cli_arm_status_disarm_round_trip(root):
    write_state(root, quota_runs())
    armed = invoke("arm", "--approved", "reviewed digest")
    assert armed.exit_code == 0
    assert "armed at" in armed.output
    live = invoke("status", "--require-armed")
    assert live.exit_code == 0
    assert "mode: live, armed" in live.output
    assert "(reviewed digest)" in live.output
    disarmed = invoke("disarm")
    assert disarmed.exit_code == 0
    assert "disarmed" in disarmed.output
    assert "mode: dry (not armed)" in invoke("status").output
    assert invoke("status", "--require-armed").exit_code == 1


def test_cli_run_passes_the_limit_and_prints_the_summary(monkeypatch):
    seen = {}

    def fake_run(conn_arg, *, limit=None):
        seen["limit"] = limit
        return fake_summary()

    monkeypatch.setattr(autoapply_module, "run_autoapply", fake_run)
    result = invoke("run", "--limit", "3")
    assert result.exit_code == 0
    assert seen["limit"] == 3
    assert "autoapply run (dry): 2 reached the browser (dry_run_ok 2)" in result.output
    assert "stopped: limit reached (3 postings)" in result.output


def test_cli_run_exits_one_when_the_run_aborted(monkeypatch):
    aborted = fake_summary(aborted=True, stop_reason="browser unavailable: gone")
    monkeypatch.setattr(autoapply_module, "run_autoapply", lambda conn_arg, *, limit=None: aborted)
    result = invoke("run")
    assert result.exit_code == 1
    assert "stopped: browser unavailable: gone" in result.output


def test_cli_run_exits_one_with_the_message_when_another_apply_session_is_open(monkeypatch):
    forbid_real_seams(monkeypatch)
    with applylock.held():
        result = invoke("run")
    assert result.exit_code == 1
    assert "apply session" in result.output


def test_cli_run_over_an_empty_database_reaches_no_real_seam(monkeypatch, root):
    forbid_real_seams(monkeypatch)
    result = invoke("run")
    assert result.exit_code == 0
    assert "autoapply run (dry): 0 reached the browser" in result.output
    assert "stopped: pool exhausted" in result.output
    assert "Stopped: pool exhausted. Attempted 0. Tailor calls 0." in digest_text(root)


# --- the files that schedule it ------------------------------------------------------------------------


def test_the_launchd_plist_runs_make_autoapply_daily_without_spend_keys():
    with open(REPO / "launchd" / "com.jobhunt.autoapply.plist.template", "rb") as handle:
        plist = plistlib.load(handle)
    assert plist["Label"] == "com.jobhunt.autoapply"
    assert plist["StartCalendarInterval"] == {"Hour": 19, "Minute": 0}
    env = plist["EnvironmentVariables"]
    assert env["JOBHUNT_LLM_BACKEND"] == "claude_cli"
    assert "SPEND_OK" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert "make autoapply" in plist["ProgramArguments"][-1]
    # Machine-specific paths are filled in by scripts/install-launchd.sh, not hardcoded.
    assert "__REPO__" in plist["ProgramArguments"][-1]
    assert "__HOME__" in env["PATH"]


def test_the_make_targets_run_the_cli_and_install_only_after_the_arm_check():
    text = (REPO / "Makefile").read_text()
    run_target = text.split("\nautoapply:\n", 1)[1]
    assert run_target.splitlines()[0].strip() == "uv run python -m jobhunt autoapply run"
    install = text.split("\nschedule-install-autoapply:\n", 1)[1]
    check = install.index("jobhunt autoapply status --require-armed")
    copy = install.index("scripts/install-launchd.sh autoapply")
    load = install.index("launchctl load")
    assert check < copy < load


def test_gitignore_keeps_the_autoapply_state_out_of_git():
    assert "data/autoapply/" in (REPO / ".gitignore").read_text().splitlines()


# --- readiness: postings the submit gate would refuse are skipped before any work -----------


def test_an_eligibility_blocked_posting_never_enters_the_lane_pool(conn, root, real_readiness):
    blocked, fine = add_many(conn, 2, company="Acme")
    assess(conn, blocked, "blocked")
    assess(conn, fine)
    # The submit gate would refuse it (a lane posting has no application row yet, so
    # select_resume_variant is listed first), but the pool drops it before the gate is asked.
    assert submitwatch.record_blockers(conn, blocked) == [
        "select_resume_variant", "eligibility_blocked",
    ]
    pool = autoapply_module._lane_pool(conn, AutoApplyConfig())
    assert [cand.posting_id for cand in pool] == [fine]
    rig = Rig(conn, root, AutoApplyConfig(dry_run_quota=1, dry_run_per_ats=1))
    summary = rig.run()

    assert summary["skipped"] == {}  # not skipped one by one: it was never in the pool
    assert rig.tailor.calls == [fine]
    # It took no dry-run, per-ATS or company slot: `fine` (same ATS, same company) got the only one.
    assert summary["processed"] == [fine]
    assert summary["stop_reason"] == autoapply_module.DRY_DONE_REASON
    assert [entry["posting_id"] for entry in dry_runs_of(root)] == [fine]
    assert not (root / "blocked.json").exists()
    assert not (root / "needs-you.md").exists()


def test_a_blocked_posting_takes_no_company_slot_when_live(conn, root, real_readiness):
    blocked, fine = add_many(conn, 2, company="Acme")
    assess(conn, blocked, "blocked")
    assess(conn, fine)
    arm_now(root)
    summary = Rig(conn, root, AutoApplyConfig(company_cap=1)).run()
    assert summary["mode"] == "live"
    assert summary["skipped"] == {}
    assert summary["processed"] == [fine]
    assert statuses(summary) == ["submitted"]


def test_the_lane_pool_drops_only_blocked_postings(conn):
    blocked, review, eligible, unassessed = add_many(conn, 4)
    assess(conn, blocked, "blocked")
    assess(conn, review, "review")
    assess(conn, eligible)
    pool = autoapply_module._lane_pool(conn, AutoApplyConfig())
    # Unassessed postings wait for score_sweep in _skip_reason; only a stored "blocked" is final.
    assert sorted(cand.posting_id for cand in pool) == sorted([review, eligible, unassessed])


def test_a_tailored_posting_that_turns_blocked_is_still_refused_at_the_gate(
    conn, root, real_readiness
):
    # The ready pool is not filtered by assessment, so the gate in _skip_reason stays the backstop.
    posting = add(conn)
    make_tailored(conn, posting)
    assess(conn, posting, "blocked")
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"not_ready:eligibility_blocked": 1}
    assert rig.tailor.calls == []


def test_a_review_posting_still_proceeds(conn, root, real_readiness):
    posting = add(conn)
    assess(conn, posting, "review")
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {}
    assert summary["processed"] == [posting]
    assert rig.tailor.calls == [posting]
    assert statuses(summary) == ["dry_run_ok"]


def test_an_unassessed_posting_waits_for_score_sweep(conn, root, real_readiness):
    unassessed, fine = add_many(conn, 2)
    assess(conn, fine)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"not_ready:eligibility_not_assessed": 1}
    assert rig.tailor.calls == [fine]
    # Assessing is score_sweep's job: the run must not write an assessment of its own.
    count = conn.execute(
        "SELECT COUNT(*) FROM eligibility_assessments WHERE posting_id = ?", (unassessed,)
    ).fetchone()[0]
    assert count == 0


def test_with_no_reviewed_resume_variant_nothing_is_ready(conn, root, real_readiness):
    posting = add(conn)
    assess(conn, posting)
    review_default_variant(conn, reviewed=False)  # nothing for record_submission to auto-link
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"not_ready:select_resume_variant": 1}
    assert rig.tailor.calls == []


def test_a_not_ready_posting_is_picked_up_once_it_is_ready(conn, root, real_readiness):
    posting = add(conn)
    rig = Rig(conn, root)
    first = rig.run()
    assert first["skipped"] == {"not_ready:eligibility_not_assessed": 1}
    assess(conn, posting)  # score_sweep got to it; the skip left nothing behind to wait out
    second = rig.run()
    assert second["skipped"] == {}
    assert second["processed"] == [posting]
    assert rig.tailor.calls == [posting]


def test_the_readiness_check_comes_after_the_cheap_skips(conn, root, readiness):
    marked, worked, blocked, plain = add_many(conn, 4)
    write_marker(root, marked)
    add_activity(conn, worked, "apply_lite_open", 1)
    write_blocked(root, {blocked: 1})
    for posting_id in (marked, worked, blocked, plain):
        readiness.blockers[posting_id] = ["eligibility_blocked"]
    summary = Rig(conn, root).run()
    assert summary["skipped"] == {
        "submitted_marker": 1, "hand_worked": 1, "blocked_recently": 1,
        "not_ready:eligibility_blocked": 1,
    }
    assert readiness.calls == [plain]  # the others never cost a readiness query


def test_a_not_ready_posting_is_not_blamed_on_the_dry_run_limits(conn, root, readiness):
    earlier = add_many(conn, 4)
    later = add(conn)  # a fifth Greenhouse posting: dry_ats_limit, were it ready
    write_state(root, [dry_entry(posting_id) for posting_id in earlier])
    readiness.blockers[later] = ["eligibility_blocked"]
    summary = Rig(conn, root).run()
    assert summary["skipped"] == {"already_dry_run": 4, "not_ready:eligibility_blocked": 1}
    assert summary["stop_reason"] == "pool exhausted"


def test_a_tailored_posting_that_is_not_ready_never_reaches_the_browser(conn, root, readiness):
    posting = add(conn)
    make_tailored(conn, posting)
    readiness.blockers[posting] = ["resume_not_reviewed"]
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {"not_ready:resume_not_reviewed": 1}
    assert rig.browser.opened == 0
    assert rig.assist.calls == [] and rig.submit.calls == []


def test_a_blocker_every_posting_has_is_one_tally_and_writes_nothing(conn, root, readiness):
    ids = add_many(conn, 5)
    reason = "missing_required_answer:work_authorization"
    for posting_id in ids:
        readiness.blockers[posting_id] = [reason]
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["skipped"] == {f"not_ready:{reason}": 5}
    assert summary["processed"] == [] and rig.tailor.calls == []
    assert summary["stop_reason"] == "pool exhausted"
    assert not (root / "state.json").exists() and not (root / "blocked.json").exists()
    assert not (root / "needs-you.md").exists()
    text = digest_text(root)
    assert f"Skipped: not_ready:{reason} 5." in text
    assert not any(f"| {posting_id} |" in text for posting_id in ids)  # a tally, not a line each
    assert f"skipped: not_ready:{reason} 5" in autoapply_module.format_summary(summary, root)


def test_a_readiness_check_that_fails_stops_the_run_loudly(conn, root, readiness):
    first, _ = add_many(conn, 2)
    readiness.blockers[first] = ValueError("search preferences unreadable")
    rig = Rig(conn, root)
    with pytest.raises(ValueError, match="search preferences unreadable"):
        rig.run()
    assert rig.tailor.calls == []
    assert "Stopped: crashed: ValueError: search preferences unreadable" in digest_text(root)


# --- a run that keeps getting blocked -------------------------------------------------------------
# A block is not a failure, so the failure breaker never sees a gate that blocks every form. This
# breaker counts browser attempts that ended blocked, in a row, within one run.


def blocked_plan(ids):
    """Submit plan: every posting is blocked, with its own first blocker and a second one."""
    return {pid: ("blocked", [f"form {n} unreadable", "second blocker"])
            for n, pid in enumerate(ids, 1)}


@pytest.mark.parametrize("armed", [False, True], ids=["dry", "live"])
def test_blocks_in_a_row_abort_the_run(conn, root, armed):
    ids = add_many(conn, 5)
    if armed:
        arm_now(root)
    rig = Rig(conn, root, AutoApplyConfig(max_consecutive_blocked=3), submit=blocked_plan(ids))
    summary = rig.run()

    reason = "3 blocked in a row, last: form 3 unreadable"  # the last block's first blocker only
    assert summary["aborted"] is True
    assert summary["stop_reason"] == reason
    assert summary["processed"] == ids[:3]
    assert rig.tailor.calls == ids[:3]  # the other two were never tailored
    assert counts_of(summary) == {"blocked": 3}
    assert set(autoapply_module.load_blocked(root)) == {str(pid) for pid in ids[:3]}
    assert_run_entry(root, reason, mode="live" if armed else "dry")
    assert reason in digest_text(root)
    assert rig.browser.handle.stopped


@pytest.mark.parametrize(
    "status, armed",
    [("dry_run_ok", False), ("submitted", True), ("unconfirmed", True), ("error", False)],
)
def test_any_other_attempt_status_ends_the_blocked_streak(conn, root, status, armed):
    ids = add_many(conn, 5)
    if armed:
        arm_now(root)
    plan = blocked_plan(ids)
    plan[ids[2]] = RuntimeError("boom") if status == "error" else status
    cfg = AutoApplyConfig(max_consecutive_blocked=3, dry_run_quota=50, dry_run_per_ats=50)
    summary = Rig(conn, root, cfg, submit=plan).run()

    # Two blocks, the other status, two more blocks: never three in a row.
    assert statuses(summary) == ["blocked", "blocked", status, "blocked", "blocked"]
    assert summary["processed"] == ids
    assert summary["aborted"] is False
    assert summary["stop_reason"] == "pool exhausted"
    assert run_entries(root) == []


@pytest.mark.parametrize("outcome", ["dropped", "failed"])
def test_a_tailor_outcome_neither_ends_nor_extends_the_blocked_streak(conn, root, outcome):
    first, second, tailor_out, third, fourth = add_many(conn, 5)
    plan = blocked_plan([first, second, third, fourth])
    cfg = AutoApplyConfig(max_consecutive_blocked=3)
    rig = Rig(conn, root, cfg, tailor={tailor_out: outcome}, submit=plan)
    summary = rig.run()

    # Two blocks, a posting that never reaches the browser, a third block. A reset would leave
    # the streak at one; counting it would stop the run before the third block. It is three.
    assert statuses(summary) == ["blocked", "blocked", f"tailor_{outcome}", "blocked"]
    assert summary["aborted"] is True
    assert summary["stop_reason"] == "3 blocked in a row, last: form 3 unreadable"
    assert summary["processed"] == [first, second, third]
    assert rig.tailor.calls == [first, second, tailor_out, third]


def test_the_blocked_limit_defaults_to_fifteen():
    assert AutoApplyConfig().max_consecutive_blocked == 15


def test_a_live_run_with_the_default_limit_stops_after_fifteen_blocks(conn, root):
    ids = add_many(conn, 17)
    arm_now(root)  # a dry run ends at its quota of 10 before fifteen blocks can happen
    rig = Rig(conn, root, submit={pid: "blocked" for pid in ids})  # no blocker strings given
    summary = rig.run()

    reason = "15 blocked in a row, last: blocked by the submit gate (no reason given)"
    assert summary["aborted"] is True
    assert summary["stop_reason"] == reason
    assert summary["processed"] == ids[:15]
    assert rig.tailor.calls == ids[:15]
    assert_run_entry(root, reason, mode="live")


# --- the submit guard and the posting's own answers -----------------------------------------------
# The lane fills forms and never submits one by hand, so a guard goes on every page before it
# loads (dry and live) and a submit the guard stops ends the run. The answers a posting carries
# of its own are laid over the stored ones for that posting alone.

GUARD_LINE = "submit_guard: form.submit stopped on the Apply page"
# What the guard also reports, in its own words, when it could not read the page it guards.
UNREADABLE = [
    "submit_guard: could not list frames (TargetClosedError)",
    "submit_guard: could not read guard state in the main frame (Error)",
    "submit_guard: DOM layer missing in the main frame",
]
# A guard stop beside an entry that has instructions of its own (a click, a marker) is added to
# that entry's `What you do`, so the posting still has one entry.
ALSO_GUARD = autoapply_module._TODO_ALSO_GUARD


def store_answers(conn, **answers):
    """Stored profile answers (application_answers), the way `jobhunt` keeps them."""
    stamp = iso()
    for key, value in answers.items():
        conn.execute(
            "INSERT INTO application_answers (semantic_key, value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)", (key, value, stamp, stamp),
        )
    conn.commit()


def stored_answers(conn):
    rows = conn.execute("SELECT semantic_key, value FROM application_answers")
    return {row["semantic_key"]: row["value"] for row in rows}


def posting_entry(root, posting_id):
    """One posting's needs-you.md entry as its lines, the heading without its `## `."""
    parts = needs_you_text(root).split("\n## ")[1:]
    [entry] = [
        part.rstrip("\n").splitlines() for part in parts if part.startswith(f"#{posting_id} ")
    ]
    return entry


def stand_in_module(monkeypatch, name, **attrs):
    """jobhunt.apply_assist.<name> swapped for a stand-in for one test, whether or not the real
    module exists yet. The lane imports these lazily, so this is all a test has to replace."""
    module = ModuleType(f"jobhunt.apply_assist.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(apply_assist_package, name, module, raising=False)
    return module


# -- the guard goes on before the form loads


@pytest.mark.parametrize("armed", [False, True])
def test_the_guard_goes_on_every_page_before_the_form_loads(conn, root, armed):
    first, second = add_many(conn, 2)
    if armed:
        arm_now(root)
    rig = Rig(conn, root)
    guards_before_fill = []
    rig.assist.hook = lambda pid: guards_before_fill.append(len(rig.guard.calls))
    summary = rig.run()

    pages = rig.browser.context.pages
    assert summary["processed"] == [first, second]
    assert len(pages) == 2
    assert [call["page"] for call in rig.guard.calls] == pages
    assert [call["gotos_before"] for call in rig.guard.calls] == [0, 0]
    assert [call["closed_before"] for call in rig.guard.calls] == [False, False]
    assert [len(page.goto_calls) for page in pages] == [1, 1]
    assert guards_before_fill == [1, 2]  # each fill began with its own page already guarded
    assert [call["page"] for call in rig.assist.calls] == pages
    assert [call["page"] for call in rig.submit.calls] == pages


@pytest.mark.parametrize("armed", [False, True])
def test_a_guard_that_will_not_install_stops_the_run_before_the_form_loads(conn, root, armed):
    first = add(conn, company="Globex")
    add(conn, company="Initech")
    if armed:
        arm_now(root)
    rig = Rig(conn, root, guard_error=RuntimeError("Target closed\nat Page.addInitScript"))
    summary = rig.run()

    reason = "submit guard could not be installed: RuntimeError: Target closed at Page.addInitScript"
    stop = f"{reason}; run stopped, posting #{first} not blocked"
    mode = "live" if armed else "dry"
    [page] = rig.browser.context.pages
    assert statuses(summary) == ["error"]
    assert reasons(summary) == {first: reason}
    assert summary["aborted"] is True
    assert summary["stop_reason"] == stop
    assert summary["processed"] == [first]
    assert rig.tailor.calls == [first]  # the second posting is never reached
    # Nothing was loaded, filled or clicked, and the page is closed again.
    assert page.goto_calls == []
    assert page.closed is True
    assert rig.assist.calls == [] and rig.submit.calls == []
    assert rig.browser.handle.stopped
    # The guard's fault, not the posting's: not blocked, and no dry run recorded for it.
    assert autoapply_module.load_blocked(root) == {}
    assert autoapply_module.load_state(root)["dry_runs"] == []
    # The candidate is told in the posting's entry and in the run's.
    headings = needs_you_headings(root)
    assert len(headings) == 2
    assert headings[0] == f"## #{first} Globex | Financial Analyst"
    entry = posting_entry(root, first)
    assert entry[1].endswith(f"({mode})")
    assert entry[2] == "- Status: error"
    assert entry[4] == f"- Reason: {reason}"
    assert entry[5].startswith("- What you do: The submit guard stopped the run")
    assert "A submit marker exists" not in entry[5]
    assert_run_entry(root, stop, mode=mode)
    assert autoapply_module.status_summary(root)["needs_you"] == 2
    digest = digest_text(root)
    assert f"| {first} | Globex | Financial Analyst | greenhouse | error | {reason} |" in digest
    assert f"Stopped: {stop}. Attempted 1. Tailor calls 1." in digest


def test_a_guard_that_fails_on_a_later_page_stops_the_run_there(conn, root):
    first, second, third = add_many(conn, 3)
    rig = Rig(conn, root)
    rig.assist.hook = lambda pid: setattr(rig.guard, "error", RuntimeError("Target closed"))
    summary = rig.run()

    assert statuses(summary) == ["dry_run_ok", "error"]
    assert summary["processed"] == [first, second]
    assert third not in rig.tailor.calls
    assert [entry["posting_id"] for entry in dry_runs_of(root)] == [first]
    assert len(rig.assist.calls) == 1  # the second form was never filled


# -- a submit the guard stopped


@pytest.mark.parametrize("armed", [False, True])
def test_a_submit_the_guard_stopped_ends_the_run_and_goes_to_needs_you(conn, root, armed):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    if armed:
        arm_now(root)
    rig = Rig(conn, root, submit={first: ("blocked", [GUARD_LINE, "salary_required"])})
    summary = rig.run()

    reason = f"{GUARD_LINE}; salary_required"
    stop = f"{GUARD_LINE}; run stopped after posting #{first}"
    mode = "live" if armed else "dry"
    assert statuses(summary) == ["blocked"]
    assert reasons(summary) == {first: reason}
    assert summary["aborted"] is True
    assert summary["stop_reason"] == stop
    assert summary["processed"] == [first]
    assert second not in rig.tailor.calls
    assert rig.browser.handle.stopped
    # A guard stop is no verdict on the posting: not out of the pool for seven days, and no dry
    # run recorded for it.
    assert autoapply_module.load_blocked(root) == {}
    assert autoapply_module.load_state(root)["dry_runs"] == []
    entry = posting_entry(root, first)
    assert entry[2] == "- Status: blocked"
    assert entry[4] == f"- Reason: {reason}"
    assert entry[5].startswith("- What you do: The submit guard stopped the run")
    assert "confirm-scan" not in entry[5]  # not the generic "apply by hand, then confirm-scan"
    assert len(needs_you_headings(root)) == 2
    assert_run_entry(root, stop, mode=mode)
    assert stop in digest_text(root)


# What the network layer says for a request it aborts without recognising it as a submit, a
# recognised submit, and what the DOM layer says (GUARD_LINE).
UNKNOWN_ABORT = (
    "submit_guard: aborted greenhouse POST not on the allowlist "
    "(analytics.greenhouse.io/collect/events)"
)
SUBMIT_ABORT = "submit_guard: aborted greenhouse application POST (boards.greenhouse.io/acme/jobs/123)"


def test_an_unknown_abort_blocks_the_posting_but_not_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: ("blocked", [UNKNOWN_ABORT, "salary_required"])})
    summary = rig.run()

    assert summary["aborted"] is False
    assert summary["stop_reason"] != f"{UNKNOWN_ABORT}; run stopped after posting #{first}"
    assert "run stopped after posting" not in (summary["stop_reason"] or "")
    assert summary["processed"] == [first, second]  # the next posting was tried
    assert second in rig.tailor.calls
    # Blocked for BLOCKED_DAYS with the URL and host named, and not counted as a dry run.
    assert str(first) in autoapply_module.load_blocked(root)
    assert "analytics.greenhouse.io/collect/events" in autoapply_module.load_blocked(root)[str(first)]["reason"]
    assert first not in [e["posting_id"] for e in autoapply_module.load_state(root)["dry_runs"]]
    entry = posting_entry(root, first)
    assert entry[4].startswith(f"- Reason: {UNKNOWN_ABORT}; salary_required")
    assert entry[5].startswith("- What you do: The submit guard aborted a request it does not recognise")
    assert "stopped the run" not in entry[5]


CHALLENGE_ABORT = (
    "submit_guard: aborted lever POST bot challenge (Cloudflare) (jobs.lever.co/cdn-cgi/challenge-platform/)"
)


def test_a_cloudflare_challenge_blocks_the_posting_but_not_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: ("blocked", [CHALLENGE_ABORT, "bot challenge: AI CAPTCHA"])})
    summary = rig.run()

    assert summary["aborted"] is False
    assert "run stopped after posting" not in (summary["stop_reason"] or "")
    assert summary["processed"] == [first, second]
    assert second in rig.tailor.calls
    assert "bot challenge (Cloudflare)" in autoapply_module.load_blocked(root)[str(first)]["reason"]
    assert first not in [e["posting_id"] for e in autoapply_module.load_state(root)["dry_runs"]]
    assert "bot challenge (Cloudflare)" in posting_entry(root, first)[4]


def test_the_cut_dry_run_7_line_blocks_the_posting_but_not_the_run(conn, root):
    cut = (
        "submit_guard: aborted lever POST not on the allowlist (jobs.lever.co/cdn-cgi/challenge-platform/"
        "h/b/jsd/oneshot/d2d3316b729e/0.3321456192769964:1792854619:6Zm9hl_LQFFYwUlHUy2NyUN9vOz55aRUUpxyloApIvU/a4fa49334a63cef"
    )
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: ("blocked", [cut])})
    summary = rig.run()

    assert summary["aborted"] is False
    assert summary["processed"] == [first, second]
    assert str(first) in autoapply_module.load_blocked(root)


def test_an_unknown_abort_beside_a_dom_catch_still_stops_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: ("blocked", [UNKNOWN_ABORT, GUARD_LINE])})
    summary = rig.run()

    assert summary["aborted"] is True
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls
    assert autoapply_module.load_blocked(root) == {}


def test_an_aborted_request_that_is_a_recognised_submit_still_stops_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: ("blocked", [SUBMIT_ABORT])})
    summary = rig.run()

    assert summary["aborted"] is True
    assert summary["stop_reason"] == f"{SUBMIT_ABORT}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls
    assert autoapply_module.load_blocked(root) == {}
    assert posting_entry(root, first)[5].startswith("- What you do: The submit guard stopped the run")


@pytest.mark.parametrize(
    "line",
    [
        "submit_guard: aborted greenhouse application PUT (boards.greenhouse.io/acme/jobs/123)",
        "submit_guard: aborted greenhouse application POST (boards-api.greenhouse.io/v1/boards/acme/jobs/123)",
        "submit_guard: aborted ashby multipart graphql call (jobs.ashbyhq.com)",
        "submit_guard: aborted ashby graphql call without a readable operation name (jobs.ashbyhq.com)",
    ],
)
def test_a_submit_shaped_abort_of_any_method_stops_the_run(conn, root, line):
    first, second = add_many(conn, 2)
    summary = Rig(conn, root, submit={first: ("blocked", [line])}).run()

    assert summary["aborted"] is True
    assert summary["stop_reason"] == f"{line}; run stopped after posting #{first}"
    assert autoapply_module.load_blocked(root) == {}


def test_the_guards_own_words_come_first_in_the_needs_you_reason(conn, root):
    pid = add(conn)
    other = "submit_guard: a second submit was stopped"
    blockers = ["salary_required", GUARD_LINE, "required_consent_checkbox", other]
    summary = Rig(conn, root, submit={pid: ("blocked", blockers)}).run()

    assert reasons(summary) == {pid: "; ".join(blockers)}  # the table keeps the gate's order
    assert posting_entry(root, pid)[4] == (
        f"- Reason: {GUARD_LINE}; {other}; salary_required; required_consent_checkbox"
    )
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{pid}"


def test_a_long_guard_blocker_stays_on_one_line_and_cannot_forge_an_entry(conn, root):
    pid = add(conn, company="Globex")
    nasty = "submit_guard: " + "A" * 400 + "\n## #99 Fake | entry\n" + "B" * 400
    Rig(conn, root, submit={pid: ("blocked", [nasty, "salary_required"])}).run()

    flat = " ".join(nasty.split())
    assert posting_entry(root, pid)[4] == "- Reason: " + f"{flat}; salary_required"[:597] + "..."
    assert len(needs_you_headings(root)) == 2  # the posting and the run: no forged third
    assert_run_entry(root, f"{flat[:197]}...; run stopped after posting #{pid}")


def test_a_guard_blocker_with_leading_space_still_stops_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: ("blocked", [f"  {GUARD_LINE}"])})
    summary = rig.run()

    assert summary["aborted"] is True
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls


def test_a_guard_stop_outranks_the_blocked_streak(conn, root):
    first, _second = add_many(conn, 2)
    cfg = AutoApplyConfig(max_consecutive_blocked=1)
    summary = Rig(conn, root, cfg, submit={first: ("blocked", [GUARD_LINE])}).run()

    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"


def test_a_blocker_that_only_mentions_the_guard_does_not_stop_the_run(conn, root):
    first, second = add_many(conn, 2)
    note = "required_field: the label says submit_guard: please"
    rig = Rig(conn, root, submit={first: ("blocked", [note])})
    summary = rig.run()

    assert statuses(summary) == ["blocked", "dry_run_ok"]
    assert summary["aborted"] is False
    assert summary["processed"] == [first, second]
    todo = posting_entry(root, first)[5]
    assert todo.startswith("- What you do: Apply by hand at the URL: the form was not submitted")
    assert run_entries(root) == []


@pytest.mark.parametrize("blocker", [
    "SUBMIT_GUARD: form.submit stopped on the Apply page",
    "Submit_guard: form.submit stopped on the Apply page",
], ids=["upper case", "capitalised"])
def test_a_blocker_that_spells_the_guards_prefix_differently_is_an_ordinary_blocker(
    conn, root, blocker
):
    # The guard reports "submit_guard:" and nothing else, so that exact prefix is what counts.
    first, second = add_many(conn, 2)
    summary = Rig(conn, root, submit={first: ("blocked", [blocker])}).run()

    assert statuses(summary) == ["blocked", "dry_run_ok"]
    assert summary["aborted"] is False
    assert autoapply_module.load_blocked(root)[str(first)]["reason"] == blocker
    assert posting_entry(root, first)[5].startswith("- What you do: Apply by hand at the URL")


NASTY = "gate said no\n## #99 Fake | entry\nsee below"
NASTY_FLAT = "gate said no ## #99 Fake | entry see below"


@pytest.mark.parametrize("live, step, marked, reason", [
    (False, ("submitted", [NASTY]), False, f"submitted; record incomplete: {NASTY_FLAT}"),
    (True, ("submitted", [NASTY]), False, f"submitted; record incomplete: {NASTY_FLAT}"),
    (True, ("unconfirmed", [NASTY]), False, f"submit click sent, no success page seen; {NASTY_FLAT}"),
    (True, ("blocked", [NASTY]), False, NASTY_FLAT),
    (True, RuntimeError(NASTY), True, f"RuntimeError: {NASTY_FLAT}"),
], ids=["click in a dry run", "record incomplete", "unconfirmed", "blocked", "raised after a click"])
def test_a_reason_with_line_breaks_is_one_line_in_every_needs_you_entry(
    conn, root, live, step, marked, reason
):
    pid = add(conn, company="Globex")
    if live:
        arm_now(root)
    rig = Rig(conn, root, submit={pid: step})
    if marked:
        rig.submit.hook = lambda posting_id: write_marker(root, posting_id, "clicking")
    rig.run()

    headings = needs_you_headings(root)
    assert [heading for heading in headings if "#99" in heading] == []  # nothing forged
    assert len([heading for heading in headings if heading.startswith(f"## #{pid} ")]) == 1
    assert posting_entry(root, pid)[4] == f"- Reason: {reason}"
    assert len(posting_entry(root, pid)) == 6


@pytest.mark.parametrize("status, todo", [
    ("unconfirmed", "A submit click went out but no success page was seen"),
    ("submitted", "The site confirmed the application, but the database record is incomplete"),
])
def test_a_guard_blocker_beside_a_live_click_keeps_the_click_instructions(
    conn, root, status, todo
):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    arm_now(root)
    rig = Rig(conn, root, submit={first: (status, [GUARD_LINE])})
    summary = rig.run()

    assert statuses(summary) == [status]
    assert summary["aborted"] is True
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls
    entry = posting_entry(root, first)
    assert entry[5].startswith(f"- What you do: {todo}")
    # One entry for the posting (and one for the run) that carries the click's instructions and
    # the guard's.
    assert entry[5] == f"- What you do: {autoapply_module._TODO[status]} {ALSO_GUARD}"
    assert len(needs_you_headings(root)) == 2
    assert autoapply_module.status_summary(root)["submitted_today"] == 1  # the click is counted


@pytest.mark.parametrize("armed", [False, True])
def test_a_guard_blocker_on_an_error_stops_the_run_and_goes_to_needs_you(conn, root, armed):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    if armed:
        arm_now(root)
    rig = Rig(conn, root, submit={first: ("error", [GUARD_LINE])})
    summary = rig.run()

    stop = f"{GUARD_LINE}; run stopped after posting #{first}"
    assert statuses(summary) == ["error"]
    assert summary["aborted"] is True
    assert summary["stop_reason"] == stop
    assert second not in rig.tailor.calls
    entry = posting_entry(root, first)  # no marker: the guard's instructions, like a blocked form
    assert entry[2] == "- Status: error"
    assert entry[4] == f"- Reason: {GUARD_LINE}"
    assert entry[5].startswith("- What you do: The submit guard stopped the run")
    assert len(needs_you_headings(root)) == 2
    assert_run_entry(root, stop, mode="live" if armed else "dry")


def test_a_guard_blocker_beside_a_submit_marker_keeps_the_marker_instructions(conn, root):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    arm_now(root)
    rig = Rig(conn, root, submit={first: ("error", [GUARD_LINE])})
    rig.submit.hook = lambda pid: write_marker(root, pid, "clicking")  # a click was about to go
    summary = rig.run()

    stop = f"{GUARD_LINE}; run stopped after posting #{first}"
    assert summary["stop_reason"] == stop
    assert second not in rig.tailor.calls
    entry = posting_entry(root, first)
    assert entry[5].startswith("- What you do: A submit marker exists")
    assert entry[5] == f"- What you do: {autoapply_module._TODO['error']} {ALSO_GUARD}"
    assert len(needs_you_headings(root)) == 2
    assert autoapply_module.load_blocked(root) == {}  # a marked posting is skipped by its marker
    assert_run_entry(root, stop, mode="live")


def test_a_click_in_a_dry_run_keeps_its_own_stop_reason_beside_a_guard_blocker(conn, root):
    first = add(conn)
    summary = Rig(conn, root, submit={first: ("submitted", [GUARD_LINE])}).run()

    assert summary["stop_reason"] == "submitted reported in dry mode; check apply_assist/submit.py"
    entry = posting_entry(root, first)
    assert entry[5].startswith("- What you do: A click went out during a DRY run")
    assert entry[5] == f"- What you do: {autoapply_module._TODO['dry_click']} {ALSO_GUARD}"
    assert len(needs_you_headings(root)) == 2
    assert_run_entry(root, "submitted reported in dry mode; check apply_assist/submit.py")


# -- a guard stop is no verdict on the posting


@pytest.mark.parametrize("armed", [False, True])
@pytest.mark.parametrize("status", ["blocked", "dry_run_ok", "error"])
def test_a_posting_the_guard_stopped_is_neither_a_dry_run_nor_blocked(conn, root, status, armed):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    if armed:
        arm_now(root)
    rig = Rig(conn, root, submit={first: (status, [GUARD_LINE])})
    summary = rig.run()

    assert statuses(summary) == [status]
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls
    # A fill that tried to submit says nothing about the posting, so it counts toward no dry-run
    # quota or per-ATS limit and sits out no days.
    assert autoapply_module.load_blocked(root) == {}
    assert autoapply_module.load_state(root)["dry_runs"] == []
    info = autoapply_module.status_summary(root)
    assert info["dry_runs"] == 0
    assert info["dry_runs_by_ats"]["greenhouse"] == 0


def test_the_next_run_opens_a_posting_the_guard_stopped_again(conn, root):
    first, _second = add_many(conn, 2)
    cfg = AutoApplyConfig(dry_run_per_ats=1)
    stopped = Rig(conn, root, cfg, submit={first: ("blocked", [GUARD_LINE])}).run()
    assert stopped["processed"] == [first]
    assert stopped["aborted"] is True

    rig = Rig(conn, root, cfg)
    again = rig.run()

    # Had the stop been recorded as a dry run, the one greenhouse slot would be spent and #first
    # skipped. It is tried again instead, and takes the slot once it passes.
    assert again["processed"] == [first]
    assert statuses(again) == ["dry_run_ok"]
    assert again["skipped"] == {"dry_ats_limit": 1}
    assert [entry["posting_id"] for entry in dry_runs_of(root)] == [first]


def test_a_guard_stop_brings_the_arm_check_no_closer(conn, root):
    first, _second = add_many(conn, 2)
    cfg = AutoApplyConfig(dry_run_quota=1)
    Rig(conn, root, cfg, submit={first: ("blocked", [GUARD_LINE])}).run()

    with pytest.raises(autoapply_module.ArmError, match="only 0 of 1 dry runs recorded"):
        autoapply_module.arm(root, approved="reviewed the digest", cfg=cfg)
    assert autoapply_module.load_state(root)["armed_at"] is None


# -- a guard blocker beside any outcome status goes to needs-you


@pytest.mark.parametrize("armed", [False, True])
def test_a_guard_blocker_beside_a_dry_run_ok_still_goes_to_needs_you(conn, root, armed):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    if armed:
        arm_now(root)
    rig = Rig(conn, root, submit={first: ("dry_run_ok", ["salary_required", GUARD_LINE])})
    summary = rig.run()

    stop = f"{GUARD_LINE}; run stopped after posting #{first}"
    assert statuses(summary) == ["dry_run_ok"]
    assert reasons(summary) == {first: "gate passed; nothing clicked (dry run)"}
    assert summary["aborted"] is True
    assert summary["stop_reason"] == stop
    assert second not in rig.tailor.calls
    entry = posting_entry(root, first)  # nothing was clicked: the guard's instructions stand alone
    assert entry[2] == "- Status: dry_run_ok"
    assert entry[4] == f"- Reason: {GUARD_LINE}; salary_required"
    assert entry[5] == f"- What you do: {autoapply_module._TODO['submit_guard']}"
    assert len(needs_you_headings(root)) == 2
    assert_run_entry(root, stop, mode="live" if armed else "dry")


@pytest.mark.parametrize("step, armed, todo", [
    ("unconfirmed", True, "unconfirmed"),
    ("unconfirmed", False, "dry_click"),
    ("submitted", False, "dry_click"),
    ("marker_then_error", True, "error"),
])
def test_an_entry_without_a_guard_blocker_has_no_guard_addition(conn, root, step, armed, todo):
    first = add(conn)
    if armed:
        arm_now(root)
    Rig(conn, root, submit={first: step}).run()

    entry = posting_entry(root, first)
    assert entry[5] == f"- What you do: {autoapply_module._TODO[todo]}"


def test_the_guard_addition_never_tells_anyone_to_apply_again():
    assert ALSO_GUARD.startswith("The submit guard also stopped the run (see the reason)")
    assert "Apply by hand" not in ALSO_GUARD
    assert "apply again" not in ALSO_GUARD.lower()


# -- a fill or a submit that raised after the guard stopped a submit
# The guard may be what made the page throw, so what it holds is read before the page closes.


@pytest.mark.parametrize("armed", [False, True])
def test_a_fill_that_raised_after_the_guard_stopped_a_submit_stops_the_run(conn, root, armed):
    first = add(conn, company="Globex")
    second = add(conn, company="Initech")
    if armed:
        arm_now(root)
    rig = Rig(conn, root, assist_fail={first: TimeoutError("form never rendered")},
              guard_reads=[GUARD_LINE])
    summary = rig.run()

    reason = f"{GUARD_LINE}; TimeoutError: form never rendered"
    stop = f"{GUARD_LINE}; run stopped after posting #{first}"
    [page] = rig.browser.context.pages
    assert statuses(summary) == ["error"]
    assert reasons(summary) == {first: reason}
    assert summary["aborted"] is True
    assert summary["stop_reason"] == stop
    assert summary["processed"] == [first]
    assert second not in rig.tailor.calls
    assert rig.submit.calls == []  # the fill raised before the submit
    # Read on the attempt's own page, and before that page closed.
    assert rig.guard_reads.calls == [{"page": page, "closed_before": False}]
    assert page.closed is True
    # A stopped submit is no verdict on the posting, however the fill ended.
    assert autoapply_module.load_blocked(root) == {}
    assert autoapply_module.load_state(root)["dry_runs"] == []
    entry = posting_entry(root, first)
    assert entry[2] == "- Status: error"
    assert entry[4] == f"- Reason: {reason}"
    assert entry[5] == f"- What you do: {autoapply_module._TODO['submit_guard']}"
    assert len(needs_you_headings(root)) == 2
    assert_run_entry(root, stop, mode="live" if armed else "dry")
    assert f"| {first} | Globex | Financial Analyst | greenhouse | error | {reason} |" in (
        digest_text(root)
    )


def test_an_http_error_page_beside_a_guard_stop_stops_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, browser=FakeBrowser(codes=[500]), guard_reads=[GUARD_LINE])
    summary = rig.run()

    reason = f"{GUARD_LINE}; HTTP 500 on the application page; the posting may be closed"
    assert statuses(summary) == ["error"]
    assert reasons(summary) == {first: reason}
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls
    assert rig.assist.calls == []
    assert rig.guard_reads.calls[0]["closed_before"] is False
    assert autoapply_module.load_blocked(root) == {}
    assert posting_entry(root, first)[4] == f"- Reason: {reason}"


def test_a_submit_that_raised_after_the_guard_stopped_a_submit_stops_the_run(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, submit={first: RuntimeError("click lost")}, guard_reads=[GUARD_LINE])
    summary = rig.run()

    assert statuses(summary) == ["error"]
    assert reasons(summary) == {first: f"{GUARD_LINE}; RuntimeError: click lost"}
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls
    assert autoapply_module.load_blocked(root) == {}
    assert autoapply_module.load_state(root)["dry_runs"] == []
    assert posting_entry(root, first)[5] == (
        f"- What you do: {autoapply_module._TODO['submit_guard']}"
    )


def test_a_raised_submit_beside_a_marker_and_a_guard_stop_is_one_entry_that_says_both(conn, root):
    first, second = add_many(conn, 2)
    arm_now(root)
    rig = Rig(conn, root, submit={first: RuntimeError("page crashed")}, guard_reads=[GUARD_LINE])
    rig.submit.hook = lambda pid: write_marker(root, pid, "clicking")  # a click was about to go
    summary = rig.run()

    reason = f"{GUARD_LINE}; RuntimeError: page crashed"
    stop = f"{GUARD_LINE}; run stopped after posting #{first}"
    assert reasons(summary) == {first: reason}
    assert summary["stop_reason"] == stop
    assert second not in rig.tailor.calls
    entry = posting_entry(root, first)
    assert entry[4] == f"- Reason: {reason}"
    assert entry[5] == f"- What you do: {autoapply_module._TODO['error']} {ALSO_GUARD}"
    assert len(needs_you_headings(root)) == 2
    assert autoapply_module.load_blocked(root) == {}  # a marked posting is skipped by its marker
    assert_run_entry(root, stop, mode="live")


def test_only_what_the_guard_stopped_is_kept_when_it_also_could_not_read_the_page(conn, root):
    first, second = add_many(conn, 2)
    other = "submit_guard: a second submit was stopped"
    held = [UNREADABLE[0], GUARD_LINE, UNREADABLE[2], other]
    rig = Rig(conn, root, assist_fail={first: TimeoutError("form never rendered")},
              guard_reads=held)
    summary = rig.run()

    assert reasons(summary) == {first: f"{GUARD_LINE}; {other}; TimeoutError: form never rendered"}
    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls


def test_a_guard_stop_after_a_raised_fill_outranks_the_failure_limit(conn, root):
    first, second = add_many(conn, 2)
    cfg = AutoApplyConfig(max_consecutive_failures=1)
    rig = Rig(conn, root, cfg, assist_fail={first: TimeoutError("form never rendered")},
              guard_reads=[GUARD_LINE])
    summary = rig.run()

    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls


class TextLike:
    """Not a str, but reads as one: the guard's lines are read as text, as a gate's blockers are."""

    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


def test_the_guards_lines_are_read_as_text_whatever_their_type(conn, root):
    first, second = add_many(conn, 2)
    rig = Rig(conn, root, assist_fail={first: TimeoutError("form never rendered")},
              guard_reads=[TextLike(GUARD_LINE)])
    summary = rig.run()

    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert second not in rig.tailor.calls


def test_the_reason_beside_a_guard_stop_is_one_line_whatever_the_error_says(conn, root):
    first = add(conn)
    error = TimeoutError("form never rendered\nwaiting for locator\n## #99 Fake | entry")
    summary = Rig(conn, root, assist_fail={first: error}, guard_reads=[GUARD_LINE]).run()

    flat = "TimeoutError: form never rendered waiting for locator ## #99 Fake | entry"
    assert reasons(summary) == {first: f"{GUARD_LINE}; {flat}"}
    assert posting_entry(root, first)[4] == f"- Reason: {GUARD_LINE}; {flat}"
    assert len(needs_you_headings(root)) == 2  # the posting and the run: no forged third


@pytest.mark.parametrize("held", [
    [],
    lambda page: None,
    ["required_field: the label says submit_guard: please"],
    ["other_guard: form.submit stopped"],
    ["SUBMIT_GUARD: form.submit stopped on the Apply page"],
    ["Submit_guard: form.submit stopped on the Apply page"],
    UNREADABLE,
    ["  submit_guard: Could not list frames"],
    RuntimeError("Target closed"),
], ids=["nothing held", "nothing returned", "only mentions the guard", "another prefix",
        "upper case", "capitalised", "page could not be read", "unreadable with a leading space",
        "reading it failed"])
def test_an_error_the_guard_did_not_stop_is_blocked_and_the_run_goes_on(conn, root, held):
    bad, good = add_many(conn, 2)
    rig = Rig(conn, root, assist_fail={bad: TimeoutError("form never rendered")}, guard_reads=held)
    summary = rig.run()

    reason = "TimeoutError: form never rendered"
    assert statuses(summary) == ["error", "dry_run_ok"]
    assert reasons(summary)[bad] == reason
    assert summary["aborted"] is False
    assert summary["processed"] == [bad, good]
    assert autoapply_module.load_blocked(root)[str(bad)]["reason"] == reason  # blocked as before
    assert not (root / "needs-you.md").exists()
    assert len(rig.guard_reads.calls) == 1  # asked once, for the one attempt that raised


def test_the_guard_is_only_read_for_an_attempt_that_raised(conn, root):
    add_many(conn, 3)
    rig = Rig(conn, root, guard_reads=[GUARD_LINE])
    summary = rig.run()

    assert statuses(summary) == ["dry_run_ok"] * 3
    assert rig.guard_reads.calls == []


def test_a_guard_that_will_not_install_is_not_read(conn, root):
    first = add(conn)
    rig = Rig(conn, root, guard_error=RuntimeError("Target closed"), guard_reads=[GUARD_LINE])
    summary = rig.run()

    reason = "submit guard could not be installed: RuntimeError: Target closed"
    assert summary["stop_reason"] == f"{reason}; run stopped, posting #{first} not blocked"
    assert rig.guard_reads.calls == []


# -- the posting's own answers


def test_each_posting_gets_its_own_answers_laid_over_the_stored_ones(conn, root):
    first, second, third = add_many(conn, 3)
    stored = {"first_name": "Alex", "location": "Atlanta, GA", "salary_expectation": "80000"}
    store_answers(conn, **stored)
    rig = Rig(conn, root, posting_answers={
        first: {"salary_expectation": "95000", "how_heard": "LinkedIn"},
        second: {"location": "New York, NY"},
    })
    rig.run()

    # Theirs win, new keys are added, nothing carries from one posting to the next.
    assert [call["answers"] for call in rig.assist.calls] == [
        {**stored, "salary_expectation": "95000", "how_heard": "LinkedIn"},
        {**stored, "location": "New York, NY"},
        stored,
    ]
    assert [posting_id for _conn, posting_id in rig.posting_answers.calls] == [first, second, third]
    assert all(seen_conn is conn for seen_conn, _posting_id in rig.posting_answers.calls)
    assert stored_answers(conn) == stored  # the stored answers themselves are never touched


@pytest.mark.parametrize("fails", [False, True])
def test_an_assist_that_changes_its_answers_changes_nothing_for_the_next_posting(
    conn, root, fails
):
    first, second = add_many(conn, 2)
    store_answers(conn, first_name="Alex")
    own = {first: {"how_heard": "a friend"}}
    if fails:  # the stored answers alone are a copy too
        own = {first: RuntimeError("no answers"), second: RuntimeError("no answers")}
    rig = Rig(conn, root, posting_answers=own)
    received = []

    def scribble(posting_id):
        answers = rig.assist.calls[-1]["answers"]
        received.append(dict(answers))
        answers["first_name"] = "SCRIBBLED"
        answers["extra"] = "left behind"

    rig.assist.hook = scribble
    rig.run()

    if fails:
        assert received == [{"first_name": "Alex"}, {"first_name": "Alex"}]
    else:
        assert received == [{"first_name": "Alex", "how_heard": "a friend"},
                            {"first_name": "Alex"}]
    assert stored_answers(conn) == {"first_name": "Alex"}


def test_posting_answers_that_cannot_be_read_fall_back_to_the_stored_answers(
    conn, root, capsys
):
    first, second = add_many(conn, 2)
    store_answers(conn, first_name="Alex")
    lock = sqlite3.OperationalError("database is locked")
    cfg = AutoApplyConfig(max_consecutive_failures=1)  # one counted failure would end the run
    rig = Rig(conn, root, cfg, posting_answers={first: lock, second: {"how_heard": "a friend"}})
    summary = rig.run()

    assert statuses(summary) == ["dry_run_ok", "dry_run_ok"]
    assert summary["aborted"] is False
    assert [call["answers"] for call in rig.assist.calls] == [
        {"first_name": "Alex"},  # the stored answers alone
        {"first_name": "Alex", "how_heard": "a friend"},  # the next posting is unaffected
    ]
    assert autoapply_module.load_blocked(root) == {}
    assert needs_you_headings(root) == []
    why = "OperationalError: database is locked"
    assert (
        "Posting answers could not be read for 1 posting; the stored answers alone were used: "
        f"#{first} ({why})."
    ) in digest_text(root)
    assert f"autoapply: #{first} posting answers failed, stored answers only: {why}" in (
        capsys.readouterr().out
    )


def test_a_run_whose_posting_answers_all_read_cleanly_adds_nothing_to_the_digest(conn, root):
    add(conn)
    Rig(conn, root).run()

    assert "could not be read" not in digest_text(root)


@pytest.mark.parametrize("count, listed, more", [
    (1, 1, ""), (5, 5, ""), (6, 5, "; and 1 more"), (8, 5, "; and 3 more"),
])
def test_the_digest_names_the_first_five_postings_whose_answers_failed(
    conn, root, count, listed, more
):
    ids = add_many(conn, count)
    cfg = AutoApplyConfig(dry_run_quota=20, dry_run_per_ats=20)
    summary = Rig(
        conn, root, cfg, posting_answers={pid: RuntimeError(f"bad {pid}") for pid in ids}
    ).run()

    assert summary["processed"] == ids
    assert summary["aborted"] is False
    noun = "posting" if count == 1 else "postings"
    named = "; ".join(f"#{pid} (RuntimeError: bad {pid})" for pid in ids[:listed])
    assert (
        f"Posting answers could not be read for {count} {noun}; "
        f"the stored answers alone were used: {named}{more}."
    ) in digest_text(root)


def test_a_long_posting_answers_error_is_cut_to_a_short_note(conn, root):
    pid = add(conn)
    Rig(conn, root, posting_answers={pid: RuntimeError("x" * 500 + "\nsecond line")}).run()

    note = "RuntimeError: " + "x" * 103 + "..."  # 120 characters, on one line
    assert len(note) == 120
    assert f"were used: #{pid} ({note})." in digest_text(root)


def test_posting_answers_that_are_not_a_mapping_count_as_unreadable(conn, root):
    pid = add(conn)
    store_answers(conn, first_name="Alex")
    rig = Rig(conn, root)
    summary = rig.run(posting_answers_fn=lambda connection, posting_id: None)

    assert statuses(summary) == ["dry_run_ok"]
    assert rig.assist.calls[0]["answers"] == {"first_name": "Alex"}
    assert f"the stored answers alone were used: #{pid} (TypeError: " in digest_text(root)


# -- what the assist is told, and the real seams


@pytest.mark.parametrize("armed", [False, True])
def test_the_assist_is_always_told_it_is_the_autoapply_lane(conn, root, armed):
    add_many(conn, 2)
    if armed:
        arm_now(root)
    rig = Rig(conn, root)
    rig.run()

    assert len(rig.assist.calls) == 2
    assert [call["kwargs"]["autoapply"] for call in rig.assist.calls] == [True, True]


def test_the_new_seams_are_optional_keyword_arguments():
    params = inspect.signature(autoapply_module.run_autoapply).parameters
    for name in ("posting_answers_fn", "guard_fn", "guard_blockers_fn"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None


def test_the_default_assist_hands_every_argument_on_to_fill_run_assist(monkeypatch):
    from jobhunt.apply_assist import fill

    seen = []
    monkeypatch.setattr(fill, "run_assist", lambda *a, **k: seen.append((a, k)) or "digest")
    result = autoapply_module._default_assist(
        "page", 7, "https://example.test/apply", {"first_name": "Alex"}, "/tmp/r.pdf",
        conn="conn", tailor=False, company="Acme", autoapply=True,
    )

    assert result == "digest"
    assert seen == [(
        ("page", 7, "https://example.test/apply", {"first_name": "Alex"}, "/tmp/r.pdf"),
        {"conn": "conn", "tailor": False, "company": "Acme", "autoapply": True},
    )]


def test_the_default_guard_installs_the_submit_guard_on_the_page(monkeypatch):
    installed = []
    stand_in_module(monkeypatch, "submit_guard", install=installed.append)
    page = object()

    assert autoapply_module._default_guard(page) is None
    assert installed == [page]


def test_the_default_guard_blockers_come_from_the_submit_guard(monkeypatch):
    asked = []
    stand_in_module(
        monkeypatch, "submit_guard", blockers=lambda page: asked.append(page) or [GUARD_LINE]
    )
    page = object()

    assert autoapply_module._default_guard_blockers(page) == [GUARD_LINE]
    assert asked == [page]


def test_the_default_posting_answers_come_from_the_posting_answers_module(monkeypatch, conn):
    calls = []

    def posting_answers(connection, posting_id):
        calls.append((connection, posting_id))
        return {"how_heard": "a friend"}

    stand_in_module(monkeypatch, "posting_answers", posting_answers=posting_answers)

    assert autoapply_module._default_posting_answers(conn, 7) == {"how_heard": "a friend"}
    assert calls == [(conn, 7)]


def test_a_run_without_an_injected_guard_or_answers_uses_the_real_modules(
    monkeypatch, conn, root
):
    first = add(conn)
    store_answers(conn, first_name="Alex")
    guarded = []
    stand_in_module(monkeypatch, "submit_guard", install=guarded.append)
    stand_in_module(
        monkeypatch, "posting_answers",
        posting_answers=lambda connection, posting_id: {"how_heard": f"post {posting_id}"},
    )
    rig = Rig(conn, root)
    summary = rig.run(guard_fn=None, posting_answers_fn=None)

    assert statuses(summary) == ["dry_run_ok"]
    assert guarded == rig.browser.context.pages
    assert rig.assist.calls[0]["answers"] == {"first_name": "Alex", "how_heard": f"post {first}"}
    assert rig.guard.calls == [] and rig.posting_answers.calls == []  # the fakes were bypassed


def test_a_run_without_an_injected_guard_reader_reads_the_real_submit_guard(
    monkeypatch, conn, root
):
    first, _second = add_many(conn, 2)
    asked = []
    stand_in_module(
        monkeypatch, "submit_guard", install=lambda page: None,
        blockers=lambda page: asked.append(page) or [GUARD_LINE],
    )
    rig = Rig(conn, root, assist_fail={first: TimeoutError("form never rendered")})
    summary = rig.run(guard_fn=None, guard_blockers_fn=None)

    assert summary["stop_reason"] == f"{GUARD_LINE}; run stopped after posting #{first}"
    assert asked == rig.browser.context.pages
    assert rig.guard.calls == [] and rig.guard_reads.calls == []  # the fakes were bypassed


# -- the real modules still fit what the lane passes them. These fail until each of the other
# builders' pieces (apply_assist/posting_answers.py, fill.run_assist(autoapply=...)) has landed:
# a missing piece must not degrade quietly into "stored answers only" or an unguarded fill.


def test_the_real_posting_answers_takes_the_connection_and_the_posting_id():
    from jobhunt.apply_assist import posting_answers

    inspect.signature(posting_answers.posting_answers).bind(object(), 7)


def test_the_real_guard_installs_on_a_page_alone():
    from jobhunt.apply_assist import submit_guard

    inspect.signature(submit_guard.install).bind(object())


def test_the_real_guard_reads_a_page_alone():
    from jobhunt.apply_assist import submit_guard

    inspect.signature(submit_guard.blockers).bind(object())


def test_the_real_assist_accepts_everything_the_lane_passes_it():
    from jobhunt.apply_assist import fill

    inspect.signature(fill.run_assist).bind(
        object(), 7, "https://example.test/apply", {}, "/tmp/r.pdf",
        conn=object(), tailor=False, company="Acme", autoapply=True,
    )


# --- hard-requirement filter (no model call) ----------------------------------------------------


def test_a_posting_with_a_hard_requirement_is_skipped_before_any_tailor_call(conn, root):
    jd = add(conn, desc="Minimum qualifications: a J.D. degree is required. Bar admission needed. " + AI_DESC)
    plain = add(conn)
    rig = Rig(conn, root)
    summary = rig.run()
    assert rig.tailor.calls == [plain]
    assert summary["processed"] == [plain]
    assert summary["skipped"] == {"hard_req:jd": 1}
    assert jd not in summary["processed"]


def test_the_hard_requirement_filter_can_be_switched_off_or_narrowed(conn, root):
    jd = add(conn, desc="Minimum qualifications: a J.D. degree is required. " + AI_DESC)
    off = Rig(conn, root, AutoApplyConfig(hard_req_filter=False))
    off.run(limit=1)
    assert off.tailor.calls == [jd]
    other = add(conn, desc="Minimum qualifications: a J.D. degree is required. " + AI_DESC)
    narrow = Rig(conn, root, AutoApplyConfig(hard_req_rules=frozenset({"phd"})))
    narrow.run(limit=1)
    assert narrow.tailor.calls == [other]  # the jd rule is off: not skipped


def test_the_years_floor_comes_from_the_config(conn, root):
    add(conn, desc="Requirements: 6+ years of experience in finance. " + AI_DESC)
    five = Rig(conn, root)
    assert five.run()["skipped"] == {"hard_req:years": 1}
    seven = Rig(conn, root, AutoApplyConfig(hard_req_min_years=7))
    assert seven.run(limit=1)["skipped"] == {}


def test_an_already_tailored_posting_skips_the_hard_requirement_check(conn, root):
    pid = add(conn, desc="Minimum qualifications: a J.D. degree is required. " + AI_DESC)
    make_tailored(conn, pid)
    rig = Rig(conn, root)
    summary = rig.run()
    assert summary["processed"] == [pid]
    assert summary["skipped"] == {}


# --- run-local company skips --------------------------------------------------------------------

HUMAN = "required field needs a human: Have you worked at DoorDash? (unrecognized field)"


def test_a_company_whose_form_needs_a_human_is_skipped_for_the_rest_of_the_run(conn, root):
    first, second, third = (add(conn, company="DoorDash") for _ in range(3))
    other = add(conn, company="Globex")
    rig = Rig(conn, root, AutoApplyConfig(dry_company_cap=9),
              submit={first: ("blocked", [HUMAN])})
    summary = rig.run()
    assert summary["processed"] == [first, other]
    assert summary["skipped"] == {"company_blocked_this_run": 2}
    assert rig.tailor.calls == [first, other]  # no tailor call is spent on the skipped two
    assert second not in summary["processed"] and third not in summary["processed"]


def test_another_blocker_does_not_close_the_company(conn, root):
    ids = [add(conn, company="DoorDash") for _ in range(2)]
    rig = Rig(conn, root, AutoApplyConfig(dry_company_cap=9),
              submit={ids[0]: ("blocked", ["no confirmation page seen"])})
    summary = rig.run()
    assert summary["processed"] == ids
    assert "company_blocked_this_run" not in summary["skipped"]


def test_browser_attempts_per_company_are_capped_whatever_the_outcome(conn, root):
    ids = [add(conn, company="DoorDash") for _ in range(4)]
    rig = Rig(conn, root, AutoApplyConfig(company_cap=2, dry_company_cap=9),
              submit={pid: ("blocked", ["page glitch"]) for pid in ids})
    summary = rig.run()
    assert summary["processed"] == ids[:2]
    assert summary["skipped"] == {"company_attempts_this_run": 2}
    assert len(rig.tailor.calls) == 2


def test_caps_follow_the_autoapply_preferences(monkeypatch):
    limits = eligibility.load_autoapply({"daily_cap": 7, "per_company_30d": 2})
    prefs = dataclasses.replace(PREFS, autoapply=limits)
    monkeypatch.setattr(eligibility, "user_preferences", lambda: prefs)
    cfg = AutoApplyConfig()
    assert (cfg.daily_cap, cfg.company_cap) == (7, 2)
