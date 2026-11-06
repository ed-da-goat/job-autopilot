"""BuiltIn job aggregator source (https://builtin.com), via HTML scraping of
robots-allowed category listing pages.

Keyless: no credentials, no key-gated frontend endpoints. No JSON API is
publicly documented for builtin.com, so this module parses the same data a
browser would render — HTML job cards on paginated category listing pages —
never an ATS-style raw JSON feed.

Compliance, verified live 2026-08-02 against https://builtin.com/robots.txt:
  - `Disallow: /jobs*?search=` blocks keyword search entirely, so this
    module uses category listing pages (like muse.py's CATEGORIES), not a
    QUERIES search concept.
  - `Disallow: *?page=` (generic) is overridden for job listings by the
    more specific `Allow: /jobs*?page=` — per standard robots.txt
    longest-match precedence, the more specific rule wins regardless of
    file order, so `/jobs/<category>?page=N` is robots-allowed.
  - The category paths used below (`dev-engineering`, `artificial-
    intelligence`, `data-analytics`) are not matched by any Disallow rule.
    Only more specific sub-paths are blocked (e.g.
    `/jobs/*dev-engineering/*sales-engineer`), not the category root.
  - Only builtin.com (root) is used, never city subdomains (builtinnyc.com
    etc.) — those carry `Disallow: /jobs*?page=`, i.e. pagination is
    blocked there.
  - No login, no bot-challenge workaround: a non-200 response (including a
    403 challenge page) raises FetchError and the sweep moves on.

Each category listing page embeds an `application/ld+json` `ItemList`
(schema.org) plus server-rendered job cards carrying company/location/
posted-recency text alongside the title link — all in the *same* fetch, so
no per-job detail-page requests are needed (that would blow the politeness
budget: MAX_PAGES * ~25 items/page detail fetches per category). Company
names and job detail pages are BuiltIn-authored summaries, not the
employer's own ATS post, so `url` here is the builtin.com job page, not a
direct ATS URL.

Posted-recency text (e.g. "Reposted 2 Days Ago") is relative, not an
absolute timestamp — deriving one would inject false precision, so
`posted_at` is left `None` and the raw text is kept under `raw`.
"""

from __future__ import annotations

import re

from jobhunt.net import FetchError, polite_get

BASE_URL = "https://builtin.com"
LISTING_URL = "https://builtin.com/jobs/{category}"
MAX_PAGES = 2

# Tuned to PROFILE.md's target roles, restricted to category slugs that
# clear builtin.com's robots.txt Disallow list (verified live 2026-08-02).
# "finance" (the job function) and fintech-flavored slugs are excluded:
# `/jobs/*financial-services` and `/jobs/*payments` are both disallowed and
# there is no clean allowed slug for that angle.
CATEGORIES: tuple[str, ...] = (
    "dev-engineering",
    "artificial-intelligence",
    "data-analytics",
)

_TITLE_RE = re.compile(
    r'data-id="job-card-title" data-alias="([^"]+)" data-builtin-track-job-id="(\d+)"[^>]*>([^<]+)</a>'
)
_COMPANY_RE = re.compile(r'data-id="company-title"[^>]*><span>([^<]*)</span>')
_LOCATION_MULTI_RE = re.compile(r"fa-location-dot.*?data-bs-title=\"([^\"]*)\"", re.S)
_LOCATION_MULTI_ITEM_RE = re.compile(r"text-truncate&#x27;&gt;([^&<]+)&lt;/div&gt;")
_LOCATION_SIMPLE_RE = re.compile(
    r'fa-location-dot[^<]*</i>\s*</div>\s*<div><span class="font-barlow text-gray-04">([^<]*)</span>'
)
_POSTED_RE = re.compile(r"fa-clock[^<]*</i>([^<]*)</span>")


def _fetch_page(category: str, page: int) -> str:
    resp = polite_get(
        LISTING_URL.format(category=category),
        params={"page": page} if page > 1 else None,
    )
    if resp.status_code != 200:
        raise FetchError(
            f"builtin: category={category!r} page={page} HTTP {resp.status_code}",
            resp.status_code,
        )
    return resp.text


def _parse_location(window: str) -> str | None:
    multi_m = _LOCATION_MULTI_RE.search(window)
    if multi_m:
        items = _LOCATION_MULTI_ITEM_RE.findall(multi_m.group(1))
        if items:
            return "; ".join(item.strip() for item in items)
    simple_m = _LOCATION_SIMPLE_RE.search(window)
    if simple_m:
        return simple_m.group(1).strip()
    return None


def _parse_cards(html: str) -> list[dict]:
    """Extract job cards from a category listing page's server-rendered
    HTML. Returns [] when the page has no cards (used as the early-stop
    signal for pagination, not treated as an error)."""
    titles = list(_TITLE_RE.finditer(html))
    companies = list(_COMPANY_RE.finditer(html))

    cards = []
    prev_end = 0
    for i, title_match in enumerate(titles):
        start = title_match.start()
        # Only a company element between the previous card's title and this
        # one belongs to this card — otherwise a card with no company block
        # of its own would silently inherit the prior card's company.
        card_companies = [c for c in companies if prev_end <= c.start() < start]
        company = card_companies[-1].group(1).strip() if card_companies else None

        window_end = titles[i + 1].start() if i + 1 < len(titles) else start + 4000
        window = html[start:window_end]
        prev_end = title_match.end()
        posted_m = _POSTED_RE.search(window)

        cards.append(
            {
                "alias": title_match.group(1),
                "title": title_match.group(3).strip(),
                "company": company,
                "location": _parse_location(window),
                "posted_text": posted_m.group(1).strip() if posted_m else None,
            }
        )
    return cards


def _normalize(card: dict) -> dict | None:
    alias = card.get("alias")
    company = card.get("company")
    title = card.get("title")
    if not alias or not company or not title:
        return None
    # alias looks like "/job/<slug>/<numeric-id>" — the numeric id is the
    # canonical identity tied to the public URL (a card's tracking id can
    # differ from this on reposts; the URL id is what stays consistent).
    posting_id = alias.rstrip("/").rsplit("/", 1)[-1]
    if not posting_id.isdigit():
        return None
    return {
        "source_posting_id": f"bi-{posting_id}",
        "company": company,
        "title": title,
        "location": card.get("location"),
        "url": f"{BASE_URL}{alias}",
        "posted_at": None,
        "raw": card,
    }


def fetch() -> list[dict]:
    seen_ids: set[str] = set()
    postings: list[dict] = []
    skipped = 0

    for category in CATEGORIES:
        for page in range(1, MAX_PAGES + 1):
            html = _fetch_page(category, page)
            cards = _parse_cards(html)
            if not cards:
                break
            for card in cards:
                posting = _normalize(card)
                if posting is None:
                    skipped += 1
                    continue
                if posting["source_posting_id"] in seen_ids:
                    continue
                seen_ids.add(posting["source_posting_id"])
                postings.append(posting)

    if skipped:
        print(f"WARNING: builtin: skipped {skipped} card(s) missing company/title/url")

    return postings
