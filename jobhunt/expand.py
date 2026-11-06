"""Grow the harvested company universe by mining SimplifyJobs' New-Grad-Positions
README (plus the local postings table) for ATS links, verifying each
candidate slug with one polite probe against the platform's public API, and
inserting confirmed-live companies into the companies table.

Some Greenhouse boards are embedded on a first-party career-site URL, so the
slug never appears in the URL at all — only a gh_jid/gh_src query param
marks it as Greenhouse-backed. For those, and (opt-in) for existing
watchlist/platformless companies, slugs are GUESSED from the company name or
URL domain and probed the same way, first live hit wins.

Dead candidates are appended to data/bad_slugs.md in the same bullet format
`harvest.py`'s board sweep already writes there, under a new dated section —
never under the existing header, which names a specific past sweep.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jobhunt.net import FetchError, polite_get, polite_post
from jobhunt.sources import simplify, workday

DEFAULT_BAD_SLUGS_PATH = Path(__file__).resolve().parent.parent / "data" / "bad_slugs.md"

_URL_RE = re.compile(r"""https?://[^\s"'>\]]+""")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
# SmartRecruiters companyIds are mixed-case in their public URLs
# (e.g. "SmartRecruiters", "Visa") and that case must survive into the
# stored slug — lowercasing it would build a broken jobs.smartrecruiters.com
# URL. See jobhunt/sources/smartrecruiters.py for the live-verified detail.
_SLUG_RE_CASE_PRESERVING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Workday career-site hosts: {tenant}.{instance}.myworkdayjobs.com, where
# instance is a Workday pod subdomain like "wd1" or "wd5". Matched against
# the ORIGINAL (non-lowercased) netloc so tenant/instance case survives into
# the composite "tenant:instance:site" slug — see jobhunt/sources/workday.py.
_WORKDAY_HOST_RE = re.compile(r"^([^.]+)\.(wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
# Optional language segment before the site name, e.g. /en-US/, /fr-CA/.
_WORKDAY_LANG_SEGMENT_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")

_PROBE_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
}

_BAD_SLUG_LINE_RE = re.compile(
    r"^-\s+(?P<platform>greenhouse|lever|ashby|smartrecruiters|workable|workday):\s+"
    r".*\(slug\s+`(?P<slug>[^`]+)`\)",
    re.IGNORECASE,
)


def _normalize_slug(raw: str) -> str | None:
    slug = raw.strip().lower()
    if not slug or slug == "embed" or not _SLUG_RE.match(slug):
        return None
    return slug


def _normalize_slug_case_preserving(raw: str) -> str | None:
    slug = raw.strip()
    if not slug or not _SLUG_RE_CASE_PRESERVING.match(slug):
        return None
    return slug


