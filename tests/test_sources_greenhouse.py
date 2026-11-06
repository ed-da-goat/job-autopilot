from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import greenhouse

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_parses_jobs():
    payload = load_fixture_json("greenhouse_jobs.json")
    with patch("jobhunt.sources.greenhouse.polite_get", return_value=MockResponse(200, payload)):
        postings = greenhouse.fetch("acme", company="Acme Corp")

    assert len(postings) == 2
    first = postings[0]
    assert first["source_posting_id"] == "1001"
    assert first["company"] == "Acme Corp"
    assert first["title"] == "Backend Engineer"
    assert first["location"] == "San Francisco, CA"
    assert first["url"] == "https://boards.greenhouse.io/acme/jobs/1001"
    assert first["posted_at"] == "2026-07-10T12:00:00-00:00"
    assert first["raw"]["id"] == 1001


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.greenhouse.polite_get", return_value=MockResponse(404)),
        pytest.raises(greenhouse.BoardNotFound),
    ):
        greenhouse.fetch("bad-slug")


def test_fetch_defaults_company_to_slug_when_unspecified():
    payload = load_fixture_json("greenhouse_jobs.json")
    with patch("jobhunt.sources.greenhouse.polite_get", return_value=MockResponse(200, payload)):
        postings = greenhouse.fetch("acme")
    assert postings[0]["company"] == "acme"
