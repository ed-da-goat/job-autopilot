from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import teamtailor

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_parses_jobs():
    payload = load_fixture_json("teamtailor_jobs.json")
    with patch("jobhunt.sources.teamtailor.polite_get", return_value=MockResponse(200, payload)):
        postings = teamtailor.fetch("teamlewis", company="TEAM LEWIS")

    assert len(postings) == 2
    first = postings[0]
    assert first["source_posting_id"] == "8147893"
    assert first["company"] == "TEAM LEWIS"
    assert first["title"] == "Marketing Executive"
    assert first["location"] == "Madrid, Southern Europe, ES"
    assert first["url"] == "https://teamlewis.teamtailor.com/jobs/8147893-marketing-executive"
    assert first["posted_at"] == "2026-07-30T09:35:00+01:00"
    assert first["raw"]["id"] == "0362c43a-128f-4c10-87a8-71d7d8b03b92"


def test_fetch_handles_missing_job_location():
    payload = load_fixture_json("teamtailor_jobs.json")
    with patch("jobhunt.sources.teamtailor.polite_get", return_value=MockResponse(200, payload)):
        postings = teamtailor.fetch("recruitgo")
    second = postings[1]
    assert second["location"] is None
    assert second["company"] == "RecruitGo"  # falls back to hiringOrganization.name


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.teamtailor.polite_get", return_value=MockResponse(404)),
        pytest.raises(teamtailor.BoardNotFound),
    ):
        teamtailor.fetch("bad-slug")


def test_fetch_empty_board_returns_empty_list():
    payload = {"items": []}
    with patch("jobhunt.sources.teamtailor.polite_get", return_value=MockResponse(200, payload)):
        postings = teamtailor.fetch("emptyco")
    assert postings == []


def test_fetch_skips_malformed_row_missing_id():
    payload = {"items": [{"title": "No ID Job", "_jobposting": {}}]}
    with patch("jobhunt.sources.teamtailor.polite_get", return_value=MockResponse(200, payload)):
        postings = teamtailor.fetch("acme")
    assert postings == []


def test_fetch_falls_back_to_id_parsed_from_url_when_identifier_missing():
    payload = {
        "items": [
            {
                "id": "uuid-1",
                "title": "Job",
                "url": "https://acme.teamtailor.com/jobs/12345-job-title",
                "date_published": "2026-01-01T00:00:00+00:00",
                "_jobposting": {},
            }
        ]
    }
    with patch("jobhunt.sources.teamtailor.polite_get", return_value=MockResponse(200, payload)):
        postings = teamtailor.fetch("acme")
    assert postings[0]["source_posting_id"] == "12345"
