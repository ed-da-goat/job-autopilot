"""Workable job-board source.

GET https://apply.workable.com/api/v1/widget/accounts/{slug}

Workable exposes two unauthenticated read paths for a board: this v1 "jobs
widget" GET endpoint (built for embedding a company's open jobs on their own
site) and a v3 POST search endpoint. We use the v1 widget: it's a plain GET
that needs no new POST support in jobhunt/net.py, and it is served by every
apply.workable.com board. Live-verified against a real tenant
(apply.workable.com/api/v1/widget/accounts/typeform -> 200 with a jobs
board payload); the widget does not appear to paginate — it returns the
full open-jobs list in one response. Exact per-job field names (beyond
title/shortcode/location, confirmed structurally similar to Workable's
publicly documented widget schema) are handled defensively since the full
job-array shape wasn't captured during the bounded live-verification budget.
"""

from __future__ import annotations

import json

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://apply.workable.com/api/v1/widget/accounts/{slug}"


class BoardNotFound(Exception):
    """The account slug returned 404 — bad/stale slug, not a crash."""


def _location_str(location: dict) -> str | None:
    if not location:
        return None
    parts = [location.get("city"), location.get("region"), location.get("country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize open postings for one Workable account slug."""
    resp = polite_get(BASE_URL.format(slug=slug))

    if resp.status_code == 404:
        raise BoardNotFound(slug)
    if resp.status_code != 200:
        raise FetchError(f"workable {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"workable {slug}: invalid JSON: {exc}") from exc

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    out: list[dict] = []
    for job in jobs:
        shortcode = job.get("shortcode")
        if shortcode is None:
            continue
        url = job.get("url") or f"https://apply.workable.com/{slug}/j/{shortcode}/"
        posted_at = job.get("published_on") or job.get("published") or job.get("created_at")
        out.append(
            {
                "source_posting_id": str(shortcode),
                "company": company or payload.get("name") or slug,
                "title": job.get("title"),
                "location": _location_str(job.get("location") or {}),
                "url": url,
                "posted_at": posted_at,
                "raw": job,
            }
        )
    return out
