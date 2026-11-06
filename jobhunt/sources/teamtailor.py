"""Teamtailor job-board source.

GET https://{slug}.teamtailor.com/jobs.json

Teamtailor career sites are SSR, but every tenant also serves a public,
unauthenticated JSON Feed (https://jsonfeed.org) of open jobs at this path —
no API key, no separate /api/v1/jobs or ?format=json endpoint was found.
Live-verified against two real tenants:
  - teamlewis.teamtailor.com/jobs.json -> 200, 44 jobs
  - recruitgo.teamtailor.com/jobs.json -> 200, 43 jobs
robots.txt on both tenants disallows only /app/, /messages/, /messenger/,
/facebook/tab/, /jobs/internal/ — /jobs.json and /jobs are unrestricted
(both also carry `Content-Signal: search=yes`, an explicit crawl invitation).
Feed shape: {"items": [{id, title, url, date_published, content_html,
"_jobposting": {identifier: {value: <numeric job id>}, datePosted,
hiringOrganization: {name}, jobLocation: [{address: {addressLocality,
addressCountry, addressRegion}}]}}]}. The numeric id embedded in both the
job URL and _jobposting.identifier.value is Teamtailor's stable job id (the
top-level "id" is a feed-entry UUID that is not exposed on the site itself).
"""

from __future__ import annotations

import json
import re

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://{slug}.teamtailor.com/jobs.json"

_URL_ID_RE = re.compile(r"/jobs/(\d+)-")


class BoardNotFound(Exception):
    """The tenant slug returned 404 — bad/stale slug, not a crash."""


def _job_id(item: dict, jobposting: dict) -> str | None:
    identifier = jobposting.get("identifier") or {}
    value = identifier.get("value")
    if value is not None:
        return str(value)
    match = _URL_ID_RE.search(item.get("url") or "")
    if match:
        return match.group(1)
    return item.get("id")


def _location_str(jobposting: dict) -> str | None:
    locations = jobposting.get("jobLocation") or []
    if not locations:
        return None
    address = (locations[0] or {}).get("address") or {}
    parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize open postings for one Teamtailor tenant slug."""
    resp = polite_get(BASE_URL.format(slug=slug))

    if resp.status_code == 404:
        raise BoardNotFound(slug)
    if resp.status_code != 200:
        raise FetchError(f"teamtailor {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"teamtailor {slug}: invalid JSON: {exc}") from exc

    items = payload.get("items", []) if isinstance(payload, dict) else []
    out: list[dict] = []
    for item in items:
        jobposting = item.get("_jobposting") or {}
        job_id = _job_id(item, jobposting)
        if job_id is None:
            continue
        hiring_org = jobposting.get("hiringOrganization") or {}
        posted_at = jobposting.get("datePosted") or item.get("date_published")
        out.append(
            {
                "source_posting_id": str(job_id),
                "company": company or hiring_org.get("name") or slug,
                "title": item.get("title"),
                "location": _location_str(jobposting),
                "url": item.get("url"),
                "posted_at": posted_at,
                "raw": item,
            }
        )
    return out
