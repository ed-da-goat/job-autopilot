from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import remotive

from tests.conftest import MockResponse


def _job(
    job_id: int = 2091081,
    title: str = "Senior Backend Engineer",
    company_name: str = "Acme Corp",
    location: str = "Americas, Europe",
    url: str | None = None,
    publication_date: str = "2026-07-28T14:23:05",
) -> dict:
    return {
        "id": job_id,
        "url": url or f"https://remotive.com/remote-jobs/software-dev/acme-{job_id}",
        "title": title,
        "company_name": company_name,
        "candidate_required_location": location,
        "publication_date": publication_date,
    }


def _payload(jobs: list[dict]) -> dict:
    return {
        "00-warning": "...",
        "0-legal-notice": "...",
        "job-count": len(jobs),
        "total-job-count": len(jobs),
        "jobs": jobs,
    }


def test_fetch_maps_fields(monkeypatch):
    payload = _payload([_job()])
    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, payload)

    monkeypatch.setattr(remotive, "polite_get", fake_polite_get)
    postings = remotive.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "rmtv-2091081"
    assert posting["company"] == "Acme Corp"
    assert posting["title"] == "Senior Backend Engineer"
    assert posting["location"] == "Americas, Europe"
    assert posting["url"] == "https://remotive.com/remote-jobs/software-dev/acme-2091081"
    assert posting["posted_at"] == "2026-07-28T14:23:05"
    assert posting["raw"] == payload["jobs"][0]

    # Single request, no pagination.
    assert len(calls) == 1
    assert calls[0][1] == {"category": remotive.CATEGORY}


def test_fetch_skips_jobs_missing_company_or_url(monkeypatch, capsys):
    good = _job(job_id=1)
    no_company = {**_job(job_id=2), "company_name": None}
    no_url = {**_job(job_id=3), "url": ""}
    payload = _payload([good, no_company, no_url])

    monkeypatch.setattr(remotive, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = remotive.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"rmtv-1"}
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "skipped" in out


def test_fetch_raises_fetch_error_on_non_200(monkeypatch):
    monkeypatch.setattr(remotive, "polite_get", lambda *a, **k: MockResponse(503))

    with pytest.raises(FetchError):
        remotive.fetch()


def test_fetch_handles_non_list_jobs_defensively(monkeypatch):
    payload = {"jobs": "not-a-list"}
    monkeypatch.setattr(remotive, "polite_get", lambda *a, **k: MockResponse(200, payload))

    assert remotive.fetch() == []
