from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import recruitee

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_parses_offers():
    payload = load_fixture_json("recruitee_offers.json")
    with patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(200, payload)):
        postings = recruitee.fetch("luzmo", company="Luzmo")

    assert len(postings) == 2
    first = postings[0]
    assert first["source_posting_id"] == "2461420"
    assert first["company"] == "Luzmo"
    assert first["title"] == "Account Executive - NY"
    assert first["location"] == "New York, United States"
    assert first["url"] == "https://luzmo.recruitee.com/o/account-executive-ny"
    assert first["posted_at"] == "2026-06-26 12:46:11 UTC"
    assert first["raw"]["id"] == 2461420


def test_fetch_falls_back_to_location_field_when_city_and_country_blank():
    payload = load_fixture_json("recruitee_offers.json")
    with patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(200, payload)):
        postings = recruitee.fetch("addepto", company="Addepto")
    assert postings[1]["location"] == "Remote job"


def test_fetch_defaults_company_to_slug_when_unspecified():
    payload = {"offers": [{"id": 1, "title": "Job", "slug": "job", "careers_url": "https://acme.recruitee.com/o/job"}]}
    with patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(200, payload)):
        postings = recruitee.fetch("acme")
    assert postings[0]["company"] == "acme"


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(404)),
        pytest.raises(recruitee.BoardNotFound),
    ):
        recruitee.fetch("bad-slug")


def test_fetch_empty_board_returns_empty_list():
    payload = {"offers": []}
    with patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(200, payload)):
        postings = recruitee.fetch("emptyco")
    assert postings == []


def test_fetch_skips_malformed_row_missing_id():
    payload = {"offers": [{"title": "No ID Job"}]}
    with patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(200, payload)):
        postings = recruitee.fetch("acme")
    assert postings == []


def test_fetch_builds_url_when_missing_from_payload():
    payload = {"offers": [{"id": 999, "title": "Job", "slug": "job"}]}
    with patch("jobhunt.sources.recruitee.polite_get", return_value=MockResponse(200, payload)):
        postings = recruitee.fetch("acme")
    assert postings[0]["url"] == "https://acme.recruitee.com/o/job"
    assert postings[0]["posted_at"] is None
