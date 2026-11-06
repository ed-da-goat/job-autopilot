from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import smartrecruiters

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_parses_postings_and_builds_public_url():
    payload = load_fixture_json("smartrecruiters_postings.json")
    with patch(
        "jobhunt.sources.smartrecruiters.polite_get", return_value=MockResponse(200, payload)
    ):
        postings = smartrecruiters.fetch("Visa", company="Visa Inc")

    assert len(postings) == 2
    first = postings[0]
    assert first["source_posting_id"] == "744000133907678"
    assert first["company"] == "Visa Inc"
    assert first["title"] == "Sr. Manager"
    assert first["location"] == "Austin, TX, United States"
    assert first["url"] == "https://jobs.smartrecruiters.com/Visa/744000133907678-sr-manager"
    assert first["posted_at"] == "2026-06-24T10:00:11.853Z"
    assert first["raw"]["id"] == "744000133907678"

    second = postings[1]
    assert second["url"] == (
        "https://jobs.smartrecruiters.com/Visa/"
        "744000137613800-senior-information-security-engineer"
    )
    assert second["location"] == "Poland, REMOTE, Poland"


def test_fetch_defaults_company_to_api_company_name_when_unspecified():
    payload = load_fixture_json("smartrecruiters_postings.json")
    with patch(
        "jobhunt.sources.smartrecruiters.polite_get", return_value=MockResponse(200, payload)
    ):
        postings = smartrecruiters.fetch("Visa")
    assert postings[0]["company"] == "Visa"


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.smartrecruiters.polite_get", return_value=MockResponse(404)),
        pytest.raises(smartrecruiters.BoardNotFound),
    ):
        smartrecruiters.fetch("BadSlug")


def test_fetch_paginates_across_offset_until_total_found():
    page0 = {
        "offset": 0,
        "limit": 1,
        "totalFound": 2,
        "content": [
            {
                "id": "1",
                "name": "Job One",
                "company": {"identifier": "Acme", "name": "Acme"},
                "releasedDate": "2026-07-01T00:00:00Z",
                "location": {"fullLocation": "Remote"},
            }
        ],
    }
    page1 = {
        "offset": 1,
        "limit": 1,
        "totalFound": 2,
        "content": [
            {
                "id": "2",
                "name": "Job Two",
                "company": {"identifier": "Acme", "name": "Acme"},
                "releasedDate": "2026-07-02T00:00:00Z",
                "location": {"fullLocation": "Remote"},
            }
        ],
    }
    responses = [MockResponse(200, page0), MockResponse(200, page1)]
    smartrecruiters.PAGE_LIMIT = 1
    try:
        with patch(
            "jobhunt.sources.smartrecruiters.polite_get", side_effect=lambda *a, **k: responses.pop(0)
        ):
            postings = smartrecruiters.fetch("Acme")
    finally:
        smartrecruiters.PAGE_LIMIT = 100

    assert [p["source_posting_id"] for p in postings] == ["1", "2"]


def test_fetch_empty_board_returns_empty_list():
    payload = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}
    with patch(
        "jobhunt.sources.smartrecruiters.polite_get", return_value=MockResponse(200, payload)
    ):
        postings = smartrecruiters.fetch("Empty")
    assert postings == []


def test_fetch_skips_malformed_row_missing_id():
    payload = {
        "offset": 0,
        "limit": 100,
        "totalFound": 1,
        "content": [{"name": "No Id Job", "company": {"identifier": "Acme"}}],
    }
    with patch(
        "jobhunt.sources.smartrecruiters.polite_get", return_value=MockResponse(200, payload)
    ):
        postings = smartrecruiters.fetch("Acme")
    assert postings == []


def test_fetch_falls_back_to_bare_id_url_when_name_missing():
    payload = {
        "offset": 0,
        "limit": 100,
        "totalFound": 1,
        "content": [{"id": "9", "company": {"identifier": "Acme"}}],
    }
    with patch(
        "jobhunt.sources.smartrecruiters.polite_get", return_value=MockResponse(200, payload)
    ):
        postings = smartrecruiters.fetch("Acme")
    assert postings[0]["url"] == "https://jobs.smartrecruiters.com/Acme/9"
