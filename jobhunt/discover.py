"""Two candidate feeders for growing the companies table beyond
SimplifyJobs (see jobhunt/expand.py): YC's public hiring directory and
Google Programmable Search over the ATS host domains. Both reuse
expand.py's probe/dedupe/insert pipeline via its public entrypoints
(probe_and_insert_guess_groups, dedupe_and_probe_candidates) rather than
duplicating that logic.

yc_candidates() GUESSES slugs (name + website domain, tried against
ashby/greenhouse/lever) since YC's directory carries no ATS link at all.
cse_candidates() needs no guessing — a CSE result link already has the
platform + slug in the URL, extracted via expand.extract_ats_refs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from jobhunt import expand as expand_module
from jobhunt.net import FetchError, polite_get

YC_URL = "https://yc-oss.github.io/api/companies/all.json"
# YC skews Ashby; this order is the probe order within each guess group.
YC_PLATFORMS: tuple[str, ...] = ("ashby", "greenhouse", "lever")
YC_MAX_PROBES_PER_COMPANY = 6

CSE_URL = "https://www.googleapis.com/customsearch/v1"
CSE_SITE_FILTER = (
    "(site:boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com "
    "OR site:jobs.smartrecruiters.com OR site:apply.workable.com OR site:myworkdayjobs.com)"
)
# Tuned to PROFILE.md's target roles, same universe as expand.py's other
# source modules query for the job feeds themselves.
CSE_QUERIES: tuple[str, ...] = (
    "forward deployed engineer",
    "AI engineer new grad",
    "founding engineer",
    "solutions engineer",
    "fintech software engineer",
)
CSE_STARTS: tuple[int, ...] = (1, 11)  # two pages of 10 per query

DEFAULT_DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
DEFAULT_DISCOVERY_LIMIT = 300


def yc_candidates() -> list[dict]:
    """One polite GET against yc-oss's static, MIT-licensed mirror of YC's
    official company directory, filtered to isHiring=true. Each hiring
    company becomes a multi-platform guess group: slug variants from the
    company name and the website's registrable domain (expand.py's variant
    machinery), to be probed against ashby/greenhouse/lever, first live hit
    across all (platform, variant) combos wins. NOT yet deduped against the
    companies table or bad_slugs.md — that happens in run_discovery via
    expand.probe_and_insert_guess_groups.
    """
    resp = polite_get(YC_URL)
    if resp.status_code != 200:
        raise FetchError(f"discover: YC companies fetch HTTP {resp.status_code}", resp.status_code)
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise FetchError(f"discover: YC companies payload not JSON: {exc}") from exc
    if not isinstance(payload, list):
        return []

    groups: list[dict] = []
    for company in payload:
        if not isinstance(company, dict) or not company.get("isHiring"):
            continue
        name = (company.get("name") or "").strip()
        if not name:
            continue
        website = company.get("website") or ""
        domain_base = expand_module._registrable_domain_base(website) if website else None
        slugs = expand_module._guess_slugs(name, domain_base)
        if not slugs:
            continue
        groups.append({"name": name, "platforms": list(YC_PLATFORMS), "slugs": slugs})
    return groups


def _read_dotenv_var(key: str, path: Path | str = DEFAULT_DOTENV_PATH) -> str | None:
    """Fallback KEY=VALUE parser for the repo-root .env (stdlib only, no
    dependency on python-dotenv), used only when the environment variable
    itself isn't already set. Ignores blank lines and #-comments. Never
    prints or logs the values it parses."""
    p = Path(path)
    if not p.exists():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def _cse_credentials() -> tuple[str, str] | None:
    # Pass DEFAULT_DOTENV_PATH explicitly (looked up fresh from the module
    # namespace on every call) rather than relying on _read_dotenv_var's own
    # default parameter, which would be bound once at import time and immune
    # to a test monkeypatching the module attribute afterward.
    key = os.environ.get("GOOGLE_CSE_KEY") or _read_dotenv_var("GOOGLE_CSE_KEY", DEFAULT_DOTENV_PATH)
    cx = os.environ.get("GOOGLE_CSE_ID") or _read_dotenv_var("GOOGLE_CSE_ID", DEFAULT_DOTENV_PATH)
    if not key or not cx:
        return None
    return key, cx


def is_configured() -> bool:
    """True when both GOOGLE_CSE_KEY and GOOGLE_CSE_ID are available, from
    the environment or the repo-root .env."""
    return _cse_credentials() is not None


def cse_candidates(queries: Iterable[str] = CSE_QUERIES) -> list[dict]:
    """Google Programmable Search over the six ATS host domains: each role
    phrase x 2 pages (start=1, start=11) = up to 10 requests/run for the
    default 5 queries, well under the 100/day free tier. Result links
    already carry the platform + slug in the URL (fed through
    expand.extract_ats_refs — certain candidates, no guessing needed).
    Returns deduped {platform, slug, name} candidates, name derived from
    the slug (CSE result titles aren't reliably the company name).

    Raises RuntimeError if GOOGLE_CSE_KEY/GOOGLE_CSE_ID aren't configured;
    callers should check is_configured() first to skip this feeder
    gracefully rather than treat it as an error.
    """
    creds = _cse_credentials()
    if creds is None:
        raise RuntimeError(
            "discover: GOOGLE_CSE_KEY and GOOGLE_CSE_ID must both be set "
            "(env or repo-root .env); call is_configured() first to skip gracefully"
        )
    key, cx = creds

    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []

    for query in queries:
        q = f"{query} {CSE_SITE_FILTER}"
        for start in CSE_STARTS:
            resp = polite_get(CSE_URL, params={"key": key, "cx": cx, "q": q, "start": start})
            if resp.status_code != 200:
                # Deliberately omit params (would leak key/cx) — query/start/status only.
                raise FetchError(
                    f"discover: CSE query={query!r} start={start} HTTP {resp.status_code}", resp.status_code
                )
            try:
                payload = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise FetchError(f"discover: CSE payload not JSON: {exc}") from exc

            items = payload.get("items", []) if isinstance(payload, dict) else []
            links = [item.get("link") for item in items if isinstance(item, dict) and item.get("link")]
            for ref in expand_module.extract_ats_refs(links):
                dkey = (ref["platform"], ref["slug"])
                if dkey in seen:
                    continue
                seen.add(dkey)
                name = ref["slug"].replace("-", " ").replace("_", " ").title()
                candidates.append({"platform": ref["platform"], "slug": ref["slug"], "name": name})

    return candidates


def run_discovery(
    conn: sqlite3.Connection,
    source: str = "all",
    limit: int | None = DEFAULT_DISCOVERY_LIMIT,
    dry_run: bool = False,
    bad_slugs_path: Path | str = expand_module.DEFAULT_BAD_SLUGS_PATH,
) -> dict:
    """Gather candidates from the requested feeder(s), dedupe against the
    companies table and bad_slugs.md, probe — capped at `limit` per feeder,
    applied as a total raw-probe (HTTP call) budget for yc (each guess
    group can cost up to YC_MAX_PROBES_PER_COMPANY combos) and as a
    candidate count for cse (one probe per candidate) — insert confirmed-
    live ones, all via expand.py's shared pipeline. Prints one summary line
    per feeder actually run.

    source: "yc", "cse", or "all". If cse is requested but not configured
    (see is_configured()), prints a NOTE and falls back to running yc
    instead — never raises for a missing CSE key.

    dry_run: skips probing and all writes; still fetches candidates (the
    yc/cse listing calls themselves), and prints up to the first 20
    candidates per feeder that survived dedupe.

    bad_slugs_path defaults to the real data/bad_slugs.md (same default
    expand.py's own entrypoints use); tests should override it to keep dead
    guesses out of the real file.
    """
    if source not in ("yc", "cse", "all"):
        raise ValueError(f"discover: unknown source {source!r}; expected yc, cse, or all")

    run_yc = source in ("yc", "all")
    run_cse = source in ("cse", "all")

    if run_cse and not is_configured():
        print("NOTE: Google CSE not configured (GOOGLE_CSE_KEY/GOOGLE_CSE_ID) — skipping cse, running yc instead")
        run_cse = False
        run_yc = True

    results: dict = {"yc": None, "cse": None}

    if run_yc:
        groups = yc_candidates()
        summary = expand_module.probe_and_insert_guess_groups(
            conn,
            groups,
            max_probes_per_group=YC_MAX_PROBES_PER_COMPANY,
            limit=limit,
            bad_slugs_path=bad_slugs_path,
            dry_run=dry_run,
        )
        survivors = summary.pop("survivors", [])
        print(
            f"discover (yc): {summary['groups_found']} candidate(s), "
            f"{summary['already_known']} already known, {summary['probed']} probed, "
            f"{summary['inserted']} inserted, {summary['promoted']} promoted, "
            f"{summary['dead']} dead, {summary['errors']} error(s)"
        )
        if dry_run:
            for group in survivors[:20]:
                print(f"  {group['name']} — platforms: {', '.join(group['platforms'])} (candidates: {', '.join(group['slugs'])})")
        results["yc"] = summary

    if run_cse:
        candidates = cse_candidates()
        summary = expand_module.dedupe_and_probe_candidates(
            conn, candidates, limit=limit, bad_slugs_path=bad_slugs_path, dry_run=dry_run
        )
        survivors = summary.pop("survivors", [])
        print(
            f"discover (cse): {len(CSE_QUERIES)} quer{'y' if len(CSE_QUERIES) == 1 else 'ies'} used, "
            f"{summary['candidates_found']} candidate(s), {summary['already_known']} already known, "
            f"{summary['probed']} probed, {summary['inserted']} inserted, {summary['promoted']} promoted, "
            f"{summary['dead']} dead, {summary['errors']} error(s)"
        )
        if dry_run:
            for cand in survivors[:20]:
                print(f"  {cand['platform']}: {cand['name']} (slug `{cand['slug']}`)")
        results["cse"] = summary

    return results
