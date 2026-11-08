"""Unattended apply orchestrator for Greenhouse, Lever and Ashby postings.

Spec: specs/2026-10-23-autoapply.md, section C. One run takes postings from the
lane configured in `[lane]` (plus anything already tailored and at stage `ready`), tailors
the resume, opens the application form in ONE browser (one page per posting),
fills it, runs the submit gate, and, only when armed, HANDS the filled form to the candidate:
the pipeline never clicks Submit (cfg.handoff, on by default). It lifts the submit guard,
writes the `handed_off` marker and leaves the page open in its own tab (nothing waits yet). When
cfg.handoff_stack forms are open, or the loop ends, the whole stack goes to the candidate at once: one
list, one macOS notification, then a read-only wait (apply_assist/submit.py `await_handoffs`) for
them to click Submit and clear any human check themselves. A confirmation page records `submitted`; a
closed page, or no tab resolving for cfg.handoff_timeout_min, leaves `handed_off` (not confirmed
sent). The caps count a form when it is filled; its digest line and needs-you entry come after
the wait. The old click-once path is still in
apply_assist/submit.py behind cfg.handoff=False; no CLI flag turns it off. Until `autoapply arm`
the run is dry: fill, gate, screenshot, never click, never hand off.

Essays and cover letters (handoff mode only; 2026-10-27): textarea questions the answer
bank cannot fill are drafted on the Claude subscription (apply_assist/tailor_answers.py, grounded
in data/facts.local.md and the master resume) and typed into the form. Their Submit click in that
tab is their approval of them: the gate does not block on a draft in handoff mode, the handoff list
and notification name every drafted question, and every draft goes to runs/<date>/drafts.md. A
form with an AI policy on it gets no drafts (the essays go to needs-you). Click mode (handoff
False) never drafts and its gate still blocks on any draft.

State under data/autoapply/ (all of it holds the candidate's contact details, gitignored):
  state.json       dry runs recorded + armed_at/armed_note   (written here)
  blocked.json     posting_id -> {reason, at}; skipped for 7 days   (written here)
                   (gate-blocked, tailor-failed, and error-with-no-marker postings; not a
                   posting the submit guard stopped, which the next run opens again)
  YYYY-MM-DD.md    one digest per day, a section per run     (written here)
  needs-you.md     cumulative list of what the candidate must finish: postings, and runs that
                   stopped on a failure (written here)
  KILL             if present, stops a click-mode run (cfg.handoff False) before its next posting
                   (the candidate). A handoff run never clicks and needs the candidate present, so it ignores
                   KILL and says so.
  submitted/       double-apply markers, written BEFORE the click or handoff (apply_assist/submit.py);
                   status clicking | confirmed | unconfirmed | not_submitted | handed_off
  runs/            per-posting audit files                    (apply_assist/submit.py)

Every page gets the submit guard (apply_assist/submit_guard.py) before its first
navigation, dry or live: filling an Ashby form clicks buttons that the browser treats as
submit buttons, and the guard is what stops them from sending anything. A guard that
cannot be installed, or a submit_guard blocker in the outcome (or one read off a page whose
fill raised), stops the run. That stop is no verdict on the posting: it is not recorded as a
dry run and not skipped for 7 days, so it counts toward no quota and the next run opens it
again. Each posting is filled with the stored answers plus its own
(apply_assist/posting_answers.py), which win.

The tailor, submit, assist, browser, posting-answers and guard seams are injectable so
tests run with fakes and never touch a browser or the network. The real implementations
are imported lazily, so importing this module costs nothing and works before they exist.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jobhunt import applylock, eligibility
from jobhunt import finai as finai_module
from jobhunt.apply_assist import detect as detect_module
from jobhunt.apply_assist import profile_store
from jobhunt.apply_assist import submit_guard
from jobhunt.apply_assist import submitwatch
from jobhunt.hardreq import hard_requirement
from jobhunt.text_extract import extract_text

DEFAULT_ROOT = Path("data/autoapply")
TAILORED_DIR = Path("data/apply_packs/resume/tailored")
TAILORED_PDF_NAME = f"{eligibility.resume_basename()}.pdf"

ALLOWED_ATS = ("greenhouse", "lever", "ashby")
BLOCKED_DAYS = 7
# The candidate opened or pre-filled a posting themselves in this many days: they may have submitted
# it by hand in their own browser, so auto-apply leaves it alone (same events apply-queue skips).
HAND_WORKED_DAYS = 30
HAND_WORKED_ACTIONS = ("apply_lite_open", "apply_assist_fill")
GOTO_TIMEOUT_MS = 45_000
# finai.candidates slices after ranking; ask for everything so a long run of
# non-Greenhouse/Lever/Ashby postings at the top can never starve the ones below.
POOL_FETCH_LIMIT = 10_000

STATE_FILE = "state.json"
BLOCKED_FILE = "blocked.json"
NEEDS_YOU_FILE = "needs-you.md"
KILL_FILE = "KILL"
MARKER_DIR = "submitted"

DRY_STATUSES = ("dry_run_ok", "blocked")
# Count toward the daily and per-company caps. A handoff counts like a click (over-counting is the
# safe error). `not_submitted` (a human check stood after the click, nothing was sent) does not
# count, but its marker still blocks the lane from reopening the posting.
CLICKED_STATUSES = ("submitted", "unconfirmed", "handed_off")
NOT_COUNTED_MARKER_STATUSES = ("not_submitted",)
OUTCOME_STATUSES = (
    "dry_run_ok", "blocked", "submitted", "unconfirmed", "not_submitted", "handed_off", "error"
)
COUNT_KEYS = (*OUTCOME_STATUSES, "tailor_dropped", "tailor_failed")

# The status submit_application returns for a deferred handoff (apply_assist/submit.py).
HANDOFF_PENDING = "handoff_pending"

KILL_REASON = "KILL file present"
DRY_DONE_REASON = "dry run complete: review, then arm"

# apply_assist/submit_guard.py starts every blocker it reports with this.
GUARD_BLOCKER_PREFIX = "submit_guard:"
# It also reports, with the same prefix, that it could not read a page ("could not list frames",
# "could not read guard state in the main frame", "DOM layer missing in the main frame"). That is
# no stopped submit, and a closed, crashed or error page reads like that. Where a fill or a submit
# has already raised, only a submit the guard stopped stops the run.
GUARD_UNREADABLE_STARTS = ("could not ", "dom layer missing")
# Postings named one by one in the digest line about failed posting answers; the rest are counted.
ANSWER_FAILURES_LISTED = 5

_COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|lp|llp)\b"
)
_MARKER_TIME_KEYS = ("confirmed_at", "submitted_at", "clicked_at", "at", "updated_at", "created_at")

_TODO = {
    "blocked": (
        "Apply by hand at the URL: the form was not submitted. After you send it, "
        "`uv run python -m jobhunt confirm-scan` records it from the confirmation email. "
        f"Auto-apply will not retry this posting for {BLOCKED_DAYS} days."
    ),
    "unconfirmed": (
        "A submit click went out but no success page was seen, so the application may or "
        "may not have gone through. Look for a confirmation email: when it arrives, "
        "`uv run python -m jobhunt confirm-scan` records it. With no email, apply by hand. "
        "Auto-apply will never retry this posting."
    ),
    "not_submitted": (
        "Submit was clicked, but the site put a human check after the click (see the reason), "
        "so the application was NOT sent. Apply by hand at the URL. Auto-apply will not retry it."
    ),
    "handed_off": (
        "Auto-apply filled this form and handed it to you, but no confirmation page was seen. "
        "If you sent it, `uv run python -m jobhunt confirm-scan` records it from the confirmation "
        "email; if not, apply by hand at the URL. Auto-apply will not reopen it."
    ),
    "submitted": (
        "The site confirmed the application, but the database record is incomplete (see the "
        "reason). Fix that, then run `uv run python -m jobhunt confirm-scan` or confirm it "
        "in the web UI. Do not apply again."
    ),
    "error": (
        "A submit marker exists for this posting, so a click may have gone out. Check the "
        "site and your inbox before anything else; auto-apply will never reopen it."
    ),
    "dry_click": (
        "A click went out during a DRY run, which must never happen. The run was stopped. "
        "Check the site and your inbox, then fix apply_assist/submit.py before the next run."
    ),
    "submit_guard": (
        "The submit guard stopped the run: it either caught the form being submitted while "
        "auto-apply was only filling it, which a fill must never cause, or could not be put on "
        "the page. Nothing was sent. Read the reason, fix the cause (apply_assist/submit_guard.py, "
        "the fill or the form), then run again. Apply by hand at the URL if you still want this "
        "posting."
    ),
    "submit_guard_unknown": (
        "The submit guard aborted a request it does not recognise (host and path in the reason) "
        "while auto-apply was filling the form, so this posting is blocked. Nothing was sent and "
        "the run went on. If the request is harmless (analytics, telemetry), add it to the "
        "allowlist in apply_assist/submit_guard.py. Apply by hand at the URL if you still want "
        f"this posting; auto-apply will not retry it for {BLOCKED_DAYS} days."
    ),
}
# Added to an entry that already has its own instructions (a click, a marker): the guard stop is a
# second problem beside it, and nothing here may tell anyone to apply again.
_TODO_ALSO_GUARD = (
    "The submit guard also stopped the run (see the reason): it caught the form being submitted "
    "while auto-apply was only filling it, which a fill must never cause. Fix the cause "
    "(apply_assist/submit_guard.py, the fill or the form) before the next run."
)


@dataclass
class AutoApplyConfig:
    # Both come from [autoapply] in the preferences file (default 25 a day, 3 per company
    # per 30 days; hard-limited to 100 in eligibility.load_autoapply).
    daily_cap: int = field(default_factory=lambda: eligibility.user_preferences().autoapply.daily_cap)
    company_cap: int = field(
        default_factory=lambda: eligibility.user_preferences().autoapply.per_company_30d
    )
    company_window_days: int = 30
    dry_run_quota: int = 10
    dry_run_per_ats: int = 4
    pause_s: float = 20.0  # click path (handoff=False)
    # Handoff mode: the pause between postings. The pause exists to pace real submits; a handoff
    # run only fills forms, so a short one is enough.
    handoff_pause_s: float = 5.0
    max_tier: int = 4
    # Beyond the spec: stop after this many tailor failures / posting errors in a
    # row, so a logged-out claude CLI or a broken browser cannot grind through the pool.
    max_consecutive_failures: int = 5
    # Beyond the spec: stop after this many blocks in a row. A blocked posting is not a failure,
    # so without this a page change that blocks every form would tailor and block the whole pool.
    max_consecutive_blocked: int = 15
    # Dry mode only: dry attempts (dry_run_ok or blocked) allowed per company, so the
    # dry sample covers many companies and forms instead of one company's three postings.
    dry_company_cap: int = 1
    # Fresh tailor calls (model work) per run; None means daily_cap. With the pool no
    # longer sliced, this and `limit` are what bound a run's work.
    max_tailors_per_run: int | None = None
    # True: the pipeline never clicks Submit. It fills, lifts the guard and hands a visible
    # browser to the candidate, who clicks Submit and clears any check (see the header). False is the
    # old click-once path: UNUSED, kept in apply_assist/submit.py, and no CLI flag selects it.
    handoff: bool = True
    # Handoff only: minutes with no tab resolving (an idle timeout over the whole stack) before
    # every form still open is left `handed_off`.
    handoff_timeout_min: float = 60.0
    # Handoff only: filled forms kept open (one tab each) before the stack is handed to the candidate.
    handoff_stack: int = 10
    # No-model hard-requirement filter (jobhunt/hardreq.py): skip, before a tailoring call, a
    # posting that states a requirement the candidate cannot meet. Defaults from the 2026-10-26 validation
    # (scripts/validate_hardreq.py): a rule is on only with zero hits on the KEEP-labeled history.
    hard_req_filter: bool = True
    hard_req_min_years: int = 5
    hard_req_rules: frozenset[str] = frozenset(
        {"jd", "phd", "masters", "license", "domain_years", "years"}
    )


class ArmError(RuntimeError):
    """`arm` refused (too few dry runs, or no approval text)."""


@dataclass
class _Pending:
    """A filled form left open for the stack handoff."""

    cand: _Candidate
    page: Any
    audit_dir: Path | None
    # Questions of the answers typed from an AI draft on this form (handoff mode).
    drafted: list[str] = field(default_factory=list)


class _BrowserGone(RuntimeError):
    """The browser could not be opened or died; the run cannot continue."""


class _PageError(RuntimeError):
    """The application page itself failed to load (HTTP error); nothing to fill."""


class _GuardError(RuntimeError):
    """The submit guard could not be put on the new page. Nothing may be filled without it."""


@dataclass
class _Candidate:
    posting_id: int
    company: str
    title: str
    url: str
    ats: str


@dataclass
class _Submission:
    at: datetime
    auto: bool  # via autoapply (marker, or an autoapply:<id> confirmation ref)


# --- small helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _local_day(moment: datetime) -> date:
    return moment.astimezone().date()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    """Temp file + rename, so a crash never leaves half a JSON file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(text)


