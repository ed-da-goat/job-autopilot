from __future__ import annotations

import json

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import netflix

from tests.conftest import MockResponse


class _ChallengeResponse:
    """Simulates a 200-status HTML challenge page where .json() blows up
    exactly like requests.Response does on non-JSON bodies."""

    status_code = 200

    def json(self):
        raise json.JSONDecodeError("Expecting value", "<html>captcha challenge</html>", 0)


def _position(
    position_id: int = 790317608818,
    name: str = "Forward Deployed Engineer",
    locations: list[str] | None = None,
    canonical_url: str | None = None,
    t_create: int = 1785456000,
) -> dict:
    locs = locations if locations is not None else ["USA - Remote"]
    return {
        "id": position_id,
        "name": name,
        "location": locs[0] if locs else None,
        "locations": locs,
        "ats_job_id": "JR41909",
        "canonicalPositionUrl": canonical_url or f"https://explore.jobs.netflix.net/careers/job/{position_id}",
        "t_create": t_create,
        "t_update": t_create,
    }


def _page(positions: list[dict]) -> dict:
    return {"positions": positions, "count": len(positions)}


def test_fetch_maps_fields(monkeypatch):
    payload = _page([_position()])

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, payload)

    monkeypatch.setattr(netflix, "polite_get", fake_polite_get)
    postings = netflix.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "nflx-790317608818"
    assert posting["company"] == "Netflix"
    assert posting["title"] == "Forward Deployed Engineer"
    assert posting["location"] == "USA - Remote"
    assert posting["url"] == "https://explore.jobs.netflix.net/careers/job/790317608818"
    assert posting["posted_at"] == 1785456000
    assert posting["raw"] == payload["positions"][0]

    for _, params in calls:
        assert params["domain"] == "netflix.com"


def test_fetch_joins_multiple_locations(monkeypatch):
    payload = _page([_position(locations=["USA - Remote", "Los Gatos, CA"])])
    monkeypatch.setattr(netflix, "polite_get", lambda *a, **k: MockResponse(200, payload))

    postings = netflix.fetch()
    assert postings[0]["location"] == "USA - Remote; Los Gatos, CA"


def test_fetch_pagination_early_stops_on_short_page(monkeypatch):
    full_page = _page([_position(position_id=i) for i in range(netflix.RESULTS_PER_PAGE)])
    short_page = _page([_position(position_id=999)])
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        call_count["n"] += 1
        return MockResponse(200, full_page if params["start"] == 0 else short_page)

    monkeypatch.setattr(netflix, "polite_get", fake_polite_get)
    netflix.fetch()

    assert call_count["n"] == 2 * len(netflix.QUERIES)


def test_fetch_caps_at_max_pages_when_always_full(monkeypatch):
    full_page = _page([_position(position_id=i) for i in range(netflix.RESULTS_PER_PAGE)])
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        call_count["n"] += 1
        return MockResponse(200, full_page)

    monkeypatch.setattr(netflix, "polite_get", fake_polite_get)
    netflix.fetch()

    assert call_count["n"] == netflix.MAX_PAGES * len(netflix.QUERIES)


def test_fetch_dedupes_across_queries(monkeypatch):
    payload = _page([_position(position_id=333)])
    monkeypatch.setattr(netflix, "polite_get", lambda *a, **k: MockResponse(200, payload))

    postings = netflix.fetch()
    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["nflx-333"]


def test_fetch_skips_positions_missing_essentials(monkeypatch, capsys):
    good = _position(position_id=1)
    no_id = {**_position(position_id=2), "id": None}
    no_name = {**_position(position_id=3), "name": None}
    no_url = {**_position(position_id=4), "canonicalPositionUrl": None}
    payload = _page([good, no_id, no_name, no_url])

    monkeypatch.setattr(netflix, "polite_get", lambda *a, **k: MockResponse(200, payload))
    postings = netflix.fetch()

    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"nflx-1"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(netflix, "polite_get", lambda *a, **k: MockResponse(503, {}))

    with pytest.raises(FetchError):
        netflix.fetch()


def test_fetch_raises_on_challenge_html(monkeypatch):
    monkeypatch.setattr(netflix, "polite_get", lambda *a, **k: _ChallengeResponse())

    with pytest.raises(FetchError):
        netflix.fetch()
