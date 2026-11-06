"""Workday on-demand JD fetch. Offline: polite_get and robots are mocked."""

from __future__ import annotations

import json

import pytest

from jobhunt.net import FetchError
from jobhunt.sources import workday

POSTING = "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/New-York/Analyst_R123"
CXS = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/job/New-York/Analyst_R123"


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def allowed(monkeypatch):
    monkeypatch.setattr(workday, "_robots_allowed", lambda url: True)


def _mock_get(monkeypatch, resp):
    calls = []

    def fake(url, params=None, headers=None, session=None):
        calls.append((url, headers))
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(workday, "polite_get", fake)
    return calls


@pytest.mark.parametrize("url", [
    POSTING,
    "https://acme.wd5.myworkdayjobs.com/Careers/job/New-York/Analyst_R123",
    POSTING + "?source=x",
])
def test_detail_url_parsing(url):
    assert workday._detail_url(url) == CXS


@pytest.mark.parametrize("url", ["", "not a url", "https://example.com/en-US/Careers/job/x",
                                 "https://acme.wd5.myworkdayjobs.com/en-US/Careers/nojob"])
def test_detail_url_rejects_non_workday(url):
    assert workday._detail_url(url) is None


def test_fetch_description_success(monkeypatch, allowed):
    payload = {"jobPostingInfo": {"jobDescription": "<p>Use <b>Python</b> and AI</p>"}}
    calls = _mock_get(monkeypatch, Resp(payload=payload))
    assert workday.fetch_description(POSTING) == "Use Python and AI"
    assert calls == [(CXS, {"Accept": "application/json"})]


@pytest.mark.parametrize("resp", [
    FetchError("boom"),
    Resp(status=404),
    Resp(payload=json.JSONDecodeError("bad", "", 0)),
    Resp(payload={"jobPostingInfo": {}}),
    Resp(payload=["nope"]),
])
def test_fetch_description_failures_return_empty(monkeypatch, allowed, resp):
    _mock_get(monkeypatch, resp)
    assert workday.fetch_description(POSTING) == ""


def test_fetch_description_unparseable_url_makes_no_request(monkeypatch, allowed):
    calls = _mock_get(monkeypatch, Resp(payload={}))
    assert workday.fetch_description("https://example.com/x") == ""
    assert calls == []


def test_fetch_description_robots_disallow_skips_fetch(monkeypatch):
    calls = _mock_get(monkeypatch, Resp(payload={}))
    monkeypatch.setattr(workday, "_robots_allowed", lambda url: False)
    assert workday.fetch_description(POSTING) == ""
    assert calls == []


@pytest.mark.parametrize("resp,expected", [
    (Resp(text="User-agent: *\nDisallow: /wday/\n"), False),
    (Resp(text="User-agent: *\nDisallow: /search\n"), True),
    (Resp(status=404), True),
    (Resp(status=403), False),
    (Resp(status=503), False),
    (FetchError("down"), False),
])
def test_robots_allowed(monkeypatch, resp, expected):
    _mock_get(monkeypatch, resp)
    assert workday._robots_allowed(CXS) is expected
