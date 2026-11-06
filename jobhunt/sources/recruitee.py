"""Recruitee job-board source.

GET https://{slug}.recruitee.com/api/offers/

This is Recruitee's documented, unauthenticated Careers Site API (see
https://docs.recruitee.com/reference/intro-to-careers-site-api): a plain GET
that returns the full open-offers list for a tenant in one response, no
pagination. Live-verified against two real tenants:
  - luzmo.recruitee.com/api/offers/ -> 200, 1 offer
  - addepto.recruitee.com/api/offers/ -> 200, 10 offers
robots.txt on both tenants only disallows /v/ (the candidate application
flow); /api/ is unrestricted. Response shape observed on both:
{"offers": [{id, title, slug, careers_url, careers_apply_url, city, country,
location, published_at, created_at, status, ...}]}.
"""

from __future__ import annotations

import json

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://{slug}.recruitee.com/api/offers/"


class BoardNotFound(Exception):
    """The tenant slug returned 404 — bad/stale slug, not a crash."""


def _location_str(offer: dict) -> str | None:
    parts = [offer.get("city"), offer.get("country")]
    joined = ", ".join(p for p in parts if p)
    return joined or offer.get("location") or None


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize open postings for one Recruitee tenant slug."""
    resp = polite_get(BASE_URL.format(slug=slug))

    if resp.status_code == 404:
        raise BoardNotFound(slug)
    if resp.status_code != 200:
        raise FetchError(f"recruitee {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise FetchError(f"recruitee {slug}: invalid JSON: {exc}") from exc

    offers = payload.get("offers", []) if isinstance(payload, dict) else []
    out: list[dict] = []
    for offer in offers:
        offer_id = offer.get("id")
        if offer_id is None:
            continue
        url = offer.get("careers_url") or f"https://{slug}.recruitee.com/o/{offer.get('slug', '')}"
        posted_at = offer.get("published_at") or offer.get("created_at")
        out.append(
            {
                "source_posting_id": str(offer_id),
                "company": company or offer.get("company_name") or slug,
                "title": offer.get("title"),
                "location": _location_str(offer),
                "url": url,
                "posted_at": posted_at,
                "raw": offer,
            }
        )
    return out
