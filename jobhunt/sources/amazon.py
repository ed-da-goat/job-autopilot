"""Amazon jobs source, via the public search endpoint backing
https://www.amazon.jobs (https://www.amazon.jobs/en/search.json).

Keyless: no credentials or session/CSRF bootstrapping required. robots.txt
for www.amazon.jobs (verified live 2026-08-02) disallows only /internal
paths; /en/search.json is unrestricted.

GET https://www.amazon.jobs/en/search.json per query, paging by `offset`
(0-indexed, `result_limit` per page) until a page returns fewer than
RESULTS_PER_PAGE jobs or MAX_PAGES is hit. `normalized_country_code[]=USA`
is pinned on every request to keep results to the US market (Amazon's
default search spans every country Amazon hires in). Results are deduped
by Amazon's own `id_icims`/`id` across queries.

QUERIES deviates from the standard 4-query list: "software engineer early
career" and "software engineer new grad" both return 0 hits live (verified
2026-08-02) — Amazon's search appears to AND every token in the query
string, and Amazon's own new-grad postings don't literally contain "new"
and "grad" as separate words. Both are dropped per the "drop queries an API
can't express" convention. "software development engineer" (Amazon's actual
SDE title convention) covers the mainline engineering roles instead; the
scorer downstream separates entry-level from senior.
"""

from __future__ import annotations

import json

from jobhunt.net import FetchError, polite_get

API_URL = "https://www.amazon.jobs/en/search.json"
RESULTS_PER_PAGE = 20
MAX_PAGES = 2

QUERIES: tuple[str, ...] = (
    "solutions engineer",
    "forward deployed engineer",
    "software development engineer",
)


def _fetch_page(query: str, offset: int) -> list[dict]:
    resp = polite_get(
        API_URL,
        params={
            "base_query": query,
            "offset": offset,
            "result_limit": RESULTS_PER_PAGE,
            "normalized_country_code[]": "USA",
        },
    )
    if resp.status_code != 200:
        raise FetchError(f"amazon: query={query!r} offset={offset} HTTP {resp.status_code}", resp.status_code)
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"amazon: query={query!r} offset={offset} invalid JSON (challenge page?): {exc}") from exc

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return jobs if isinstance(jobs, list) else []


def _normalize(job: dict) -> dict | None:
    raw_id = job.get("id_icims") or job.get("id")
    job_path = job.get("job_path")
    title = job.get("title")
    if not raw_id or not job_path or not title:
        return None

    company = job.get("company_name") or "Amazon"

    location = job.get("normalized_location")
    if not location:
        city = job.get("city")
        state = job.get("state") or job.get("state_or_region")
        location = ", ".join(part for part in (city, state) if part) or None

    return {
        "source_posting_id": f"amzn-{raw_id}",
        "company": company,
        "title": title,
        "location": location,
        "url": f"https://www.amazon.jobs{job_path}",
        "posted_at": job.get("posted_date"),
        "raw": job,
    }


def fetch() -> list[dict]:
    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for query in QUERIES:
        for page in range(MAX_PAGES):
            offset = page * RESULTS_PER_PAGE
            jobs = _fetch_page(query, offset)
            for job in jobs:
                posting = _normalize(job)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)
            if len(jobs) < RESULTS_PER_PAGE:
                break

    if skipped:
        print(f"WARNING: amazon: skipped {skipped} result(s) missing id/title/job_path")

    return postings
