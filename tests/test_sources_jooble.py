from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import jooble

from tests.conftest import MockResponse

SECRET_KEY = "sh-super-secret-jooble-key"


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOOBLE_API_KEY", SECRET_KEY)


def _clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)


def _job(
    posting_id: str,
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    location: str = "Atlanta, GA",
    link: str | None = None,
    updated: str = "2026-07-20T00:00:00Z",
) -> dict:
    return {
        "id": posting_id,
        "title": title,
        "company": company,
        "location": location,
        "link": link or f"https://jooble.org/desc/{posting_id}",
        "updated": updated,
    }


def test_is_configured_false_when_env_var_missing(monkeypatch):
    _clear_key(monkeypatch)
    assert jooble.is_configured() is False


def test_is_configured_true_when_env_var_set(monkeypatch):
    _set_key(monkeypatch)
    assert jooble.is_configured() is True


def test_fetch_raises_runtime_error_when_unconfigured(monkeypatch):
    _clear_key(monkeypatch)
    with pytest.raises(RuntimeError, match="JOOBLE_API_KEY"):
        jooble.fetch()


def test_fetch_happy_path_maps_fields(monkeypatch):
    _set_key(monkeypatch)
    payload = {"totalCount": 1, "jobs": [_job("111", title="Backend Engineer, New Grad")]}

    calls = []

    def fake_polite_post(url, json_body=None, headers=None, session=None):
        calls.append((url, json_body))
        return MockResponse(200, payload)

    monkeypatch.setattr(jooble, "polite_post", fake_polite_post)
    postings = jooble.fetch()

    # Same job "111" is returned by every query/page combo; dedupe collapses
    # it to a single posting regardless of how many pages/queries ran.
    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "111"
    assert posting["company"] == "Acme Corp"
    assert posting["title"] == "Backend Engineer, New Grad"
    assert posting["location"] == "Atlanta, GA"
    assert posting["url"] == "https://jooble.org/desc/111"
    assert posting["posted_at"] == "2026-07-20T00:00:00Z"
    assert posting["raw"] == payload["jobs"][0]

    # The key is embedded in the URL path passed to polite_post — verify it's
    # actually used (not silently dropped) without asserting its exact value here.
    for url, _ in calls:
        assert SECRET_KEY in url


def test_fetch_pagination_stops_when_page_empty(monkeypatch):
    _set_key(monkeypatch)
    page1 = {"totalCount": 5, "jobs": [_job(str(i)) for i in range(3)]}
    empty = {"totalCount": 5, "jobs": []}

    call_count = {"n": 0}

    def fake_polite_post(url, json_body=None, headers=None, session=None):
        page = json_body["page"]
        call_count["n"] += 1
        return MockResponse(200, page1 if page == 1 else empty)

    monkeypatch.setattr(jooble, "polite_post", fake_polite_post)
    jooble.fetch()

    # page 1 returns jobs (continues to page 2), page 2 empty (stops) = 2 calls/query.
    assert call_count["n"] == 2 * len(jooble.QUERIES)


def test_fetch_never_exceeds_max_pages(monkeypatch):
    _set_key(monkeypatch)
    always_full = {"totalCount": 999, "jobs": [_job("x")]}
    call_count = {"n": 0}

    def fake_polite_post(url, json_body=None, headers=None, session=None):
        call_count["n"] += 1
        return MockResponse(200, always_full)

    monkeypatch.setattr(jooble, "polite_post", fake_polite_post)
    jooble.fetch()

    assert call_count["n"] == jooble.MAX_PAGES * len(jooble.QUERIES)


def test_fetch_dedupes_across_queries(monkeypatch):
    _set_key(monkeypatch)
    payload = {"totalCount": 1, "jobs": [_job("222")]}
    monkeypatch.setattr(jooble, "polite_post", lambda *a, **k: MockResponse(200, payload))

    postings = jooble.fetch()
    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["222"]


def test_fetch_skips_jobs_missing_company_or_link(monkeypatch, capsys):
    _set_key(monkeypatch)
    good = _job("333")
    no_company = {**_job("334"), "company": ""}
    no_link = {**_job("335"), "link": None}
    payload = {"totalCount": 3, "jobs": [good, no_company, no_link]}

    monkeypatch.setattr(jooble, "polite_post", lambda *a, **k: MockResponse(200, payload))
    postings = jooble.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"333"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_error_redacts_key_from_url(monkeypatch):
    _set_key(monkeypatch)
    monkeypatch.setattr(jooble, "polite_post", lambda *a, **k: MockResponse(500))

    with pytest.raises(FetchError) as exc_info:
        jooble.fetch()

    assert SECRET_KEY not in str(exc_info.value)
    assert SECRET_KEY not in repr(exc_info.value)
    assert "<redacted>" in str(exc_info.value)
