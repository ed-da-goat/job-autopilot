from __future__ import annotations

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import builtin

from tests.conftest import MockResponse

# Structural fragments below mirror real builtin.com/jobs/<category> markup
# observed live 2026-08-02 (class names, attribute order, and the
# data-bs-title tooltip encoding for multi-location cards) — not a
# hand-simplified approximation.

_LOCATION_SIMPLE_TPL = """\
<div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-location-dot fs-xs text-pretty-blue"></i></div>
<div><span class="font-barlow text-gray-04">{location}</span></div></div>"""

_LOCATION_MULTI_TPL = """\
<div class="d-flex align-items-start gap-sm"><div class="d-flex justify-content-center align-items-center h-lg min-w-md"><i class="fa-regular fa-location-dot fs-xs text-pretty-blue"></i></div>
<div><span class="font-barlow text-gray-04 text-decoration-underline z-1 cursor-pointer" data-bs-toggle="tooltip" data-bs-placement="bottom" data-bs-html="true" role="tooltip" aria-label="Job locations" data-bs-title="{tooltip}">{n} Locations
</span></div></div>"""


def _tooltip(locations: list[str]) -> str:
    items = "".join(
        f"&lt;div class=&#x27;text-truncate&#x27;&gt;{loc}&lt;/div&gt;"
        for loc in locations
    )
    return items


def _card(
    posting_id: str = "3656874",
    track_id: str | None = None,
    title: str = "Software Engineer",
    company: str = "Simplex Trading",
    locations: list[str] | None = None,
    posted_text: str = "Reposted 2 Days Ago",
    omit_company: bool = False,
    omit_title: bool = False,
) -> str:
    alias = f"/job/software-engineer/{posting_id}"
    track_id = track_id or posting_id
    locations = locations if locations is not None else ["Chicago, IL, USA"]

    company_html = (
        ""
        if omit_company
        else (
            f'<div class="left-side-tile-item-2"><a href="/company/simplex-trading" target="_blank" '
            f'data-id="company-title" data-builtin-track-job-id="{track_id}" '
            f'class="font-barlow fw-medium fs-xl"><span>{company}</span></a></div>'
        )
    )
    title_html = (
        ""
        if omit_title
        else (
            f'<div class="left-side-tile-item-3"><h2 class="font-barlow fw-medium fs-xl"><a href="{alias}" '
            f'target="_blank" data-id="job-card-title" data-alias="{alias}" '
            f'data-builtin-track-job-id="{track_id}" class="card-alias-after-overlay text-break">{title}</a></h2></div>'
        )
    )
    if len(locations) > 1:
        location_html = _LOCATION_MULTI_TPL.format(
            tooltip=_tooltip(locations), n=len(locations)
        )
    else:
        location_html = _LOCATION_SIMPLE_TPL.format(location=locations[0])

    posted_html = (
        f'<span class="fs-xs fw-bold bg-gray-01"><i class="fa-regular fa-clock fs-xs text-gray-03">'
        f"</i>{posted_text}</span>"
    )

    return f"""
<div class="job-card">
  {company_html}
  {title_html}
  <div class="col-12 bounded-attribute-section">
    <div class="d-flex flex-column">
      {posted_html}
      {location_html}
    </div>
  </div>
</div>
"""


def _page_html(cards: list[str]) -> str:
    return "<html><body><main>" + "".join(cards) + "</main></body></html>"


def test_fetch_maps_fields(monkeypatch):
    html = _page_html([_card()])

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append((url, params))
        return MockResponse(200, text=html)

    monkeypatch.setattr(builtin, "polite_get", fake_polite_get)
    postings = builtin.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "bi-3656874"
    assert posting["company"] == "Simplex Trading"
    assert posting["title"] == "Software Engineer"
    assert posting["location"] == "Chicago, IL, USA"
    assert posting["url"] == "https://builtin.com/job/software-engineer/3656874"
    assert posting["posted_at"] is None
    assert posting["raw"]["posted_text"] == "Reposted 2 Days Ago"

    # Page 1 of each category carries no ?page= param.
    for url, params in calls:
        assert (
            params is None or params.get("page", 1) != 1 or "page" not in (params or {})
        )


def test_fetch_joins_multiple_locations(monkeypatch):
    html = _page_html(
        [_card(locations=["Dallas, TX, USA", "Toronto, ON, CAN", "Chicago, IL, USA"])]
    )
    monkeypatch.setattr(
        builtin, "polite_get", lambda *a, **k: MockResponse(200, text=html)
    )

    postings = builtin.fetch()
    assert (
        postings[0]["location"] == "Dallas, TX, USA; Toronto, ON, CAN; Chicago, IL, USA"
    )


def test_fetch_pagination_early_stops_on_empty_page(monkeypatch):
    page1 = _page_html([_card(posting_id="111")])
    page2 = _page_html([])  # no cards -> stop, not an error

    calls = []

    def fake_polite_get(url, params=None, headers=None, session=None):
        calls.append(params)
        html = page1 if (params is None or "page" not in params) else page2
        return MockResponse(200, text=html)

    monkeypatch.setattr(builtin, "polite_get", fake_polite_get)
    builtin.fetch()

    # 2 requests per category (page 1 with cards, page 2 empty -> stop).
    assert len(calls) == 2 * len(builtin.CATEGORIES)


def test_fetch_caps_at_max_pages(monkeypatch):
    call_count = {"n": 0}

    def fake_polite_get(url, params=None, headers=None, session=None):
        call_count["n"] += 1
        # Every page returns a (uniquely-id'd) card, so nothing ever early-stops.
        html = _page_html([_card(posting_id=str(1000 + call_count["n"]))])
        return MockResponse(200, text=html)

    monkeypatch.setattr(builtin, "polite_get", fake_polite_get)
    builtin.fetch()

    assert call_count["n"] == builtin.MAX_PAGES * len(builtin.CATEGORIES)


def test_fetch_dedupes_across_categories(monkeypatch):
    html = _page_html([_card(posting_id="333")])
    monkeypatch.setattr(
        builtin, "polite_get", lambda *a, **k: MockResponse(200, text=html)
    )

    postings = builtin.fetch()
    ids = [p["source_posting_id"] for p in postings]
    assert ids == ["bi-333"]  # same id from every category/page, only one posting kept


def test_fetch_uses_url_id_not_tracking_id_for_identity(monkeypatch):
    # Live-observed case: a reposted card's data-builtin-track-job-id can
    # differ from the numeric id embedded in the canonical job URL.
    html = _page_html([_card(posting_id="3656874", track_id="4993252")])
    monkeypatch.setattr(
        builtin, "polite_get", lambda *a, **k: MockResponse(200, text=html)
    )

    postings = builtin.fetch()
    assert postings[0]["source_posting_id"] == "bi-3656874"
    assert postings[0]["url"].endswith("/3656874")


def test_fetch_skips_cards_missing_company_or_title(monkeypatch, capsys):
    good = _card(posting_id="444")
    no_company = _card(posting_id="445", omit_company=True)
    no_title = _card(posting_id="446", omit_title=True)
    html = _page_html([good, no_company, no_title])
    monkeypatch.setattr(
        builtin, "polite_get", lambda *a, **k: MockResponse(200, text=html)
    )

    postings = builtin.fetch()
    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"bi-444"}
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_challenge_raises_fetch_error(monkeypatch):
    # A CloudFront/WAF challenge page or any non-200 response is a hard
    # stop — never retried or bypassed.
    monkeypatch.setattr(
        builtin, "polite_get", lambda *a, **k: MockResponse(403, text="Request blocked")
    )

    with pytest.raises(FetchError):
        builtin.fetch()
