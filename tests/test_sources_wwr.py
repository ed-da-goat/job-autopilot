from __future__ import annotations

import hashlib

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import wwr

from tests.conftest import MockResponse

FEED1, FEED2 = wwr.FEED_URLS


def _rss(items_xml: str, title: str = "We Work Remotely: Programming") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss">
  <channel>
    <title>{title}</title>
    <link>https://weworkremotely.com/categories/remote-programming-jobs.rss</link>
    <description>desc</description>
    <language>en-US</language>
    <ttl>60</ttl>
{items_xml}
  </channel>
</rss>"""


def _item(
    title: str = "Cloudflare: Principal Partner Solutions Engineer",
    region: str = "Anywhere in the World",
    category: str = "Full-Stack Programming",
    link: str = "https://weworkremotely.com/remote-jobs/cloudflare-pse",
    guid: str | None = None,
    pub_date: str = "Wed, 22 Jul 2026 07:00:51 +0000",
) -> str:
    guid_val = guid if guid is not None else link
    guid_xml = f"<guid>{guid_val}</guid>" if guid_val else ""
    return f"""    <item>
      <title>{title}</title>
      <region>{region}</region>
      <category>{category}</category>
      <description>desc</description>
      <pubDate>{pub_date}</pubDate>
      {guid_xml}
      <link>{link}</link>
    </item>"""


def _empty_feed() -> MockResponse:
    return MockResponse(200, text=_rss(""))


def test_fetch_maps_fields(monkeypatch):
    body = _rss(_item())

    def fake_polite_get(url, params=None, headers=None, session=None):
        if url == FEED1:
            return MockResponse(200, text=body)
        return _empty_feed()

    monkeypatch.setattr(wwr, "polite_get", fake_polite_get)
    postings = wwr.fetch()

    assert len(postings) == 1
    posting = postings[0]
    assert posting["source_posting_id"] == "wwr-https://weworkremotely.com/remote-jobs/cloudflare-pse"
    assert posting["company"] == "Cloudflare"
    assert posting["title"] == "Principal Partner Solutions Engineer"
    assert posting["location"] == "Anywhere in the World"
    assert posting["url"] == "https://weworkremotely.com/remote-jobs/cloudflare-pse"
    assert posting["posted_at"] == "2026-07-22T07:00:51+00:00"
    assert posting["raw"]["title"] == "Cloudflare: Principal Partner Solutions Engineer"


def test_fetch_dedupes_across_both_feeds(monkeypatch):
    shared_item = _item(guid="https://weworkremotely.com/remote-jobs/doximity-data-eng")
    body = _rss(shared_item)

    monkeypatch.setattr(wwr, "polite_get", lambda url, **k: MockResponse(200, text=body))
    postings = wwr.fetch()

    ids = [p["source_posting_id"] for p in postings]
    assert len(ids) == 1  # same guid seen in both feeds, kept once


def test_fetch_no_colon_title_falls_back_to_region(monkeypatch):
    item = _item(title="Data Engineering Manager", region="United States")
    body = _rss(item)

    def fake_polite_get(url, params=None, headers=None, session=None):
        if url == FEED1:
            return MockResponse(200, text=body)
        return _empty_feed()

    monkeypatch.setattr(wwr, "polite_get", fake_polite_get)
    postings = wwr.fetch()

    assert len(postings) == 1
    assert postings[0]["company"] == "United States"
    assert postings[0]["title"] == "Data Engineering Manager"


def test_fetch_no_colon_no_region_is_skipped(monkeypatch, capsys):
    item = _item(title="Data Engineering Manager", region="")
    body = _rss(item)

    def fake_polite_get(url, params=None, headers=None, session=None):
        if url == FEED1:
            return MockResponse(200, text=body)
        return _empty_feed()

    monkeypatch.setattr(wwr, "polite_get", fake_polite_get)
    postings = wwr.fetch()

    assert postings == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "skipped" in out


def test_fetch_missing_guid_falls_back_to_sha1_of_link(monkeypatch):
    link = "https://weworkremotely.com/remote-jobs/no-guid-job"
    item = _item(link=link, guid="")
    body = _rss(item)

    def fake_polite_get(url, params=None, headers=None, session=None):
        if url == FEED1:
            return MockResponse(200, text=body)
        return _empty_feed()

    monkeypatch.setattr(wwr, "polite_get", fake_polite_get)
    postings = wwr.fetch()

    expected = f"wwr-{hashlib.sha1(link.encode('utf-8')).hexdigest()}"
    assert postings[0]["source_posting_id"] == expected


def test_fetch_malformed_item_missing_title_and_link_is_skipped(monkeypatch, capsys):
    malformed = """    <item>
      <region>Anywhere</region>
      <pubDate>Wed, 22 Jul 2026 07:00:51 +0000</pubDate>
    </item>"""
    body = _rss(_item() + malformed)

    def fake_polite_get(url, params=None, headers=None, session=None):
        if url == FEED1:
            return MockResponse(200, text=body)
        return _empty_feed()

    monkeypatch.setattr(wwr, "polite_get", fake_polite_get)
    postings = wwr.fetch()

    assert len(postings) == 1  # only the well-formed item survives
    out = capsys.readouterr().out
    assert "skipped" in out


def test_fetch_rfc822_pubdate_parses_to_iso(monkeypatch):
    item = _item(pub_date="Sun, 02 Aug 2026 07:31:05 +0000")
    body = _rss(item)

    def fake_polite_get(url, params=None, headers=None, session=None):
        if url == FEED1:
            return MockResponse(200, text=body)
        return _empty_feed()

    monkeypatch.setattr(wwr, "polite_get", fake_polite_get)
    postings = wwr.fetch()

    assert postings[0]["posted_at"] == "2026-08-02T07:31:05+00:00"


def test_fetch_raises_fetch_error_on_non_200(monkeypatch):
    monkeypatch.setattr(wwr, "polite_get", lambda *a, **k: MockResponse(503))

    with pytest.raises(FetchError):
        wwr.fetch()


def test_fetch_raises_fetch_error_on_non_parseable_body(monkeypatch):
    # e.g. an HTML challenge page returned instead of RSS.
    monkeypatch.setattr(wwr, "polite_get", lambda *a, **k: MockResponse(200, text="<html>not rss"))

    with pytest.raises(FetchError):
        wwr.fetch()
