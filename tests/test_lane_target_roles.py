"""The target_roles lane preset (the default). Tmp DBs only; fixtures built as in test_finai.py."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from jobhunt import db as db_module
from jobhunt import eligibility
from jobhunt import finai as finai_module
from jobhunt.eligibility import LaneConfig, SearchPreferences

BASE = SearchPreferences(
    version=1, allowed_countries=("US",), allow_us_remote=True, max_eligible_years=3,
    review_years_max=5, blocked_title_terms=("senior", "director", "head of"),
    review_title_terms=(), target_roles=("software engineer",), target_start="",
    required_application_fields=(),
)
DESC = "Build services in Python."


@pytest.fixture
def prefs(monkeypatch):
    """Set the active preferences for one test; the city tiers stay the template's."""
    def use(**changes):
        p = replace(BASE, **changes)
        monkeypatch.setattr(finai_module.eligibility, "load_preferences", lambda: p)
        return p
    use()
    return use


@pytest.fixture
def conn(tmp_path):
    connection = db_module.connect(tmp_path / "lane.db")
    try:
        yield connection
    finally:
        connection.close()


def add(conn, spid, title, location="New York, NY", desc=DESC, company="Acme",
        posted="2026-10-01", status="active"):
    conn.execute(
        """INSERT INTO postings (source, source_posting_id, company, title, location, url,
               posted_at, raw_json, status, first_seen_at, last_seen_at)
           VALUES ('ashby', ?, ?, ?, ?, 'https://x/' || ?, ?, ?, ?, '2026-10-01', '2026-10-01')""",
        (spid, company, title, location, spid, posted, json.dumps({"descriptionPlain": desc}), status),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def score(conn, posting_id, total):
    conn.execute(
        "INSERT INTO scores (posting_id, rubric_version, total) VALUES (?, 1, ?)", (posting_id, total))
    conn.commit()


def ids(conn, **kw):
    return [r["id"] for r in finai_module.candidates(conn, **kw)]


def test_template_default_preset_is_target_roles():
    lane = eligibility.load_preferences(eligibility.TEMPLATE_PREFERENCES_PATH).lane
    assert lane.preset == "target_roles"
    assert eligibility.load_preferences(eligibility.TEMPLATE_PREFERENCES_PATH).target_roles
    assert LaneConfig().preset == "target_roles"


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="preset must be one of"):
        eligibility.load_lane({"preset": "nonsense"})


def test_swe_in_lane_and_finance_analyst_not(conn, prefs):
    swe = add(conn, "s1", "Software Engineer, Backend")
    add(conn, "s2", "Financial Analyst", desc="Python automation for banks.")
    rows = finai_module.candidates(conn)
    assert [r["id"] for r in rows] == [swe]
    assert rows[0]["route"] == "T" and rows[0]["city_tier"] == 1
    assert set(rows[0]) == {"id", "company", "title", "location", "city_tier", "route", "url", "score"}


def test_include_terms_override_target_roles_and_support_wildcard(conn, prefs):
    prefs(lane=LaneConfig(include_title_terms=("data engineer*",)))
    add(conn, "i1", "Software Engineer")
    yes = add(conn, "i2", "Data Engineers, Platform")
    assert ids(conn) == [yes]


def test_exclude_blocked_and_intern_terms_drop_posting(conn, prefs):
    prefs(lane=LaneConfig(exclude_title_terms=("contract",)))
    yes = add(conn, "e1", "Software Engineer")
    add(conn, "e2", "Software Engineer (Contract)")
    add(conn, "e3", "Senior Software Engineer")
    add(conn, "e4", "Software Engineer Intern")
    assert ids(conn) == [yes]


def test_remote_us_only_when_allow_us_remote(conn, prefs):
    remote = add(conn, "r1", "Software Engineer", location="Remote - US")
    bare = add(conn, "r2", "Software Engineer", location="Remote")
    add(conn, "r3", "Software Engineer", location="Remote - Canada")
    add(conn, "r4", "Software Engineer", location="Berlin, Germany")
    assert set(ids(conn)) == {remote, bare}
    assert {r["city_tier"] for r in finai_module.candidates(conn)} == {4}  # after template tiers 1-3
    prefs(allow_us_remote=False)
    assert ids(conn) == []


def test_remote_tier_follows_last_configured_tier(conn, prefs, monkeypatch):
    import re
    monkeypatch.setattr(finai_module, "CITY_TIERS", [(1, re.compile("boston", re.I))])
    boston = add(conn, "t1", "Software Engineer", location="Boston, MA")
    remote = add(conn, "t2", "Software Engineer", location="Remote - US")
    rows = finai_module.candidates(conn)
    assert [(r["id"], r["city_tier"]) for r in rows] == [(boston, 1), (remote, 2)]


def test_require_jd_terms_filters_on_description(conn, prefs):
    prefs(lane=LaneConfig(require_jd_terms=("rust", "golang")))
    add(conn, "j1", "Software Engineer", desc="We write Python.")
    yes = add(conn, "j2", "Software Engineer", desc="We write Rust services.")
    assert ids(conn) == [yes]


def test_min_score_filters(conn, prefs):
    prefs(lane=LaneConfig(min_score=70))
    hi = add(conn, "m1", "Software Engineer")
    lo = add(conn, "m2", "Software Engineer")
    add(conn, "m3", "Software Engineer")  # unscored
    score(conn, hi, 70)
    score(conn, lo, 69)
    assert ids(conn) == [hi]


def test_lane_max_tier_caps_locations(conn, prefs):
    prefs(lane=LaneConfig(max_tier=2))
    nyc = add(conn, "x1", "Software Engineer", location="New York, NY")
    add(conn, "x2", "Software Engineer", location="Boston, MA")  # tier 3
    add(conn, "x3", "Software Engineer", location="Remote - US")
    assert ids(conn) == [nyc]


def test_ordering_is_tier_then_score_then_date(conn, prefs):
    remote_hi = add(conn, "o1", "Software Engineer", location="Remote - US")
    atl_lo = add(conn, "o2", "Software Engineer", location="Atlanta, GA")
    atl_hi = add(conn, "o3", "Software Engineer", location="Atlanta, GA")
    atl_new = add(conn, "o4", "Software Engineer", location="Atlanta, GA", posted="2026-10-05")
    atl_old = add(conn, "o5", "Software Engineer", location="Atlanta, GA", posted="2026-09-01")
    nyc = add(conn, "o6", "Software Engineer", location="New York, NY")
    score(conn, remote_hi, 99)
    score(conn, atl_lo, 40)
    score(conn, atl_hi, 80)
    # atl_new / atl_old are unscored: after scored ones, newest first
    assert ids(conn) == [nyc, atl_hi, atl_lo, atl_new, atl_old, remote_hi]


def test_worked_duplicate_inactive_and_reported_are_excluded(conn, prefs):
    yes = add(conn, "w1", "Software Engineer")
    closed = add(conn, "w2", "Software Engineer", status="closed")
    applied = add(conn, "w3", "Software Engineer")
    reported = add(conn, "w4", "Software Engineer")
    dup = add(conn, "w5", "Software Engineer")
    conn.execute("INSERT INTO funnel (posting_id, stage, updated_at) VALUES (?, 'applied', '2026-10-02')", (applied,))
    conn.execute("UPDATE postings SET duplicate_of=? WHERE id=?", (yes, dup))
    conn.execute(
        "INSERT INTO artifacts (posting_id, kind, content, created_at, updated_at) VALUES (?, ?, '{}', 'x', 'x')",
        (reported, finai_module.REPORT_KIND))
    conn.commit()
    assert closed and ids(conn) == [yes]
