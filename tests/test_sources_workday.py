from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt.sources import workday

from tests.conftest import MockResponse


# --- parse_slug --------------------------------------------------------------


def test_parse_slug_splits_three_parts_case_preserved():
    assert workday.parse_slug("nvidia:wd5:NVIDIAExternalCareerSite") == (
        "nvidia",
        "wd5",
        "NVIDIAExternalCareerSite",
    )


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "nvidia",
        "nvidia:wd5",
        "nvidia:wd5:site:extra",
        "nvidia::site",
        ":wd5:site",
        "nvidia:wd5:",
    ],
)
def test_parse_slug_rejects_malformed_slugs(slug):
    with pytest.raises(workday.BoardNotFound):
        workday.parse_slug(slug)


def test_fetch_malformed_slug_raises_board_not_found_without_network_call():
    with patch("jobhunt.sources.workday.polite_post") as mock_post:
        with pytest.raises(workday.BoardNotFound):
            workday.fetch("bad-slug")
    mock_post.assert_not_called()


# --- fetch / mapping -----------------------------------------------------


def test_fetch_parses_postings_and_builds_url_and_posted_at_is_none():
    payload = {
        "total": 1,
        "jobPostings": [
            {
                "title": "System Software Engineer",
                "externalPath": "/job/US-CA-Santa-Clara/System-Software-Engineer_JR2020767",
                "locationsText": "US, CA, Santa Clara",
                "postedOn": "Posted Today",
                "bulletFields": ["JR2020767"],
            }
        ],
    }
    with patch("jobhunt.sources.workday.polite_post", return_value=MockResponse(200, payload)):
        postings = workday.fetch("nvidia:wd5:NVIDIAExternalCareerSite", company="NVIDIA")

    assert len(postings) == 1
    p = postings[0]
    assert p["source_posting_id"] == "/job/US-CA-Santa-Clara/System-Software-Engineer_JR2020767"
    assert p["company"] == "NVIDIA"
    assert p["title"] == "System Software Engineer"
    assert p["location"] == "US, CA, Santa Clara"
    assert p["url"] == (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
        "/job/US-CA-Santa-Clara/System-Software-Engineer_JR2020767"
    )
    assert p["posted_at"] is None
    assert p["raw"]["postedOn"] == "Posted Today"


def test_fetch_defaults_company_to_tenant_when_unspecified():
    payload = {
        "total": 1,
        "jobPostings": [
            {"title": "Job", "externalPath": "/job/x", "locationsText": "Remote", "postedOn": "x"}
        ],
    }
    with patch("jobhunt.sources.workday.polite_post", return_value=MockResponse(200, payload)):
        postings = workday.fetch("nvidia:wd5:NVIDIAExternalCareerSite")
    assert postings[0]["company"] == "nvidia"


def test_fetch_skips_postings_missing_external_path():
    payload = {
        "total": 1,
        "jobPostings": [{"title": "No Path Job", "locationsText": "Remote", "postedOn": "x"}],
    }
    with patch("jobhunt.sources.workday.polite_post", return_value=MockResponse(200, payload)):
        postings = workday.fetch("acme:wd1:acme")
    assert postings == []


def test_fetch_404_raises_board_not_found():
    with (
        patch("jobhunt.sources.workday.polite_post", return_value=MockResponse(404)),
        pytest.raises(workday.BoardNotFound),
    ):
        workday.fetch("badtenant:wd1:badsite")


def test_fetch_non_200_non_404_raises_fetch_error():
    from jobhunt.net import FetchError

    with (
        patch("jobhunt.sources.workday.polite_post", return_value=MockResponse(500)),
        pytest.raises(FetchError),
    ):
        workday.fetch("acme:wd1:acme")


# --- pagination ------------------------------------------------------------


def test_fetch_paginates_by_offset_until_total_reached():
    def make_page(offset, n):
        return {
            "total": 3,
            "jobPostings": [
                {
                    "title": f"Job {i}",
                    "externalPath": f"/job/{offset + i}",
                    "locationsText": "Remote",
                    "postedOn": "x",
                }
                for i in range(n)
            ],
        }

    responses = [MockResponse(200, make_page(0, 1)), MockResponse(200, make_page(1, 1)), MockResponse(200, make_page(2, 1))]
    workday.PAGE_LIMIT = 1
    try:
        with patch(
            "jobhunt.sources.workday.polite_post", side_effect=lambda *a, **k: responses.pop(0)
        ):
            postings = workday.fetch("acme:wd1:acme")
    finally:
        workday.PAGE_LIMIT = 20

    assert [p["source_posting_id"] for p in postings] == ["/job/0", "/job/1", "/job/2"]


def test_fetch_stops_on_empty_page_even_if_total_not_reached():
    first_page = {
        "total": 100,
        "jobPostings": [
            {"title": "Job", "externalPath": "/job/0", "locationsText": "Remote", "postedOn": "x"}
        ],
    }
    empty_page = {"total": 100, "jobPostings": []}
    responses = [MockResponse(200, first_page), MockResponse(200, empty_page)]
    with patch(
        "jobhunt.sources.workday.polite_post", side_effect=lambda *a, **k: responses.pop(0)
    ):
        postings = workday.fetch("acme:wd1:acme")
    assert len(postings) == 1


def test_fetch_enforces_max_postings_cap_and_warns(capsys):
    def make_page(offset, n):
        return {
            "total": 2000,
            "jobPostings": [
                {
                    "title": f"Job {offset + i}",
                    "externalPath": f"/job/{offset + i}",
                    "locationsText": "Remote",
                    "postedOn": "x",
                }
                for i in range(n)
            ],
        }

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        offset = kwargs["json_body"]["offset"]
        call_count["n"] += 1
        return MockResponse(200, make_page(offset, workday.PAGE_LIMIT))

    with patch("jobhunt.sources.workday.polite_post", side_effect=fake_post):
        postings = workday.fetch("bigco:wd1:bigco")

    assert len(postings) == workday.MAX_POSTINGS
    assert call_count["n"] == workday.MAX_POSTINGS // workday.PAGE_LIMIT
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "bigco:wd1:bigco" in captured.out
    assert "1000" in captured.out


def test_fetch_empty_board_returns_empty_list():
    payload = {"total": 0, "jobPostings": []}
    with patch("jobhunt.sources.workday.polite_post", return_value=MockResponse(200, payload)):
        postings = workday.fetch("acme:wd1:acme")
    assert postings == []


def test_workday_opts_into_parallel_boards_because_tenants_are_distinct_hosts():
    # One hostname per tenant: parallel boards never exceed any host's budget.
    assert workday.PARALLEL_BOARDS > 1
    assert "{tenant}.{instance}.myworkdayjobs.com" in workday.ENDPOINT
