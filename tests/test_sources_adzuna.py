from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import adzuna

from tests.conftest import MockResponse

SECRET_KEY = "sh-super-secret-app-key"


def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "test-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", SECRET_KEY)


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)


def _result(
    posting_id: str,
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str = "Atlanta, GA",
    redirect_url: str | None = None,
    created: str = "2026-07-20T00:00:00Z",
) -> dict:
    return {
        "id": posting_id,
        "title": title,
        "company": {"display_name": company},
        "location": {"display_name": location},
        "redirect_url": redirect_url or f"https://adzuna.com/jobs/{posting_id}",
        "created": created,
    }


def test_is_configured_false_when_env_vars_missing(monkeypatch):
    _clear_creds(monkeypatch)
    assert adzuna.is_configured() is False


def test_is_configured_true_when_both_env_vars_set(monkeypatch):
    _set_creds(monkeypatch)
    assert adzuna.is_configured() is True


def test_fetch_raises_runtime_error_when_unconfigured(monkeypatch):
    _clear_creds(monkeypatch)
    with pytest.raises(RuntimeError, match="ADZUNA_APP_ID and ADZUNA_APP_KEY"):
        adzuna.fetch()


def test_fetch_happy_path_maps_fields(monkeypatch):
    _set_creds(monkeypatch)
    payload = {"count": 1, "results": [_result("111", title="Backend Engineer, New Grad")]}

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, payload)

    monkeypatch.setattr(adzuna, "polite_get", fake_polite_get)
    postings = adzuna.fetch()

    # One page per query short-circuits (results < RESULTS_PER_PAGE), and the
    # single posting is shared across all 5 QUERIES but deduped to one.
    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "111"
    assert posting["company"] == "Acme Corp"
    assert posting["title"] == "Backend Engineer, New Grad"
    assert posting["location"] == "Atlanta, GA"
    assert posting["url"] == "https://adzuna.com/jobs/111"
    assert posting["posted_at"] == "2026-07-20T00:00:00Z"
    assert posting["raw"] == payload["results"][0]

    # 1 page per query (page 1 returns 1 result < RESULTS_PER_PAGE, stops early).
    assert len(calls) == len(adzuna.QUERIES)


def test_fetch_pagination_stops_when_page_short(monkeypatch):
    _set_creds(monkeypatch)
    full_page = {"count": 100, "results": [_result(str(i)) for i in range(adzuna.RESULTS_PER_PAGE)]}
    short_page = {"count": 100, "results": [_result("999")]}

    responses = [full_page, short_page] + [short_page] * (2 * (len(adzuna.QUERIES) - 1))
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        payload = responses[call_count["n"]]
        call_count["n"] += 1
        return MockResponse(200, payload)

    monkeypatch.setattr(adzuna, "polite_get", fake_polite_get)
    adzuna.fetch()

    # First query: full page (continues to page 2) + short page (stops) = 2 calls.
    # Remaining queries: single short page each = 1 call.
    assert call_count["n"] == 2 + (len(adzuna.QUERIES) - 1)


def test_fetch_dedupes_across_queries(monkeypatch):
    _set_creds(monkeypatch)
    payload = {"count": 1, "results": [_result("222")]}

    monkeypatch.setattr(adzuna, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = adzuna.fetch()

    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["222"]  # same id across every query, only one posting kept


def test_fetch_skips_results_missing_company_or_url(monkeypatch, capsys):
    _set_creds(monkeypatch)
    good = _result("333")
    no_company = {**_result("334"), "company": {}}
    no_url = {**_result("335"), "redirect_url": None}
    payload = {"count": 3, "results": [good, no_company, no_url]}

    monkeypatch.setattr(adzuna, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = adzuna.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"333"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_error_never_contains_app_key(monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setattr(adzuna, "polite_get", lambda *a, **k: MockResponse(500))

    with pytest.raises(FetchError) as exc_info:
        adzuna.fetch()

    assert SECRET_KEY not in str(exc_info.value)
    assert SECRET_KEY not in repr(exc_info.value)
