"""Greenhouse job-board source.

GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
"""

from __future__ import annotations

import json

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


class BoardNotFound(Exception):
    """The slug returned 404 — bad/stale slug, not a crash."""


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize postings for one Greenhouse board slug.

    Raises BoardNotFound on 404 (data problem, caller should record and
    move on). Raises FetchError on other unrecoverable HTTP/network issues.
    """
    resp = polite_get(BASE_URL.format(slug=slug), params={"content": "true"})

    if resp.status_code == 404:
        raise BoardNotFound(slug)
    if resp.status_code != 200:
        raise FetchError(f"greenhouse {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"greenhouse {slug}: invalid JSON: {exc}") from exc

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    out: list[dict] = []
    for job in jobs:
        job_id = job.get("id")
        if job_id is None:
            continue
        location = job.get("location") or {}
        out.append(
            {
                "source_posting_id": str(job_id),
                "company": company or slug,
                "title": job.get("title"),
                "location": location.get("name"),
                "url": job.get("absolute_url"),
                "posted_at": job.get("updated_at"),
                "raw": job,
            }
        )
    return out
