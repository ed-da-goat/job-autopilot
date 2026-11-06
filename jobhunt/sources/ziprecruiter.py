"""ZipRecruiter job aggregator source, via the official partner API
(https://api.ziprecruiter.com/jobs/v1; access requested at
https://www.ziprecruiter.com/partner).

Env-key-gated: the candidate has not registered for a ZipRecruiter partner key yet,
so this module is inert (`is_configured()` returns False, `fetch()` raises)
until ZIPRECRUITER_API_KEY is set. The orchestrator's harvest wiring is
expected to check `is_configured()` and skip this source with a note rather
than call `fetch()` blind.

GET https://api.ziprecruiter.com/jobs/v1 per query, paging (1-indexed) until
a page returns fewer than JOBS_PER_PAGE results or MAX_PAGES is hit. Results
are deduped by ZipRecruiter's own posting id across queries, since the same
job commonly matches more than one query string.

The api_key must never appear in printed/logged output — errors raised here
carry only the query, page, and HTTP status, never params or URLs with the
key embedded.
"""

from __future__ import annotations

import os

from jobhunt.net import FetchError, polite_get
from jobhunt.sources.adzuna import QUERIES

__all__ = ["QUERIES", "fetch", "is_configured"]

API_URL = "https://api.ziprecruiter.com/jobs/v1"
JOBS_PER_PAGE = 100
MAX_PAGES = 2
RADIUS_MILES = 50
DAYS_AGO = 35


def is_configured() -> bool:
    """True when ZIPRECRUITER_API_KEY is set."""
    return bool(os.environ.get("ZIPRECRUITER_API_KEY"))


def _api_key() -> str:
    api_key = os.environ.get("ZIPRECRUITER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ziprecruiter: ZIPRECRUITER_API_KEY must be set (partner-granted "
            "keys at https://www.ziprecruiter.com/partner); call "
            "is_configured() first to skip this source gracefully"
        )
    return api_key


def _fetch_page(query: str, page: int, api_key: str) -> list[dict]:
    resp = polite_get(
        API_URL,
        params={
            "search": query,
            "location": "United States",
            "radius_miles": RADIUS_MILES,
            "days_ago": DAYS_AGO,
            "jobs_per_page": JOBS_PER_PAGE,
            "page": page,
            "api_key": api_key,
        },
    )
    if resp.status_code != 200:
        # Deliberately omit params (would leak api_key) — query/page/status only.
        raise FetchError(f"ziprecruiter: query={query!r} page={page} HTTP {resp.status_code}", resp.status_code)
    payload = resp.json()
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return jobs if isinstance(jobs, list) else []


def _normalize(job: dict) -> dict | None:
    posting_id = job.get("id")
    company = (job.get("hiring_company") or {}).get("name")
    url = job.get("url")
    if not posting_id or not company or not url:
        return None
    location = job.get("location")
    if not location:
        city = job.get("city")
        state = job.get("state")
        location = ", ".join(part for part in (city, state) if part) or None
    return {
        "source_posting_id": f"zr-{posting_id}",
        "company": company,
        "title": job.get("name"),
        "location": location,
        "url": url,
        "posted_at": job.get("posted_time"),
        "raw": job,
    }


def fetch() -> list[dict]:
    api_key = _api_key()

    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for query in QUERIES:
        for page in range(1, MAX_PAGES + 1):
            jobs = _fetch_page(query, page, api_key)
            for job in jobs:
                posting = _normalize(job)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)
            if len(jobs) < JOBS_PER_PAGE:
                break

    if skipped:
        print(f"WARNING: ziprecruiter: skipped {skipped} result(s) missing company/url")

    return postings
