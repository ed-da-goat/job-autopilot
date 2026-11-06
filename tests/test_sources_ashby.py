from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import ashby

from tests.conftest import MockResponse, load_fixture_json


def test_fetch_tolerates_both_field_spellings():
    payload = load_fixture_json("ashby_jobs.json")
    with patch("jobhunt.sources.ashby.polite_get", return_value=MockResponse(200, payload)):
        postings = ashby.fetch("acme", company="Acme Corp")

    assert len(postings) == 2

    job_url_variant = postings[0]
    assert job_url_variant["url"] == "https://jobs.ashbyhq.com/acme/ashby-1"
    assert job_url_variant["location"] == "San Francisco, CA"
    assert job_url_variant["posted_at"] == "2026-07-11T00:00:00.000Z"

    apply_url_variant = postings[1]
    assert apply_url_variant["url"] == "https://jobs.ashbyhq.com/acme/ashby-2/apply"
    assert apply_url_variant["location"] == "Remote"
    assert apply_url_variant["posted_at"] == "2026-07-13"


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.ashby.polite_get", return_value=MockResponse(404)),
        pytest.raises(ashby.BoardNotFound),
    ):
        ashby.fetch("bad-slug")
