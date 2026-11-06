from __future__ import annotations

from unittest.mock import patch

from jobhunt.sources import zapply

from tests.conftest import MockResponse, load_fixture


def test_parse_zapply_markdown_from_fixture():
    text = load_fixture("zapply_readme.md")
    postings, skipped = zapply.parse_zapply_markdown(text)

    # Curtiss-Wright + Acme (SWE table) + Globus Medical (Business table) = 3;
    # "No Link Co" has no extractable URL.
    assert len(postings) == 3
    assert skipped == 1

    curtiss, acme, globus = postings

    assert curtiss["company"] == "Curtiss-Wright"
    assert curtiss["title"] == "Associate Software Development Engineer"
    assert curtiss["location"] == "Tewksbury, MA"
    assert curtiss["url"].startswith("https://curtisswright.wd1.myworkdayjobs.com/")
    assert curtiss["posted_at"] == "3m"
    assert curtiss["raw"]["visa"] is None

    assert acme["company"] == "Acme Corp"
    assert acme["url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert acme["raw"]["visa"] == "✅ Sponsor"

    # Header rows from BOTH section tables are skipped, not parsed as postings.
    assert globus["company"] == "Globus Medical"
    assert globus["raw"]["visa"] == "🏛 H-1B Co."

    # source_posting_id is a stable sha1 of company|role|link
    assert len(curtiss["source_posting_id"]) == 40


def test_fetch_wraps_polite_get():
    text = load_fixture("zapply_readme.md")
    with patch("jobhunt.sources.zapply.polite_get", return_value=MockResponse(200, text=text)):
        postings = zapply.fetch()
    assert len(postings) == 3
