"""ATS detection from a job posting/apply URL, by hostname only."""

from __future__ import annotations

from urllib.parse import urlparse

_GREENHOUSE_HOSTS_EXACT = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
_GREENHOUSE_SUFFIX = ".greenhouse.io"
_LEVER_HOSTS_EXACT = {"jobs.lever.co", "jobs.eu.lever.co"}
_ASHBY_HOSTS_EXACT = {"jobs.ashbyhq.com"}


def detect_ats(url: str) -> str | None:
    """Return "greenhouse" | "lever" | "ashby" based on the URL's hostname,
    or None if it doesn't match a known ATS."""
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None

    if host in _GREENHOUSE_HOSTS_EXACT or host.endswith(_GREENHOUSE_SUFFIX):
        return "greenhouse"
    if host in _LEVER_HOSTS_EXACT:
        return "lever"
    if host in _ASHBY_HOSTS_EXACT:
        return "ashby"
    return None


# Ashby and Lever serve the job *description* at the harvested URL and the
# application *form* at a suffixed path. Scanning the description page found
# zero fields and raised no error, so every Ashby/Lever tab opened blank and
# the run still reported success (verified 2026-08-01: 0 fields on the bare
# URL, 25 and 68 on the suffixed one). Greenhouse serves the form inline and
# must be left alone.
_APPLY_SUFFIX = {"ashby": "/application", "lever": "/apply"}


def application_url(url: str) -> str:
    """Return the URL of the application form for `url`.

    Idempotent, and a no-op for any ATS that serves its form inline or that
    we don't recognise. This only rewrites the path — it never clicks the
    page's Apply control, keeping fill.py's no-click safety rule intact.
    """
    suffix = _APPLY_SUFFIX.get(detect_ats(url) or "")
    if not suffix:
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith(suffix):
        return url
    return parsed._replace(path=path + suffix).geturl()
