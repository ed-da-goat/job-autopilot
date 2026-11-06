"""Fast new-posting watcher: board sweep -> score only what is new -> phone push.

Runs every 30 minutes under launchd (com.jobhunt.watch.plist). It is the thin
loop the daily run is too heavy for: only the three ATS board sources with
public JSON feeds (greenhouse / lever / ashby), only postings first seen since
the previous tick get scored, and every hit at or above the threshold is pushed
to the phone through ntfy (https://ntfy.sh — a topic is a shared secret, no
account). Nothing here applies, drafts, or spends.

State lives in data/watch_state.json: the previous tick's start time and the
posting ids already pushed (pruned after NOTIFIED_TTL_DAYS), so a posting is
pushed once even if a later tick re-scores it.

Configuration (both optional; without a topic the tick prints instead of pushing):
    JOBHUNT_NTFY_TOPIC   ntfy topic name, or put it on one line in data/ntfy_topic.local
    JOBHUNT_NTFY_URL     ntfy server (default https://ntfy.sh)
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from jobhunt.eligibility import assess_posting, load_preferences, upsert_assessment
from jobhunt.harvest import run_harvest
from jobhunt.scores_db import upsert_score
from jobhunt.scoring import load_companies_by_name, score_posting

WATCH_SOURCES = ["greenhouse", "lever", "ashby"]
DEFAULT_MIN_SCORE = 70
MAX_PUSHES_PER_TICK = 10
NOTIFIED_TTL_DAYS = 14
NTFY_DEFAULT_URL = "https://ntfy.sh"
NTFY_TIMEOUT_SECONDS = 10

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_STATE_PATH = _DATA_DIR / "watch_state.json"
DEFAULT_TOPIC_PATH = _DATA_DIR / "ntfy_topic.local"

DEFAULT_LOCK_PATH = _DATA_DIR / "watch.lock"
DAILY_IN_FLIGHT_WINDOW = timedelta(hours=12)


class WatchTickActiveError(RuntimeError):
    """Raised when another watch tick already holds data/watch.lock."""


@contextmanager
def tick_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    """Non-blocking exclusive flock for one tick; released when the fd closes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WatchTickActiveError("another watch tick is running") from exc
        yield
    finally:
        os.close(fd)


def daily_in_flight(conn: sqlite3.Connection, now: datetime) -> bool:
    """True if a `daily` run_log row is open and started within the last 12h."""
    cutoff = (now - DAILY_IN_FLIGHT_WINDOW).isoformat()
    row = conn.execute(
        "SELECT 1 FROM run_log WHERE job='daily' AND ok IS NULL AND started_at > ? LIMIT 1",
        (cutoff,),
    ).fetchone()
    return row is not None


# pusher(title, body, click_url, priority) -> None
Pusher = Callable[[str, str, str | None, str], None]


@dataclass
class Hit:
    posting_id: int
    company: str
    title: str
    location: str | None
    url: str | None
    total: int


@dataclass
class WatchResult:
    since: str
    new_postings: int = 0
    scored: int = 0
    hits: list[Hit] = field(default_factory=list)
    pushed: int = 0
    notes: list[str] = field(default_factory=list)


# --- state ------------------------------------------------------------------


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def _prune_notified(notified: dict[str, str], now: datetime) -> dict[str, str]:
    cutoff = (now - timedelta(days=NOTIFIED_TTL_DAYS)).isoformat()
    return {pid: stamp for pid, stamp in notified.items() if stamp >= cutoff}


# --- ntfy -------------------------------------------------------------------


def ntfy_topic() -> str | None:
    """Topic from the environment, else from data/ntfy_topic.local, else None."""
    topic = os.environ.get("JOBHUNT_NTFY_TOPIC", "").strip()
    if topic:
        return topic
    try:
        topic = DEFAULT_TOPIC_PATH.read_text().strip().splitlines()[0].strip()
    except (FileNotFoundError, IndexError):
        return None
    return topic or None


def ntfy_push(title: str, body: str, click_url: str | None, priority: str = "default") -> None:
    """POST one notification to the configured ntfy topic. Never raises:
    a push failure is logged to stderr; the tick must still record state."""
    topic = ntfy_topic()
    if not topic:
        print(f"watch: NO TOPIC (would push) {title} — {body}", file=sys.stderr)
        return
    server = os.environ.get("JOBHUNT_NTFY_URL", NTFY_DEFAULT_URL).rstrip("/")
    headers = {"Title": title, "Priority": priority, "Tags": "briefcase"}
    if click_url:
        headers["Click"] = click_url
    try:
        resp = requests.post(
            f"{server}/{topic}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=NTFY_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            print(f"watch: ntfy HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"watch: ntfy push failed: {exc}", file=sys.stderr)


