"""Orchestrates a harvest sweep across all sources.

For board sources (greenhouse/lever/ashby) each company's board is fetched
independently; a bad slug (404) or a transient error is recorded and does
NOT crash the sweep. "Closed" status is only ever applied to postings whose
(source, company) board was fetched successfully THIS run — a failed board
never causes its company's existing postings to be marked closed.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from jobhunt.net import FetchError
from jobhunt.postings import SourceStats, close_stale, ingest, record_sweep, upsert_posting
from jobhunt.sources import (
    adzuna,
    alerts,
    amazon,
    ashby,
    builtin,
    greenhouse,
    hn,
    jooble,
    lever,
    muse,
    netflix,
    recruitee,
    remoteok,
    remotive,
    simplify,
    smartrecruiters,
    teamtailor,
    workable,
    workday,
    wwr,
    zapply,
)

logger = logging.getLogger(__name__)

__all__ = ["ALL_SOURCES", "SourceStats", "close_stale", "run_harvest", "upsert_posting"]

BOARD_SOURCES = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "workday": workday,
    "recruitee": recruitee,
    "teamtailor": teamtailor,
}
SINGLE_SOURCES = {
    "simplify": simplify,
    "hn": hn,
    "adzuna": adzuna,
    "muse": muse,
    "jooble": jooble,
    "remotive": remotive,
    "remoteok": remoteok,
    "wwr": wwr,
    "builtin": builtin,
    "amazon": amazon,
    "netflix": netflix,
    "zapply": zapply,
    "alerts": alerts,
}
ALL_SOURCES = [*BOARD_SOURCES.keys(), *SINGLE_SOURCES.keys()]

# Alert-email sources (Gmail job-alert ingestion) announce newly-seen postings
# but never enumerate the full set of live postings on a given run, so a
# posting's absence from an alerts sweep must never be treated as "closed".
NON_SNAPSHOT_SOURCES = frozenset({"alerts"})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_companies_by_platform(conn: sqlite3.Connection, platform: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, slug FROM companies "
        "WHERE watchlist=0 AND platform=? AND slug IS NOT NULL AND slug != '' "
        "ORDER BY name",
        (platform,),
    ).fetchall()


def _active_count(conn: sqlite3.Connection, source: str, company: str | None) -> int:
    if company is None:
        return conn.execute(
            "SELECT COUNT(*) c FROM postings WHERE source=? AND status='active'", (source,)
        ).fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) c FROM postings WHERE source=? AND company=? AND status='active'",
        (source, company),
    ).fetchone()["c"]


def _record_source_state(
    conn: sqlite3.Connection, source: str, *, success: bool, at: str, error: str | None = None
) -> None:
    if success:
        conn.execute(
            """INSERT INTO source_state (source, baseline_at, last_success_at, consecutive_failures)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(source) DO UPDATE SET
                 last_success_at=excluded.last_success_at,
                 last_error=NULL,
                 consecutive_failures=0""",
            (source, at, at),
        )
    else:
        conn.execute(
            """INSERT INTO source_state
                   (source, baseline_at, last_error_at, last_error, consecutive_failures)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(source) DO UPDATE SET
                 last_error_at=excluded.last_error_at,
                 last_error=excluded.last_error,
                 consecutive_failures=source_state.consecutive_failures + 1""",
            (source, at, at, error or "source sweep failed"),
        )


def _fetch_board(module, name: str, slug: str) -> tuple[list[dict] | None, dict | None]:
    """Fetch one board. Returns (postings, None) or (None, error_record);
    never raises, so it is safe to run on a worker thread."""
    try:
        return module.fetch(slug, company=name), None
    except module.BoardNotFound:
        return None, {"company": name, "slug": slug, "error": "404 board not found"}
    except FetchError as exc:
        return None, {"company": name, "slug": slug, "error": str(exc)}
    except Exception as exc:
        return None, {"company": name, "slug": slug, "error": f"unexpected: {exc}"}


def _iter_board_fetches(module, companies: list) -> Iterator[tuple[str, str, list[dict] | None, dict | None]]:
    """Yield (name, slug, postings, error) per company, in companies order.

    A module may set PARALLEL_BOARDS > 1 when its boards live on distinct
    hosts (Workday: one hostname per tenant); fetches then overlap on a small
    thread pool while jobhunt.net's per-host limiter keeps every host at its
    politeness budget. Everything else stays strictly sequential. SQLite work
    never happens here — the caller ingests on its own thread, in order.
    """
    workers = int(getattr(module, "PARALLEL_BOARDS", 1) or 1)
    pairs = [(row["name"], row["slug"]) for row in companies]
    if workers <= 1 or len(pairs) <= 1:
        for name, slug in pairs:
            postings, error = _fetch_board(module, name, slug)
            yield name, slug, postings, error
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(pairs))) as pool:
        results = pool.map(lambda ns: _fetch_board(module, *ns), pairs)
        for (name, slug), (postings, error) in zip(pairs, results, strict=True):
            yield name, slug, postings, error


def _run_board_source(conn: sqlite3.Connection, source_name: str, module, limit_boards: int | None) -> SourceStats:
    started_at = _now()
    stats = SourceStats(source=source_name)
    companies = _load_companies_by_platform(conn, source_name)
    if limit_boards is not None:
        companies = companies[:limit_boards]

    for name, slug, postings, error in _iter_board_fetches(module, companies):
        stats.boards_attempted += 1
        if error is not None:
            stats.errors.append(error)
            continue

        stats.boards_ok += 1
        # Commit each complete board before fetching another. Network retries
        # and the global politeness delay must never hold the SQLite writer.
        # Ingest + closure remain atomic; a persistence error rolls both back.
        with conn:
            seen_ids = ingest(conn, source_name, stats, postings, _now())
            if not seen_ids and _active_count(conn, source_name, name) > 0:
                stats.errors.append(
                    {
                        "company": name,
                        "slug": slug,
                        "error": "anomalous empty board; retained existing active postings",
                    }
                )
            else:
                close_stale(conn, source_name, seen_ids, company=name)

    finished_at = _now()
    if stats.boards_attempted == 0:
        # No companies configured for this platform: nothing was tried, so
        # there is no health signal to record. Counting it as a failure made
        # recruitee/teamtailor report a false "failure" every day.
        logger.info("%s: no companies configured; skipped", source_name)
    else:
        _record_source_state(
            conn,
            source_name,
            success=stats.boards_ok > 0,
            at=finished_at,
            error=stats.errors[0]["error"] if stats.errors else None,
        )
        conn.commit()
    record_sweep(conn, source_name, stats, started_at, finished_at)
    return stats


def _run_single_source(conn: sqlite3.Connection, source_name: str, module) -> SourceStats:
    started_at = _now()
    stats = SourceStats(source=source_name, boards_attempted=1)
    try:
        postings = module.fetch()
        stats.boards_ok = 1
    except FetchError as exc:
        stats.errors.append({"error": str(exc)})
        finished_at = _now()
        _record_source_state(conn, source_name, success=False, at=finished_at, error=str(exc))
        record_sweep(conn, source_name, stats, started_at, finished_at)
        return stats
    except Exception as exc:
        stats.errors.append({"error": f"unexpected: {exc}"})
        finished_at = _now()
        _record_source_state(
            conn, source_name, success=False, at=finished_at, error=f"unexpected: {exc}"
        )
        record_sweep(conn, source_name, stats, started_at, finished_at)
        return stats

    seen_ids = ingest(conn, source_name, stats, postings, _now())
    if source_name in NON_SNAPSHOT_SOURCES:
        # Alert emails announce newly-seen postings; they never enumerate all
        # live postings, so absence from a sweep (including an empty sweep)
        # must not close existing postings or be flagged as anomalous.
        pass
    elif not seen_ids and _active_count(conn, source_name, None) > 0:
        stats.errors.append(
            {"error": "anomalous empty source response; retained existing active postings"}
        )
    else:
        close_stale(conn, source_name, seen_ids, company=None)
    finished_at = _now()
    _record_source_state(conn, source_name, success=True, at=finished_at)
    conn.commit()
    record_sweep(conn, source_name, stats, started_at, finished_at)
    return stats


def run_harvest(
    conn: sqlite3.Connection,
    sources: list[str] | None = None,
    limit_boards: int | None = None,
) -> dict[str, SourceStats]:
    targets = sources or ALL_SOURCES
    results: dict[str, SourceStats] = {}

    for source_name, module in BOARD_SOURCES.items():
        if source_name in targets:
            results[source_name] = _run_board_source(conn, source_name, module, limit_boards)

    for source_name, module in SINGLE_SOURCES.items():
        if source_name not in targets:
            continue
        if source_name == "adzuna" and not adzuna.is_configured():
            # Skipped, not failed: absent keys must not close existing adzuna
            # postings or trip the source-health failure counter.
            print("NOTE: adzuna: skipped (ADZUNA_APP_ID/ADZUNA_APP_KEY not set)")
            continue
        if source_name == "jooble" and not jooble.is_configured():
            # Same skip pattern as adzuna: absent key must not close existing
            # jooble postings or trip the source-health failure counter.
            print("NOTE: jooble: skipped (JOOBLE_API_KEY not set)")
            continue
        results[source_name] = _run_single_source(conn, source_name, module)

    print_sweep_report(results)
    return results


def print_sweep_report(results: dict[str, SourceStats]) -> None:
    print(f"\n{'source':<12}{'attempted':>10}{'ok':>6}{'seen':>8}{'new':>6}{'errors':>8}")
    print("-" * 52)
    warnings: list[str] = []
    for source_name, stats in results.items():
        print(
            f"{source_name:<12}{stats.boards_attempted:>10}{stats.boards_ok:>6}"
            f"{stats.postings_seen:>8}{stats.postings_new:>6}{len(stats.errors):>8}"
        )
        if stats.boards_ok == 0:
            warnings.append(f"WARNING: {source_name}: 0 boards succeeded (boards_attempted={stats.boards_attempted})")
        elif stats.postings_seen == 0:
            warnings.append(f"WARNING: {source_name}: {stats.boards_ok} board(s) ok but 0 postings seen")
    if warnings:
        print()
        for w in warnings:
            print(w)