def _cell(value: Any) -> str:
    """One markdown table cell: no pipes, no newlines, bounded length."""
    text = " ".join(str(value).replace("|", "/").split())
    return text if len(text) <= 300 else text[:297] + "..."


def _one_line(value: Any, cap: int = 300) -> str:
    """Text on one line, bounded: an exception message can run to many lines."""
    text = " ".join(str(value).split())
    return text if len(text) <= cap else text[: cap - 3] + "..."


def _guard_blockers(blockers: list[str]) -> list[str]:
    """The submit guard's own blockers (apply_assist/submit_guard.py) among a gate's blockers."""
    return [b for b in blockers if b.lstrip().startswith(GUARD_BLOCKER_PREFIX)]


def _guard_catches(lines: list[str]) -> list[str]:
    """The guard's blockers that say it stopped a submit: not those that say it could not read the
    page."""
    return [
        line for line in _guard_blockers(lines)
        if not line.lstrip()[len(GUARD_BLOCKER_PREFIX):].strip().lower().startswith(
            GUARD_UNREADABLE_STARTS
        )
    ]


def normalize_company(name: str | None) -> str:
    """Lowercase, punctuation and legal suffixes removed, so 'Acme, Inc.' == 'acme'."""
    text = (name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    stripped = " ".join(_COMPANY_SUFFIX.sub(" ", text).split())
    return stripped or " ".join(text.split())


def _ats_for(url: str | None) -> str | None:
    ats = detect_module.detect_ats(detect_module.application_url(url or ""))
    return ats if ats in ALLOWED_ATS else None


def _tailored_pdf(posting_id: int) -> Path:
    return TAILORED_DIR / str(posting_id) / TAILORED_PDF_NAME


def _has_report(conn: sqlite3.Connection, posting_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM artifacts WHERE posting_id = ? AND kind = ? LIMIT 1",
        (posting_id, finai_module.REPORT_KIND),
    ).fetchone()
    return row is not None


# --- state files -------------------------------------------------------------


def load_state(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """state.json, normalized. A missing or corrupt file reads as disarmed with no dry runs."""
    raw = _read_json(Path(root) / STATE_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    runs = raw.get("dry_runs")
    armed_at = raw.get("armed_at")
    if not (isinstance(armed_at, str) and _parse_ts(armed_at) is not None):
        armed_at = None  # fail safe: anything but a real timestamp (`arm` writes one) is disarmed
    return {
        "dry_runs": [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else [],
        "armed_at": armed_at,
        "armed_note": str(raw.get("armed_note") or ""),
    }


def save_state(root: Path, state: dict[str, Any]) -> None:
    _write_json(Path(root) / STATE_FILE, state)


def load_blocked(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    raw = _read_json(Path(root) / BLOCKED_FILE, {})
    return raw if isinstance(raw, dict) else {}


def _blocked_recently(blocked: dict[str, Any], posting_id: int, now: datetime) -> bool:
    entry = blocked.get(str(posting_id))
    if not isinstance(entry, dict):
        return False
    at = _parse_ts(entry.get("at"))
    # An entry with no readable timestamp can't be aged out, so it stays blocked.
    return at is None or now - at < timedelta(days=BLOCKED_DAYS)


def _ats_of_posting(conn: sqlite3.Connection, posting_id: Any) -> str | None:
    if not isinstance(posting_id, int):
        return None
    row = conn.execute("SELECT url FROM postings WHERE id = ?", (posting_id,)).fetchone()
    return _ats_for(row["url"]) if row is not None else None


def _dry_counts(
    state: dict[str, Any], conn: sqlite3.Connection | None = None
) -> tuple[int, Counter[str], set[Any]]:
    """(total dry runs, per-ATS counts, posting ids). Only dry_run_ok and blocked count."""
    per_ats: Counter[str] = Counter()
    ids: set[Any] = set()
    total = 0
    for entry in state["dry_runs"]:
        if entry.get("status") not in DRY_STATUSES:
            continue
        total += 1
        ids.add(entry.get("posting_id"))
        ats = entry.get("ats")
        if not ats and conn is not None:
            ats = _ats_of_posting(conn, entry.get("posting_id"))
        per_ats[ats or "unknown"] += 1
    return total, per_ats, ids


def _needs_human_field(blocker: str) -> bool:
    """True for a gate blocker that says a required field has no answer a machine can give."""
    text = blocker.lower()
    return (
        text.startswith("required field needs a human")
        or text.startswith("required field was skipped")
        or "unrecognized field" in text
    )


def _marker_path(root: Path, posting_id: int) -> Path:
    return Path(root) / MARKER_DIR / f"{posting_id}.json"


def _mark_reason(root: Path, posting_id: int, reason: str) -> None:
    """Best effort: add why to a `handed_off` marker the run could not hand to the candidate."""
    path = _marker_path(root, posting_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update({"status": "handed_off", "reason": reason, "updated_at": _stamp(_now())})
        _write_json(path, data)
    except Exception:  # noqa: BLE001 — the marker already says handed_off
        pass


def _marker_time(path: Path, data: Any) -> datetime:
    if isinstance(data, dict):
        for key in _MARKER_TIME_KEYS:
            parsed = _parse_ts(data.get(key))
            if parsed is not None:
                return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return _now()


def _submissions(conn: sqlite3.Connection | None, root: Path) -> dict[int, _Submission]:
    """Every application known to have gone out: DB rows plus submitted/ markers.

    A marker means a click was at least attempted, so it counts whatever its status
    (clicking, confirmed, unconfirmed, handed_off): over-counting toward a cap is the safe error.
    The one exception is `not_submitted`: a human check stood after the click, so nothing was
    sent. Its marker still blocks a re-click (see `_Run._skip_reason`), but it is no submission.
    """
    found: dict[int, _Submission] = {}
    if conn is not None:
        rows = conn.execute(
            "SELECT posting_id, submitted_at, confirmation_ref FROM applications "
            "WHERE submitted_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            at = _parse_ts(row["submitted_at"])
            if at is not None:
                ref = row["confirmation_ref"] or ""
                found[row["posting_id"]] = _Submission(at, ref.startswith("autoapply:"))
    for path in sorted((Path(root) / MARKER_DIR).glob("*.json")):
        if path.stem.isdigit():
            data = _read_json(path, {})
            if isinstance(data, dict) and data.get("status") in NOT_COUNTED_MARKER_STATUSES:
                continue
            found[int(path.stem)] = _Submission(_marker_time(path, data), True)
    return found


def _company_counts(conn: sqlite3.Connection, posting_ids: list[int]) -> Counter[str]:
    """Postings per normalized company name, for the given posting ids."""
    counts: Counter[str] = Counter()
    for start in range(0, len(posting_ids), 500):
        chunk = posting_ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT company FROM postings WHERE id IN ({marks})", chunk):
            name = normalize_company(row["company"])
            if name:
                counts[name] += 1
    return counts


def _dry_posting_ids(state: dict[str, Any]) -> list[int]:
    """Postings with a recorded dry attempt that counts (dry_run_ok or blocked)."""
    return [
        entry["posting_id"]
        for entry in state["dry_runs"]
        if entry.get("status") in DRY_STATUSES and isinstance(entry.get("posting_id"), int)
    ]


def _hand_worked(conn: sqlite3.Connection, posting_id: int, now: datetime) -> bool:
    """The candidate opened or pre-filled this posting themselves lately (activity_events).

    They may have submitted it by hand in their own browser, which no marker or
    applications row records until the confirmation email is scanned in.
    """
    cutoff = (now - timedelta(days=HAND_WORKED_DAYS)).isoformat()
    marks = ",".join("?" * len(HAND_WORKED_ACTIONS))
    row = conn.execute(
        "SELECT 1 FROM activity_events WHERE entity_type = 'posting' AND entity_id = ? "
        f"AND action IN ({marks}) AND at >= ? LIMIT 1",
        (posting_id, *HAND_WORKED_ACTIONS, cutoff),
    ).fetchone()
    return row is not None


def _needs_you_count(root: Path) -> int:
    try:
        text = (Path(root) / NEEDS_YOU_FILE).read_text()
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line.startswith("## "))


# --- the pool ------------------------------------------------------------------


def _ready_pool(conn: sqlite3.Connection) -> list[_Candidate]:
    """Postings at stage `ready` that already have a tailored PDF on disk."""
    rows = conn.execute(
        """SELECT p.id, p.company, p.title, p.url
           FROM funnel f JOIN postings p ON p.id = f.posting_id
           WHERE f.stage = 'ready' AND p.status = 'active' AND p.duplicate_of IS NULL
             AND NOT EXISTS (SELECT 1 FROM applications a
                             WHERE a.posting_id = p.id AND a.submitted_at IS NOT NULL)
           ORDER BY f.updated_at, p.id"""
    ).fetchall()
    pool = []
    for row in rows:
        ats = _ats_for(row["url"])
        if ats and _tailored_pdf(row["id"]).is_file():
            pool.append(_Candidate(row["id"], row["company"] or "", row["title"] or "",
                                   row["url"] or "", ats))
    return pool


def _lane_pool(conn: sqlite3.Connection, cfg: AutoApplyConfig) -> list[_Candidate]:
    """Every lane posting on a supported ATS, in finai order. Deliberately not sliced:
    the skip filters (company cap, per-ATS dry limit, blocked, hand-worked) run later,
    so cutting the pool here would hide postings that survive them (a Lever posting at
    index 358 never reached). A run's work is bounded by `limit`, `max_tailors_per_run`
    and the consecutive-failure abort instead.

    The one cut made here is postings whose latest eligibility assessment is `blocked`
    (6+ years, say): they can never pass the readiness gate, and in the 2026-10-26 dry
    run 44% of the pool was such postings sitting at its top. Unassessed postings stay,
    since score_sweep may still assess them; `_skip_reason` keeps the full readiness
    check as the gate."""
    pool = []
    for row in finai_module.candidates(conn, limit=POOL_FETCH_LIMIT, max_tier=cfg.max_tier):
        ats = _ats_for(row["url"])
        if not ats:
            continue
        assessment = eligibility.latest_assessment(conn, row["id"])
        if assessment is not None and assessment["status"] == "blocked":
            continue
        pool.append(_Candidate(row["id"], row["company"], row["title"], row["url"], ats))
    return pool


def _candidate_pool(conn: sqlite3.Connection, cfg: AutoApplyConfig) -> list[_Candidate]:
    """Ready-and-tailored first (no model calls needed), then the finai lane in its order."""
    seen: set[int] = set()
    pool = []
    for cand in [*_ready_pool(conn), *_lane_pool(conn, cfg)]:
        if cand.posting_id not in seen:
            seen.add(cand.posting_id)
            pool.append(cand)
    return pool


# --- default seams (imported lazily: the sibling modules are built separately) ---


def _default_tailor(conn: sqlite3.Connection, posting_id: int) -> Any:
    from jobhunt import autotailor
    from jobhunt.resume import docx_resume

    # Checked before the first model call: without a converter every tailor attempt would
    # spend a call, fail at the PDF step and block the posting for days.
    docx_resume.require_pdf_converter()
    return autotailor.tailor_posting(conn, posting_id)


def _is_spend_gate_error(exc: BaseException) -> bool:
    """True for connectors.openrouter.SpendGateError (imported lazily, like the other seams).

    That includes claude_cli.SubscriptionUnavailableError: the claude CLI is signed out or
    its usage limit is hit."""
    try:
        from jobhunt.connectors.openrouter import SpendGateError
    except ImportError:
        return type(exc).__name__ == "SpendGateError"
    return isinstance(exc, SpendGateError)


def _default_assist(*args: Any, **kwargs: Any) -> Any:
    from jobhunt.apply_assist import fill

    return fill.run_assist(*args, **kwargs)  # incl. autoapply=True, which the lane always passes


def _default_posting_answers(conn: sqlite3.Connection, posting_id: int) -> dict[str, str]:
    from jobhunt.apply_assist import posting_answers

    return posting_answers.posting_answers(conn, posting_id)


def _default_guard(page: Any) -> None:
    from jobhunt.apply_assist import submit_guard

    submit_guard.install(page)


def _default_guard_blockers(page: Any) -> list[str]:
    from jobhunt.apply_assist import submit_guard

    return submit_guard.blockers(page)


def _default_submit(*args: Any, **kwargs: Any) -> Any:
    from jobhunt.apply_assist import submit

    return submit.submit_application(*args, **kwargs)


def _default_await(*args: Any, **kwargs: Any) -> Any:
    from jobhunt.apply_assist import submit

    return submit.await_handoffs(*args, **kwargs)


def _record_drafts(root: Path, digest: Any, company: str) -> list[str]:
    """Write every AI-drafted answer on this form to the run's drafts.md and return the
    questions that were typed from a draft. Best effort: a failed write must never cost
    the posting its handoff, so it only prints."""
    if not getattr(digest, "drafts", None):
        return []
    from jobhunt.apply_assist import submit

    try:
        submit.write_drafts(root, digest, company)
    except Exception as exc:  # noqa: BLE001
        print(f"autoapply: could not write drafts.md: {type(exc).__name__}: {exc}", flush=True)
    try:
        return submit.drafted_questions(digest)
    except Exception:  # noqa: BLE001
        return []


def _default_browser() -> tuple[Any, Any, bool]:
    from jobhunt.apply_assist import driver

    return driver.open_browser()


def _close_context(handle: Any, context: Any, is_cdp: bool) -> None:
    """Same rule as driver.close(), on a context: never close the candidate's own browser
    (CDP mode only disconnects); a persistent profile closes the context, then playwright."""
    if is_cdp:
        try:
            browser = context.browser
            if browser is not None:
                browser.close()
        finally:
            handle.stop()
        return
    try:
        context.close()
    finally:
        handle.stop()


# --- the run ---------------------------------------------------------------------


def run_autoapply(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    cfg: AutoApplyConfig = AutoApplyConfig(),
    root: Path = DEFAULT_ROOT,
    tailor_fn: Callable[..., Any] | None = None,
    submit_fn: Callable[..., Any] | None = None,
    assist_fn: Callable[..., Any] | None = None,
    browser_fn: Callable[[], tuple[Any, Any, bool]] | None = None,
    posting_answers_fn: Callable[[sqlite3.Connection, int], dict[str, str]] | None = None,
    guard_fn: Callable[[Any], Any] | None = None,
    guard_blockers_fn: Callable[[Any], list[str]] | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    await_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One auto-apply run. Holds the apply-session lock for its whole length.

    Raises applylock.ApplySessionActiveError if another apply session is running.
    Returns {mode, counts, processed, outcomes, skipped, stop_reason, aborted};
    `processed` lists postings that reached the browser, `aborted` is True when the
    run stopped on a failure (browser, repeated errors, model unavailable, the submit
    guard) rather than on a limit. An aborted or crashed run also leaves one `Run stopped`
    entry in needs-you.md.

    `posting_answers_fn(conn, posting_id)` returns the answers that come from the posting
    itself; they are laid over the stored answers for that posting only. `guard_fn(page)`
    puts the submit guard on a fresh page before its first navigation. `guard_blockers_fn(page)`
    reads what the guard stopped on a page whose fill or submit raised, before the page closes.
    """
    with applylock.held():
        return _Run(
            conn,
            limit=limit,
            cfg=cfg,
            root=Path(root),
            tailor_fn=tailor_fn or _default_tailor,
            submit_fn=submit_fn or _default_submit,
            assist_fn=assist_fn or _default_assist,
            browser_fn=browser_fn or _default_browser,
            posting_answers_fn=posting_answers_fn or _default_posting_answers,
            guard_fn=guard_fn or _default_guard,
            guard_blockers_fn=guard_blockers_fn or _default_guard_blockers,
            sleep_fn=sleep_fn,
            await_fn=await_fn or _default_await,
        ).execute()


class _Run:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        limit: int | None,
        cfg: AutoApplyConfig,
        root: Path,
        tailor_fn: Callable[..., Any],
        submit_fn: Callable[..., Any],
        assist_fn: Callable[..., Any],
        browser_fn: Callable[[], tuple[Any, Any, bool]],
        posting_answers_fn: Callable[[sqlite3.Connection, int], dict[str, str]],
        guard_fn: Callable[[Any], Any],
        guard_blockers_fn: Callable[[Any], list[str]],
        sleep_fn: Callable[[float], Any],
        await_fn: Callable[..., Any] = _default_await,
    ) -> None:
        self.conn = conn
        self.limit = limit
        self.cfg = cfg
        self.root = root
        self.tailor_fn = tailor_fn
        self.submit_fn = submit_fn
        self.assist_fn = assist_fn
        self.browser_fn = browser_fn
        self.posting_answers_fn = posting_answers_fn
        self.guard_fn = guard_fn
        self.guard_blockers_fn = guard_blockers_fn
        self.sleep_fn = sleep_fn
        self.await_fn = await_fn
        self.pending: list[_Pending] = []  # filled forms still open, waiting for the stack handoff

        self.started = _now()
        self.armed = False
        self.browser: tuple[Any, Any, bool] | None = None
        self.answers: dict[str, str] | None = None  # the stored answers; never changed after loading
        self.answer_failures: list[tuple[int, str]] = []  # (posting id, why) for the digest
        self.attempted = 0
        self.tailor_calls = 0
        self.tailor_cap = (
            cfg.max_tailors_per_run if cfg.max_tailors_per_run is not None else cfg.daily_cap
        )
        self.failures = 0
        self.last_failure = ""
        self.blocked_streak = 0  # browser attempts in a row that ended blocked
        self.last_blocker = ""  # first blocker of the latest blocked attempt
        self.fatal = ""
        self.stop = ""
        self.aborted = False
        self.counts: Counter[str] = Counter({key: 0 for key in COUNT_KEYS})
        self.skips: Counter[str] = Counter()
        self.processed: list[int] = []
        self.outcomes: list[dict[str, Any]] = []
        self.company_counts: Counter[str] = Counter()
        self.dry_company_counts: Counter[str] = Counter()  # dry mode: attempts, state.json + run
        # Run-local, never persisted: browser attempts per company this run (any outcome), and
        # companies whose form blocked on a question only a human can answer.
        self.company_attempts: Counter[str] = Counter()
        self.company_blocked: set[str] = set()
        self.submitted_today: set[int] = set()
        self.blocked: dict[str, Any] = {}
        self.digest_path = self.root / f"{_local_day(self.started).isoformat()}.md"

    @property
    def mode(self) -> str:
        return "live" if self.armed else "dry"

    def execute(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        state = load_state(self.root)
        self.armed = bool(state["armed_at"])
        self._digest_start()
        if self.cfg.handoff and (self.root / KILL_FILE).exists():
            print(
                "autoapply: KILL file present but ignored: this is a handoff run, which never "
                "clicks Submit",
                flush=True,
            )
        try:
            self._prepare(state)
            self._loop()
            self._resolve_pending(wait=True)  # the loop ended: hand off what is still open
        except _BrowserGone as exc:
            self.stop, self.aborted = str(exc), True
            self._resolve_pending(wait=False, reason=f"browser lost before the handoff: {exc}")
        except Exception as exc:
            self.stop, self.aborted = f"crashed: {type(exc).__name__}: {exc}", True
            self._resolve_pending(
                wait=False, reason=f"run crashed before the handoff: {type(exc).__name__}: {exc}"
            )
            raise
        finally:
            # Safety net (an interrupt, say): no pending form is left without a record, and the
            # browser closes only after the stack is resolved.
            self._resolve_pending(wait=False, reason="run ended with the form still pending")
            self._close_browser()
            self._digest_end()
            if self.aborted:  # a failure, not a limit or a KILL: nobody reads the log under launchd
                self._run_stopped()
        return {
            "mode": self.mode,
            "counts": dict(self.counts),
            "processed": self.processed,
            "outcomes": self.outcomes,
            "skipped": dict(self.skips),
            "tailor_calls": self.tailor_calls,
            "stop_reason": self.stop,
            "aborted": self.aborted,
        }

    def _prepare(self, state: dict[str, Any]) -> None:
        """What the loop needs before the first posting. Inside the try, so a DB error here is
        reported like any other crash."""
        subs = _submissions(self.conn, self.root)
        since = self.started - timedelta(days=self.cfg.company_window_days)
        self.company_counts = _company_counts(
            self.conn, [pid for pid, sub in subs.items() if sub.at >= since]
        )
        if not self.armed:
            self.dry_company_counts = _company_counts(self.conn, _dry_posting_ids(state))
        today = _local_day(self.started)
        self.submitted_today = {
            pid for pid, sub in subs.items() if sub.auto and _local_day(sub.at) == today
        }
        self.blocked = load_blocked(self.root)

    def _loop(self) -> None:
        queue = deque(_candidate_pool(self.conn, self.cfg))
        while True:
            halt = self._halt()
            if halt is not None:
                self.stop, self.aborted = halt
                return
            if not queue:
                self.stop = self._exhausted_reason()
                return
            cand = queue.popleft()
            reason = self._skip_reason(cand)
            if reason is not None:
                self.skips[reason] += 1
                continue
            if self._needs_tailor(cand) and self.tailor_calls >= self.tailor_cap:
                self.stop = f"tailor cap reached ({self.tailor_cap} tailor calls)"
                return
            self._ensure_browser()  # fail before a tailoring call is spent, not after
            resume_pdf = self._tailor(cand)
            if resume_pdf is None:
                continue
            if self.attempted:
                self.sleep_fn(self.cfg.handoff_pause_s if self.cfg.handoff else self.cfg.pause_s)
            if self._killed():  # tailoring and the pause can take minutes
                self.stop = KILL_REASON
                return
            self._attempt(cand, resume_pdf)
            if len(self.pending) >= max(1, self.cfg.handoff_stack):
                self._resolve_pending(wait=True)

    # -- stop conditions and skips

    def _killed(self) -> bool:
        """KILL stops a click-mode run. A handoff run never clicks and needs the candidate present, so
        it does not honor KILL (`execute` prints a line saying so)."""
        return not self.cfg.handoff and (self.root / KILL_FILE).exists()

    def _halt(self) -> tuple[str, bool] | None:
        """(stop reason, aborted) when the run must end before the next posting."""
        if self.fatal:
            return self.fatal, True
        if self._killed():
            return KILL_REASON, False
        state = load_state(self.root)
        if bool(state["armed_at"]) != self.armed:  # arm/disarm while running
            return "armed state changed during the run", False
        if self.armed:
            if len(self.submitted_today) >= self.cfg.daily_cap:
                return f"daily cap reached ({self.cfg.daily_cap} submissions today)", False
        elif _dry_counts(state)[0] >= self.cfg.dry_run_quota:
            return DRY_DONE_REASON, False
        if self.limit is not None and self.attempted >= self.limit:
            return f"limit reached ({self.limit} postings)", False
        if self.failures >= self.cfg.max_consecutive_failures:
            return f"{self.failures} failures in a row, last: {self.last_failure}", True
        if self.blocked_streak >= self.cfg.max_consecutive_blocked:
            return f"{self.blocked_streak} blocked in a row, last: {self.last_blocker}", True
        return None

    def _exhausted_reason(self) -> str:
        reason = "pool exhausted"
        left = self.skips.get("dry_ats_limit", 0)
        if left:
            reason += (
                f" ({left} postings left over at the per-ATS dry-run limit of "
                f"{self.cfg.dry_run_per_ats})"
            )
        return reason

    def _skip_reason(self, cand: _Candidate) -> str | None:
        pid = cand.posting_id
        if _marker_path(self.root, pid).exists():
            return "submitted_marker"
        row = self.conn.execute(
            """SELECT p.status, a.submitted_at
               FROM postings p LEFT JOIN applications a ON a.posting_id = p.id
               WHERE p.id = ?""",
            (pid,),
        ).fetchone()
        if row is None:
            return "not_active"
        if row["submitted_at"]:
            return "already_submitted"
        if _hand_worked(self.conn, pid, _now()):
            return "hand_worked"
        if _blocked_recently(self.blocked, pid, _now()):
            return "blocked_recently"
        name = normalize_company(cand.company)
        if name and self.company_counts[name] >= self.cfg.company_cap:
            return "company_cap"
        # Run-local: a company's own required questions ("Have you worked at X?", a privacy
        # acknowledgement) block every one of its forms alike, and attempts that end blocked or
        # in error count toward no cap above, so bound them here. Writes and counts nothing.
        if name and name in self.company_blocked:
            return "company_blocked_this_run"
        if name and self.company_attempts[name] >= self.cfg.company_cap:
            return "company_attempts_this_run"
        if cand.ats not in ALLOWED_ATS:
            return "ats_not_allowed"
        if row["status"] != "active":
            return "not_active"
        # The submit gate refuses any posting with a readiness blocker, so tailoring one is
        # wasted model calls and, in a dry run, a burned slot. Read-only; it already lets a
        # lone select_resume_variant through when a reviewed default variant exists. Eligibility
        # is score_sweep's to assess, so an unassessed posting waits for it. A skip writes and
        # counts nothing. Every posting with no variant linked lists select_resume_variant
        # first, so name the first blocker that says why it is really not ready.
        blockers = submitwatch.record_blockers(self.conn, pid)
        if blockers:
            why = next((b for b in blockers if b != "select_resume_variant"), blockers[0])
            return f"not_ready:{why}"
        if self.cfg.hard_req_filter and self._needs_tailor(cand):
            hit = self._hard_requirement(pid)  # no model call; an already-tailored posting costs none
            if hit is not None:
                return f"hard_req:{hit[0]}"
        if not self.armed:
            _, per_ats, done = _dry_counts(load_state(self.root), self.conn)
            if pid in done:
                return "already_dry_run"
            if name and self.dry_company_counts[name] >= self.cfg.dry_company_cap:
                return "dry_company_cap"
            if per_ats[cand.ats] >= self.cfg.dry_run_per_ats:
                return "dry_ats_limit"
        return None

    def _hard_requirement(self, pid: int) -> tuple[str, str] | None:
        """(rule, evidence) when the posting states a requirement the candidate cannot meet. Read-only."""
        row = self.conn.execute(
            "SELECT title, source, raw_json FROM postings WHERE id = ?", (pid,)
        ).fetchone()
        if row is None:
            return None
        title, description, _ = extract_text(row)
        return hard_requirement(
            title, description, min_years=self.cfg.hard_req_min_years,
            rules=self.cfg.hard_req_rules,
        )

    # -- tailoring

    def _needs_tailor(self, cand: _Candidate) -> bool:
        """True unless the tailored PDF and its match report are both already there."""
        has_pdf = _tailored_pdf(cand.posting_id).is_file()
        return not (has_pdf and _has_report(self.conn, cand.posting_id))

    def _tailor(self, cand: _Candidate) -> Path | None:
        """The tailored PDF to upload, or None when the posting is dropped or failed."""
        pdf = _tailored_pdf(cand.posting_id)
        if not self._needs_tailor(cand):
            return pdf
        self.tailor_calls += 1
        try:
            result = self.tailor_fn(self.conn, cand.posting_id)
        except Exception as exc:
            from jobhunt.resume.docx_resume import PdfConverterMissing

            if isinstance(exc, PdfConverterMissing):
                self.fatal = f"{exc}; run stopped, posting #{cand.posting_id} not blocked"
                return None
            if _is_spend_gate_error(exc):
                # Not this posting's fault and not a failure to route around: the model is
                # unavailable (the claude CLI is signed out or at its limit) or the spend gate
                # refused the call, and every later call would end the same way. Stop now;
                # block nothing, count nothing.
                self.fatal = (
                    f"LLM unavailable or refused ({exc}); run stopped, "
                    f"posting #{cand.posting_id} not blocked"
                )
                return None
            return self._tailor_failed(cand, f"{type(exc).__name__}: {exc}")
        status = str(getattr(result, "status", "") or "")
        reason = str(getattr(result, "reason", "") or "")
        if status == "tailored":
            path = Path(getattr(result, "resume_pdf", None) or pdf)
            if path.is_file():
                return path
            return self._tailor_failed(cand, f"tailor reported success but {path} is missing")
        if status == "dropped":
            self._note(cand, "tailor_dropped", reason or "dropped by the match gate")
            return None
        return self._tailor_failed(cand, reason or f"tailor returned status {status!r}")

    def _tailor_failed(self, cand: _Candidate, reason: str) -> None:
        self.failures += 1
        self.last_failure = reason
        self._note(cand, "tailor_failed", reason)
        # Skipped for BLOCKED_DAYS, so a posting the model cannot handle does not sit at the
        # top of the pool and trip the failure breaker every night.
        self._block(cand.posting_id, reason)

    def _block(self, posting_id: int, reason: str) -> None:
        """Record posting_id in blocked.json: skipped for BLOCKED_DAYS."""
        self.blocked[str(posting_id)] = {"reason": reason, "at": _stamp(_now())}
        _write_json(self.root / BLOCKED_FILE, self.blocked)

    # -- the browser stage

    def _ensure_browser(self) -> Any:
        if self.browser is None:
            try:
                self.browser = self.browser_fn()
            except Exception as exc:
                raise _BrowserGone(f"browser could not be opened: {exc}") from exc
        return self.browser[1]

    def _close_browser(self) -> None:
        if self.browser is None:
            return
        handle, context, is_cdp = self.browser
        self.browser = None
        try:
            _close_context(handle, context, is_cdp)
        except Exception as exc:
            print(f"autoapply: browser close failed: {exc}", flush=True)

    def _attempt(self, cand: _Candidate, resume_pdf: Path) -> None:
        context = self._ensure_browser()
        if self.answers is None:
            self.answers = profile_store.get_answers(self.conn)
        answers = self._answers_for(cand)
        resume_pdf = resume_pdf.resolve()  # the upload and the gate must see one absolute path
        blockers: list[str] = []
        guard_failed = False
        keep_open = False  # a deferred handoff: the page stays open until the stack is handed off
        drafted: list[str] = []  # questions typed from an AI draft (handoff mode)
        audit_dir: Path | None = None
        page = None
        try:
            try:
                page = context.new_page()
            except Exception as exc:
                raise _BrowserGone(f"browser unavailable: {exc}") from exc
            self.attempted += 1  # a posting counts once it has a page, not before
            attempt_name = normalize_company(cand.company)
            if attempt_name:
                self.company_attempts[attempt_name] += 1
            self.processed.append(cand.posting_id)
            try:
                self.guard_fn(page)  # before the first goto, dry or live: no page loads unguarded
            except Exception as exc:
                raise _GuardError(f"{type(exc).__name__}: {exc}") from exc
            response = page.goto(
                detect_module.application_url(cand.url),
                wait_until="domcontentloaded",
                timeout=GOTO_TIMEOUT_MS,
            )
            code = getattr(response, "status", None)
            if isinstance(code, int) and code >= 400:
                raise _PageError(f"HTTP {code} on the application page; the posting may be closed")
            # Essay and cover-letter drafting (subscription, grounded in the resume and the
            # facts file) is a handoff-lane feature only: the candidate's Submit click in the tab is
            # their approval of a draft. Click mode never drafts, and its call is unchanged.
            draft_lane = (
                {"tailor": True, "type_drafts": True, "audit_root": self.root}
                if self.cfg.handoff
                else {"tailor": False}
            )
            digest = self.assist_fn(
                page, cand.posting_id, cand.url, answers, str(resume_pdf),
                conn=self.conn, company=cand.company or None, autoapply=True, **draft_lane,
            )
            drafted = _record_drafts(self.root, digest, cand.company) if self.cfg.handoff else []
            extra: dict[str, Any] = {}
            if self.armed and self.cfg.handoff:
                extra = {
                    "handoff": True,
                    "handoff_timeout_s": self.cfg.handoff_timeout_min * 60,
                    "handoff_label": f"#{cand.posting_id} {cand.company} | {cand.title}",
                    "handoff_defer": True,
                }
            elif self.cfg.handoff:
                # A dry rehearsal of the handoff lane: the gate judges it as the handoff it
                # rehearses (drafts are not blockers), and nothing is clicked or handed off.
                extra = {"handoff": True}
            outcome = self.submit_fn(
                page, self.conn, digest, resume_path=resume_pdf, ats=cand.ats,
                live=self.armed, audit_root=self.root, **extra,
            )
            if extra and getattr(outcome, "status", "") == HANDOFF_PENDING:
                keep_open = True
                audit_dir = Path(getattr(outcome, "audit_dir", "") or "")
            else:
                status, reason, blockers = _read_outcome(outcome)
        except _BrowserGone:
            raise
        except _GuardError as exc:
            guard_failed = True
            status = "error"
            reason = _one_line(f"submit guard could not be installed: {exc}")
        except _PageError as exc:
            status, reason = "error", str(exc)
            reason, blockers = self._with_guard_catches(page, reason)
        except Exception as exc:
            status, reason = "error", f"{type(exc).__name__}: {exc}"
            reason, blockers = self._with_guard_catches(page, reason)
        finally:
            if page is not None and not keep_open:
                try:
                    page.close()
                except Exception:
                    pass
        if keep_open:
            self._pend(cand, page, audit_dir, drafted)
            return
        self._note(cand, status, reason)
        if guard_failed:
            self._guard_failed(cand, reason)
        else:
            self._after_attempt(cand, status, reason, blockers)

    def _pend(
        self, cand: _Candidate, page: Any, audit_dir: Path | None, drafted: list[str] | None = None
    ) -> None:
        """A filled, marked form left open for the stack handoff. Its caps count now, so the daily
        and per-company limits hold while the stack fills; the digest line and the needs-you entry
        wait for the final status."""
        self.pending.append(_Pending(cand, page, audit_dir, list(drafted or [])))
        self.failures = 0
        self.blocked_streak = 0
        self.submitted_today.add(cand.posting_id)
        name = normalize_company(cand.company)
        if name:
            self.company_counts[name] += 1
        print(
            f"autoapply: #{cand.posting_id} {cand.company} | {cand.title} filled, "
            f"waiting in the stack ({len(self.pending)}/{max(1, self.cfg.handoff_stack)})",
            flush=True,
        )

    def _resolve_pending(self, *, wait: bool, reason: str = "") -> None:
        """Hand the pending stack to the candidate (`wait`) or, when the browser is gone or the run is
        dying, record it as `handed_off` without waiting. Every item gets its note, its needs-you
        entry and a closed page; none is lost. Never raises, except to pass on an interrupt."""
        items, self.pending = self.pending, []
        if not items:
            return
        outcomes: dict[int, Any] = {}
        interrupt: BaseException | None = None
        if wait:
            try:
                from jobhunt.apply_assist.submit import PendingHandoff

                outcomes = self.await_fn(
                    [
                        PendingHandoff(
                            page=item.page, posting_id=item.cand.posting_id,
                            audit_dir=item.audit_dir or Path("."), audit_root=self.root,
                            label=f"#{item.cand.posting_id} {item.cand.company} | {item.cand.title}",
                            drafted=list(item.drafted),
                        )
                        for item in items
                    ],
                    self.conn,
                    timeout_s=self.cfg.handoff_timeout_min * 60,
                ) or {}
            except BaseException as exc:  # noqa: BLE001 — every item must still be recorded
                outcomes = {}
                reason = f"the handoff wait failed: {type(exc).__name__}: {exc}"
                if not isinstance(exc, Exception):
                    interrupt = exc
        for item in items:
            cand = item.cand
            outcome = outcomes.get(cand.posting_id)
            try:
                if outcome is None:
                    why = reason or "no outcome came back from the handoff"
                    _mark_reason(self.root, cand.posting_id, why)
                    status, reason_text, blockers = _read_outcome(
                        SimpleNamespace(status="handed_off", blockers=[why])
                    )
                else:
                    status, reason_text, blockers = _read_outcome(outcome)
                self._note(cand, status, reason_text)
                self._after_attempt(cand, status, reason_text, blockers, counted=True)
            except Exception as exc:  # noqa: BLE001 — keep recording the others
                print(f"autoapply: #{cand.posting_id} could not be recorded: {exc}", flush=True)
            try:
                item.page.close()
            except Exception:  # noqa: BLE001 — best effort
                pass
        if interrupt is not None:
            raise interrupt

    def _with_guard_catches(self, page: Any, reason: str) -> tuple[str, list[str]]:
        """(reason, blockers) for an attempt that raised. A submit the guard stopped can be what
        made the fill raise, so what the guard holds is read before the page closes; its words then
        come first, and the attempt takes the same stop as a guard blocker in an outcome. Reading
        the guard is best effort: a failure there changes nothing."""
        if page is None:
            return reason, []
        try:
            caught = _guard_catches([str(line) for line in self.guard_blockers_fn(page) or []])
        except Exception:
            return reason, []
        if not caught:
            return reason, []
        blockers = [*caught, reason]
        return _one_line("; ".join(blockers)), blockers

    def _answers_for(self, cand: _Candidate) -> dict[str, str]:
        """The stored answers with this posting's own answers laid over them (theirs win), as a
        new dict: self.answers is never changed, so nothing leaks from one posting to the next.
        When the posting's answers cannot be read, the stored answers alone are used and the
        digest says so: the form is then likelier to come out blocked, but the run goes on."""
        stored = self.answers or {}
        try:
            return {**stored, **self.posting_answers_fn(self.conn, cand.posting_id)}
        except Exception as exc:
            why = _one_line(f"{type(exc).__name__}: {exc}", 120)
            self.answer_failures.append((cand.posting_id, why))
            print(
                f"autoapply: #{cand.posting_id} posting answers failed, stored answers only: {why}",
                flush=True,
            )
            return dict(stored)

    def _guard_failed(self, cand: _Candidate, reason: str) -> None:
        """The guard would not go on the page, so the form was never opened. Nothing may be
        filled without it, and a guard that fails once fails on the next posting too: stop the
        run. Like an unavailable model, that is not this posting's fault: it is not blocked and
        counts as no failure."""
        self.fatal = f"{reason}; run stopped, posting #{cand.posting_id} not blocked"
        self._needs_you(cand, "error", reason, todo="submit_guard")

    # -- recording

    def _note(self, cand: _Candidate, status: str, reason: str) -> None:
        self.counts[status] += 1
        self.outcomes.append({
            "posting_id": cand.posting_id, "company": cand.company, "title": cand.title,
            "ats": cand.ats, "status": status, "reason": reason,
        })
        _append(
            self.digest_path,
            f"| {cand.posting_id} | {_cell(cand.company)} | {_cell(cand.title)} | {cand.ats} "
            f"| {status} | {_cell(reason)} |\n",
        )
        tail = f": {reason}" if reason else ""
        print(
            f"autoapply: #{cand.posting_id} {cand.company} | {cand.title} [{cand.ats}] "
            f"{status}{tail}",
            flush=True,
        )

    def _after_attempt(
        self, cand: _Candidate, status: str, reason: str, blockers: list[str],
        *, counted: bool = False,
    ) -> None:
        """`counted`: the caps were already counted when the form was filled (`_pend`)."""
        pid = cand.posting_id
        guard = _guard_blockers(blockers)
        # An unknown request the guard aborted (nothing was sent) blocks this posting only; a
        # recognised submit, a DOM-layer catch or an unreadable page stops the run.
        unknown = [b for b in guard if submit_guard.is_unknown_abort(b)]
        stops = [b for b in guard if b not in unknown]
        marked = _marker_path(self.root, pid).exists()
        if status == "error":
            self.failures += 1
            self.last_failure = reason
        else:
            self.failures = 0
        # Only a browser attempt gets here: a tailor drop or failure never reaches this method,
        # so it neither extends nor resets the streak. Any status but blocked ends it.
        if status == "blocked":
            self.blocked_streak += 1
            self.last_blocker = blockers[0] if blockers else reason
        else:
            self.blocked_streak = 0
        name = normalize_company(cand.company)
        if name and status == "blocked" and any(_needs_human_field(b) for b in blockers):
            self.company_blocked.add(name)
        if name and status == "not_submitted" and not counted:
            # A human check after the click is the site's verdict, not a form problem: like the
            # caps above it takes no slot, so the per-run attempt cap gives it back.
            self.company_attempts[name] = max(0, self.company_attempts[name] - 1)
        # A guard stop is no verdict on the posting: a form that tried to submit while it was only
        # being filled points at the fill, the guard or the form's script. It is not recorded as a
        # dry run and not blocked, in either mode, so it counts toward no dry-run quota and sits
        # out no days: the run stops below, and the next one opens the posting again.
        if status in DRY_STATUSES and not self.armed and not guard:
            state = load_state(self.root)  # re-read: arm/disarm may have written meanwhile
            state["dry_runs"].append(
                {"posting_id": pid, "at": _stamp(_now()), "status": status, "ats": cand.ats}
            )
            save_state(self.root, state)
            if name:
                self.dry_company_counts[name] += 1
        # blocked.json keeps a posting out of the pool for BLOCKED_DAYS. An error with a
        # marker is different: the marker itself skips it forever (and it goes to needs-you).
        clicked = status in CLICKED_STATUSES  # counts toward the caps
        went_out = clicked or status == "not_submitted"  # a click (or handoff) happened
        if not stops and (
            status == "blocked" or (status == "error" and not marked)
            or (unknown and not clicked and not marked)
        ):
            self._block(pid, _one_line("; ".join(unknown), 300) if unknown else reason)
        if clicked:
            self.submitted_today.add(pid)
            if name and not counted:
                self.company_counts[name] += 1
        if went_out and not self.armed:
            # A click in a dry run is a submitter bug: stop the run and say so.
            self.fatal = f"{status} reported in dry mode; check apply_assist/submit.py"
            self._needs_you(cand, status, reason, todo="dry_click", also_guard=bool(stops))
        elif guard and not went_out and not (status == "error" and marked):
            # No click went out, whatever the status says (dry_run_ok included), so the guard's
            # own instructions stand alone. Its words come first: the gate may list many other
            # blockers. A click or a marker keeps its own instructions below (what to check by
            # hand matters more), with the guard's added.
            rest = [b for b in blockers if b not in guard]
            self._needs_you(
                cand, status, _one_line("; ".join(guard + rest), 600),
                todo="submit_guard" if stops else "submit_guard_unknown",
            )
        elif status in ("blocked", "unconfirmed", "not_submitted", "handed_off") or (
            status == "submitted" and blockers
        ):
            self._needs_you(cand, status, reason, also_guard=bool(stops))
        elif status == "error" and marked:
            self._needs_you(cand, status, reason, also_guard=bool(stops))
        if stops and not self.fatal:
            # A form that tried to submit during a fill is a bug in the fill or in the guard,
            # in dry and live mode alike. Whatever else the outcome says, no further posting
            # is filled until someone has looked. (An unknown request the guard aborted does
            # not stop the run: it blocked this posting above, and the other postings go on.)
            self.fatal = f"{_one_line(stops[0], 200)}; run stopped after posting #{pid}"

    def _needs_you(
        self, cand: _Candidate, status: str, reason: str, *, todo: str | None = None,
        also_guard: bool = False,
    ) -> None:
        """One entry per posting. `also_guard` adds the guard's stop to an entry that has its own
        instructions, so a click beside a guard blocker is still one entry that says both."""
        local = self.started.astimezone().strftime("%Y-%m-%d %H:%M")
        what = _TODO[todo or status]
        if also_guard:
            what = f"{what} {_TODO_ALSO_GUARD}"
        # One line: an entry per heading, whatever the text. A blocker or an exception message can
        # carry page text with line breaks, and a line that starts with "## " would be a forged entry.
        reason = " ".join(str(reason).split())
        self._append_needs_you(
            f"## #{cand.posting_id} {cand.company} | {cand.title}\n"
            f"- Run: {local} ({self.mode})\n"
            f"- Status: {status}\n"
            f"- URL: {detect_module.application_url(cand.url)}\n"
            f"- Reason: {reason}\n"
            f"- What you do: {what}\n\n"
        )

    def _run_stopped(self) -> None:
        """One entry for a run that ended on a failure (browser, repeated errors, model
        unavailable, crash). It says nothing about what was blocked: that depends on the stop,
        and the reason already says so where it matters."""
        local = _now().astimezone().strftime("%Y-%m-%d %H:%M")
        reason = " ".join(self.stop.split())  # one line: an entry per heading, whatever the text
        self._append_needs_you(
            f"## Run stopped {local} ({self.mode})\n"
            f"- Reason: {reason}\n"
            "- What you do: Fix the cause; the next scheduled run retries.\n\n"
        )

    def _append_needs_you(self, entry: str) -> None:
        path = self.root / NEEDS_YOU_FILE
        head = ""
        if not path.exists():
            head = (
                "# Needs you (autoapply)\n\nPostings auto-apply could not finish. "
                "Delete an entry once you have handled it.\n\n"
            )
        _append(path, head + entry)

    # -- digest

    def _digest_start(self) -> None:
        text = ""
        if not self.digest_path.exists():
            text = f"# Autoapply digest {_local_day(self.started).isoformat()}\n\n"
        local = self.started.astimezone().strftime("%H:%M")
        text += (
            f"## Run {local} ({self.mode})\n\n"
            "| id | company | title | ATS | status | reason |\n|---|---|---|---|---|---|\n"
        )
        _append(self.digest_path, text)

    def _digest_end(self) -> None:
        line = (
            f"\nStopped: {self.stop or 'finished'}. Attempted {self.attempted}. "
            f"Tailor calls {self.tailor_calls}."
        )
        if self.skips:
            line += " Skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(self.skips.items()))
            line += "."
        line += self._answer_failures_line()
        _append(self.digest_path, line + "\n\n")

    def _answer_failures_line(self) -> str:
        """What the digest says about postings whose own answers could not be read ('' if none)."""
        failed = self.answer_failures
        if not failed:
            return ""
        shown = "; ".join(f"#{pid} ({why})" for pid, why in failed[:ANSWER_FAILURES_LISTED])
        more = len(failed) - ANSWER_FAILURES_LISTED
        rest = f"; and {more} more" if more > 0 else ""
        noun = "posting" if len(failed) == 1 else "postings"
        return (
            f"\n\nPosting answers could not be read for {len(failed)} {noun}; "
            f"the stored answers alone were used: {shown}{rest}."
        )


def _read_outcome(outcome: Any) -> tuple[str, str, list[str]]:
    """(status, reason, blockers) from a SubmitOutcome; anything unexpected becomes an error."""
    status = str(getattr(outcome, "status", "") or "")
    blockers = [str(b) for b in (getattr(outcome, "blockers", None) or [])]
    joined = "; ".join(blockers)
    ref = getattr(outcome, "confirmation_ref", None)
    if status == "dry_run_ok":
        return status, "gate passed; nothing clicked (dry run)", blockers
    if status == "blocked":
        return status, joined or "blocked by the submit gate (no reason given)", blockers
    if status == "submitted":
        if blockers:
            return status, f"submitted; record incomplete: {joined}", blockers
        return status, f"confirmed ({ref})" if ref else "confirmed", blockers
    if status == "unconfirmed":
        reason = "submit click sent, no success page seen"
        return status, f"{reason}; {joined}" if joined else reason, blockers
    if status == "not_submitted":
        reason = "submit clicked, but a human check stood after the click; nothing was sent"
        return status, f"{reason}; {joined}" if joined else reason, blockers
    if status == "handed_off":
        reason = "handed to you; no confirmation page seen"
        return status, f"{reason}; {joined}" if joined else reason, blockers
    if status == "error":
        return status, joined or "submitter reported an error", blockers
    return "error", f"unexpected submit status {status!r}", blockers


# --- status / arm / disarm (the CLI is a thin wrapper over these) --------------------


def status_summary(
    root: Path = DEFAULT_ROOT,
    *,
    cfg: AutoApplyConfig = AutoApplyConfig(),
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Mode and counters from the state files alone (no DB needed; `conn` only sharpens counts)."""
    root = Path(root)
    state = load_state(root)
    total, per_ats, _ = _dry_counts(state, conn)
    today = _local_day(_now())
    subs = _submissions(conn, root)
    return {
        "armed": bool(state["armed_at"]),
        "armed_at": state["armed_at"],
        "armed_note": state["armed_note"],
        "dry_runs": total,
        "dry_run_quota": cfg.dry_run_quota,
        "dry_run_per_ats": cfg.dry_run_per_ats,
        "dry_runs_by_ats": {ats: per_ats.get(ats, 0) for ats in ALLOWED_ATS}
        | {ats: n for ats, n in per_ats.items() if ats not in ALLOWED_ATS},
        "submitted_today": sum(
            1 for sub in subs.values() if sub.auto and _local_day(sub.at) == today
        ),
        "daily_cap": cfg.daily_cap,
        "kill": (root / KILL_FILE).exists(),
        "handoff": cfg.handoff,
        "needs_you": _needs_you_count(root),
    }


def format_status(info: dict[str, Any]) -> str:
    if info["armed"]:
        note = f" ({info['armed_note']})" if info["armed_note"] else ""
        mode = f"mode: live, armed {info['armed_at']}{note}"
    else:
        mode = "mode: dry (not armed)"
    per_ats = ", ".join(
        f"{ats} {n}/{info['dry_run_per_ats']}" for ats, n in info["dry_runs_by_ats"].items()
    )
    if not info["kill"]:
        kill = "absent"
    elif info.get("handoff", True):
        kill = (
            "PRESENT, but ignored: handoff runs never click Submit and need you present "
            "(it only stops click-mode runs)"
        )
    else:
        kill = "PRESENT, runs stop before their next posting"
    return "\n".join([
        mode,
        f"dry runs: {info['dry_runs']}/{info['dry_run_quota']} ({per_ats})",
        f"submitted today: {info['submitted_today']}/{info['daily_cap']}",
        f"KILL file: {kill}",
        f"needs-you entries: {info['needs_you']}",
    ])


def arm(
    root: Path = DEFAULT_ROOT,
    *,
    approved: str,
    cfg: AutoApplyConfig = AutoApplyConfig(),
) -> dict[str, Any]:
    """Switch to live mode. Refuses without approval text or before the dry-run quota is met."""
    note = approved.strip()
    if not note:
        raise ArmError("--approved needs your approval text; nothing was changed")
    state = load_state(root)
    total, _, _ = _dry_counts(state)
    if total < cfg.dry_run_quota:
        raise ArmError(
            f"only {total} of {cfg.dry_run_quota} dry runs recorded; "
            "run `autoapply run` in dry mode and review the digest first"
        )
    state["armed_at"] = _stamp(_now())
    state["armed_note"] = note
    save_state(root, state)
    return state


def disarm(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    state = load_state(root)
    state["armed_at"] = None
    save_state(root, state)
    return state


def format_summary(summary: dict[str, Any], root: Path = DEFAULT_ROOT) -> str:
    counts = summary["counts"]
    parts = [f"{key} {counts.get(key, 0)}" for key in COUNT_KEYS if counts.get(key)]
    skipped = summary["skipped"]
    lines = [
        f"autoapply run ({summary['mode']}): {len(summary['processed'])} reached the browser"
        + (f" ({', '.join(parts)})" if parts else ""),
        f"stopped: {summary['stop_reason'] or 'finished'}",
    ]
    if summary.get("tailor_calls"):
        lines.append(f"tailor calls: {summary['tailor_calls']}")
    if skipped:
        lines.append(
            "skipped: " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items()))
        )
    lines.append(f"digest and needs-you list: {root}/")
    return "\n".join(lines)
