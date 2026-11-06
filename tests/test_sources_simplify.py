from __future__ import annotations

from unittest.mock import patch

from jobhunt.sources import simplify

from tests.conftest import MockResponse, load_fixture


def test_parse_simplify_markdown_from_fixture():
    text = load_fixture("simplify_readme.md")
    postings, skipped = simplify.parse_simplify_markdown(text)

    # Acme Corp x2 (second uses the "↳" continuation marker) + Widget Inc = 3 parsed.
    assert len(postings) == 3
    assert skipped >= 1  # "No Link Co" row has no extractable URL

    acme_first = postings[0]
    assert acme_first["company"] == "Acme Corp"
    assert acme_first["title"] == "Software Engineer, New Grad"
    assert acme_first["url"] == "https://acme.example.com/jobs/1"

    acme_second = postings[1]
    assert acme_second["company"] == "Acme Corp"  # inherited via ↳
    assert acme_second["title"] == "Backend Engineer"

    widget = postings[2]
    assert widget["company"] == "Widget Inc"
    assert widget["url"] == "https://widget.example.com/apply"

    # source_posting_id is a stable sha1 of company|role|link
    assert len(acme_first["source_posting_id"]) == 40


def test_fetch_wraps_polite_get():
    text = load_fixture("simplify_readme.md")
    with patch("jobhunt.sources.simplify.polite_get", return_value=MockResponse(200, text=text)):
        postings = simplify.fetch()
    assert len(postings) == 3


def test_parse_simplify_html_table_from_fixture():
    """The live README format shipped as raw HTML <table> rows as of 2026-07."""
    text = load_fixture("simplify_readme_html.md")
    postings, skipped = simplify.parse_simplify_markdown(text)

    assert len(postings) == 2  # Acme Corp x2 (via ↳); Widget Inc has no apply link (🔒 closed)
    assert skipped == 0  # closed (🔒) rows are excluded from postings but don't count as unparseable

    first, second = postings
    assert first["company"] == "Acme Corp"
    assert first["title"] == "Software Engineer New Grad"
    assert first["location"] == "San Francisco, CA"
    assert first["url"] == "https://acme.example.com/jobs/1?utm_source=Simplify"
    assert first["posted_at"] == "0d"

    assert second["company"] == "Acme Corp"  # inherited via ↳
    assert second["title"] == "Backend Engineer New Grad"
    assert second["location"] == "Remote"


def test_parse_simplify_html_table_from_live_2026_07_fixture():
    """Regression fixture for the 2026-07 drift: the live README marks a
    closed/withdrawn posting with a bare 🔒 Application cell (no href at
    all). That's a legitimate non-job row and must NOT count toward the
    unparseable-row warning, unlike a genuinely malformed row."""
    text = load_fixture("simplify_readme_html_2026_07.md")
    postings, skipped = simplify.parse_simplify_markdown(text)

    # 25 total rows: 6 closed (🔒, no link) + 19 real postings (incl. 2
    # "↳" continuation rows). All 6 closed rows are excluded from postings
    # AND excluded from the skipped/unparseable count.
    assert len(postings) == 19
    assert skipped == 0

    by_title = {p["title"]: p for p in postings}

    # Continuation row ("↳") inherits company from the row directly above.
    cont = by_title["New Grad Design Engineer - Design Engineer - DRAM Technology and Products"]
    assert cont["company"] == "Micron Technology"

    cont2 = by_title["New College Grad - Semiconductor Verification Design Engineer - DRAM Products Group"]
    assert cont2["company"] == "Micron Technology"

    # None of the closed companies (Charles Schwab, Ameriprise, Truist,
    # JP Morgan Chase, Graham Capital Management, KeyBank) produced postings.
    closed_companies = {
        "Charles Schwab",
        "Ameriprise",
        "Truist",
        "JP Morgan Chase",
        "Graham Capital Management",
        "KeyBank",
    }
    assert {p["company"] for p in postings}.isdisjoint(closed_companies)

    # Non-standard "</br>" multi-location cells are joined with ", " rather
    # than concatenated with no separator.
    multi_loc = by_title["New Grad Product Architecture Engineer - Product Architecture"]
    assert multi_loc["location"] == "San Jose, CA, Boise, ID, Folsom, CA"
