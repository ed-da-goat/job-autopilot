"""Gmail job-alert ingester: reads job-alert emails from a registry of
job-board senders (LinkedIn, Wellfound, Indeed, Dice, ZipRecruiter, Monster,
Glassdoor, Otta/Welcome to the Jungle, Built In, Work at a Startup), read via
the Gmail REST API with a read-only OAuth scope.

Token-gated: dormant until the candidate completes a one-time OAuth consent (the
`gmail-auth` CLI command). `is_configured()` is a simple file check — no
network call, no refresh attempt — so the harvest orchestrator can skip this
source cheaply when it hasn't been set up yet.

Auth model: a Desktop-app OAuth client downloaded from
console.cloud.google.com (project job-hunt-sheets-5002) is expected at
CLIENT_PATH. `run_auth_flow()` runs the interactive installed-app flow once
and persists a refresh token at TOKEN_PATH; `fetch()` never triggers that
flow itself — it only loads and (if needed) silently refreshes the saved
token. Both paths are gitignored.

Gmail access goes through `google.auth.transport.requests.AuthorizedSession`
directly against the REST API (same google-auth machinery `sheets.py` already
depends on) rather than pulling in `google-api-python-client`.

Provider registry (PROVIDERS): each entry maps a provider slug to
{sender_domains, id_prefix, parser}. Sender matching is by domain suffix of
the From header, so "em.linkedin.com" matches the "linkedin.com" entry. The
Gmail search query is built entirely from the registry (one `@domain` OR
clause per sender_domains entry) — adding a provider is exactly one dict
entry plus (for anything beyond the two bespoke parsers) a job-link regex and
an id-extraction rule fed into the shared generic block parser.

Parsing is best-effort and defensive: job-alert HTML is written for email
clients, not scrapers, and shifts over time. A single malformed job block
within an email is skipped and does not affect the rest of that email; a
failure processing an entire message (bad HTML, missing body, decode error,
unrecognized sender) is caught, counted, and folded into one aggregate
WARNING per sweep rather than crashing the run.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_module
import re
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from jobhunt.net import FetchError

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CLIENT_PATH = DATA_DIR / "gmail-oauth-client.json"
TOKEN_PATH = DATA_DIR / "gmail-token.json"

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
ALERT_LOOKBACK_DAYS = 14
MAX_MESSAGES = 200
PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 15


# --------------------------------------------------------------------------
# Auth plumbing
# --------------------------------------------------------------------------


def is_configured() -> bool:
    """True once a saved Gmail token exists. Pure file check — no refresh,
    no network — so callers can skip this source cheaply when unset up."""
    return TOKEN_PATH.exists()


def load_credentials():
    """Load the saved token and refresh it in place if expired. Returns None
    if no token file exists yet (never raises for the "not configured" case
    — callers should check `is_configured()` first)."""
    if not TOKEN_PATH.exists():
        return None

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def run_auth_flow() -> None:
    """Interactive one-time OAuth consent. Raises RuntimeError with setup
    instructions if CLIENT_PATH is missing. Never prints token material —
    only a success message and the destination path."""
    if not CLIENT_PATH.exists():
        raise RuntimeError(
            f"alerts: missing {CLIENT_PATH} — create a Desktop-app OAuth client: "
            "console.cloud.google.com -> project job-hunt-sheets-5002 -> APIs & "
            "Services -> Credentials -> Create OAuth client ID -> Desktop app -> "
            f"download the JSON to {CLIENT_PATH}, then re-run this command."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"alerts: OAuth consent complete — token saved to {TOKEN_PATH}")


# --------------------------------------------------------------------------
# HTML/text helpers shared by every parser (bespoke and generic alike)
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ANCHOR_SPAN_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_SEP_RE = re.compile(r"[·•|]|\s{2,}")
_TR_RE = re.compile(r"<tr[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
_CLOSE_A_RE = re.compile(r"</a\s*>", re.IGNORECASE)

_NOISE_ANCHOR_TEXT = {
    "",
    "apply",
    "easy apply",
    "view job",
    "see job",
    "see jobs",
    "actively hiring",
    "promoted",
}

# Mirrors jobhunt/dedupe.py's `_TRACKING_PARAM_PREFIXES`/`_TRACKING_PARAM_NAMES`
# (kept as a local copy — this module's file boundary doesn't include
# dedupe.py, so it isn't imported). Used to clean tracking-redirect job URLs
# (Indeed's /rc/clk, Glassdoor's partner links, Wellfound's /l/ links, ...)
# without discarding real identifying query params like Indeed's `jk`.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = {
    "gclid",
    "fbclid",
    "ref",
    "src",
    "source",
    "gh_src",
    "lever-source",
    "lever-origin",
}


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAM_NAMES or lowered.startswith(_TRACKING_PARAM_PREFIXES)


def _strip_tracking_params(url: str) -> str:
    """Drop only obvious tracking junk from a URL's query string, keeping
    everything else (e.g. Indeed's `jk` job-key param) intact. Never follows
    redirects — the alert's target URL is stored as-is, tracking params
    aside, to keep fetch() network-light."""
    parsed = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _is_tracking_param(k)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(kept), ""))


def _clean_text(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _blocks_for(html_text: str, url_re: re.Pattern[str]) -> list[str]:
    """Split an alert email's HTML into per-job blocks. Table rows are the
    primary unit (job-board digest emails are built from HTML tables for
    email-client compatibility); if no `<tr>` contains a job link, fall back
    to splitting on link occurrence so unstructured markup still parses
    instead of yielding zero postings."""
    rows = [b for b in _TR_RE.findall(html_text) if url_re.search(b)]
    if rows:
        return rows

    matches = list(url_re.finditer(html_text))
    blocks = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html_text)
        blocks.append(html_text[start:end])
    return blocks


def _title_from_matches(block_html: str, matches: list[re.Match[str]]) -> str | None:
    """First anchor (in document order) whose text isn't boilerplate. Job
    alert emails commonly wrap a job link twice per block — once around a
    logo image (empty text) and once around the title — so the first match
    is often the wrong one."""
    for match in matches:
        tail = block_html[match.end():]
        # `match` only covers the `href="..."` attribute, not the rest of the
        # opening `<a ...>` tag — skip past its closing '>' before reading
        # anchor text, or a stray '>' character leaks in as bogus "text".
        tag_close = tail.find(">")
        if tag_close == -1:
            continue
        after_open_tag = tail[tag_close + 1:]
        close = _CLOSE_A_RE.search(after_open_tag)
        inner = after_open_tag[: close.start()] if close else after_open_tag
        text = _clean_text(inner)
        if text and text.lower() not in _NOISE_ANCHOR_TEXT:
            return text
    return None


def _company_location(block_html: str) -> tuple[str | None, str | None]:
    """Strip every anchor entirely (so link text never leaks into these
    fields), then read the remaining plain text as up to two "·"/"|"/
    double-space-separated segments: company, then location."""
    remainder = _ANCHOR_SPAN_RE.sub(" ", block_html)
    text = _clean_text(remainder)
    if not text:
        return None, None
    parts = [p.strip() for p in _SEP_RE.split(text) if p.strip()]
    company = parts[0] if parts else None
    location = parts[1] if len(parts) > 1 else None
    return company, location


def _id_from_url(url: str, query_param: str | None) -> str | None:
    """Prefer a named query param (e.g. Indeed's `jk`, ZipRecruiter's `jid`)
    when present; otherwise fall back to the URL's last non-empty path
    segment (typically a numeric id or a slug that includes one)."""
    parsed = urlsplit(url)
    if query_param:
        values = parse_qs(parsed.query).get(query_param)
        if values and values[0]:
            return values[0]
    segments = [s for s in parsed.path.split("/") if s]
    return segments[-1] if segments else None


def _id_or_hash(url: str, query_param: str | None = None) -> str:
    """`_id_from_url`, falling back to a sha1 of the full URL for tracking
    links with no stable identifier in the path or query (e.g. an opaque
    Indeed `/rc/clk` link with no `jk` param)."""
    found = _id_from_url(url, query_param)
    if found:
        return found
    return hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()


def _trailing_numeric_segment(url: str) -> str | None:
    """The digit run at the start of the last non-empty path segment (e.g.
    "123456789-software-engineer" -> "123456789"), for providers (Google
    Careers) whose job id is embedded in an otherwise slug-shaped segment
    rather than isolated as its own segment or query param."""
    parsed = urlsplit(url)
    segments = [s for s in parsed.path.split("/") if s]
    for segment in reversed(segments):
        digits = re.match(r"\d+", segment)
        if digits:
            return digits.group(0)
    return None


def _google_careers_id(match: re.Match[str]) -> str:
    url = match.group(1)
    return _trailing_numeric_segment(url) or hashlib.sha1(
        url.encode("utf-8"), usedforsecurity=False
    ).hexdigest()


# --------------------------------------------------------------------------
# LinkedIn parser (bespoke — LinkedIn's markup and canonical-URL shape don't
# fit the generic block parser cleanly enough to be worth generalizing)
# --------------------------------------------------------------------------

# Matches both the tracking-wrapped `/comm/jobs/view/{id}/...` form and the
# plain `/jobs/view/{id}` form; query strings (trackingId, refId, ...) are
# tolerated and discarded.
_LI_JOB_URL_RE = re.compile(
    r'href="(https://www\.linkedin\.com/(?:comm/)?jobs/view/(\d+)[^"]*)"',
    re.IGNORECASE,
)


def _parse_linkedin_block(block_html: str) -> dict | None:
    matches = list(_LI_JOB_URL_RE.finditer(block_html))
    if not matches:
        return None
    job_id = matches[0].group(2)

    title = _title_from_matches(block_html, matches)
    if not title:
        return None

    company, location = _company_location(block_html)
    if not company:
        return None

    return {
        "source_posting_id": f"li-{job_id}",
        "title": title,
        "company": company,
        "location": location,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
    }


def parse_linkedin_alert(html_text: str) -> list[dict]:
    """Parse a LinkedIn job-alert email body. Returns a list of
    {source_posting_id, title, company, location, url} dicts. Unparseable
    blocks (missing title/company after best-effort extraction, or any
    unexpected error walking a single block) are skipped rather than
    raising."""
    postings: list[dict] = []
    for block in _blocks_for(html_text, _LI_JOB_URL_RE):
        try:
            posting = _parse_linkedin_block(block)
        except Exception:
            continue
        if posting is not None:
            postings.append(posting)
    return postings


# --------------------------------------------------------------------------
# Wellfound parser (bespoke — two distinct id shapes: /jobs/{slug} vs the
# /l/{token} tracking-redirect form)
# --------------------------------------------------------------------------

# Direct job links (`/jobs/{id}-{slug}`) and email-tracking redirect links
# (`/l/{token}`) both appear in Wellfound digests; the redirect target isn't
# resolvable without an extra HTTP hop, which is out of scope here, so the
# redirect URL itself (tracking params stripped) is used as-is.
_WF_JOB_URL_RE = re.compile(
    r'href="(https://(?:www\.)?wellfound\.com/(?:jobs/([a-zA-Z0-9][\w-]*)|l/([A-Za-z0-9_-]+))[^"]*)"',
    re.IGNORECASE,
)


def _parse_wellfound_block(block_html: str) -> dict | None:
    matches = list(_WF_JOB_URL_RE.finditer(block_html))
    if not matches:
        return None
    first = matches[0]
    slug_id = first.group(2) or first.group(3)
    if not slug_id:
        return None
    canonical_url = _strip_tracking_params(first.group(1))

    title = _title_from_matches(block_html, matches)
    if not title:
        return None

    company, location = _company_location(block_html)
    if not company:
        return None

    return {
        "source_posting_id": f"wf-{slug_id}",
        "title": title,
        "company": company,
        "location": location,
        "url": canonical_url,
    }


def parse_wellfound_alert(html_text: str) -> list[dict]:
    """Parse a Wellfound job-alert email body. Same contract and
    never-crash behavior as `parse_linkedin_alert`."""
    postings: list[dict] = []
    for block in _blocks_for(html_text, _WF_JOB_URL_RE):
        try:
            posting = _parse_wellfound_block(block)
        except Exception:
            continue
        if posting is not None:
            postings.append(posting)
    return postings


# --------------------------------------------------------------------------
# Generic parser — shared by every other provider in the registry. Each
# provider supplies a job-link regex (must capture the full URL as group 1)
# and an id-extraction rule; the block-splitting and title/company/location
# heuristics are the same ones the bespoke parsers above use.
# --------------------------------------------------------------------------

IdFromMatch = Callable[[re.Match[str]], str | None]


def _parse_generic_block(
    block_html: str, id_prefix: str, link_re: re.Pattern[str], id_from_match: IdFromMatch
) -> dict | None:
    matches = list(link_re.finditer(block_html))
    if not matches:
        return None
    raw_id = id_from_match(matches[0])
    if not raw_id:
        return None

    title = _title_from_matches(block_html, matches)
    if not title:
        return None

    company, location = _company_location(block_html)
    if not company:
        return None

    return {
        "source_posting_id": f"{id_prefix}{raw_id}",
        "title": title,
        "company": company,
        "location": location,
        "url": _strip_tracking_params(matches[0].group(1)),
    }


def _make_generic_parser(
    id_prefix: str, link_re: re.Pattern[str], id_from_match: IdFromMatch
) -> Callable[[str], list[dict]]:
    """Build a `parse(html_text) -> list[dict]` closure for one registry
    entry. Same never-crash contract as the bespoke parsers: an unresolvable
    or exception-raising block is silently skipped, not fatal to the email."""

    def parse(html_text: str) -> list[dict]:
        postings: list[dict] = []
        for block in _blocks_for(html_text, link_re):
            try:
                posting = _parse_generic_block(block, id_prefix, link_re, id_from_match)
            except Exception:
                continue
            if posting is not None:
                postings.append(posting)
        return postings

    return parse


# Job-link patterns, one per generic provider. Each captures the full URL as
# group 1 (consumed by `_strip_tracking_params` and `id_from_match` below).
_INDEED_JOB_URL_RE = re.compile(r'href="(https://[^"]*\bindeed\.com/(?:viewjob|rc/clk)[^"]*)"', re.IGNORECASE)
_ZIPRECRUITER_JOB_URL_RE = re.compile(
    r'href="(https://[^"]*\bziprecruiter\.com/(?:c/|jobs/)[^"]*)"', re.IGNORECASE
)
_DICE_JOB_URL_RE = re.compile(
    r'href="(https://[^"]*\bdice\.com/(?:job-detail|jobs/detail)[^"]*)"', re.IGNORECASE
)
_MONSTER_JOB_URL_RE = re.compile(r'href="(https://[^"]*\bmonster\.com/job-openings[^"]*)"', re.IGNORECASE)
_GLASSDOOR_JOB_URL_RE = re.compile(
    r'href="(https://[^"]*\bglassdoor\.com/(?:job-listing|partner/jobListing)[^"]*)"', re.IGNORECASE
)
# otta.com/jobs/... or welcometothejungle.com/<locale>/companies/<slug>/jobs/...
_OTTA_JOB_URL_RE = re.compile(
    r'href="(https://[^"]*(?:\botta\.com/jobs|welcometothejungle\.com/[^"]*/jobs)[^"]*)"', re.IGNORECASE
)
# Built In has city-specific sites (builtinnyc.com, builtinsf.com, ...) in
# addition to builtin.com itself; \b keeps this from matching a domain that
# merely contains "builtin" as part of an unrelated word.
_BUILTIN_JOB_URL_RE = re.compile(r'href="(https://[^"]*\bbuiltin[a-z]{0,12}\.com/job/[^"]*)"', re.IGNORECASE)
_WORKATASTARTUP_JOB_URL_RE = re.compile(
    r'href="(https://[^"]*\bworkatastartup\.com/(?:jobs|companies/[^"]*/jobs)[^"]*)"', re.IGNORECASE
)
# Google Careers saved-search alerts link to either the "About Careers" path
# or the careers.google.com subdomain, depending on template version.
_GOOGLE_CAREERS_JOB_URL_RE = re.compile(
    r'href="(https://[^"]*(?:google\.com/about/careers/applications/jobs/results'
    r'|careers\.google\.com/jobs/results)[^"]*)"',
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Provider registry — sender_domains drives both the Gmail search query and
# runtime sender->provider routing; id_prefix + parser drive posting output.
# --------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "linkedin": {
        "sender_domains": ("linkedin.com",),
        "id_prefix": "li-",
        "parser": parse_linkedin_alert,
    },
    "wellfound": {
        # The two senders this module has always watched for.
        "sender_domains": ("hi.wellfound.com", "wellfound.com"),
        "id_prefix": "wf-",
        "parser": parse_wellfound_alert,
    },
    "indeed": {
        "sender_domains": ("indeed.com",),
        "id_prefix": "in-",
        "parser": _make_generic_parser(
            "in-", _INDEED_JOB_URL_RE, lambda m: _id_or_hash(m.group(1), query_param="jk")
        ),
    },
    "dice": {
        "sender_domains": ("email.dice.com", "dice.com"),
        "id_prefix": "dice-",
        "parser": _make_generic_parser("dice-", _DICE_JOB_URL_RE, lambda m: _id_or_hash(m.group(1))),
    },
    "ziprecruiter": {
        "sender_domains": ("ziprecruiter.com",),
        # "-mail-" avoids colliding with the ziprecruiter API source module's
        # existing "zr-{posting_id}" ids (see jobhunt/sources/ziprecruiter.py).
        "id_prefix": "zr-mail-",
        "parser": _make_generic_parser(
            "zr-mail-", _ZIPRECRUITER_JOB_URL_RE, lambda m: _id_or_hash(m.group(1), query_param="jid")
        ),
    },
    "monster": {
        "sender_domains": ("monster.com",),
        "id_prefix": "mon-",
        "parser": _make_generic_parser(
            "mon-", _MONSTER_JOB_URL_RE, lambda m: _id_or_hash(m.group(1), query_param="jobid")
        ),
    },
    "glassdoor": {
        "sender_domains": ("glassdoor.com",),
        "id_prefix": "gd-",
        "parser": _make_generic_parser(
            "gd-", _GLASSDOOR_JOB_URL_RE, lambda m: _id_or_hash(m.group(1), query_param="jl")
        ),
    },
    "otta": {
        "sender_domains": ("otta.com", "welcometothejungle.com"),
        "id_prefix": "otta-",
        "parser": _make_generic_parser("otta-", _OTTA_JOB_URL_RE, lambda m: _id_or_hash(m.group(1))),
    },
    "builtin": {
        "sender_domains": ("builtin.com",),
        # "-mail-" avoids colliding with a builtin API source module's ids
        # ("bi-{posting_id}"), same rationale as ziprecruiter's zr-mail-.
        "id_prefix": "bi-mail-",
        "parser": _make_generic_parser("bi-mail-", _BUILTIN_JOB_URL_RE, lambda m: _id_or_hash(m.group(1))),
    },
    "workatastartup": {
        "sender_domains": ("ycombinator.com", "workatastartup.com"),
        "id_prefix": "waas-",
        "parser": _make_generic_parser(
            "waas-", _WORKATASTARTUP_JOB_URL_RE, lambda m: _id_or_hash(m.group(1))
        ),
    },
    "googlecareers": {
        # A bare "google.com" domain entry would suffix-match every Google
        # service email (calendar, drive, security, ...). An entry containing
        # "@" is matched as an exact sender address instead of a domain
        # suffix — see _provider_for_sender / _build_query.
        "sender_domains": ("careers-noreply@google.com",),
        # "goog-" is reserved for a possible future Google Careers API source
        # module; "-mail-" keeps this ingester's ids distinct from it.
        "id_prefix": "goog-mail-",
        "parser": _make_generic_parser("goog-mail-", _GOOGLE_CAREERS_JOB_URL_RE, _google_careers_id),
    },
}


def _build_query() -> str:
    """One `from:(...)` clause built straight off the registry's
    sender_domains — adding a provider to PROVIDERS is enough to add it to
    the sweep, no separate query edit required. Entries containing "@" are
    exact sender addresses (e.g. "careers-noreply@google.com") and go into
    the clause as-is; plain domains are prefixed with "@" so Gmail matches
    any sender at that domain or a subdomain of it."""
    entries: list[str] = []
    for config in PROVIDERS.values():
        for domain in config["sender_domains"]:
            if domain not in entries:
                entries.append(domain)
    clause = " OR ".join(entry if "@" in entry else f"@{entry}" for entry in entries)
    return f"from:({clause}) newer_than:{ALERT_LOOKBACK_DAYS}d"


QUERY = _build_query()

_SENDER_ADDRESS_RE = re.compile(r"([\w.+-]+@[\w.-]+)")


def _provider_for_sender(from_header: str) -> str | None:
    """Match a From header against every registered provider. A
    sender_domains entry containing "@" must match the sender's full address
    exactly (case-insensitive) — used when a bare domain would be too broad
    (e.g. "google.com" would suffix-match every Google service, not just
    Careers alerts). A plain-domain entry matches by domain suffix, e.g. a
    sender at "em.linkedin.com" matches the "linkedin.com" registry entry."""
    match = _SENDER_ADDRESS_RE.search(from_header)
    if not match:
        return None
    address = match.group(1).lower()
    domain = address.split("@", 1)[1]
    for slug, config in PROVIDERS.items():
        for sender_domain in config["sender_domains"]:
            if "@" in sender_domain:
                if address == sender_domain.lower():
                    return slug
            elif domain == sender_domain or domain.endswith(f".{sender_domain}"):
                return slug
    return None


# --------------------------------------------------------------------------
# Gmail REST fetch
# --------------------------------------------------------------------------


def _header(headers: list[dict], name: str) -> str | None:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value")
    return None


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_html_part(payload: dict) -> str | None:
    mime_type = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")
    if mime_type == "text/html" and data:
        return _b64url_decode(data)
    for part in payload.get("parts") or []:
        found = _extract_html_part(part)
        if found:
            return found
    return None


def _parse_email_date(date_header: str | None) -> str | None:
    if not date_header:
        return None
    try:
        dt = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt is None:
        return None
    return dt.isoformat()


def _list_message_ids(session) -> list[str]:
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < MAX_MESSAGES:
        params = {"q": QUERY, "maxResults": min(PAGE_SIZE, MAX_MESSAGES - len(ids))}
        if page_token:
            params["pageToken"] = page_token
        resp = session.get(f"{GMAIL_API_BASE}/messages", params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise FetchError(f"alerts: messages.list HTTP {resp.status_code}", resp.status_code)
        payload = resp.json()
        ids.extend(m["id"] for m in (payload.get("messages") or []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return ids[:MAX_MESSAGES]


def _get_message(session, message_id: str) -> dict:
    resp = session.get(
        f"{GMAIL_API_BASE}/messages/{message_id}",
        params={"format": "full"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise FetchError(f"alerts: messages.get HTTP {resp.status_code} (id={message_id})", resp.status_code)
    return resp.json()


def _postings_from_message(message: dict) -> list[dict]:
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    from_header = _header(headers, "From") or ""
    subject = _header(headers, "Subject") or ""
    posted_at = _parse_email_date(_header(headers, "Date"))

    provider_slug = _provider_for_sender(from_header)
    if provider_slug is None:
        return []

    html_body = _extract_html_part(payload)
    if not html_body:
        return []

    parser = PROVIDERS[provider_slug]["parser"]
    parsed = parser(html_body)

    postings = []
    for item in parsed:
        postings.append(
            {
                "source_posting_id": item["source_posting_id"],
                "company": item["company"],
                "title": item["title"],
                "location": item.get("location"),
                "url": item["url"],
                "posted_at": posted_at,
                "raw": {"subject": subject, "message_id": message.get("id"), "provider": provider_slug, **item},
            }
        )
    return postings


def fetch() -> list[dict]:
    """Fetch and parse recent job-alert emails across every registered
    provider. Raises RuntimeError with setup instructions if not
    `is_configured()`. Dedupes postings by source_posting_id across messages
    (the same job commonly repeats across several days of alerts)."""
    if not is_configured():
        raise RuntimeError(
            f"alerts: no Gmail token at {TOKEN_PATH} — run "
            "`uv run python -m jobhunt gmail-auth` once to complete the OAuth "
            "consent (requires a Desktop-app OAuth client saved at "
            f"{CLIENT_PATH}; see that command's help for how to create one)."
        )

    creds = load_credentials()
    if creds is None:
        raise RuntimeError(f"alerts: failed to load Gmail credentials from {TOKEN_PATH}")

    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(creds)
    message_ids = _list_message_ids(session)

    postings_by_id: dict[str, dict] = {}
    message_errors = 0

    for message_id in message_ids:
        try:
            message = _get_message(session, message_id)
            for posting in _postings_from_message(message):
                postings_by_id.setdefault(posting["source_posting_id"], posting)
        except Exception:
            message_errors += 1
            continue

    if message_errors:
        print(f"WARNING: alerts: skipped {message_errors} unparseable message(s)")

    return list(postings_by_id.values())
