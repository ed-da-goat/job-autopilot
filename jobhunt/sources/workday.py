"""Workday job-board source.

POST https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

Workday's career-site (CXS) endpoint is not a documented public API — it is
the same unauthenticated JSON endpoint every myworkdayjobs.com career page
calls from the browser to render its own job list. No official public
Workday API exists; admitting this endpoint is a deliberate call recorded in
the README's hard-rules bullet (2026-08-02), not an oversight.

A Workday board is identified by three independent pieces baked into the
URL, not one slug: the `tenant` (company id), the `instance` (a Workday pod
subdomain like "wd1" or "wd5" — not consistent across tenants), and the
`site` (career-site name, case-sensitive, e.g. "NVIDIAExternalCareerSite" or
a plain lowercase tenant name like "adtran"). We store all three as one
composite slug "tenant:instance:site" in the companies table's existing
`slug` column rather than adding schema.

Paginated via offset, PAGE_LIMIT=20 per request (matches what the real
career-site frontend requests); the response's `total` tells us when to
stop. Live-verified against nvidia:wd5:NVIDIAExternalCareerSite,
cadence:wd1:External_Careers, and adtran:wd3:adtran — response shape is
exactly {"total": int, "jobPostings": [{"title", "externalPath",
"locationsText", "postedOn", "bulletFields"}], ...}. `postedOn` is relative
text ("Posted 3 Days Ago", "Posted Today") with no absolute date anywhere in
the payload, so posted_at is always None; the raw string survives in `raw`.
Capped at MAX_POSTINGS per board — if `total` exceeds the cap, the
remainder is skipped with one WARNING (never silently truncated).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from jobhunt.net import USER_AGENT, FetchError, polite_get, polite_post
from jobhunt.text_extract import strip_html

ENDPOINT = "https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
PAGE_LIMIT = 20
MAX_POSTINGS = 1000
# Every tenant is its own hostname (tenant.wdN.myworkdayjobs.com), so the
# harvester may fetch this many boards concurrently without any host seeing
# more than the per-host politeness budget in jobhunt.net. Live-verified
# 2026-10-23 that the API ignores limit>20 (returns an empty page), so a
# bigger page size is not an option; parallel boards are.
PARALLEL_BOARDS = 8


class BoardNotFound(Exception):
    """Malformed slug, or the tenant/instance/site 404s — bad/stale slug, not a crash."""


def parse_slug(slug: str) -> tuple[str, str, str]:
    """Split "tenant:instance:site" into its three parts, case preserved.

    Raises BoardNotFound if `slug` isn't exactly three non-empty
    colon-separated parts.
    """
    parts = (slug or "").split(":")
    if len(parts) != 3 or not all(parts):
        raise BoardNotFound(
            f'workday: malformed slug {slug!r}; expected "tenant:instance:site" '
            '(e.g. "nvidia:wd5:NVIDIAExternalCareerSite")'
        )
    tenant, instance, site = parts
    return tenant, instance, site


def _posting_url(tenant: str, instance: str, site: str, external_path: str) -> str:
    return f"https://{tenant}.{instance}.myworkdayjobs.com/en-US/{site}{external_path}"


_HOST_RE = re.compile(r"^([^.]+)\.([^.]+)\.myworkdayjobs\.com$", re.IGNORECASE)
_LOCALE_RE = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$")
DETAIL_ENDPOINT = "https://{tenant}.{instance}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"


def _detail_url(posting_url: str) -> str | None:
    """Map a stored posting URL to its CXS detail URL; None if it isn't Workday-shaped."""
    parsed = urlparse(posting_url or "")
    host = _HOST_RE.match(parsed.netloc)
    if parsed.scheme != "https" or not host:
        return None
    idx = parsed.path.find("/job/")
    if idx < 0:
        return None
    segments = [s for s in parsed.path[:idx].split("/") if s]
    if len(segments) == 2 and _LOCALE_RE.match(segments[0]):
        segments = segments[1:]
    if len(segments) != 1:
        return None
    return DETAIL_ENDPOINT.format(
        tenant=host.group(1), instance=host.group(2), site=segments[0], path=parsed.path[idx:]
    )


def _robots_allowed(detail_url: str) -> bool:
    """True only when the host's robots.txt permits `detail_url` for our agent.
    Fail closed: an unreachable/5xx robots.txt means no fetch. 401/403 = disallow-all,
    other 4xx (e.g. 404) = no rules, per RFC 9309."""
    parsed = urlparse(detail_url)
    try:
        resp = polite_get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
    except FetchError:
        return False
    if resp.status_code in (401, 403):
        return False
    if resp.status_code >= 500:
        return False
    if resp.status_code != 200:
        return True
    parser = RobotFileParser()
    parser.parse((resp.text or "").splitlines())
    return parser.can_fetch(USER_AGENT, detail_url)


def fetch_description(posting_url: str) -> str:
    """On-demand JD text for one stored Workday posting URL; "" on any failure
    (unparseable URL, robots disallow, HTTP/JSON/shape problem). Never raises."""
    detail = _detail_url(posting_url)
    if detail is None or not _robots_allowed(detail):
        return ""
    try:
        resp = polite_get(detail, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return ""
        html = resp.json()["jobPostingInfo"]["jobDescription"]
        return strip_html(html) if isinstance(html, str) else ""
    except (FetchError, ValueError, KeyError, TypeError):
        return ""


def fetch(slug: str, company: str | None = None) -> list[dict]:
    """Fetch and normalize postings for one Workday tenant/instance/site board.

    Raises BoardNotFound on a malformed slug or a 404 (bad tenant/instance/
    site combination). Raises FetchError on other unrecoverable HTTP/network/
    JSON issues. Paginates by offset until `total` postings have been seen,
    a page comes back short/empty, or MAX_POSTINGS is reached — whichever
    comes first.
    """
    tenant, instance, site = parse_slug(slug)
    endpoint = ENDPOINT.format(tenant=tenant, instance=instance, site=site)

    out: list[dict] = []
    offset = 0
    total: int | None = None

    while True:
        resp = polite_post(
            endpoint,
            json_body={
                "appliedFacets": {},
                "limit": PAGE_LIMIT,
                "offset": offset,
                "searchText": "",
            },
        )

        if resp.status_code == 404:
            raise BoardNotFound(slug)
        if resp.status_code != 200:
            raise FetchError(f"workday {slug}: HTTP {resp.status_code}", resp.status_code)

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise FetchError(f"workday {slug}: invalid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            break

        job_postings = payload.get("jobPostings") or []
        if total is None:
            total = payload.get("total")

        for posting in job_postings:
            external_path = posting.get("externalPath")
            if not external_path:
                continue
            out.append(
                {
                    "source_posting_id": external_path,
                    "company": company or tenant,
                    "title": posting.get("title"),
                    "location": posting.get("locationsText"),
                    "url": _posting_url(tenant, instance, site, external_path),
                    "posted_at": None,
                    "raw": posting,
                }
            )

        offset += len(job_postings)

        if offset >= MAX_POSTINGS:
            if isinstance(total, int) and total > MAX_POSTINGS:
                print(
                    f"WARNING: workday {slug}: board has {total} postings, "
                    f"fetched {MAX_POSTINGS} (cap reached, {total - MAX_POSTINGS} left unfetched)"
                )
            break
        if not job_postings or (isinstance(total, int) and offset >= total):
            break

    return out
