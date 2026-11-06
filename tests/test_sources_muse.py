from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import muse

from tests.conftest import MockResponse

SECRET_KEY = "sh-super-secret-muse-api-key"


def _result(
    posting_id: int,
    title: str = "Software Engineer",
    company: str = "Acme Corp",
    locations: list[str] | None = None,
    landing_page: str | None = None,
    publication_date: str = "2026-07-20T00:00:00Z",
) -> dict:
    loc_names = locations if locations is not None else ["Atlanta, GA"]
    return {
        "id": posting_id,
        "name": title,
        "company": {"name": company},
        "locations": [{"name": n} for n in loc_names],
        "refs": {"landing_page": landing_page or f"https://www.themuse.com/jobs/acme/{posting_id}"},
        "publication_date": publication_date,
    }


def _page(results: list[dict], page_count: int = 1) -> dict:
    return {"page_count": page_count, "results": results}


def test_fetch_maps_fields(monkeypatch):
    payload = _page([_result(111, title="Backend Engineer, New Grad")])

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, payload)

    monkeypatch.setattr(muse, "polite_get", fake_polite_get)
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    postings = muse.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "111"
    assert posting["company"] == "Acme Corp"
    assert posting["title"] == "Backend Engineer, New Grad"
    assert posting["location"] == "Atlanta, GA"
    assert posting["url"] == "https://www.themuse.com/jobs/acme/111"
    assert posting["posted_at"] == "2026-07-20T00:00:00Z"
    assert posting["raw"] == payload["results"][0]

    # page_count=1 stops after a single page per category.
    assert len(calls) == len(muse.CATEGORIES)
    for _, params in calls:
        assert "api_key" not in params


def test_fetch_joins_multiple_locations(monkeypatch):
    payload = _page([_result(222, locations=["Atlanta, GA", "Remote"])])
    monkeypatch.setattr(muse, "polite_get", lambda *a, **k: MockResponse(200, payload))
    monkeypatch.delenv("MUSE_API_KEY", raising=False)

    postings = muse.fetch()
    assert postings[0]["location"] == "Atlanta, GA; Remote"


def test_fetch_pagination_capped_and_stops_on_page_count(monkeypatch):
    # page_count=3 means pages 0,1,2 are fetched, page index 2 is last (page >= page_count-1 stops).
    pages = [
        _page([_result(1)], page_count=3),
        _page([_result(2)], page_count=3),
        _page([_result(3)], page_count=3),
    ]
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        idx = call_count["n"]
        call_count["n"] += 1
        return MockResponse(200, pages[idx % len(pages)])

    monkeypatch.setattr(muse, "polite_get", fake_polite_get)
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    muse.fetch()

    # 3 pages per category (pages 0,1,2), 2 categories = 6 calls, never MAX_PAGES(5) exceeded.
    assert call_count["n"] == 3 * len(muse.CATEGORIES)


def test_fetch_caps_at_max_pages_when_page_count_huge(monkeypatch):
    # page_count huge (like live Muse) — must still stop at MAX_PAGES per category.
    payload = _page([_result(9)], page_count=5000)
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        call_count["n"] += 1
        return MockResponse(200, payload)

    monkeypatch.setattr(muse, "polite_get", fake_polite_get)
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    muse.fetch()

    assert call_count["n"] == muse.MAX_PAGES * len(muse.CATEGORIES)


def test_fetch_dedupes_across_categories(monkeypatch):
    payload = _page([_result(333)])
    monkeypatch.setattr(muse, "polite_get", lambda *a, **k: MockResponse(200, payload))
    monkeypatch.delenv("MUSE_API_KEY", raising=False)

    postings = muse.fetch()
    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["333"]  # same id in both categories, only one posting kept


def test_fetch_skips_results_missing_company_or_landing_page(monkeypatch, capsys):
    good = _result(444)
    no_company = {**_result(445), "company": {}}
    no_landing_page = {**_result(446), "refs": {}}
    payload = _page([good, no_company, no_landing_page])

    monkeypatch.setattr(muse, "polite_get", lambda *a, **k: MockResponse(200, payload))
    monkeypatch.delenv("MUSE_API_KEY", raising=False)
    postings = muse.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"444"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_includes_api_key_param_only_when_env_set(monkeypatch):
    payload = _page([_result(555)])
    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append(params)
        return MockResponse(200, payload)

    monkeypatch.setattr(muse, "polite_get", fake_polite_get)
    monkeypatch.setenv("MUSE_API_KEY", SECRET_KEY)
    muse.fetch()

    assert all(p.get("api_key") == SECRET_KEY for p in calls)


def test_fetch_error_never_contains_api_key(monkeypatch):
    monkeypatch.setattr(muse, "polite_get", lambda *a, **k: MockResponse(500))
    monkeypatch.setenv("MUSE_API_KEY", SECRET_KEY)

    with pytest.raises(FetchError) as exc_info:
        muse.fetch()

    assert SECRET_KEY not in str(exc_info.value)
    assert SECRET_KEY not in repr(exc_info.value)
