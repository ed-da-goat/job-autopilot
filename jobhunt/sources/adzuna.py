"""Adzuna job aggregator source, via the official public API
(https://developer.adzuna.com).

Env-key-gated: the candidate has not registered for Adzuna credentials yet, so this
module is inert (`is_configured()` returns False, `fetch()` raises) until
ADZUNA_APP_ID and ADZUNA_APP_KEY are set. The orchestrator's harvest wiring
is expected to check `is_configured()` and skip this source with a note
rather than call `fetch()` blind.

GET https://api.adzuna.com/v1/api/jobs/us/search/{page} per query, paging
until a page returns fewer than `RESULTS_PER_PAGE` results or `MAX_PAGES` is
hit. Results are deduped by Adzuna's own posting id across queries, since the
same job commonly matches more than one query string.

The app key must never appear in printed/logged output — errors raised here
carry only the query, page, and HTTP status, never params or URLs with the
key embedded.
"""

from __future__ import annotations

import os

from jobhunt.net import FetchError, polite_get

API_URL = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"
RESULTS_PER_PAGE = 50
MAX_PAGES = 2
MAX_DAYS_OLD = 35

# Tuned to PROFILE.md's target roles: founder-engineer positioning across
# AI/agent engineering, general backend/full-stack new-grad SWE, forward
# deployed engineering, solutions engineering, and fintech SWE.
QUERIES: tuple[str, ...] = (
    "entry level AI engineer",
    "new grad software engineer",
    "forward deployed engineer",
    "solutions engineer",
    "fintech software engineer",
)


def is_configured() -> bool:
    """True when both ADZUNA_APP_ID and ADZUNA_APP_KEY are set."""
    return bool(os.environ.get("ADZUNA_APP_ID")) and bool(os.environ.get("ADZUNA_APP_KEY"))


def _credentials() -> tuple[str, str]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError(
            "adzuna: ADZUNA_APP_ID and ADZUNA_APP_KEY must both be set "
            "(register at https://developer.adzuna.com); call is_configured() "
            "first to skip this source gracefully"
        )
    return app_id, app_key


def _fetch_page(query: str, page: int, app_id: str, app_key: str) -> list[dict]:
    resp = polite_get(
        API_URL.format(page=page),
        params={
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": RESULTS_PER_PAGE,
            "what": query,
            "max_days_old": MAX_DAYS_OLD,
            "sort_by": "date",
        },
    )
    if resp.status_code != 200:
        # Deliberately omit params (would leak app_key) — query/page/status only.
        raise FetchError(f"adzuna: query={query!r} page={page} HTTP {resp.status_code}", resp.status_code)
    payload = resp.json()
    results = payload.get("results", []) if isinstance(payload, dict) else []
    return results if isinstance(results, list) else []


def _normalize(result: dict) -> dict | None:
    posting_id = result.get("id")
    company = (result.get("company") or {}).get("display_name")
    redirect_url = result.get("redirect_url")
    if not posting_id or not company or not redirect_url:
        return None
    location = (result.get("location") or {}).get("display_name")
    return {
        "source_posting_id": str(posting_id),
        "company": company,
        "title": result.get("title"),
        "location": location or None,
        "url": redirect_url,
        "posted_at": result.get("created"),
        "raw": result,
    }


def fetch() -> list[dict]:
    app_id, app_key = _credentials()

    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for query in QUERIES:
        for page in range(1, MAX_PAGES + 1):
            results = _fetch_page(query, page, app_id, app_key)
            for result in results:
                posting = _normalize(result)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)
            if len(results) < RESULTS_PER_PAGE:
                break

    if skipped:
        print(f"WARNING: adzuna: skipped {skipped} result(s) missing company/url")

    return postings