def _classify_url(url: str) -> tuple[str, str] | None:
    """Return (platform, slug) for a recognized ATS URL, or None for
    simplify.jobs redirects and every other host."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if host in (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    ):
        if path.startswith("/embed/job_board") or path.startswith("/embed/job_app"):
            for_vals = parse_qs(parsed.query).get("for")
            slug = _normalize_slug(for_vals[0]) if for_vals else None
            return ("greenhouse", slug) if slug else None
        segments = [s for s in path.split("/") if s]
        slug = _normalize_slug(segments[0]) if segments else None
        return ("greenhouse", slug) if slug else None

    if host == "jobs.lever.co":
        segments = [s for s in path.split("/") if s]
        slug = _normalize_slug(segments[0]) if segments else None
        return ("lever", slug) if slug else None

    if host == "jobs.ashbyhq.com":
        segments = [s for s in path.split("/") if s]
        slug = _normalize_slug(segments[0]) if segments else None
        return ("ashby", slug) if slug else None

    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        segments = [s for s in path.split("/") if s]
        slug = _normalize_slug_case_preserving(segments[0]) if segments else None
        return ("smartrecruiters", slug) if slug else None

    if host == "apply.workable.com":
        segments = [s for s in path.split("/") if s]
        slug = _normalize_slug(segments[0]) if segments else None
        return ("workable", slug) if slug else None

    if host.endswith(".workable.com") and host not in ("apply.workable.com", "www.workable.com"):
        subdomain = host.split(".")[0]
        slug = _normalize_slug(subdomain)
        return ("workable", slug) if slug else None

    workday_host = _WORKDAY_HOST_RE.match(parsed.netloc)
    if workday_host:
        tenant, instance = workday_host.group(1), workday_host.group(2)
        segments = [s for s in path.split("/") if s]
        if segments and _WORKDAY_LANG_SEGMENT_RE.match(segments[0]):
            segments = segments[1:]
        if not segments:
            return None
        site = segments[0]
        if not (
            _SLUG_RE_CASE_PRESERVING.match(tenant)
            and _SLUG_RE_CASE_PRESERVING.match(instance)
            and _SLUG_RE_CASE_PRESERVING.match(site)
        ):
            return None
        return ("workday", f"{tenant}:{instance}:{site}")

    return None


def extract_ats_refs(texts: Iterable[str]) -> list[dict]:
    """Scan text blobs for recognized ATS URLs (greenhouse/lever/ashby,
    including the greenhouse embed `?for=` form). Returns deduped
    {platform, slug, company_hint} dicts in first-seen order; company_hint
    is always None here — this function has no row-level context, callers
    that do (e.g. a README row's company cell) attach it themselves."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for text in texts:
        if not text:
            continue
        for match in _URL_RE.finditer(text):
            classified = _classify_url(match.group(0))
            if classified is None:
                continue
            if classified in seen:
                continue
            seen.add(classified)
            platform, slug = classified
            out.append({"platform": platform, "slug": slug, "company_hint": None})
    return out


def _derive_name(slug: str, company_hint: str | None) -> str:
    if company_hint and company_hint.strip():
        return company_hint.strip()
    return slug.replace("-", " ").replace("_", " ").title()


_GH_EMBED_PARAMS = ("gh_jid", "gh_src")


def _has_greenhouse_embed_markers(url: str) -> bool:
    """A gh_jid/gh_src query param is Greenhouse's own embed-tracking
    marker; it survives even when a company mounts the board widget on its
    own first-party career-site URL, where the slug itself is invisible."""
    qs = parse_qs(urlparse(url).query)
    return any(param in qs for param in _GH_EMBED_PARAMS)


def _registrable_domain_base(url: str) -> str | None:
    """Best-effort registrable domain minus TLD, e.g.
    "https://careers.example.com/..." -> "example". Just the second-to-last
    dot-label of the host — no public-suffix list, so multi-part TLDs like
    .co.uk degrade (yields "co"). That's an acceptable seed for a guess,
    never a source of truth (probe() is what actually confirms it)."""
    host = urlparse(url).netloc.lower().split(":")[0]
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return None
    return labels[-2] or None


def extract_embed_signals(texts: Iterable[str]) -> list[dict]:
    """Scan text blobs for a Greenhouse-embedded-board signal: a URL on ANY
    host that isn't already a recognized ATS URL (see _classify_url) but
    whose query string carries gh_jid or gh_src. That URL is confirmed
    Greenhouse-backed even though its slug never appears in it. Returns
    deduped {platform: "greenhouse", domain_base, source_url} dicts;
    company_hint is attached by the caller, same convention as
    extract_ats_refs — this function has no row-level context either."""
    seen_urls: set[str] = set()
    out: list[dict] = []
    for text in texts:
        if not text:
            continue
        for match in _URL_RE.finditer(text):
            url = match.group(0)
            if url in seen_urls:
                continue
            if _classify_url(url) is not None:
                continue  # already a certain ref on a known ATS host
            if not _has_greenhouse_embed_markers(url):
                continue
            seen_urls.add(url)
            out.append(
                {
                    "platform": "greenhouse",
                    "domain_base": _registrable_domain_base(url),
                    "source_url": url,
                }
            )
    return out


def _slugify_alnum(base: str) -> str:
    return re.sub(r"[^a-z0-9]", "", base.lower())


def _slugify_hyphenated(base: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


def _slugify_first_word(base: str) -> str:
    m = re.search(r"[a-z0-9]+", base.lower())
    return m.group(0) if m else ""


def _slug_variants(base: str) -> list[str]:
    """Up to 3 valid, deduped slug guesses from one base string: lowercase
    alphanumeric-only, lowercase with runs of separators collapsed to a
    single hyphen, and the bare first word (dropped if the base is a single
    word — that variant would just repeat the alnum one)."""
    if not base:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in (_slugify_alnum(base), _slugify_hyphenated(base), _slugify_first_word(base)):
        slug = _normalize_slug(raw)
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _guess_slugs(company_hint: str | None, domain_base: str | None) -> list[str]:
    """Slug guesses for one company: company_hint's variants first (when
    present), then domain_base's, deduped in first-seen order and capped at
    4 — the cap only bites when both sources contribute distinct guesses."""
    slugs: list[str] = []
    seen: set[str] = set()
    for base in (company_hint, domain_base):
        if not base:
            continue
        for slug in _slug_variants(base):
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs[:4]


def _guess_name(company_hint: str | None, domain_base: str | None) -> str | None:
    if company_hint and company_hint.strip():
        return company_hint.strip()
    if domain_base:
        return domain_base.replace("-", " ").replace("_", " ").title()
    return None


def _gather_candidates(conn: sqlite3.Connection, readme_text: str) -> dict[tuple[str, str], dict]:
    """All ATS candidates found in the README + local postings table, keyed
    by (platform, slug), each with a derived company name. Not yet filtered
    against known companies or bad_slugs.md — see discover()."""
    candidates: dict[tuple[str, str], dict] = {}

    postings, _skipped = simplify.parse_simplify_markdown(readme_text)
    for posting in postings:
        link_text = (posting.get("raw") or {}).get("link") or posting.get("url") or ""
        for ref in extract_ats_refs([link_text]):
            key = (ref["platform"], ref["slug"])
            candidates.setdefault(
                key,
                {
                    "platform": ref["platform"],
                    "slug": ref["slug"],
                    "name": _derive_name(ref["slug"], posting.get("company")),
                },
            )

    rows = conn.execute("SELECT company, url, raw_json FROM postings").fetchall()
    for row in rows:
        text = " ".join(filter(None, [row["url"], row["raw_json"]]))
        for ref in extract_ats_refs([text]):
            key = (ref["platform"], ref["slug"])
            candidates.setdefault(
                key,
                {
                    "platform": ref["platform"],
                    "slug": ref["slug"],
                    "name": _derive_name(ref["slug"], row["company"]),
                },
            )

    return candidates


def _existing_keys(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], set[str]]:
    rows = conn.execute("SELECT name, platform, slug FROM companies").fetchall()
    platform_slug: set[tuple[str, str]] = set()
    names: set[str] = set()
    for row in rows:
        if row["name"]:
            names.add(row["name"].strip().lower())
        if row["platform"] and row["slug"]:
            platform_slug.add((row["platform"].strip().lower(), row["slug"].strip().lower()))
    return platform_slug, names


def _bad_slug_keys(path: Path | str = DEFAULT_BAD_SLUGS_PATH) -> set[tuple[str, str]]:
    p = Path(path)
    if not p.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _BAD_SLUG_LINE_RE.match(line.strip())
        if m:
            keys.add((m.group("platform").lower(), m.group("slug").lower()))
    return keys


def _add_guess_group(
    groups: dict[tuple[str, str], dict],
    signal: dict,
    company_hint: str | None,
    guess_source: str,
) -> None:
    name = _guess_name(company_hint, signal.get("domain_base"))
    if not name:
        return
    slugs = _guess_slugs(company_hint, signal.get("domain_base"))
    if not slugs:
        return
    key = (signal["platform"], name.lower())
    groups.setdefault(
        key,
        {
            "platform": signal["platform"],
            "name": name,
            "kind": "guessed",
            "guess_source": guess_source,
            "slugs": slugs,
        },
    )


def _gather_embed_guess_groups(conn: sqlite3.Connection, readme_text: str) -> dict[tuple[str, str], dict]:
    """Greenhouse-embedded-board guess groups: companies confirmed
    Greenhouse-backed via a gh_jid/gh_src marker on a URL that isn't itself
    a recognized ATS URL (the board is embedded on a first-party career
    site, so the slug never appears there). Keyed by (platform, name.lower())
    so repeat sightings of the same company collapse into one group."""
    groups: dict[tuple[str, str], dict] = {}

    postings, _skipped = simplify.parse_simplify_markdown(readme_text)
    for posting in postings:
        link_text = (posting.get("raw") or {}).get("link") or posting.get("url") or ""
        for signal in extract_embed_signals([link_text]):
            _add_guess_group(groups, signal, posting.get("company"), "embed_url")

    rows = conn.execute("SELECT company, url, raw_json FROM postings").fetchall()
    for row in rows:
        text = " ".join(filter(None, [row["url"], row["raw_json"]]))
        for signal in extract_embed_signals([text]):
            _add_guess_group(groups, signal, row["company"], "embed_url")

    return groups


def _gather_watchlist_guess_groups(conn: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Name-derived greenhouse guess groups for watchlist companies: rows
    already marked watchlist=1, or rows with no platform at all that aren't
    (yet) on the watchlist either. Greenhouse only — this slice is bounded
    to one platform; the caller opts in via guess_watchlist=True."""
    rows = conn.execute(
        """
        SELECT name FROM companies
        WHERE watchlist = 1 OR ((platform IS NULL OR platform = '') AND watchlist = 0)
        """
    ).fetchall()
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        name = (row["name"] or "").strip()
        if not name:
            continue
        signal = {"platform": "greenhouse", "domain_base": None}
        _add_guess_group(groups, signal, name, "watchlist_name")
    return groups


def _filter_guess_groups(
    groups: dict[tuple[str, str], dict],
    existing_platform_slug: set[tuple[str, str]],
    existing_names: set[str],
    dead_keys: set[tuple[str, str]],
    *,
    skip_name_check: bool,
) -> list[dict]:
    """Same dedupe rules as discover(), adapted for a guess GROUP (several
    candidate slugs per company): drop the whole group if its name already
    matches a known company (skip_name_check=True for watchlist groups,
    which ARE an existing company by construction — that check would zero
    every group), then drop individual slug variants already claimed by
    some other company or recorded dead."""
    out: list[dict] = []
    for cand in groups.values():
        if not skip_name_check and cand["name"].strip().lower() in existing_names:
            continue
        platform = cand["platform"]
        slugs = [
            s
            for s in cand["slugs"]
            if (platform, s) not in existing_platform_slug and (platform, s) not in dead_keys
        ]
        if not slugs:
            continue
        out.append({**cand, "slugs": slugs})
    return out


def discover(
    conn: sqlite3.Connection,
    readme_text: str,
    bad_slugs_path: Path | str = DEFAULT_BAD_SLUGS_PATH,
) -> list[dict]:
    """New {platform, slug, name} candidates: found in the README or local
    postings table, and NOT already a companies row (by (platform, slug) or
    by name, case-insensitive) and NOT already recorded dead in bad_slugs.md."""
    candidates = _gather_candidates(conn, readme_text)
    existing_platform_slug, existing_names = _existing_keys(conn)
    dead_keys = _bad_slug_keys(bad_slugs_path)

    out: list[dict] = []
    for key, cand in candidates.items():
        if key in existing_platform_slug or key in dead_keys:
            continue
        if cand["name"].strip().lower() in existing_names:
            continue
        out.append(cand)
    return out


def probe(platform: str, slug: str) -> bool:
    """One polite probe against the platform's public board API.

    True = live (200, parseable JSON, right shape). False = confirmed dead
    (404, or 200 with a payload that isn't a job list — Ashby has been
    observed returning 200 with an error body instead of 404). FetchError
    propagates on network trouble or an unexpected non-200/404 status; a
    probe error must never be recorded as a dead slug.

    Every platform but workday is a plain GET against `_PROBE_URLS`. Workday
    has no such lookup endpoint — its composite "tenant:instance:site" slug
    is parsed and the same POST the real board makes (limit=1, offset=0) is
    sent to its jobs endpoint; a malformed slug is treated as dead rather
    than raising, matching the 404 case for every other platform.
    """
    if platform == "workday":
        try:
            tenant, instance, site = workday.parse_slug(slug)
        except workday.BoardNotFound:
            return False
        url = workday.ENDPOINT.format(tenant=tenant, instance=instance, site=site)
        resp = polite_post(
            url, json_body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        )
    else:
        url = _PROBE_URLS[platform].format(slug=slug)
        resp = polite_get(url)

    if resp.status_code == 404:
        return False
    if resp.status_code != 200:
        raise FetchError(f"{platform} probe {slug}: HTTP {resp.status_code}", resp.status_code)

    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise FetchError(f"{platform} probe {slug}: invalid JSON: {exc}") from exc

    if platform == "lever":
        return isinstance(payload, list)
    if platform == "smartrecruiters":
        return isinstance(payload, dict) and isinstance(payload.get("content"), list)
    if platform == "workday":
        return isinstance(payload, dict) and isinstance(payload.get("jobPostings"), list)
    return isinstance(payload, dict) and isinstance(payload.get("jobs"), list)


def _insert_company(conn: sqlite3.Connection, name: str, platform: str, slug: str, now: str) -> bool:
    cur = conn.execute(
        """
        INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at)
        VALUES (?, 'auto-discovered', ?, ?, 'auto-yes', NULL, 0, ?)
        ON CONFLICT(name) DO NOTHING
        """,
        (name, platform, slug, now),
    )
    return cur.rowcount > 0


def _promote_watchlist_company(conn: sqlite3.Connection, name: str, platform: str, slug: str) -> bool:
    """Promote an existing companies row (a watchlist star or a platformless
    row) to a confirmed board in place, instead of inserting a duplicate:
    clears watchlist, fills platform/slug, marks verified, and backfills
    category only if it was empty. Guarded to rows that don't already have
    both platform and slug set — a row that's already placed elsewhere is
    never touched, even if it happens to carry watchlist=1. Returns True iff
    a row was actually updated."""
    cur = conn.execute(
        """
        UPDATE companies
        SET watchlist = 0,
            platform = ?,
            slug = ?,
            verified = 'auto-yes',
            category = COALESCE(category, 'auto-promoted')
        WHERE name = ? AND (platform IS NULL OR platform = '' OR slug IS NULL OR slug = '')
        """,
        (platform, slug, name),
    )
    return cur.rowcount > 0


def _append_dead_slugs(dead: list[dict], path: Path | str = DEFAULT_BAD_SLUGS_PATH) -> None:
    if not dead:
        return
    p = Path(path)
    today = datetime.now(UTC).date().isoformat()
    header = f"## Auto-discovered dead slugs ({today}, seed-expand)"
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = [f"- {d['platform']}: {d['name']} (slug `{d['slug']}`): 404 board not found" for d in dead]

    if header in existing:
        new_text = existing.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    else:
        new_text = existing.rstrip("\n") + "\n\n" + header + "\n\n" + "\n".join(lines) + "\n"
    p.write_text(new_text.lstrip("\n"), encoding="utf-8")


def _probe_guess_group(group: dict, summary: dict, dead: list[dict]) -> str | None:
    """First-hit-wins probing across one guess group's slug variants: stop
    at the first live hit (remaining variants are never probed, never
    recorded dead — they were never confirmed either way). Returns the
    winning slug, or None if every probed variant came back dead or errored."""
    for slug in group["slugs"]:
        summary["probed"] += 1
        try:
            is_live = probe(group["platform"], slug)
        except FetchError as exc:
            summary["errors"] += 1
            print(f"  ERROR probing {group['platform']}:{slug} (guess for {group['name']}): {exc}")
            continue
        if is_live:
            return slug
        summary["dead"] += 1
        dead.append({"platform": group["platform"], "slug": slug, "name": group["name"]})
    return None


def run_expand(
    conn: sqlite3.Connection,
    limit: int | None = None,
    dry_run: bool = False,
    readme_text: str | None = None,
    bad_slugs_path: Path | str = DEFAULT_BAD_SLUGS_PATH,
    include_embed_guesses: bool = True,
    guess_watchlist: bool = False,
) -> dict:
    """Discover, probe, and insert new ATS companies from SimplifyJobs +
    local postings.

    `readme_text` defaults to None, which fetches the live README via
    polite_get; passing text (as tests do) skips that network call. dry_run
    additionally skips all probes and writes, only printing the candidate
    count and first 20 of each kind.

    Beyond the certain (platform, slug) refs parsed straight out of a URL,
    two opt-in-by-default sources GUESS a Greenhouse slug for a company
    whose board is embedded on a first-party career site (no slug in the
    URL at all):

    - include_embed_guesses (default True): any URL — on any host — whose
      query string carries gh_jid or gh_src is confirmed Greenhouse-backed.
      Slug variants are guessed from the company_hint and/or the URL's
      domain, probed in order, first live hit wins and is inserted.
    - guess_watchlist (default False): the same name-derived guess
      treatment against greenhouse ONLY, applied to existing companies rows
      that are watchlist=1 or platformless. A live hit PROMOTES that row in
      place (watchlist cleared, platform/slug filled, verified='auto-yes',
      category backfilled only if empty) rather than inserting a duplicate —
      guarded so a row that already has both platform and slug set is never
      touched, even a watchlist=1 one.
    """
    if readme_text is None:
        resp = polite_get(simplify.URL)
        if resp.status_code != 200:
            raise FetchError(f"seed-expand: README fetch HTTP {resp.status_code}", resp.status_code)
        readme_text = resp.text

    all_candidates = _gather_candidates(conn, readme_text)
    new_candidates = discover(conn, readme_text, bad_slugs_path=bad_slugs_path)
    parsed_names = {cand["name"].strip().lower() for cand in new_candidates}

    existing_platform_slug, existing_names = _existing_keys(conn)
    dead_keys = _bad_slug_keys(bad_slugs_path)

    embed_groups: list[dict] = []
    if include_embed_guesses:
        raw_embed_groups = _gather_embed_guess_groups(conn, readme_text)
        embed_groups = _filter_guess_groups(
            raw_embed_groups, existing_platform_slug, existing_names, dead_keys, skip_name_check=False
        )
        # A parsed candidate found this run beats a guess for the same company.
        embed_groups = [g for g in embed_groups if g["name"].strip().lower() not in parsed_names]

    watchlist_groups: list[dict] = []
    if guess_watchlist:
        raw_watchlist_groups = _gather_watchlist_guess_groups(conn)
        watchlist_groups = _filter_guess_groups(
            raw_watchlist_groups, existing_platform_slug, existing_names, dead_keys, skip_name_check=True
        )
        watchlist_groups = [g for g in watchlist_groups if g["name"].strip().lower() not in parsed_names]

    summary = {
        "candidates_found": len(all_candidates),
        "already_known": len(all_candidates) - len(new_candidates),
        "probed": 0,
        "live_inserted": 0,
        "dead": 0,
        "errors": 0,
        "embed_guess_groups": len(embed_groups),
        "embed_guess_confirmed": 0,
        "watchlist_guess_groups": len(watchlist_groups),
        "watchlist_guess_confirmed": 0,
    }

    if dry_run:
        print(
            f"seed-expand (dry-run): {len(new_candidates)} new candidate(s), "
            f"{len(embed_groups)} embed-guess group(s), {len(watchlist_groups)} watchlist-guess group(s)"
        )
        for cand in new_candidates[:20]:
            print(f"  {cand['platform']}: {cand['name']} (slug `{cand['slug']}`) — parsed from URL")
        for group in embed_groups[:20]:
            print(
                f"  {group['platform']}: {group['name']} — guessed from embed URL "
                f"(candidates: {', '.join(group['slugs'])})"
            )
        for group in watchlist_groups[:20]:
            print(
                f"  {group['platform']}: {group['name']} — guessed from watchlist name "
                f"(candidates: {', '.join(group['slugs'])})"
            )
        return summary

    to_probe = new_candidates if limit is None else new_candidates[:limit]
    now = datetime.now(UTC).isoformat()
    dead: list[dict] = []

    for cand in to_probe:
        summary["probed"] += 1
        try:
            is_live = probe(cand["platform"], cand["slug"])
        except FetchError as exc:
            summary["errors"] += 1
            print(f"  ERROR probing {cand['platform']}:{cand['slug']}: {exc}")
            continue

        if is_live:
            if _insert_company(conn, cand["name"], cand["platform"], cand["slug"], now):
                summary["live_inserted"] += 1
        else:
            summary["dead"] += 1
            dead.append(cand)

    for group in embed_groups:
        winning_slug = _probe_guess_group(group, summary, dead)
        if winning_slug is not None:
            if _insert_company(conn, group["name"], group["platform"], winning_slug, now):
                summary["embed_guess_confirmed"] += 1

    for group in watchlist_groups:
        winning_slug = _probe_guess_group(group, summary, dead)
        if winning_slug is not None:
            summary["watchlist_guess_confirmed"] += 1
            if _promote_watchlist_company(conn, group["name"], group["platform"], winning_slug):
                print(f"PROMOTED {group['name']} -> {group['platform']}:{winning_slug}")

    conn.commit()
    _append_dead_slugs(dead, bad_slugs_path)

    print(
        f"seed-expand: {summary['candidates_found']} candidate(s) found "
        f"({summary['already_known']} already known), {summary['probed']} probed, "
        f"{summary['live_inserted']} live-inserted, {summary['dead']} dead, "
        f"{summary['errors']} error(s); "
        f"embed guesses: {summary['embed_guess_groups']} group(s), "
        f"{summary['embed_guess_confirmed']} confirmed; "
        f"watchlist guesses: {summary['watchlist_guess_groups']} group(s), "
        f"{summary['watchlist_guess_confirmed']} confirmed"
    )
    return summary


# --- Public reuse entrypoints for external feeders (jobhunt/discover.py) ---
#
# Both of these apply the same dedupe rules as run_expand (existing
# (platform, slug), existing name, or bad_slugs.md) and the same
# insert-or-promote path — they exist so a feeder that has its own
# candidate/guess-group source (YC's directory, Google CSE results)
# doesn't have to duplicate expand.py's probing, dedupe, or bad-slug
# bookkeeping. A candidate name that already matches a watchlist/
# platformless company row is NOT skipped as "already known" — it is
# probed, and a live hit PROMOTES that row (via _promote_watchlist_company)
# instead of inserting a duplicate. A name that already has BOTH platform
# and slug set is fully placed and is skipped, same as run_expand.


def _placed_company_names(conn: sqlite3.Connection) -> set[str]:
    """Lowercased names of companies that already have BOTH platform and
    slug set. Distinct from _existing_keys' `names` set, which also
    includes watchlist/platformless rows — those are NOT fully placed and
    are promotion candidates, not skip candidates, for the entrypoints
    below."""
    rows = conn.execute(
        "SELECT name FROM companies WHERE platform IS NOT NULL AND platform != '' "
        "AND slug IS NOT NULL AND slug != ''"
    ).fetchall()
    return {row["name"].strip().lower() for row in rows if row["name"]}


def _cap_groups_by_total_probes(
    groups: list[dict], max_probes_per_group: int | None, limit: int | None
) -> list[dict]:
    """`limit` caps TOTAL raw probe (HTTP) calls, not group count — a group
    can carry several (platform, slug) combos, so slicing `groups[:limit]`
    would let `limit=300` groups x up to 6 combos each balloon to ~1800
    live requests. Greedily include whole groups while the running sum of
    each group's (post max_probes_per_group cap) combo count stays within
    `limit`; a group that would push the running total over `limit` is
    excluded (not partially probed), so the live loop below never exceeds
    `limit` probes even in the worst case (first-hit-wins can only make a
    group probe FEWER of its combos than budgeted, never more)."""
    if limit is None:
        return groups
    survivors: list[dict] = []
    budget = limit
    for group in groups:
        combos = group["combos"]
        if max_probes_per_group is not None:
            combos = combos[:max_probes_per_group]
        if not combos or len(combos) > budget:
            continue
        survivors.append(group)
        budget -= len(combos)
        if budget <= 0:
            break
    return survivors


def probe_and_insert_guess_groups(
    conn: sqlite3.Connection,
    groups: list[dict],
    *,
    max_probes_per_group: int | None = None,
    limit: int | None = None,
    bad_slugs_path: Path | str = DEFAULT_BAD_SLUGS_PATH,
    dry_run: bool = False,
) -> dict:
    """Dedupe, probe, and insert MULTI-platform guess groups: each group is
    {"name": str, "platforms": list[str], "slugs": list[str]} — an ordered
    list of candidate platforms (e.g. ["ashby", "greenhouse", "lever"]) and
    an ordered list of slug variants shared across all of them. Combos are
    probed platform-major (every slug for the first platform, then the
    next), first live hit across ALL combos wins; `max_probes_per_group`
    caps how many combos are actually probed per group.

    Dedupe: drops the whole group if its name already matches a FULLY
    PLACED company (platform and slug both set) — a name matching a
    watchlist/platformless row is kept, since a live hit there promotes
    that row rather than inserting a duplicate. Drops individual (platform,
    slug) combos already claimed by another company or recorded dead in
    bad_slugs.md. `limit` then caps the TOTAL raw probe (HTTP) calls this
    run makes across all groups — whole groups are included greedily while
    their (post `max_probes_per_group` cap) combo count still fits the
    remaining budget, so `summary["probed"]` never exceeds `limit` even in
    the worst case (see _cap_groups_by_total_probes).

    dry_run skips probing and all writes; the summary's dedupe counts
    ("already_known") are still accurate. The surviving (post-dedupe,
    post-limit) groups are always returned under summary["survivors"] —
    dry-run callers use it to preview what would be probed.
    """
    existing_platform_slug, existing_names = _existing_keys(conn)
    placed_names = _placed_company_names(conn)
    dead_keys = _bad_slug_keys(bad_slugs_path)

    filtered: list[dict] = []
    for cand in groups:
        name_lower = cand["name"].strip().lower()
        if name_lower in placed_names:
            continue
        combos = [
            (platform, slug)
            for platform in cand["platforms"]
            for slug in cand["slugs"]
            if (platform, slug) not in existing_platform_slug and (platform, slug) not in dead_keys
        ]
        if not combos:
            continue
        filtered.append({**cand, "combos": combos, "_promotable": name_lower in existing_names})

    to_probe = _cap_groups_by_total_probes(filtered, max_probes_per_group, limit)

    summary = {
        "groups_found": len(groups),
        "already_known": len(groups) - len(filtered),
        "probed": 0,
        "inserted": 0,
        "promoted": 0,
        "dead": 0,
        "errors": 0,
        "survivors": to_probe,
    }

    if dry_run:
        return summary

    dead: list[dict] = []
    now = datetime.now(UTC).isoformat()

    for cand in to_probe:
        combos = cand["combos"]
        if max_probes_per_group is not None:
            combos = combos[:max_probes_per_group]

        winner: tuple[str, str] | None = None
        for platform, slug in combos:
            summary["probed"] += 1
            try:
                is_live = probe(platform, slug)
            except FetchError as exc:
                summary["errors"] += 1
                print(f"  ERROR probing {platform}:{slug} (guess for {cand['name']}): {exc}")
                continue
            if is_live:
                winner = (platform, slug)
                break
            summary["dead"] += 1
            dead.append({"platform": platform, "slug": slug, "name": cand["name"]})

        if winner is not None:
            platform, slug = winner
            if cand.get("_promotable"):
                if _promote_watchlist_company(conn, cand["name"], platform, slug):
                    summary["promoted"] += 1
                    print(f"PROMOTED {cand['name']} -> {platform}:{slug}")
            elif _insert_company(conn, cand["name"], platform, slug, now):
                summary["inserted"] += 1

    conn.commit()
    _append_dead_slugs(dead, bad_slugs_path)
    return summary


def dedupe_and_probe_candidates(
    conn: sqlite3.Connection,
    candidates: list[dict],
    *,
    limit: int | None = None,
    bad_slugs_path: Path | str = DEFAULT_BAD_SLUGS_PATH,
    dry_run: bool = False,
) -> dict:
    """Dedupe, probe, and insert-or-promote CERTAIN {platform, slug, name}
    candidates — for a feeder that already has the slug in hand (e.g. via
    extract_ats_refs) and needs no guessing. Same dedupe as discover():
    drops candidates already claimed by (platform, slug) or recorded dead
    in bad_slugs.md, or whose name already matches a FULLY PLACED company
    (platform and slug both set); a name matching a watchlist/platformless
    row is kept, and a live hit there promotes that row instead of
    inserting a duplicate (see _promote_watchlist_company — in practice
    this rarely fires here, since CSE candidate names are derived from the
    URL slug and often won't exactly match a stored company name).  `limit`
    then caps how many survivors are probed. dry_run skips probing and
    writes; summary["survivors"] always holds the post-dedupe, post-limit
    candidate list.
    """
    existing_platform_slug, existing_names = _existing_keys(conn)
    placed_names = _placed_company_names(conn)
    dead_keys = _bad_slug_keys(bad_slugs_path)

    seen_keys: set[tuple[str, str]] = set()
    new_candidates: list[dict] = []
    for cand in candidates:
        key = (cand["platform"], cand["slug"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if key in existing_platform_slug or key in dead_keys:
            continue
        name_lower = cand["name"].strip().lower()
        if name_lower in placed_names:
            continue
        new_candidates.append({**cand, "_promotable": name_lower in existing_names})

    to_probe = new_candidates if limit is None else new_candidates[:limit]

    summary = {
        "candidates_found": len(candidates),
        "already_known": len(candidates) - len(new_candidates),
        "probed": 0,
        "inserted": 0,
        "promoted": 0,
        "dead": 0,
        "errors": 0,
        "survivors": to_probe,
    }

    if dry_run:
        return summary

    dead: list[dict] = []
    now = datetime.now(UTC).isoformat()

    for cand in to_probe:
        summary["probed"] += 1
        try:
            is_live = probe(cand["platform"], cand["slug"])
        except FetchError as exc:
            summary["errors"] += 1
            print(f"  ERROR probing {cand['platform']}:{cand['slug']}: {exc}")
            continue

        if is_live:
            if cand.get("_promotable"):
                if _promote_watchlist_company(conn, cand["name"], cand["platform"], cand["slug"]):
                    summary["promoted"] += 1
                    print(f"PROMOTED {cand['name']} -> {cand['platform']}:{cand['slug']}")
            elif _insert_company(conn, cand["name"], cand["platform"], cand["slug"], now):
                summary["inserted"] += 1
        else:
            summary["dead"] += 1
            dead.append(cand)

    conn.commit()
    _append_dead_slugs(dead, bad_slugs_path)
    return summary
