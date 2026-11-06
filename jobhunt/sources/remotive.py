"""The Remotive job board source, via the official public API
(https://remotive.com/api/remote-jobs — see https://remotive.com/blog/remote-jobs-api
for docs).

Keyless: no credentials required, so this module needs no is_configured()
gate — it's wired directly into harvest's SINGLE_SOURCES.

Remotive's docs ask API consumers not to poll aggressively. This module
issues a single request (no pagination) and is only ever invoked once per
daily sweep, so it stays well within that ask.

GET https://remotive.com/api/remote-jobs?category=software-dev, verified
live 2026-08-02: response is a dict with a "jobs" list (no further paging
needed/available for a single category call). Each job has: id, url,
title, company_name, candidate_required_location, publication_date.
"""

from __future__ import annotations

from jobhunt.net import FetchError, polite_get

API_URL = "https://remotive.com/api/remote-jobs"
CATEGORY = "software-dev"


def _normalize(job: dict) -> dict | None:
    posting_id = job.get("id")
    company = job.get("company_name")
    url = job.get("url")
    if not posting_id or not company or not url:
        return None
    return {
        "source_posting_id": f"rmtv-{posting_id}",
        "company": company,
        "title": job.get("title"),
        "location": job.get("candidate_required_location") or None,
        "url": url,
        "posted_at": job.get("publication_date"),
        "raw": job,
    }


def fetch() -> list[dict]:
    resp = polite_get(API_URL, params={"category": CATEGORY})
    if resp.status_code != 200:
        raise FetchError(f"remotive: category={CATEGORY!r} HTTP {resp.status_code}", resp.status_code)
    payload = resp.json()
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    if not isinstance(jobs, list):
        jobs = []

    postings: list[dict] = []
    skipped = 0
    for job in jobs:
        posting = _normalize(job)
        if posting is None:
            skipped += 1
            continue
        postings.append(posting)

    if skipped:
        print(f"WARNING: remotive: skipped {skipped} job(s) missing company/url")

    return postings
