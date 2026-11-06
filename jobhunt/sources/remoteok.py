"""The Remote OK job board source, via the official public JSON feed
(https://remoteok.com/api).

Keyless: no credentials required, so this module needs no is_configured()
gate — it's wired directly into harvest's SINGLE_SOURCES.

Remote OK's API terms ask consumers to link back to the job on Remote OK
and mention Remote OK as the source. We satisfy that by storing their own
job URLs (returned as each posting's `url`) rather than re-hosting content.

GET https://remoteok.com/api returns a JSON array whose FIRST element is a
legal/attribution notice object (has "legal"/"last_updated" keys, no "id"
or "position") rather than a job — verified live 2026-08-02. It is skipped
explicitly, not counted as a malformed job. Remaining elements are jobs:
id, position, company, location, url, date. Filtered to items that have a
`position` field, since Remote OK occasionally mixes in non-job entries.
"""

from __future__ import annotations

from jobhunt.net import FetchError, polite_get

API_URL = "https://remoteok.com/api"


def _normalize(job: dict) -> dict | None:
    posting_id = job.get("id")
    company = job.get("company")
    url = job.get("url")
    if not posting_id or not company or not url:
        return None
    return {
        "source_posting_id": f"rok-{posting_id}",
        "company": company,
        "title": job.get("position"),
        "location": job.get("location") or None,
        "url": url,
        "posted_at": job.get("date"),
        "raw": job,
    }


def fetch() -> list[dict]:
    resp = polite_get(API_URL)
    if resp.status_code != 200:
        raise FetchError(f"remoteok: HTTP {resp.status_code}", resp.status_code)
    payload = resp.json()
    items = payload if isinstance(payload, list) else []

    postings: list[dict] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict) or "position" not in item:
            # The first array element is a legal notice, not a job; other
            # non-job entries (if any) are silently ignored the same way —
            # neither is a malformed *job* record, so not counted as skipped.
            continue
        posting = _normalize(item)
        if posting is None:
            skipped += 1
            continue
        postings.append(posting)

    if skipped:
        print(f"WARNING: remoteok: skipped {skipped} job(s) missing company/url")

    return postings
