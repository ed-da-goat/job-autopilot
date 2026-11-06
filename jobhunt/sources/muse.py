"""The Muse job aggregator source, via the official public API
(https://www.themuse.com/developers/api/v2).

Keyless: works without any credentials, so this module needs no
is_configured() gate — it's wired directly into harvest's SINGLE_SOURCES.
Setting MUSE_API_KEY (optional) raises the anonymous rate limit; the key is
appended as the `api_key` query param when present and must never appear in
printed/logged output.

GET https://www.themuse.com/api/public/jobs?page=N&category=... per category
(0-indexed pages, verified live 2026-08-02: page=0 is the first page,
response includes page_count — total pages available for that category,
typically in the hundreds to low thousands). Paging is capped at MAX_PAGES
per category regardless of page_count, since the pool is enormous and we
only need the freshest slice. Results are deduped by Muse's own numeric
job id across categories, since the same job can carry more than one
category tag.
"""

from __future__ import annotations

import os

from jobhunt.net import FetchError, polite_get

API_URL = "https://www.themuse.com/api/public/jobs"
MAX_PAGES = 5

# Tuned to PROFILE.md's target roles: software engineering broadly, plus
# data/analytics roles adjacent to the fintech/AI-engineering positioning.
CATEGORIES: tuple[str, ...] = (
    "Software Engineering",
    "Data and Analytics",
)


def _fetch_page(category: str, page: int, api_key: str | None) -> dict:
    params: dict[str, str | int] = {"page": page, "category": category}
    if api_key:
        params["api_key"] = api_key
    resp = polite_get(API_URL, params=params)
    if resp.status_code != 200:
        # Deliberately omit params (would leak api_key) — category/page/status only.
        raise FetchError(f"muse: category={category!r} page={page} HTTP {resp.status_code}", resp.status_code)
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


def _normalize(result: dict) -> dict | None:
    posting_id = result.get("id")
    company = (result.get("company") or {}).get("name")
    landing_page = (result.get("refs") or {}).get("landing_page")
    if not posting_id or not company or not landing_page:
        return None
    locations = result.get("locations") or []
    location_names = [loc.get("name") for loc in locations if isinstance(loc, dict) and loc.get("name")]
    location = "; ".join(location_names) if location_names else None
    return {
        "source_posting_id": str(posting_id),
        "company": company,
        "title": result.get("name"),
        "location": location,
        "url": landing_page,
        "posted_at": result.get("publication_date"),
        "raw": result,
    }


def fetch() -> list[dict]:
    api_key = os.environ.get("MUSE_API_KEY")

    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for category in CATEGORIES:
        for page in range(MAX_PAGES):
            payload = _fetch_page(category, page, api_key)
            results = payload.get("results", [])
            if not isinstance(results, list):
                results = []
            for result in results:
                posting = _normalize(result)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)
            page_count = payload.get("page_count")
            if not results or (isinstance(page_count, int) and page >= page_count - 1):
                break

    if skipped:
        print(f"WARNING: muse: skipped {skipped} result(s) missing company/landing_page")

    return postings
