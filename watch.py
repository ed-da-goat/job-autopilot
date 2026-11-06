"""30-minute new-posting watcher: board sweep -> score new -> ntfy push.
Thin — implementation lives in jobhunt/watch.py. Mirrors daily.py's style.

    make watch              # one tick: harvest gh/lever/ashby, score new, push
    make watch-dry          # --no-harvest, prints pushes instead of sending
    uv run python watch.py [--no-harvest] [--print] [--min-score N]

Push target: JOBHUNT_NTFY_TOPIC or data/ntfy_topic.local (see README).
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

from jobhunt import db as db_module
from jobhunt import runlog
from jobhunt.rubric import load_rubric
from jobhunt.watch import (
    DEFAULT_MIN_SCORE,
    WatchTickActiveError,
    daily_in_flight,
    format_result,
    ntfy_push,
    print_push,
    run_watch,
    tick_lock,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch ATS boards for new high-fit postings and push to phone.")
    parser.add_argument("--no-harvest", action="store_true", help="Skip the network sweep; score/push from the existing DB.")
    parser.add_argument("--print", action="store_true", help="Print pushes instead of sending them to ntfy.")
    parser.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE, help=f"Push threshold (default {DEFAULT_MIN_SCORE}).")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        with tick_lock():
            return _tick(args)
    except WatchTickActiveError:
        print("watch: another tick is running; skipped")
        return 0


def _tick(args: argparse.Namespace) -> int:
    rubric = load_rubric()
    conn = db_module.connect()
    try:
        if daily_in_flight(conn, datetime.now(UTC)):
            print("watch: daily run in flight; skipped")
            return 0
        with runlog.track(conn, "watch") as run:
            result = run_watch(
                conn,
                rubric,
                min_score=args.min_score,
                harvest=not args.no_harvest,
                pusher=print_push if args.print else ntfy_push,
            )
            run.note(f"new={result.new_postings} hits={len(result.hits)} pushed={result.pushed}")
        print(format_result(result))
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc):
            raise
        print("watch: database busy; will retry next tick")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
