from __future__ import annotations

from unittest.mock import patch

from jobhunt.sources import hn

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_picks_top_level_comments_from_latest_hiring_story():
    story_payload = load_fixture_json("hn_story_search.json")
    comments_page0 = load_fixture_json("hn_comments_page0.json")
    comments_page1_empty = {"hits": []}

    responses = [
        MockResponse(200, story_payload),
        MockResponse(200, comments_page0),
        MockResponse(200, comments_page1_empty),
    ]

    with patch("jobhunt.sources.hn.polite_get", side_effect=responses):
        postings = hn.fetch()

    # 4 hits in the fixture; one (objectID 112) is a nested reply and excluded.
    assert len(postings) == 3
    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"111", "113", "114"}

    acme = next(p for p in postings if p["source_posting_id"] == "111")
    assert acme["company"] == "Acme Corp"
    assert acme["title"] == "Backend Engineer"
    assert acme["url"] == "https://news.ycombinator.com/item?id=111"

    widget = next(p for p in postings if p["source_posting_id"] == "113")
    assert widget["company"] == "Widget Inc"
    assert widget["title"] == "Full Stack Developer"

    no_pipe = next(p for p in postings if p["source_posting_id"] == "114")
    assert no_pipe["company"].startswith("Just a company name")
    assert len(no_pipe["company"]) <= 60


def test_fetch_returns_empty_when_no_hiring_story_found():
    payload = {"hits": [{"objectID": "1", "title": "Ask HN: something else"}]}
    with patch("jobhunt.sources.hn.polite_get", return_value=MockResponse(200, payload)):
        postings = hn.fetch()
    assert postings == []
