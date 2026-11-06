"""SmartRecruiters job-board source.

GET https://api.smartrecruiters.com/v1/companies/{companyId}/postings?offset=&limit=

Paginated via offset/limit; the response's `totalFound` tells us when to
stop. The list endpoint doesn't return the public human-facing posting URL,
but its shape is confirmed by inspecting a single-posting detail response
(its `postingUrl` field): https://jobs.smartrecruiters.com/{identifier}/{id}-{slug(name)}.
Building it from `company.identifier` + id + a slugified title avoids an
extra per-posting API call for every job on a board.

`company.identifier` is the canonical, case-preserved company id as
SmartRecruiters stores it (confirmed live: querying companyId "SmartRecruiters"
returns identifier "smartrecruiters"; querying "Visa" returns identifier
"Visa"). Lookup by companyId appears case-insensitive, but the identifier
used to build public URLs is not interchangeable with an arbitrarily-cased
guess — expand.py must not lowercase smartrecruiters slugs.
"""

from __future__ import annotations

import json
import re

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE_LIMIT = 100

_SLUG_TITLE_RE = re.compile(r"[^a-z0-9]+")


class BoardNotFound(Exception):
    """The companyId returned 404 — bad/stale slug, not a crash."""


def _slugify_title(name: str) -> str:
    return _SLUG_TITLE_RE.sub("-", name.lower()).strip("-")


def _location_str(location: dict) -> str | None:
    full = location.get("fullLocation")
    if full:
        return full
    parts = [location.get("city"), location.get("region"), location.get("country")]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _posting_url(identifier: str, posting_id: str, name: str | None) -> str:
    if name:
        return f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}-{_slugify_title(name)}"
    return f"https://jobs.smartrecruiters.com/{identifier}/{posting_id}"


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize postings for one SmartRecruiters companyId.

    Raises BoardNotFound on 404. Raises FetchError on other unrecoverable
    HTTP/network/JSON issues. Paginates via offset/limit until `totalFound`
    postings have been seen or a page comes back short/empty.
    """
    out: list[dict] = []
    offset = 0
    total_found: int | None = None

    while True:
        resp = polite_get(
            BASE_URL.format(slug=slug), params={"offset": offset, "limit": PAGE_LIMIT}
        )

        if resp.status_code == 404:
            raise BoardNotFound(slug)
        if resp.status_code != 200:
            raise FetchError(f"smartrecruiters {slug}: HTTP {resp.status_code}", resp.status_code)

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise FetchError(f"smartrecruiters {slug}: invalid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            break

        content = payload.get("content") or []
        if total_found is None:
            total_found = payload.get("totalFound")

        for posting in content:
            posting_id = posting.get("id")
            if posting_id is None:
                continue
            name = posting.get("name")
            posting_company = posting.get("company") or {}
            identifier = posting_company.get("identifier") or slug
            out.append(
                {
                    "source_posting_id": str(posting_id),
                    "company": company or posting_company.get("name") or slug,
                    "title": name,
                    "location": _location_str(posting.get("location") or {}),
                    "url": _posting_url(identifier, str(posting_id), name),
                    "posted_at": posting.get("releasedDate"),
                    "raw": posting,
                }
            )

        offset += len(content)
        if not content or (isinstance(total_found, int) and offset >= total_found):
            break

    return out
