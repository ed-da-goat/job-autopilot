"""The We Work Remotely job board source, via the official public RSS
feeds for the programming categories.

Keyless: no credentials required, so this module needs no is_configured()
gate — it's wired directly into harvest's SINGLE_SOURCES.

robots.txt (https://weworkremotely.com/robots.txt, checked live 2026-08-02)
allows "/" for all user agents and only disallows /admin/, /account/,
/job-seekers/account/, /job-seekers/profile/, /manage-company/, and the
tokenized edit/cancel links — /categories/*.rss is not disallowed.

Two feeds are fetched (both verified live 2026-08-02, returning HTTP 200
RSS 2.0 documents):
  - https://weworkremotely.com/categories/remote-programming-jobs.rss
  - https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss
The two categories overlap (a job can appear in both), so postings are
deduped by source_posting_id across feeds, same pattern as muse's
cross-category dedupe.

Each <item> has: title (formatted "Company: Role"), region, category,
description, pubDate (RFC822), guid, link. Parsing is defensive throughout
(xml.etree, element-by-element .find()/.text with None checks) — a
malformed/incomplete item is skipped and counted rather than crashing the
feed. A non-parseable response (e.g. an HTML challenge page instead of RSS)
raises FetchError rather than being silently treated as zero postings.
"""

from __future__ import annotations

import hashlib
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from jobhunt.net import FetchError, polite_get

FEED_URLS: tuple[str, ...] = (
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
)


def _text(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    if el is None or el.text is None:
        return None
    stripped = el.text.strip()
    return stripped or None


def _parse_pub_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt.isoformat()


def _split_company_title(title: str, region: str | None) -> tuple[str, str] | None:
    """WWR titles are "Company: Role". Split on the first ": ". When a
    title carries no colon, fall back to the region tag as the company
    (per source instructions); with no region either, the item is
    unusable and is skipped."""
    if ": " in title:
        company, _, role = title.partition(": ")
        company = company.strip()
        role = role.strip()
        if company and role:
            return company, role
    if region:
        return region, title
    return None


def _normalize(item: ET.Element) -> dict | None:
    title = _text(item, "title")
    link = _text(item, "link")
    if not title or not link:
        return None

    split = _split_company_title(title, _text(item, "region"))
    if split is None:
        return None
    company, role_title = split

    guid = _text(item, "guid")
    posting_id = f"wwr-{guid}" if guid else f"wwr-{hashlib.sha1(link.encode('utf-8')).hexdigest()}"

    location = _text(item, "region") or _text(item, "category")

    return {
        "source_posting_id": posting_id,
        "company": company,
        "title": role_title,
        "location": location,
        "url": link,
        "posted_at": _parse_pub_date(_text(item, "pubDate")),
        "raw": {child.tag: child.text for child in item},
    }


def _fetch_feed_items(url: str) -> list[ET.Element]:
    resp = polite_get(url)
    if resp.status_code != 200:
        raise FetchError(f"wwr: HTTP {resp.status_code} for {url}", resp.status_code)
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise FetchError(f"wwr: non-parseable RSS from {url}: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        return []
    return channel.findall("item")


def fetch() -> list[dict]:
    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for feed_url in FEED_URLS:
        for item in _fetch_feed_items(feed_url):
            posting = _normalize(item)
            if posting is None:
                skipped += 1
                continue
            if posting["source_posting_id"] in seen_ids:
                continue
            seen_ids.add(posting["source_posting_id"])
            postings.append(posting)

    if skipped:
        print(f"WARNING: wwr: skipped {skipped} item(s) missing required fields")

    return postings
