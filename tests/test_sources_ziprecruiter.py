from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import ziprecruiter

from tests.conftest import MockResponse

SECRET_KEY = "sh-super-secret-zr-key"


def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIPRECRUITER_API_KEY", SECRET_KEY)


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIPRECRUITER_API_KEY", raising=False)


def _job(
    posting_id: str,
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str | None = "Atlanta, GA",
    city: str | None = None,
    state: str | None = None,
    url: str | None = None,
    posted_time: str = "2026-07-20T00:00:00Z",
) -> dict:
    job: dict = {
        "id": posting_id,
        "name": title,
        "hiring_company": {"name": company},
        "url": url or f"https://ziprecruiter.com/jobs/{posting_id}",
        "posted_time": posted_time,
    }
    if location is not None:
        job["location"] = location
    if city is not None:
        job["city"] = city
    if state is not None:
        job["state"] = state
    return job


def test_is_configured_false_when_env_var_missing(monkeypatch):
    _clear_creds(monkeypatch)
    assert ziprecruiter.is_configured() is False


def test_is_configured_true_when_env_var_set(monkeypatch):
    _set_creds(monkeypatch)
    assert ziprecruiter.is_configured() is True


def test_fetch_raises_runtime_error_when_unconfigured(monkeypatch):
    _clear_creds(monkeypatch)
    with pytest.raises(RuntimeError, match="ZIPRECRUITER_API_KEY"):
        ziprecruiter.fetch()


def test_fetch_unconfigured_error_mentions_partner_page(monkeypatch):
    _clear_creds(monkeypatch)
    with pytest.raises(RuntimeError, match="ziprecruiter.com/partner"):
        ziprecruiter.fetch()


def test_fetch_happy_path_maps_fields(monkeypatch):
    _set_creds(monkeypatch)
    payload = {"total_jobs": 1, "jobs": [_job("111", title="Backend Engineer, New Grad")]}

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, payload)

    monkeypatch.setattr(ziprecruiter, "polite_get", fake_polite_get)
    postings = ziprecruiter.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "zr-111"
    assert posting["company"] == "Acme Corp"
    assert posting["title"] == "Backend Engineer, New Grad"
    assert posting["location"] == "Atlanta, GA"
    assert posting["url"] == "https://ziprecruiter.com/jobs/111"
    assert posting["posted_at"] == "2026-07-20T00:00:00Z"
    assert posting["raw"] == payload["jobs"][0]

    # 1 page per query short-circuits (results < JOBS_PER_PAGE).
    assert len(calls) == len(ziprecruiter.QUERIES)


def test_fetch_builds_location_from_city_state_when_missing(monkeypatch):
    _set_creds(monkeypatch)
    payload = {"total_jobs": 1, "jobs": [_job("222", location=None, city="Austin", state="TX")]}

    monkeypatch.setattr(ziprecruiter, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = ziprecruiter.fetch()

    assert postings[0]["location"] == "Austin, TX"


def test_fetch_pagination_stops_when_page_short(monkeypatch):
    _set_creds(monkeypatch)
    full_page = {"total_jobs": 200, "jobs": [_job(str(i)) for i in range(ziprecruiter.JOBS_PER_PAGE)]}
    short_page = {"total_jobs": 200, "jobs": [_job("999")]}

    responses = [full_page, short_page] + [short_page] * (2 * (len(ziprecruiter.QUERIES) - 1))
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        payload = responses[call_count["n"]]
        call_count["n"] += 1
        return MockResponse(200, payload)

    monkeypatch.setattr(ziprecruiter, "polite_get", fake_polite_get)
    ziprecruiter.fetch()

    # First query: full page (continues to page 2) + short page (stops) = 2 calls.
    # Remaining queries: single short page each = 1 call.
    assert call_count["n"] == 2 + (len(ziprecruiter.QUERIES) - 1)


def test_fetch_dedupes_across_queries(monkeypatch):
    _set_creds(monkeypatch)
    payload = {"total_jobs": 1, "jobs": [_job("333")]}

    monkeypatch.setattr(ziprecruiter, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = ziprecruiter.fetch()

    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["zr-333"]  # same id across every query, only one posting kept


def test_fetch_skips_results_missing_company_or_url(monkeypatch, capsys):
    _set_creds(monkeypatch)
    good = _job("444")
    no_company = {**_job("445"), "hiring_company": {}}
    no_url = {**_job("446"), "url": None}
    payload = {"total_jobs": 3, "jobs": [good, no_company, no_url]}

    monkeypatch.setattr(ziprecruiter, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = ziprecruiter.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"zr-444"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_error_never_contains_api_key(monkeypatch):
    _set_creds(monkeypatch)
    monkeypatch.setattr(ziprecruiter, "polite_get", lambda *a, **k: MockResponse(500))

    with pytest.raises(FetchError) as exc_info:
        ziprecruiter.fetch()

    assert SECRET_KEY not in str(exc_info.value)
    assert SECRET_KEY not in repr(exc_info.value)


def test_queries_shared_with_adzuna():
    from jobhunt.sources import adzuna

    assert ziprecruiter.QUERIES is adzuna.QUERIES
