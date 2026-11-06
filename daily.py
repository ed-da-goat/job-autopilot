"""Daily automation: full sweep -> rescore -> suggest -> digest -> notify.
Thin — implementations live in jobhunt/. Mirrors resweep.py's style.

    make daily                          # full run (network harvest + notify)
    make daily-dry                      # --no-harvest dry run, no notification
    uv run python daily.py [--auto-prep] [--no-harvest]

`--auto-prep` runs free on a configured local model (JOBHUNT_LLM_BASE_URL
pointed at localhost), or on OpenRouter with SPEND_OK=1 + OPENROUTER_API_KEY
set in the environment — launchd's scheduled run can safely carry the local
env vars since local calls never spend; a non-local run without both opt-in
vars still no-ops (see README).
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime

from jobhunt import db as db_module
from jobhunt import dedupe as dedupe_module
from jobhunt.autopilot import run_autopilot
from jobhunt import digest as digest_module
from jobhunt import discover as discover_module
from jobhunt import expand as expand_module
from jobhunt import notify as notify_module
from jobhunt import runlog
from jobhunt.connectors import openrouter as openrouter_module
from jobhunt.harvest import run_harvest
from jobhunt.rubric import load_rubric
from jobhunt.score_sweep import run_score_sweep
from jobhunt.spend import SpendCapError
from jobhunt.suggest import run_suggest, run_weekly_fallback_fill

AUTO_PREP_LIMIT = 15
AUTO_PREP_MIN_SCORE = 70

# Bound daily auto-discovery probe spend: each unit is one live HTTP probe
# against a candidate ATS board, sent politely (~1 req/sec). Worst case
# 100 + 150 = 250 probes ≈ 4 minutes — acceptable inside an unattended
# daily run that also harvests, scores, and digests.
DAILY_EXPAND_PROBE_LIMIT = 100
DAILY_DISCOVER_PROBE_LIMIT = 150


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily job-hunt automation: sweep, suggest, digest, notify."
    )
    parser.add_argument(
        "--auto-prep",
        action="store_true",
        help="Generate application packets for up to 15 'suggested' postings scoring >=70 "
        "(runs free on a configured local model, or on OpenRouter with "
        "SPEND_OK=1 + OPENROUTER_API_KEY; no-ops otherwise).",
    )
    parser.add_argument(
        "--no-harvest",
        action="store_true",
        help="Dry run: skip network harvest/rescore, just suggest-from-existing + digest, "
        "printing the notify decision instead of firing a notification.",
    )
    return parser.parse_args()


def _auto_prep(conn) -> list[str]:
    """Generate up to AUTO_PREP_LIMIT packets (tailoring+cover+outreach) for
    the highest-scoring non-simplify 'suggested' postings. Returns note
    lines for the digest/log. Never raises — guards its own import so
    daily.py still runs before jobhunt.prep exists, and stops (not crashes)
    at the daily spend cap."""
    notes: list[str] = []
    if not (
        openrouter_module.is_local_configured()
        or (os.environ.get("SPEND_OK") == "1" and os.environ.get("OPENROUTER_API_KEY"))
    ):
        notes.append(
            "auto-prep: skipped (requires a local model configured via "
            "JOBHUNT_LLM_BASE_URL, or SPEND_OK=1 + OPENROUTER_API_KEY)"
        )
        return notes

    try:
        from jobhunt import prep as prep_module
    except ImportError:
        notes.append("auto-prep: unavailable (jobhunt.prep not built yet)")
        return notes

    rubric_version = conn.execute(
        "SELECT MAX(rubric_version) v FROM scores"
    ).fetchone()["v"]
    if rubric_version is None:
        notes.append("auto-prep: skipped (no scores yet)")
        return notes

    rows = conn.execute(
        """
        SELECT s.posting_id, s.total, p.company, p.title
        FROM scores s
        JOIN postings p ON p.id = s.posting_id
        JOIN funnel f ON f.posting_id = s.posting_id
        WHERE s.rubric_version = ? AND s.total >= ? AND f.stage = 'suggested' AND p.source != 'simplify'
        ORDER BY s.total DESC, s.posting_id
        LIMIT ?
        """,
        (rubric_version, AUTO_PREP_MIN_SCORE, AUTO_PREP_LIMIT),
    ).fetchall()

    for row in rows:
        try:
            result = prep_module.generate_packet(conn, row["posting_id"])
        except SpendCapError:
            notes.append(
                f"auto-prep: stopped at daily spend cap (posting [{row['posting_id']}] not fully prepped)"
            )
            break
        except Exception as exc:  # noqa: BLE001 — unattended job must survive
            notes.append(
                f"auto-prep: skipped [{row['posting_id']}] — {type(exc).__name__}: {exc}"
            )
            break
        else:
            notes.append(
                f"auto-prep: generated packet for [{row['posting_id']}] {row['company']} — {row['title']} "
                f"({', '.join(result['generated']) or 'nothing new'})"
            )
    if not rows:
        notes.append(
            "auto-prep: no qualifying 'suggested' postings (score >= 70, non-simplify)"
        )
    return notes


def _notify_body(suggested_count: int, digest_name: str, stale_jobs: list[str]) -> str:
    """Lead with the alarm that actually fired. A notification triggered by a
    dead scheduler must not read as '0 new suggestion(s)' — an alarm that
    misnames its own cause is one the operator learns to mute."""
    body = f"{suggested_count} new suggestion(s) — see {digest_name}"
    if stale_jobs:
        return f"SCHEDULER STALE: {', '.join(stale_jobs)}. {body}"
    return body


def main() -> None:
    args = _parse_args()
    conn = db_module.connect()
    try:
        with runlog.track(conn, "daily") as run:
            before_ids = {
                r["id"] for r in conn.execute("SELECT id FROM postings").fetchall()
            }
            new_ids: list[int] = []

            if args.no_harvest:
                print("--no-harvest: skipping network harvest/rescore (dry run)")
                run.note("no-harvest dry run")
                ids_for_suggest = None  # suggest-from-existing (backfill path)
            else:
                # Auto-discover new company boards before harvesting, so any
                # newly found board gets swept in this same run. Own
                # try/except (mirrors the sheets-sync guard below) — a
                # discovery outage (network, rate limit, CSE misconfig) must
                # never take down harvest/score/digest/notify.
                try:
                    expand_summary = expand_module.run_expand(
                        conn, limit=DAILY_EXPAND_PROBE_LIMIT, guess_watchlist=True
                    )
                    discover_summary = discover_module.run_discovery(
                        conn, source="all", limit=DAILY_DISCOVER_PROBE_LIMIT, dry_run=False
                    )
                    expand_inserted = (
                        expand_summary["live_inserted"] + expand_summary["embed_guess_confirmed"]
                    )
                    discover_inserted = sum(
                        (s or {}).get("inserted", 0) for s in discover_summary.values()
                    )
                    discover_promoted = sum(
                        (s or {}).get("promoted", 0) for s in discover_summary.values()
                    )
                    discover_dead = sum(
                        (s or {}).get("dead", 0) for s in discover_summary.values()
                    )
                    run.note(
                        f"discovery: expand {expand_inserted} inserted/"
                        f"{expand_summary['watchlist_guess_confirmed']} promoted/"
                        f"{expand_summary['dead']} dead; "
                        f"boards {discover_inserted} inserted/{discover_promoted} promoted/"
                        f"{discover_dead} dead"
                    )
                except Exception as exc:  # noqa: BLE001 — unattended job must survive
                    print(f"discovery skipped/failed: {exc}")
                    run.note(f"discovery failed: {exc}")

                run_harvest(conn)  # prints its own per-source new counts + any WARNINGs
                rubric = load_rubric()
                run_score_sweep(conn, rubric)
                dedupe_module.run_dedupe(conn)
                new_ids = [
                    r["id"]
                    for r in conn.execute("SELECT id FROM postings").fetchall()
                    if r["id"] not in before_ids
                ]
                ids_for_suggest = new_ids
                run.note(f"harvested {len(new_ids)} new posting(s)")

            suggested_ids = run_suggest(conn, new_posting_ids=ids_for_suggest)
            print(f"Suggested {len(suggested_ids)} new posting(s).")
            run.note(f"suggested {len(suggested_ids)}")

            if datetime.now().weekday() == 0:  # Monday only
                fallback_filled = run_weekly_fallback_fill(conn)
                print(f"Fallback-fill: {len(fallback_filled)} posting(s).")
                run.note(f"fallback-fill {len(fallback_filled)}")

            autopilot_result = run_autopilot(conn)
            print(
                f"Autopilot: {len(autopilot_result['advanced'])} advanced, "
                f"{sum(autopilot_result['parked'].values())} parked, "
                f"{len(autopilot_result['resume_attached'])} resume(s) attached, "
                f"{autopilot_result['next_actions_set']} next action(s) set"
            )
            run.note(
                f"autopilot: {len(autopilot_result['advanced'])} advanced, "
                f"{sum(autopilot_result['parked'].values())} parked"
            )

            if not args.no_harvest:
                # Auto-mark submissions from ATS confirmation emails before the
                # digest, so weekly-gate counts reflect them. Skipped silently
                # until gmail-auth is configured; an outage must never take
                # down the digest (mirrors the sheets-sync guard below).
                try:
                    from jobhunt import confirmations as confirmations_module
                    from jobhunt.sources import alerts as alerts_source

                    if alerts_source.is_configured():
                        scan = confirmations_module.run_scan(conn)
                        run.note(
                            f"confirm-scan: {len(scan['confirmed'])} confirmed, "
                            f"{len(scan['blocked'])} blocked, "
                            f"{len(scan['ambiguous'])} ambiguous"
                        )
                except Exception as exc:  # noqa: BLE001 — unattended job must survive
                    print(f"confirm-scan skipped/failed: {exc}")
                    run.note(f"confirm-scan failed: {exc}")

            prep_notes: list[str] = []
            if args.auto_prep:
                prep_notes = _auto_prep(conn)
                for note in prep_notes:
                    print(note)

            digest_path = digest_module.write_digest(conn, extra_notes=prep_notes or None)
            print(f"Digest written: {digest_path}")

            if not args.no_harvest:
                # Mirror the pipeline to the tracker Sheet. Never lets a Sheets
                # outage (or missing local config) take down the daily digest.
                try:
                    from jobhunt import sheets as sheets_module

                    sheets_module.run_sync(conn, dry_run=False)
                    run.note("sheets-sync ok")
                except Exception as exc:  # noqa: BLE001 — unattended job must survive
                    print(f"sheets-sync skipped/failed: {exc}")
                    run.note(f"sheets-sync failed: {exc}")

            fire = notify_module.should_notify(conn)
            stale_jobs = [
                item["job"] for item in runlog.job_status(conn) if item["stale"]
            ]
            body = _notify_body(len(suggested_ids), digest_path.name, stale_jobs)
            if args.no_harvest:
                print(
                    f"Notification suppressed (--no-harvest dry run); would notify: {fire}"
                )
                if stale_jobs:
                    print(f"Would say: {body}")
            elif fire:
                notify_module.notify("Job Hunt Digest", body)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
