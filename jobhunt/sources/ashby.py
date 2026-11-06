"""Ashby job-board source.

GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true

Ashby's public API has drifted field names across accounts; tolerate both
`jobUrl`/`applyUrl` and `publishedAt`/`publishedDate` spellings.
"""

from __future__ import annotations

import json

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class BoardNotFound(Exception):
    """The slug returned 404 — bad/stale slug, not a crash."""


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize postings for one Ashby job board slug."""
    resp = polite_get(BASE_URL.format(slug=slug), params={"includeCompensation": "true"})

    if resp.status_code == 404:
        raise BoardNotFound(slug)
    if resp.status_code != 200:
        raise FetchError(f"ashby {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"ashby {slug}: invalid JSON: {exc}") from exc

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    out: list[dict] = []
    for job in jobs:
        job_id = job.get("id")
        if job_id is None:
            continue
        url = job.get("jobUrl") or job.get("applyUrl")
        posted_at = job.get("publishedAt") or job.get("publishedDate")
        location = job.get("location") or job.get("locationName")
        out.append(
            {
                "source_posting_id": str(job_id),
                "company": company or slug,
                "title": job.get("title"),
                "location": location,
                "url": url,
                "posted_at": posted_at,
                "raw": job,
            }
        )
    return out
