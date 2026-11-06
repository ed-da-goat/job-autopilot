"""Lever job-board source.

GET https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://api.lever.co/v0/postings/{slug}"


class BoardNotFound(Exception):
    """The slug returned 404 — bad/stale slug, not a crash."""


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize postings for one Lever board slug."""
    resp = polite_get(BASE_URL.format(slug=slug), params={"mode": "json"})

    if resp.status_code == 404:
        raise BoardNotFound(slug)
    if resp.status_code != 200:
        raise FetchError(f"lever {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"lever {slug}: invalid JSON: {exc}") from exc

    postings = payload if isinstance(payload, list) else []
    out: list[dict] = []
    for posting in postings:
        posting_id = posting.get("id")
        if posting_id is None:
            continue
        categories = posting.get("categories") or {}
        created_ms = posting.get("createdAt")
        posted_at = None
        if isinstance(created_ms, int | float):
            posted_at = datetime.fromtimestamp(created_ms / 1000, tz=UTC).isoformat()
        out.append(
            {
                "source_posting_id": str(posting_id),
                "company": company or slug,
                "title": posting.get("text"),
                "location": categories.get("location"),
                "url": posting.get("hostedUrl"),
                "posted_at": posted_at,
                "raw": posting,
            }
        )
    return out
