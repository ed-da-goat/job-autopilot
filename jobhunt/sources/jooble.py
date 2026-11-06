"""Jooble job aggregator source, via the official API
(https://jooble.org/api/about).

Env-key-gated: the candidate has not registered for a Jooble API key yet, so this
module is inert (`is_configured()` returns False, `fetch()` raises) until
JOOBLE_API_KEY is set. The orchestrator's harvest wiring is expected to check
`is_configured()` and skip this source with a note rather than call `fetch()`
blind.

POST https://jooble.org/api/{key} (the key is a URL path segment, not a
query param or header) with a JSON body {"keywords": ..., "page": ...} per
query, paging up to MAX_PAGES and stopping early once a page returns no
jobs. Results are deduped by Jooble's own posting id across queries, since
the same job commonly matches more than one query string.

Because the key IS the URL, it must never appear in printed/logged output —
every error raised here redacts the URL to "jooble.org/api/<redacted>".
"""

from __future__ import annotations

import os

from jobhunt.net import FetchError, polite_post
from jobhunt.sources.adzuna import QUERIES

__all__ = ["QUERIES", "fetch", "is_configured"]

API_URL = "https://jooble.org/api/{key}"
MAX_PAGES = 2


def is_configured() -> bool:
    """True when JOOBLE_API_KEY is set."""
    return bool(os.environ.get("JOOBLE_API_KEY"))


def _api_key() -> str:
    key = os.environ.get("JOOBLE_API_KEY")
    if not key:
        raise RuntimeError(
            "jooble: JOOBLE_API_KEY must be set (register at "
            "https://jooble.org/api/about); call is_configured() first to "
            "skip this source gracefully"
        )
    return key


def _fetch_page(query: str, page: int, key: str) -> list[dict]:
    resp = polite_post(API_URL.format(key=key), json_body={"keywords": query, "page": page})
    if resp.status_code != 200:
        # Never interpolate the real URL (contains the key) — show a redacted form.
        raise FetchError(
            f"jooble: query={query!r} page={page} HTTP {resp.status_code} (jooble.org/api/<redacted>)",
            resp.status_code,
        )
    payload = resp.json()
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return jobs if isinstance(jobs, list) else []


def _normalize(job: dict) -> dict | None:
    posting_id = job.get("id")
    company = job.get("company")
    link = job.get("link")
    if not posting_id or not company or not link:
        return None
    return {
        "source_posting_id": str(posting_id),
        "company": company,
        "title": job.get("title"),
        "location": job.get("location") or None,
        "url": link,
        "posted_at": job.get("updated"),
        "raw": job,
    }


def fetch() -> list[dict]:
    key = _api_key()

    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for query in QUERIES:
        for page in range(1, MAX_PAGES + 1):
            jobs = _fetch_page(query, page, key)
            if not jobs:
                break
            for job in jobs:
                posting = _normalize(job)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)

    if skipped:
        print(f"WARNING: jooble: skipped {skipped} result(s) missing company/link")

    return postings
