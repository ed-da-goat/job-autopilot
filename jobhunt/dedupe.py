"""Cross-source duplicate detection.

Aggregator sources (simplify, zapply, hn, adzuna, muse, jooble) repost roles we also
harvest directly from ATS boards (greenhouse/lever/ashby, smartrecruiters/
workable/workday). Left alone, the same role shows up twice on every surface that
filters status='active' (score, shortlist, sheets-sync). `run_dedupe` groups
postings that are almost certainly the same role — by canonical URL or by a
normalized (company, title, location) triple — and demotes every loser in a
group to status='duplicate', pointing duplicate_of at the winner.

A posting already being worked (any funnel stage past 'harvested') is never
demoted, regardless of source priority — the candidate's pipeline state always wins.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

# Lower rank wins. Direct ATS boards beat every aggregator; among
# aggregators, hn (curated, low volume) beats simplify/muse beats
# adzuna/jooble. muse and jooble share rank with the aggregator they most
# resemble (simplify and adzuna respectively) rather than getting new tiers.
SOURCE_PRIORITY = {
    "greenhouse": 0,
    "lever": 0,
    "ashby": 0,
    "smartrecruiters": 0,
    "workable": 0,
    "workday": 0,
    "recruitee": 0,
    "teamtailor": 0,
    "amazon": 0,
    "netflix": 0,
    "hn": 1,
    "builtin": 1,
    "remotive": 1,
    "remoteok": 1,
    "wwr": 1,
    "simplify": 2,
    "zapply": 2,
    "muse": 2,
    "adzuna": 3,
    "jooble": 3,
}
_UNKNOWN_SOURCE_RANK = max(SOURCE_PRIORITY.values()) + 1

_GROUPABLE_STATUSES = ("active", "duplicate")

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Only these are junk. Everything else in a query string may be an identifier
# (HN's ?id=, greenhouse embed's ?for=/&token=) and must survive, or two
# distinct postings collapse into one canonical URL.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = {
    "gclid",
    "fbclid",
    "ref",
    "src",
    "source",
    "gh_src",
    "lever-source",
    "lever-origin",
}


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAM_NAMES or lowered.startswith(_TRACKING_PARAM_PREFIXES)


@dataclass
class DedupeResult:
    groups_found: int = 0
    marked_duplicate: int = 0
    protected_by_funnel: int = 0


def _canon_url(url: str) -> str:
    """Lowercase host, drop scheme, strip trailing slash. Query-string
    tracking junk (utm_*, gclid, fbclid, ref, src, source, gh_src,
    lever-source, lever-origin) is dropped; every other query param is kept
    (sorted, for stability) because it may be an identifier — HN's ?id=NNN,
    a greenhouse embed's ?for=X&token=Y — not tracking noise. Returns '' for
    a blank/missing url so it never becomes a false match key."""
    if not url or not url.strip():
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)
    )
    query = urlencode(kept)
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _norm(s: str) -> str:
    """Casefold, strip punctuation, collapse whitespace."""
    if not s:
        return ""
    s = _PUNCT_RE.sub(" ", s.casefold())
    return _WS_RE.sub(" ", s).strip()


def _source_rank(source: str) -> int:
    return SOURCE_PRIORITY.get(source, _UNKNOWN_SOURCE_RANK)


def _funnel_stage(conn: sqlite3.Connection, posting_id: int) -> str | None:
    row = conn.execute("SELECT stage FROM funnel WHERE posting_id=?", (posting_id,)).fetchone()
    return row["stage"] if row else None


def _beyond_harvested(conn: sqlite3.Connection, posting_id: int) -> bool:
    stage = _funnel_stage(conn, posting_id)
    return stage is not None and stage != "harvested"


def find_duplicate_groups(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    """Group postings (status active or duplicate — closed rows are never
    touched) that share either a canonical URL or a normalized
    (company, title, location) triple. Overlapping groups are merged
    transitively via union-find, so A~B (shared URL) and B~C (shared triple)
    end up in one group of three."""
    rows = conn.execute(
        f"SELECT id, source, company, title, location, url, status FROM postings "
        f"WHERE status IN ({','.join('?' * len(_GROUPABLE_STATUSES))})",
        _GROUPABLE_STATUSES,
    ).fetchall()

    parent: dict[int, int] = {r["id"]: r["id"] for r in rows}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    key_to_first: dict[tuple, int] = {}
    for r in rows:
        keys: list[tuple] = []
        canon = _canon_url(r["url"] or "")
        if canon:
            keys.append(("url", canon))
        company, title, location = _norm(r["company"] or ""), _norm(r["title"] or ""), _norm(r["location"] or "")
        if company and title:
            keys.append(("triple", company, title, location))
        for key in keys:
            if key in key_to_first:
                union(r["id"], key_to_first[key])
            else:
                key_to_first[key] = r["id"]

    groups: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)
    return [g for g in groups.values() if len(g) > 1]


def _select_winner(
    conn: sqlite3.Connection, group: list[sqlite3.Row]
) -> tuple[sqlite3.Row, list[sqlite3.Row], int]:
    """Returns (winner, losers, funnel_protected_count) for one group.

    Any posting with a funnel row past 'harvested' is never demoted; if
    several qualify, all stay active and the best-source-priority one among
    them becomes the canonical duplicate_of target for the rest of the
    group. Otherwise the winner is chosen by source priority, then lowest id.
    """
    funnel_protected = [r for r in group if _beyond_harvested(conn, r["id"])]
    if funnel_protected:
        winner = min(funnel_protected, key=lambda r: (_source_rank(r["source"]), r["id"]))
        keep_ids = {r["id"] for r in funnel_protected}
        losers = [r for r in group if r["id"] not in keep_ids]
        return winner, losers, len(funnel_protected)

    winner = min(group, key=lambda r: (_source_rank(r["source"]), r["id"]))
    losers = [r for r in group if r["id"] != winner["id"]]
    return winner, losers, 0


def run_dedupe(conn: sqlite3.Connection) -> DedupeResult:
    """Idempotent full recompute: reset every status='duplicate' posting back
    to active, regroup from scratch, and demote losers. Postings with any
    other status (closed) are never touched. Prints a one-line summary and
    returns the counts."""
    conn.execute("UPDATE postings SET status='active', duplicate_of=NULL WHERE status='duplicate'")

    result = DedupeResult()
    for group in find_duplicate_groups(conn):
        winner, losers, protected_count = _select_winner(conn, group)
        result.groups_found += 1
        result.protected_by_funnel += protected_count
        for loser in losers:
            conn.execute(
                "UPDATE postings SET status='duplicate', duplicate_of=? WHERE id=?",
                (winner["id"], loser["id"]),
            )
            result.marked_duplicate += 1
    conn.commit()

    print(
        f"dedupe: {result.groups_found} group(s), {result.marked_duplicate} marked duplicate, "
        f"{result.protected_by_funnel} protected by funnel"
    )
    return result


def preview_duplicate_groups(
    conn: sqlite3.Connection,
) -> tuple[DedupeResult, list[tuple[sqlite3.Row, list[sqlite3.Row]]]]:
    """Dry-run companion to run_dedupe: computes the same groups/winners
    without writing anything. Returns (counts, [(winner, losers), ...])."""
    result = DedupeResult()
    plan: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
    for group in find_duplicate_groups(conn):
        winner, losers, protected_count = _select_winner(conn, group)
        result.groups_found += 1
        result.protected_by_funnel += protected_count
        result.marked_duplicate += len(losers)
        plan.append((winner, losers))
    return result, plan
