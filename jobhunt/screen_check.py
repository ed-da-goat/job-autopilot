"""Measure the Haiku screen against the reports Sonnet already made.

    uv run python -m jobhunt.screen_check [--limit N] [--pid ID]

For every autotailor report that Sonnet wrote (a tailored resume or a drop; never a
drop the Haiku screen itself recorded), run `autotailor.screen_posting` over the same
posting and print one line comparing the two decisions. The summary answers the two
questions that decide whether the screen can stay on:

- Kept postings Haiku would drop. Each is a posting Sonnet tailored and the screen
  would have thrown away. Must be 0.
- Sonnet drops Haiku catches. Each is a Sonnet call the screen saves. The target is
  at least half of them (12 of the 23 in run 1).

Writes nothing: no report, no file, no DB row. The DB is opened read-only and every
statement runs under query_only. The only model is Haiku, called through `complete`,
which defaults to claude_cli.complete: subscription only, it refuses to run on a
metered key. Never set SPEND_OK for this. It exits 0 whatever the numbers say (a
partial run included, when the subscription gate stops it) and 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from jobhunt import autotailor, db
from jobhunt.connectors import claude_cli
from jobhunt.connectors.openrouter import SpendGateError

_REPORT_NAME = re.compile(r"([0-9]+)\.report\.json")
_SONNET_DROP = re.compile(r"\s*dropped\b", re.I)  # finai's tailor and drop notes both say so
REASON_CAP = 200  # characters of an error or drop reason printed on a line


@dataclass
class Prior:
    """What Sonnet's run decided about one posting, read from its report."""

    posting_id: int
    decision: str  # "drop" | "keep"
    score: int | None  # the report's match_before


@dataclass
class _Tally:
    checked: int = 0  # postings the screen was run on, answered or not
    calls: int = 0
    failures: int = 0  # no usable answer: left out of the four counts below
    kept: int = 0  # Sonnet kept, and Haiku gave an answer
    kept_lost: int = 0  # ...and Haiku would have dropped it
    dropped: int = 0  # Sonnet dropped, and Haiku gave an answer
    dropped_caught: int = 0  # ...and Haiku drops it too
    stopped: str = ""  # why the run ended early, if it did


def load_priors(finai_dir: Path) -> tuple[list[Prior], int]:
    """The Sonnet-made autotailor reports in `finai_dir`, by posting id, and how many
    other `<id>.report.json` files were left out: another source, a report the Haiku
    screen recorded, unreadable or without notes. Files not named like a posting's
    report are not counted."""
    priors: list[Prior] = []
    skipped = 0
    for path in finai_dir.glob("*.report.json"):
        match = _REPORT_NAME.fullmatch(path.name)
        if match is None:
            continue
        prior = _read_prior(path, int(match.group(1)))
        if prior is None:
            skipped += 1
        else:
            priors.append(prior)
    priors.sort(key=lambda prior: prior.posting_id)
    return priors, skipped


def _read_prior(path: Path, posting_id: int) -> Prior | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("source") != autotailor.SOURCE:
        return None
    notes = report.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        return None
    if notes.lstrip().lower().startswith(autotailor.SCREEN_NOTES):
        return None  # the screen's own drop: nothing of Sonnet's to compare it with
    score = report.get("match_before")
    if isinstance(score, bool) or not isinstance(score, int):
        score = None
    return Prior(posting_id, "drop" if _SONNET_DROP.match(notes) else "keep", score)


