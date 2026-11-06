from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import lever

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_parses_postings():
    payload = load_fixture_json("lever_postings.json")
    with patch("jobhunt.sources.lever.polite_get", return_value=MockResponse(200, payload)):
        postings = lever.fetch("acme", company="Acme Corp")

    assert len(postings) == 2
    first = postings[0]
    assert first["source_posting_id"] == "abc-123"
    assert first["company"] == "Acme Corp"
    assert first["title"] == "Founding Engineer"
    assert first["location"] == "New York, NY"
    assert first["url"] == "https://jobs.lever.co/acme/abc-123"
    assert first["posted_at"] is not None  # createdAt ms converted to ISO


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.lever.polite_get", return_value=MockResponse(404)),
        pytest.raises(lever.BoardNotFound),
    ):
        lever.fetch("bad-slug")
