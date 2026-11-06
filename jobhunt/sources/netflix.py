"""Netflix jobs source, via the public search endpoint backing
https://explore.jobs.netflix.net (an Eightfold.ai-hosted careers site —
this is the Eightfold platform API, generalizable to any other Eightfold
customer by swapping DOMAIN and BASE_URL host).

Keyless: no credentials or session/CSRF bootstrapping required. robots.txt
for explore.jobs.netflix.net (verified live 2026-08-02) is a default-deny
with an explicit `Allow: /api/apply` — the endpoint used here.

GET https://explore.jobs.netflix.net/api/apply/v2/jobs per query, paging by
`start` (0-indexed, `num` per page) until a page returns fewer than
RESULTS_PER_PAGE positions or MAX_PAGES is hit. Unlike Amazon's search,
Netflix/Eightfold's query matching is not strict-AND across tokens — all 4
standard queries return nonzero live hits (verified 2026-08-02), so none
are dropped. Results are deduped by Eightfold's own numeric position id
across queries.
"""

from __future__ import annotations

import json

from jobhunt.net import FetchError, polite_get

API_URL = "https://explore.jobs.netflix.net/api/apply/v2/jobs"
DOMAIN = "netflix.com"
RESULTS_PER_PAGE = 20
MAX_PAGES = 2

QUERIES: tuple[str, ...] = (
    "software engineer early career",
    "software engineer new grad",
    "solutions engineer",
    "forward deployed engineer",
)


def _fetch_page(query: str, start: int) -> list[dict]:
    resp = polite_get(
        API_URL,
        params={
            "domain": DOMAIN,
            "start": start,
            "num": RESULTS_PER_PAGE,
            "query": query,
        },
    )
    if resp.status_code != 200:
        raise FetchError(f"netflix: query={query!r} start={start} HTTP {resp.status_code}", resp.status_code)
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"netflix: query={query!r} start={start} invalid JSON (challenge page?): {exc}") from exc

    positions = payload.get("positions", []) if isinstance(payload, dict) else []
    return positions if isinstance(positions, list) else []


def _normalize(position: dict) -> dict | None:
    posting_id = position.get("id")
    title = position.get("name")
    url = position.get("canonicalPositionUrl")
    if not posting_id or not title or not url:
        return None

    locations = position.get("locations")
    if isinstance(locations, list) and locations:
        location = "; ".join(loc for loc in locations if isinstance(loc, str) and loc)
    else:
        location = position.get("location")

    return {
        "source_posting_id": f"nflx-{posting_id}",
        "company": "Netflix",
        "title": title,
        "location": location or None,
        "url": url,
        "posted_at": position.get("t_create") or position.get("postedDate"),
        "raw": position,
    }


def fetch() -> list[dict]:
    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for query in QUERIES:
        for page in range(MAX_PAGES):
            start = page * RESULTS_PER_PAGE
            positions = _fetch_page(query, start)
            for position in positions:
                posting = _normalize(position)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)
            if len(positions) < RESULTS_PER_PAGE:
                break

    if skipped:
        print(f"WARNING: netflix: skipped {skipped} result(s) missing id/name/canonicalPositionUrl")

    return postings
