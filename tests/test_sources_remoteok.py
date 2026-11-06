from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import remoteok

from tests.conftest import MockResponse

LEGAL_NOTICE = {
    "last_updated": 1785694767,
    "legal": "API Terms of Service: Please link back...",
}


def _job(
    job_id: str = "1135789",
    position: str = "Patient Outreach Specialist",
    company: str = "Grapefruit Health",
    location: str = "",
    url: str | None = None,
    date: str = "2026-08-01T16:00:13+00:00",
) -> dict:
    return {
        "slug": f"remote-job-{job_id}",
        "id": job_id,
        "epoch": 1785600013,
        "date": date,
        "company": company,
        "company_logo": "",
        "position": position,
        "tags": ["python"],
        "location": location,
        "url": url or f"https://remoteOK.com/remote-jobs/remote-job-{job_id}",
    }


def test_fetch_skips_legal_notice_and_maps_fields(monkeypatch):
    payload = [LEGAL_NOTICE, _job()]
    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append(url)
        return MockResponse(200, payload)

    monkeypatch.setattr(remoteok, "polite_get", fake_polite_get)
    postings = remoteok.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "rok-1135789"
    assert posting["company"] == "Grapefruit Health"
    assert posting["title"] == "Patient Outreach Specialist"
    assert posting["location"] is None  # empty string location normalizes to None
    assert posting["url"] == "https://remoteOK.com/remote-jobs/remote-job-1135789"
    assert posting["posted_at"] == "2026-08-01T16:00:13+00:00"
    assert posting["raw"] == payload[1]
    assert calls == [remoteok.API_URL]


def test_fetch_preserves_nonempty_location(monkeypatch):
    payload = [LEGAL_NOTICE, _job(location="Kakori, Japan")]
    monkeypatch.setattr(remoteok, "polite_get", lambda *a, **k: MockResponse(200, payload))

    postings = remoteok.fetch()
    assert postings[0]["location"] == "Kakori, Japan"


def test_fetch_skips_jobs_missing_company_or_url(monkeypatch, capsys):
    good = _job(job_id="1")
    no_company = {**_job(job_id="2"), "company": None}
    no_url = {**_job(job_id="3"), "url": ""}
    payload = [LEGAL_NOTICE, good, no_company, no_url]

    monkeypatch.setattr(remoteok, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = remoteok.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"rok-1"}
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "skipped" in out


def test_fetch_ignores_entries_without_position_field(monkeypatch):
    # Anything lacking "position" (besides the legal notice) is silently
    # skipped without inflating the skipped-count warning.
    non_job = {"id": "999", "epoch": 123}
    payload = [LEGAL_NOTICE, non_job]

    monkeypatch.setattr(remoteok, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = remoteok.fetch()

    assert postings == []


def test_fetch_raises_fetch_error_on_non_200(monkeypatch):
    monkeypatch.setattr(remoteok, "polite_get", lambda *a, **k: MockResponse(500))

    with pytest.raises(FetchError):
        remoteok.fetch()


def test_fetch_handles_non_list_payload_defensively(monkeypatch):
    monkeypatch.setattr(remoteok, "polite_get", lambda *a, **k: MockResponse(200, {"unexpected": True}))

    assert remoteok.fetch() == []