def print_push(title: str, body: str, click_url: str | None, priority: str = "default") -> None:
    """Dry-run pusher: print what would have been sent."""
    print(f"[push:{priority}] {title}\n  {body}\n  {click_url or ''}")


# --- core -------------------------------------------------------------------


def _new_postings(conn: sqlite3.Connection, since: str, skip_ids: set[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in WATCH_SOURCES)
    rows = conn.execute(
        f"""
        SELECT * FROM postings
        WHERE status = 'active'
          AND source IN ({placeholders})
          AND first_seen_at >= ?
          AND duplicate_of IS NULL
        ORDER BY id
        """,
        (*WATCH_SOURCES, since),
    ).fetchall()
    return [row for row in rows if row["id"] not in skip_ids]


def score_new(conn: sqlite3.Connection, rows: list[sqlite3.Row], rubric: dict, min_score: int) -> tuple[int, list[Hit]]:
    """Score + eligibility-assess the given postings, persisting both the way
    the daily sweep does. Returns (scored_count, hits at/above min_score)."""
    companies = load_companies_by_name(conn)
    preferences = load_preferences()
    rubric_version = rubric["version"]
    hits: list[Hit] = []
    scored = 0
    for posting in rows:
        company_row = companies.get(posting["company"]) if posting["company"] else None
        result = score_posting(posting, rubric, company_row)
        upsert_score(conn, posting["id"], rubric_version, result)
        assessment = assess_posting(posting, preferences)
        upsert_assessment(conn, posting["id"], rubric_version, preferences, assessment)
        scored += 1
        if result["excluded"] or result["total"] is None or result["total"] < min_score:
            continue
        if assessment.status == "blocked":
            continue
        hits.append(
            Hit(
                posting_id=posting["id"],
                company=posting["company"] or "?",
                title=posting["title"] or "?",
                location=posting["location"],
                url=posting["url"],
                total=int(result["total"]),
            )
        )
    conn.commit()
    hits.sort(key=lambda h: (-h.total, h.posting_id))
    return scored, hits


def push_hits(hits: list[Hit], pusher: Pusher) -> int:
    """One push per hit up to MAX_PUSHES_PER_TICK, then one summary push for
    the remainder so a burst never floods the phone. Returns pushes sent."""
    sent = 0
    for hit in hits[:MAX_PUSHES_PER_TICK]:
        where = f" · {hit.location}" if hit.location else ""
        pusher(
            f"{hit.company}: {hit.title}",
            f"score {hit.total}{where}",
            hit.url,
            "high" if hit.total >= 85 else "default",
        )
        sent += 1
    rest = hits[MAX_PUSHES_PER_TICK:]
    if rest:
        lines = [f"{h.company}: {h.title} ({h.total})" for h in rest[:15]]
        if len(rest) > 15:
            lines.append(f"…and {len(rest) - 15} more")
        pusher(f"+{len(rest)} more new matches", "\n".join(lines), None, "default")
        sent += 1
    return sent


def run_watch(
    conn: sqlite3.Connection,
    rubric: dict,
    *,
    min_score: int = DEFAULT_MIN_SCORE,
    harvest: bool = True,
    pusher: Pusher = ntfy_push,
    state_path: Path = DEFAULT_STATE_PATH,
    now: datetime | None = None,
) -> WatchResult:
    """One watcher tick. `since` is the previous tick's start (or this tick's
    start on a first run, so a fresh install never replays the whole DB)."""
    tick_start = now or datetime.now(UTC)
    state = _load_state(state_path)
    since = state.get("last_tick") or tick_start.isoformat()
    notified: dict[str, str] = _prune_notified(dict(state.get("notified", {})), tick_start)
    result = WatchResult(since=since)

    if harvest:
        run_harvest(conn, sources=WATCH_SOURCES)
    else:
        result.notes.append("harvest skipped (--no-harvest)")

    rows = _new_postings(conn, since, {int(pid) for pid in notified})
    result.new_postings = len(rows)
    result.scored, result.hits = score_new(conn, rows, rubric, min_score)
    result.pushed = push_hits(result.hits, pusher)

    stamp = tick_start.isoformat()
    for hit in result.hits:
        notified[str(hit.posting_id)] = stamp
    _save_state(state_path, {"last_tick": stamp, "notified": notified})
    return result


def format_result(result: WatchResult) -> str:
    lines = [
        f"since: {result.since}",
        f"new postings: {result.new_postings}  scored: {result.scored}  "
        f"hits: {len(result.hits)}  pushed: {result.pushed}",
    ]
    for hit in result.hits:
        lines.append(f"  [{hit.posting_id}] {hit.total:>3}  {hit.company} — {hit.title}  {hit.url or ''}")
    lines.extend(result.notes)
    return "\n".join(lines)
