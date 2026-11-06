from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import workable

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_parses_jobs():
    payload = load_fixture_json("workable_jobs.json")
    with patch("jobhunt.sources.workable.polite_get", return_value=MockResponse(200, payload)):
        postings = workable.fetch("typeform", company="Typeform Inc")

    assert len(postings) == 2
    first = postings[0]
    assert first["source_posting_id"] == "AB1234CD"
    assert first["company"] == "Typeform Inc"
    assert first["title"] == "Backend Engineer"
    assert first["location"] == "Barcelona, Catalonia, Spain"
    assert first["url"] == "https://apply.workable.com/typeform/j/AB1234CD/"
    assert first["posted_at"] == "2026-07-10"
    assert first["raw"]["shortcode"] == "AB1234CD"

    second = postings[1]
    assert second["location"] is None  # all location fields blank


def test_fetch_defaults_company_to_widget_name_when_unspecified():
    payload = load_fixture_json("workable_jobs.json")
    with patch("jobhunt.sources.workable.polite_get", return_value=MockResponse(200, payload)):
        postings = workable.fetch("typeform")
    assert postings[0]["company"] == "Typeform"


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.workable.polite_get", return_value=MockResponse(404)),
        pytest.raises(workable.BoardNotFound),
    ):
        workable.fetch("bad-slug")


def test_fetch_empty_board_returns_empty_list():
    payload = {"name": "Empty Co", "jobs": []}
    with patch("jobhunt.sources.workable.polite_get", return_value=MockResponse(200, payload)):
        postings = workable.fetch("emptyco")
    assert postings == []


def test_fetch_skips_malformed_row_missing_shortcode():
    payload = {"name": "Acme", "jobs": [{"title": "No Shortcode Job"}]}
    with patch("jobhunt.sources.workable.polite_get", return_value=MockResponse(200, payload)):
        postings = workable.fetch("acme")
    assert postings == []


def test_fetch_builds_url_when_missing_from_payload():
    payload = {"name": "Acme", "jobs": [{"shortcode": "ZZ999", "title": "Job"}]}
    with patch("jobhunt.sources.workable.polite_get", return_value=MockResponse(200, payload)):
        postings = workable.fetch("acme")
    assert postings[0]["url"] == "https://apply.workable.com/acme/j/ZZ999/"
    assert postings[0]["posted_at"] is None
