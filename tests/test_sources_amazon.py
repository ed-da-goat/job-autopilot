from __future__ import annotations

import json

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import amazon

from tests.conftest import MockResponse


class _ChallengeResponse:
    """Simulates a 200-status HTML challenge page where .json() blows up
    exactly like requests.Response does on non-JSON bodies."""

    status_code = 200

    def json(self):
        raise json.JSONDecodeError("Expecting value", "<html>captcha challenge</html>", 0)


def _job(
    job_id: int = 10386857,
    title: str = "Solutions Engineer",
    company_name: str | None = "Amazon Web Services Australia Pty Ltd",
    normalized_location: str | None = "Seattle, Washington, USA",
    job_path: str = "/en/jobs/10386857/solutions-engineer",
    posted_date: str = "April  9, 2026",
) -> dict:
    return {
        "id_icims": job_id,
        "id": f"uuid-{job_id}",
        "title": title,
        "company_name": company_name,
        "normalized_location": normalized_location,
        "city": "Seattle",
        "state": "Washington",
        "job_path": job_path,
        "posted_date": posted_date,
    }


def _page(jobs: list[dict]) -> dict:
    return {"jobs": jobs, "hits": len(jobs)}


def test_fetch_maps_fields(monkeypatch):
    payload = _page([_job()])

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, payload)

    monkeypatch.setattr(amazon, "polite_get", fake_polite_get)
    postings = amazon.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "amzn-10386857"
    assert posting["company"] == "Amazon Web Services Australia Pty Ltd"
    assert posting["title"] == "Solutions Engineer"
    assert posting["location"] == "Seattle, Washington, USA"
    assert posting["url"] == "https://www.amazon.jobs/en/jobs/10386857/solutions-engineer"
    assert posting["posted_at"] == "April  9, 2026"
    assert posting["raw"] == payload["jobs"][0]

    # every request pins the US-only filter
    for _, params in calls:
        assert params["normalized_country_code[]"] == "USA"


def test_fetch_falls_back_to_id_and_amazon_company(monkeypatch):
    job = _job(company_name=None, normalized_location=None)
    job["id_icims"] = None
    job["id"] = "raw-uuid-1"
    payload = _page([job])
    monkeypatch.setattr(amazon, "polite_get", lambda *a, **k: MockResponse(200, payload))

    postings = amazon.fetch()
    assert postings[0]["source_posting_id"] == "amzn-raw-uuid-1"
    assert postings[0]["company"] == "Amazon"
    assert postings[0]["location"] == "Seattle, Washington"


def test_fetch_pagination_caps_and_early_stops(monkeypatch):
    full_page = _page([_job(job_id=i) for i in range(amazon.RESULTS_PER_PAGE)])
    short_page = _page([_job(job_id=999)])
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        call_count["n"] += 1
        # first page full (would page again), second page short (stop early)
        return MockResponse(200, full_page if params["offset"] == 0 else short_page)

    monkeypatch.setattr(amazon, "polite_get", fake_polite_get)
    amazon.fetch()

    # 2 pages per query (full then short stops), never exceeding MAX_PAGES
    assert call_count["n"] == 2 * len(amazon.QUERIES)


def test_fetch_caps_at_max_pages_when_always_full(monkeypatch):
    full_page = _page([_job(job_id=i) for i in range(amazon.RESULTS_PER_PAGE)])
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        call_count["n"] += 1
        return MockResponse(200, full_page)

    monkeypatch.setattr(amazon, "polite_get", fake_polite_get)
    amazon.fetch()

    assert call_count["n"] == amazon.MAX_PAGES * len(amazon.QUERIES)


def test_fetch_dedupes_across_queries(monkeypatch):
    payload = _page([_job(job_id=555)])
    monkeypatch.setattr(amazon, "polite_get", lambda *a, **k: MockResponse(200, payload))

    postings = amazon.fetch()
    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["amzn-555"]


def test_fetch_skips_jobs_missing_essentials(monkeypatch, capsys):
    good = _job(job_id=1)
    no_id = {**_job(job_id=2), "id_icims": None, "id": None}
    no_path = {**_job(job_id=3), "job_path": None}
    no_title = {**_job(job_id=4), "title": None}
    payload = _page([good, no_id, no_path, no_title])

    monkeypatch.setattr(amazon, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = amazon.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"amzn-1"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(amazon, "polite_get", lambda *a, **k: MockResponse(503, {}))

    with pytest.raises(FetchError):
        amazon.fetch()


def test_fetch_raises_on_challenge_html(monkeypatch):
    monkeypatch.setattr(amazon, "polite_get", lambda *a, **k: _ChallengeResponse())

    with pytest.raises(FetchError):
        amazon.fetch()