def main(
    argv: list[str] | None = None,
    *,
    complete: Callable[..., Any] | None = None,
    conn: sqlite3.Connection | None = None,
    out: TextIO | None = None,
) -> int:
    """Run the check. `complete` is the model backend (tests inject a fake; the
    default is claude_cli.complete, resolved at call time), `conn` the DB (default:
    the job-hunt DB, opened read-only) and `out` where the report goes (default
    stdout). Returns 0, or 2 for bad arguments."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:  # argparse: 2 for bad arguments, 0 for --help
        return exit_.code if isinstance(exit_.code, int) else 2
    stream = out if out is not None else sys.stdout

    def say(line: str = "") -> None:
        print(line, file=stream, flush=True)  # a call takes seconds: show each line as it lands

    priors, skipped = load_priors(autotailor.FINAI_DIR)
    if args.pid is not None:
        priors = [prior for prior in priors if prior.posting_id == args.pid]
    if args.limit is not None:
        priors = priors[: args.limit]
    if not priors:
        say("nothing to check: no Sonnet-made autotailor reports" + (" for that posting" if args.pid else ""))
        return 0
    try:
        master = autotailor._load_master()
    except Exception as exc:  # the tool never fails: say why and stop
        say(f"stopped: {exc}")
        return 0

    owned = conn is None
    if conn is None:
        try:
            conn = _open_readonly(db.DEFAULT_DB_PATH)
        except sqlite3.Error as exc:
            say(f"stopped: cannot open the job-hunt database read-only ({type(exc).__name__})")
            return 0
    backend = complete if complete is not None else claude_cli.complete
    try:
        with _query_only(conn):
            tally = _check(conn, priors, backend, master, say)
    finally:
        if owned:
            conn.close()
    _summary(tally, len(priors), skipped, say)
    return 0


def _check(
    conn: sqlite3.Connection,
    priors: list[Prior],
    backend: Callable[..., Any],
    master: dict,
    say: Callable[[str], None],
) -> _Tally:
    tally = _Tally()
    for prior in priors:
        try:
            result = autotailor.screen_posting(conn, prior.posting_id, complete=backend, master=master)
        except SpendGateError as exc:
            # Cannot prove subscription auth, or the usage limit is hit: every later call fails the same way.
            tally.stopped = _cap(f"{type(exc).__name__}: {exc}")
            break
        except Exception as exc:  # a bug in one posting must not hide the other results
            reason = _cap(f"unexpected {type(exc).__name__}: {exc}")
            result = autotailor.ScreenResult(prior.posting_id, "error", reason=reason)
        tally.checked += 1
        tally.calls += result.calls
        verdict = _verdict(prior, result)
        say(_line(prior, result, verdict))
        if verdict == "error":
            tally.failures += 1
        elif verdict in ("agree", "LOST"):
            tally.kept += 1
            if verdict == "LOST":
                tally.kept_lost += 1
                say(f"    why: {_cap(result.reason)}")  # the case a person has to look at
        else:
            tally.dropped += 1
            if verdict == "caught":
                tally.dropped_caught += 1
    return tally


def _verdict(prior: Prior, result: autotailor.ScreenResult) -> str:
    if result.decision == "error":
        return "error"
    if prior.decision == "keep":
        return "LOST" if result.decision == "drop" else "agree"
    return "caught" if result.decision == "drop" else "missed"


def _line(prior: Prior, result: autotailor.ScreenResult, verdict: str) -> str:
    head = f"pid={prior.posting_id} sonnet={prior.decision}({_score(prior.score)})"
    if result.decision == "error":
        return f"{head} haiku=error reason={_cap(result.reason)}"
    haiku = f"{result.decision}({_score(result.match_before)})"
    return f"{head} haiku={haiku} mismatch={result.hard_mismatch or '-'} {verdict}"


def _summary(tally: _Tally, total: int, skipped: int, say: Callable[[str], None]) -> None:
    target = (tally.dropped + 1) // 2  # at least half of the Sonnet drops
    say()
    say(f"checked {tally.checked} of {total} reports" + (f" ({skipped} other report files skipped)" if skipped else ""))
    say(
        f"kept postings Haiku would drop: {tally.kept_lost} of {tally.kept} "
        f"(must be 0): {_met(tally.kept_lost == 0, tally.kept)}"
    )
    say(
        f"Sonnet drops Haiku catches: {tally.dropped_caught} of {tally.dropped} "
        f"(target at least {target}): {_met(tally.dropped_caught >= target, tally.dropped)}"
    )
    failed = " (no usable answer: left out of the counts above)" if tally.failures else ""
    say(f"model calls: {tally.calls}; failures: {tally.failures}{failed}")
    if tally.stopped:
        say(f"stopped early, after {tally.checked} of {total} reports: {tally.stopped}")


def _met(ok: bool, of: int) -> str:
    if of == 0:
        return "no data"  # an empty set proves nothing either way
    return "ok" if ok else "NOT MET"


def _score(score: int | None) -> str:
    return "-" if score is None else str(score)


def _cap(text: str) -> str:
    return " ".join(str(text).split())[:REASON_CAP]


def _open_readonly(path: Path | str) -> sqlite3.Connection:
    """The job-hunt DB opened read-only. db.connect() creates the schema and runs
    migrations, which this tool must never do, and mode=ro never creates the file."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _query_only(conn: sqlite3.Connection) -> Iterator[None]:
    """No statement on `conn` may write while the check runs. The caller's setting
    comes back afterwards, so an injected connection is left as it was."""
    before = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only=ON")
    try:
        yield
    finally:
        conn.execute(f"PRAGMA query_only={int(before)}")


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobhunt.screen_check",
        description=(
            "Run the Haiku screen over the postings Sonnet already decided and compare. "
            "Writes nothing; Haiku calls run on the Claude subscription."
        ),
    )
    parser.add_argument("--limit", type=_positive, metavar="N", help="check only the first N reports, by posting id")
    parser.add_argument("--pid", type=_positive, metavar="ID", help="check only this posting")
    return parser


if __name__ == "__main__":
    sys.exit(main())
