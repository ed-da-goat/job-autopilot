"""Shared polite-networking primitives: per-host 1 req/sec limiter, honest
User-Agent, 15s timeouts, one retry on 429/503 honoring Retry-After (capped
at 60s). Every source module funnels its HTTP calls through `polite_get`.

The limiter is keyed by hostname: no single host ever sees more than one
request per MIN_INTERVAL_SECONDS from this process, across every source and
thread, while requests to *different* hosts may proceed concurrently. Thread
safe, so a source whose boards live on distinct hosts (Workday tenants) may
fetch several boards at once without exceeding any host's budget.
"""

from __future__ import annotations

import contextlib
import threading
import time
from urllib.parse import urlsplit

import requests

from jobhunt import eligibility


def _build_user_agent() -> str:
    """Honest User-Agent; carries `contact_email` from the preferences file when set."""
    email = eligibility.user_preferences().contact_email
    return f"job-hunt personal pipeline (contact: {email})" if email else "job-hunt personal pipeline"


USER_AGENT = _build_user_agent()
TIMEOUT_SECONDS = 15
MIN_INTERVAL_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60
DEFAULT_RETRY_DELAY_SECONDS = 5.0

_lock = threading.Lock()
_next_slot_by_host: dict[str, float] = {}


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _throttle(url: str) -> None:
    """Block until at least MIN_INTERVAL_SECONDS has passed since the last
    request this process made to `url`'s host. Reserves the slot under the
    lock, then sleeps outside it, so waiting on one host never delays
    another."""
    host = _host(url)
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot_by_host.get(host, 0.0))
        _next_slot_by_host[host] = slot + MIN_INTERVAL_SECONDS
    wait = slot - now
    if wait > 0:
        time.sleep(wait)


class FetchError(Exception):
    """A non-recoverable HTTP/network failure. Callers should catch this,
    record it, and move on rather than crash the sweep."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def polite_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    """GET honoring the per-host rate limit, with one retry on 429/503."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    getter = (session or requests).get

    _throttle(url)
    try:
        resp = getter(url, params=params, headers=req_headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise FetchError(f"request failed: {exc}") from exc

    if resp.status_code in (429, 503):
        retry_after_hdr = resp.headers.get("Retry-After")
        delay = DEFAULT_RETRY_DELAY_SECONDS
        if retry_after_hdr:
            with contextlib.suppress(ValueError):
                delay = min(float(retry_after_hdr), MAX_RETRY_AFTER_SECONDS)
        time.sleep(delay)
        _throttle(url)
        try:
            resp = getter(url, params=params, headers=req_headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise FetchError(f"retry failed: {exc}") from exc

    return resp


def polite_post(
    url: str,
    json_body: dict | None = None,
    headers: dict | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    """POST honoring the same per-host rate limit as polite_get (shared
    per-host slot — not a second independent 1/sec budget), with identical
    User-Agent and one retry on 429/503 honoring Retry-After."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    poster = (session or requests).post

    _throttle(url)
    try:
        resp = poster(url, json=json_body, headers=req_headers, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise FetchError(f"request failed: {exc}") from exc

    if resp.status_code in (429, 503):
        retry_after_hdr = resp.headers.get("Retry-After")
        delay = DEFAULT_RETRY_DELAY_SECONDS
        if retry_after_hdr:
            with contextlib.suppress(ValueError):
                delay = min(float(retry_after_hdr), MAX_RETRY_AFTER_SECONDS)
        time.sleep(delay)
        _throttle(url)
        try:
            resp = poster(url, json=json_body, headers=req_headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise FetchError(f"retry failed: {exc}") from exc

    return resp
