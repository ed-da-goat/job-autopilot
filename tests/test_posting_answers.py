"""posting_answers: the answers a posting decides (location, salary, tracker facts).

Tmp DBs, a made-up master resume and made-up postings only; nothing here touches
the real database, the real master resume or the network."""

from __future__ import annotations

import json
import re

import pytest

from jobhunt import db as db_module
from jobhunt import finai
from jobhunt.apply_assist import posting_answers as pa
from jobhunt.apply_assist.keys import SEMANTIC_KEYS
from tests.conftest import operator_file

# Captured at import, before any test patches it.
REAL_MASTER_PATH = pa.MASTER_PATH
REAL_FACTS_PATH = pa.FACTS_PATH
REAL_ANSWERS_PATH = pa.ANSWERS_PATH
OPERATOR_MASTER_PATH = operator_file("data/apply_packs/finai/master.rm.json")
OPERATOR_ANSWERS_PATH = operator_file("data/answers.local.md")

MASTER = {
    "personalInfo": {"name": "Pat Example"},
    "workExperience": [
        {"title": "Founder", "company": "Fictional Advisory LLC", "years": "2024 - Present"},
        {"title": "Analyst", "company": "Sample Ledger Co.", "years": "2022 - 2023"},
        {"title": "Intern", "company": ""},
        "not a job",
    ],
}


@pytest.fixture(autouse=True)
def _no_local_facts_or_answers(tmp_path, monkeypatch):
    """Alex's real facts and answers files never leak into a test: both read as missing
    unless a test writes one (facts_file, answers_file)."""
    monkeypatch.setattr(pa, "FACTS_PATH", tmp_path / "no-facts.local.md")
    monkeypatch.setattr(pa, "ANSWERS_PATH", tmp_path / "no-answers.local.md")


@pytest.fixture
def facts_file(tmp_path, monkeypatch):
    path = tmp_path / "facts.local.md"
    monkeypatch.setattr(pa, "FACTS_PATH", path)
    return path


@pytest.fixture
def answers_file(tmp_path, monkeypatch):
    path = tmp_path / "answers.local.md"
    monkeypatch.setattr(pa, "ANSWERS_PATH", path)
    return path


@pytest.fixture
def master_path(tmp_path, monkeypatch):
    path = tmp_path / "master.rm.json"
    path.write_text(json.dumps(MASTER), encoding="utf-8")
    monkeypatch.setattr(pa, "MASTER_PATH", path)
    return path


@pytest.fixture
def conn(tmp_path, master_path):
    connection = db_module.connect(tmp_path / "answers.db")
    try:
        yield connection
    finally:
        connection.close()


def add(conn, spid, *, company="Northwind Analytics", location="New York, NY",
        source="greenhouse", raw=None, title="Analyst"):
    conn.execute(
        """INSERT INTO postings (source, source_posting_id, company, title, location, url,
               posted_at, raw_json, status, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, 'https://x/' || ?, '2026-10-01', ?, 'active',
                   '2026-10-01', '2026-10-01')""",
        (source, spid, company, title, location, spid, json.dumps(raw or {})),
    )
    conn.commit()
    return conn.execute("SELECT id FROM postings WHERE source_posting_id=?", (spid,)).fetchone()["id"]


def apply_to(conn, posting_id, submitted_at=None):
    conn.execute(
        "INSERT INTO applications (posting_id, submitted_at, channel, created_at, updated_at) "
        "VALUES (?, ?, 'company careers page', '2026-10-01', '2026-10-01')",
        (posting_id, submitted_at),
    )
    conn.commit()


def add_contact(conn, company, name="Sam Example"):
    cursor = conn.execute(
        "INSERT INTO contacts (name, company, created_at, updated_at) "
        "VALUES (?, ?, '2026-10-01', '2026-10-01')",
        (name, company),
    )
    conn.commit()
    return cursor.lastrowid


def add_touch(conn, kind, posting_id=None, contact_id=None):
    conn.execute(
        "INSERT INTO touches (contact_id, posting_id, kind, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'planned', '2026-10-01', '2026-10-01')",
        (contact_id, posting_id, kind),
    )
    conn.commit()


def greenhouse(text):
    return {"content": f"<p>{text}</p>"}


def salary(conn, posting_id):
    answers = pa.posting_answers(conn, posting_id)
    return answers["salary_expectation"], answers["salary_midpoint"]


def test_facts_path_is_the_one_autotailor_reads(monkeypatch):
    from jobhunt import autotailor

    monkeypatch.undo()  # the autouse fixture points both at nothing; compare the real paths

    assert REAL_FACTS_PATH == autotailor.FACTS_PATH
    assert REAL_ANSWERS_PATH.name == "answers.local.md"
    assert REAL_ANSWERS_PATH.parent == REAL_FACTS_PATH.parent


def test_master_path_is_the_one_autotailor_reads(monkeypatch):
    from jobhunt import autotailor

    monkeypatch.undo()

    assert REAL_MASTER_PATH == autotailor.MASTER_PATH


def test_posting_derived_keys_are_semantic_keys():
    # `apply-profile set` accepts a key only if it is a semantic key; the keys
    # posting_answers returns are the ones the classifier can name.
    assert {"work_location", "desired_location", "prior_relationship", "referred",
            "salary_expectation"} <= SEMANTIC_KEYS


def test_missing_posting_gives_nothing(conn):
    assert pa.posting_answers(conn, 999) == {}


def test_reads_never_write(conn):
    pid = add(conn, "a1")
    before = conn.total_changes
    pa.posting_answers(conn, pid)
    assert conn.total_changes == before


# --- work_location and desired_location -------------------------------------------


@pytest.mark.parametrize(
    "location, city",
    [
        ("New York, NY", "New York, NY"),
        ("New York", "New York"),
        ("New York City", "New York City"),
        ("San Francisco", "San Francisco"),
        ("Atlanta, GA", "Atlanta, GA"),
        ("Atlanta, Georgia, United States", "Atlanta, Georgia"),
        ("Chicago, IL (Hybrid)", "Chicago, IL"),
        ("Hybrid - Chicago, IL", "Chicago, IL"),
        ("New York - Hybrid", "New York"),
        ("San Francisco - Mission Bay", "San Francisco"),
        ("Costa Mesa, California, United States", "Costa Mesa, California"),
        ("Seattle, WA", "Seattle, WA"),
        ("St. Louis, MO", "St. Louis, MO"),
        ("Portland, OR", "Portland, OR"),
        # Several places: the best tier wins, then the first listed.
        ("Seattle, WA; New York, NY", "New York, NY"),
        ("San Francisco, CA | New York City, NY | Seattle, WA", "New York City, NY"),
        ("Playa Vista, CA, USA; New York City, NY, USA", "New York City, NY"),
        ("New York or San Francisco ", "New York"),
        ("Boise, ID; Provo, UT", "Boise, ID"),
    ],
)
def test_work_location_is_the_postings_city(conn, location, city):
    answers = pa.posting_answers(conn, add(conn, "loc", location=location))
    assert answers["work_location"] == city


@pytest.mark.parametrize(
    "location",
    [
        "", "Remote", "Remote - USA", "Remote, New York", "ITA - Remote",
        "New York, NY; Remote", "United States", "Canada; USA", "2 Locations",
        "Multiple Locations", "Georgia", "California, United States",
        "Hybrid, , United Kingdom", "USA - OH - Ashland", "NY-LaGuardia",
        "FRA-Paris-Paris", "NEW YORK - VIRTUAL - NY01", "1 Main Street",
    ],
)
def test_no_work_location_for_remote_or_unreadable_locations(conn, location):
    answers = pa.posting_answers(conn, add(conn, "loc", location=location))
    assert "work_location" not in answers
    assert "desired_location" not in answers


def test_a_missing_location_is_unreadable(conn):
    pid = add(conn, "none")
    conn.execute("UPDATE postings SET location = NULL WHERE id = ?", (pid,))
    conn.commit()
    assert "work_location" not in pa.posting_answers(conn, pid)


@pytest.mark.parametrize(
    "location, wanted",
    [
        ("New York, NY", True),      # tier 1
        ("Atlanta, GA", True),       # tier 2
        ("San Francisco, CA", True),  # tier 2
        ("Seattle, WA", False),      # tier 3
        ("Costa Mesa, California", False),
    ],
)
def test_desired_location_only_for_the_first_two_city_tiers(conn, location, wanted):
    answers = pa.posting_answers(conn, add(conn, "tier", location=location))
    assert answers["work_location"]
    assert ("desired_location" in answers) is wanted
    if wanted:
        assert answers["desired_location"] == answers["work_location"]
        assert finai.city_tier(answers["desired_location"]) in (1, 2)


# --- salary --------------------------------------------------------------------------


def lever(min_, max_, interval="per-year-salary", currency="USD"):
    return {"salaryRange": {"currency": currency, "interval": interval, "min": min_, "max": max_}}


def test_lever_salary_range(conn):
    pid = add(conn, "l1", source="lever", raw=lever(120000, 150000))
    assert salary(conn, pid) == ("$120,000 - $150,000", "135000")


@pytest.mark.parametrize(
    "raw",
    [
        lever(55, 70, interval="per-hour-wage"),
        lever(9000, 11000, interval="per-month-salary"),
        lever(100000, 130000, currency="CAD"),
        {"salaryRange": "not a dict"},
        {"salaryRange": {"currency": "USD", "interval": "per-year-salary", "min": 150000, "max": 120000}},
        {},
    ],
)
def test_lever_ranges_that_are_not_annual_usd_base_fall_back(conn, raw):
    pid = add(conn, "l2", source="lever", raw=raw)
    assert salary(conn, pid) == ("$85,000", "85000")


def ashby_tier(title, *components, summary=""):
    return {"title": title, "tierSummary": summary, "components": list(components)}


def component(kind, low, high, interval="1 YEAR", currency="USD"):
    return {"compensationType": kind, "interval": interval, "currencyCode": currency,
            "minValue": low, "maxValue": high}


def test_ashby_salary_ignores_equity_bonus_and_commission(conn):
    raw = {"compensation": {"compensationTiers": [ashby_tier(
        "Standard",
        component("EquityPercentage", 0.1, 0.3),
        component("Bonus", 10000, 20000),
        component("Commission", 5000, 9000),
        component("Salary", 130000, 160000),
    )]}}
    assert salary(conn, add(conn, "a1", source="ashby", raw=raw)) == ("$130,000 - $160,000", "145000")


def test_ashby_tier_titled_with_the_postings_city_wins(conn):
    raw = {"compensation": {"compensationTiers": [
        ashby_tier("San Francisco Bay Area", component("Salary", 150000, 180000)),
        ashby_tier("New York City", component("Salary", 145000, 175000)),
        ashby_tier("Other US", component("Salary", 120000, 150000)),
    ]}}
    pid = add(conn, "a2", source="ashby", raw=raw, location="New York, NY")
    assert salary(conn, pid) == ("$145,000 - $175,000", "160000")


def test_ashby_with_no_tier_for_the_city_takes_the_first(conn):
    raw = {"compensation": {"compensationTiers": [
        ashby_tier("San Francisco Bay Area", component("Salary", 150000, 180000)),
        ashby_tier("New York City", component("Salary", 145000, 175000)),
    ]}}
    pid = add(conn, "a3", source="ashby", raw=raw, location="Seattle, WA")
    assert salary(conn, pid) == ("$150,000 - $180,000", "165000")


def test_ashby_summary_components_when_there_are_no_tiers(conn):
    raw = {"compensation": {"compensationTiers": [], "summaryComponents": [
        component("Salary", 90000, 110000), component("EquityPercentage", 0.1, 0.2),
    ]}}
    assert salary(conn, add(conn, "a4", source="ashby", raw=raw)) == ("$90,000 - $110,000", "100000")


@pytest.mark.parametrize(
    "components",
    [
        [component("EquityPercentage", 0.1, 0.3)],
        [component("Salary", 50, 70, interval="1 HOUR")],
        [component("Salary", 8000, 10000, interval="1 MONTH")],
        [component("Salary", 100000, 120000, currency="EUR")],
        [],
    ],
)
def test_ashby_without_an_annual_usd_salary_falls_back(conn, components):
    raw = {"compensation": {"compensationTiers": [ashby_tier("Standard", *components)]}}
    assert salary(conn, add(conn, "a5", source="ashby", raw=raw)) == ("$85,000", "85000")


def test_ashby_structured_range_is_tried_before_the_text(conn):
    raw = {"descriptionPlain": "The salary range is $200,000 - $220,000.",
           "compensation": {"compensationTiers": [
               ashby_tier("Standard", component("Salary", 100000, 120000))]}}
    assert salary(conn, add(conn, "a6", source="ashby", raw=raw)) == ("$100,000 - $120,000", "110000")


def test_ashby_falls_back_to_the_posting_text(conn):
    raw = {"descriptionPlain": "The base salary range for this role is $110,000 - $140,000.",
           "compensation": {}}
    assert salary(conn, add(conn, "a7", source="ashby", raw=raw)) == ("$110,000 - $140,000", "125000")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("The base salary range for this role is $110,000 - $140,000 plus bonus and equity.",
         ("$110,000 - $140,000", "125000")),
        ("Salary: $120K - $150K per year", ("$120,000 - $150,000", "135000")),
        ("Pay range $120-150K a year.", ("$120,000 - $150,000", "135000")),
        ("The annual salary is $90,000 to $110,000 USD.", ("$90,000 - $110,000", "100000")),
        ("Compensation: $95,000 – $115,000 (en dash)", ("$95,000 - $115,000", "105000")),
        # A bonus or equity figure ahead of the base range is not the base range.
        ("Annual bonus target $15,000 - $25,000. Base salary: $100,000 - $120,000.",
         ("$100,000 - $120,000", "110000")),
        ("Equity and a base salary of $130,000 - $160,000.", ("$130,000 - $160,000", "145000")),
    ],
)
def test_salary_from_the_posting_text(conn, text, expected):
    pid = add(conn, "g1", raw=greenhouse(text))
    assert salary(conn, pid) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "We offer a competitive salary and great benefits.",
        "Hourly pay: $45 - $60 per hour.",
        "The pay is $55 - $70/hr for contractors.",
        "Pay of $8,000 - $9,500 per month.",
        "Total compensation: $200,000 - $250,000 including equity.",
        "OTE: $180,000 - $220,000.",
        "Sign-on bonus: $20,000 - $30,000.",
        "We raised $50,000,000 - $100,000,000 in a Series B; salary is discussed with the team.",
        "The annual base pay for this role is $1,000,000 - $2,000,000.",
        "A single figure of $120,000 is the base salary.",
    ],
)
def test_text_without_an_annual_base_range_falls_back(conn, text):
    pid = add(conn, "g2", raw=greenhouse(text))
    assert salary(conn, pid) == ("$85,000", "85000")


def test_text_with_several_ranges_takes_the_one_labeled_with_the_city(conn):
    text = ("The base salary range for this role is: San Francisco Bay Area: $150,000 - $180,000 "
            "New York City: $145,000 - $175,000 Other US locations: $130,000 - $160,000.")
    ny = add(conn, "g3", raw=greenhouse(text), location="New York, NY")
    sf = add(conn, "g4", raw=greenhouse(text), location="San Francisco, CA")
    other = add(conn, "g5", raw=greenhouse(text), location="Austin, TX")
    assert salary(conn, ny) == ("$145,000 - $175,000", "160000")
    assert salary(conn, sf) == ("$150,000 - $180,000", "165000")
    assert salary(conn, other) == ("$150,000 - $180,000", "165000")  # the first


def test_text_label_does_not_leak_from_the_previous_range(conn):
    # "New York" appears only before the first range; the second range's label
    # starts after the first range ends.
    text = ("New York or remote. Base salary: $100,000 - $120,000. "
            "Berlin: base salary $90,000 - $110,000.")
    pid = add(conn, "g6", raw=greenhouse(text), location="Berlin, Germany")
    assert salary(conn, pid) == ("$90,000 - $110,000", "100000")


@pytest.mark.parametrize(
    "low, high, midpoint",
    [
        (120000, 150000, "135000"),
        (123000, 130000, "125000"),   # 126,500 -> 125,000
        (125000, 150000, "140000"),   # 137,500 -> rounds half up to 140,000
        (100000, 100000, "100000"),
        (118000, 122000, "120000"),
    ],
)
def test_midpoint_rounds_to_the_nearest_5000(conn, low, high, midpoint):
    pid = add(conn, "mid", source="lever", raw=lever(low, high))
    assert pa.posting_answers(conn, pid)["salary_midpoint"] == midpoint


def test_equal_bounds_are_written_as_one_figure(conn):
    pid = add(conn, "eq", source="lever", raw=lever(100000, 100000))
    assert salary(conn, pid) == ("$100,000", "100000")


def test_unreadable_raw_json_falls_back(conn):
    pid = add(conn, "bad")
    conn.execute("UPDATE postings SET raw_json = 'not json' WHERE id = ?", (pid,))
    conn.commit()
    assert salary(conn, pid) == ("$85,000", "85000")


def test_workday_posting_text_is_never_fetched(conn, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("posting_answers went to the network")

    monkeypatch.setattr(finai.workday, "fetch_description", fail)
    monkeypatch.setattr(finai, "jd_text", fail)
    pid = add(conn, "w1", source="workday")
    assert salary(conn, pid) == ("$85,000", "85000")


def test_salary_defaults_are_the_documented_ones():
    assert pa.default_salary() == 85_000  # the template persona's standing salary
    assert pa.SALARY_STEP == 5_000


# --- prior_relationship -------------------------------------------------------------


def test_prior_relationship_no_for_a_new_company(conn):
    assert pa.posting_answers(conn, add(conn, "p1"))["prior_relationship"] == "No"


@pytest.mark.parametrize("company", [
    "Fictional Advisory LLC",
    "Fictional Advisory",
    "Fictional Advisory, Inc.",
    "Sample Ledger Co.",
    "Sample Ledger",
    "FICTIONAL ADVISORY",
    "🔥 Fictional Advisory",
])
def test_prior_relationship_omitted_for_a_company_on_the_resume(conn, company):
    assert "prior_relationship" not in pa.posting_answers(conn, add(conn, "p2", company=company))


def test_prior_relationship_no_after_an_unsubmitted_application_there(conn):
    earlier = add(conn, "p3a", company="Northwind Analytics")
    apply_to(conn, earlier)  # blocked or dry attempt, never submitted: does not count
    later = add(conn, "p3b", company="Northwind Analytics")
    assert pa.posting_answers(conn, later)["prior_relationship"] == "No"


def test_prior_relationship_omitted_for_a_submitted_earlier_application(conn):
    earlier = add(conn, "p4a", company="Northwind Analytics")
    apply_to(conn, earlier, submitted_at="2026-09-01")
    assert "prior_relationship" not in pa.posting_answers(conn, add(conn, "p4b", company="Northwind Analytics"))


def test_prior_relationship_omitted_for_decorated_company_names_with_submitted_applications(conn):
    earlier = add(conn, "p5a", company="Northwind Analytics (nw.example)")
    apply_to(conn, earlier, submitted_at="2026-09-01")
    later = add(conn, "p5b", company="🔥 Northwind Analytics")
    assert "prior_relationship" not in pa.posting_answers(conn, later)


def test_the_postings_own_application_is_not_an_earlier_one(conn):
    pid = add(conn, "p6")
    apply_to(conn, pid, submitted_at="2026-09-01")
    assert pa.posting_answers(conn, pid)["prior_relationship"] == "No"


def test_other_companies_applications_do_not_count(conn):
    apply_to(conn, add(conn, "p7a", company="Contoso Capital"), submitted_at="2026-09-01")
    assert pa.posting_answers(conn, add(conn, "p7b", company="Northwind Analytics"))["prior_relationship"] == "No"


def test_prior_relationship_omitted_when_the_resume_cannot_be_read(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "MASTER_PATH", tmp_path / "missing.json")
    assert "prior_relationship" not in pa.posting_answers(conn, add(conn, "p8"))
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pa, "MASTER_PATH", broken)
    assert "prior_relationship" not in pa.posting_answers(conn, add(conn, "p9"))
    shapeless = tmp_path / "shapeless.json"
    shapeless.write_text(json.dumps({"workExperience": "none"}), encoding="utf-8")
    monkeypatch.setattr(pa, "MASTER_PATH", shapeless)
    assert "prior_relationship" not in pa.posting_answers(conn, add(conn, "p10"))


def test_an_empty_work_history_is_readable(conn, tmp_path, monkeypatch):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"workExperience": []}), encoding="utf-8")
    monkeypatch.setattr(pa, "MASTER_PATH", path)
    assert pa.posting_answers(conn, add(conn, "p11"))["prior_relationship"] == "No"


# --- worked_here, applied_before, resume_skills, prop_trading ------------------------
#
# The facts a yes/no "have you worked here / applied before / do you know X" question is
# answered from (solve._prior_answer, _skills_answer, _prop_trading_answer): the master
# resume is his full work record and the tracker holds only submitted applications.


def _write_master(master_path, **sections):
    master_path.write_text(json.dumps({**MASTER, **sections}), encoding="utf-8")


def _unreadable_masters(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    return [tmp_path / "missing.json", broken]


def test_worked_here_is_no_for_a_company_not_on_the_resume(conn):
    assert pa.posting_answers(conn, add(conn, "w1"))["worked_here"] == "No"


@pytest.mark.parametrize("company", [
    "Fictional Advisory LLC",
    "Fictional Advisory",
    "Sample Ledger, Inc.",
    "FICTIONAL ADVISORY",
    "🔥 Sample Ledger",
])
def test_worked_here_is_yes_for_a_company_on_the_resume(conn, company):
    assert pa.posting_answers(conn, add(conn, "w2", company=company))["worked_here"] == "Yes"


def test_worked_here_ignores_the_tracker_and_other_companies(conn):
    # A submitted application is not a job: only the resume says he worked somewhere.
    earlier = add(conn, "w3a", company="Northwind Analytics")
    apply_to(conn, earlier, submitted_at="2026-10-02")
    assert pa.posting_answers(conn, add(conn, "w3b", company="Northwind Analytics"))["worked_here"] == "No"
    assert pa.posting_answers(conn, add(conn, "w3c", company="Fictional Holdings"))["worked_here"] == "No"


def test_worked_here_is_absent_when_the_resume_cannot_be_read(conn, tmp_path, monkeypatch):
    for path in _unreadable_masters(tmp_path):
        monkeypatch.setattr(pa, "MASTER_PATH", path)
        assert "worked_here" not in pa.posting_answers(conn, add(conn, f"w4{path.stem}"))
    shapeless = tmp_path / "shapeless.json"
    shapeless.write_text(json.dumps({"workExperience": "none"}), encoding="utf-8")
    monkeypatch.setattr(pa, "MASTER_PATH", shapeless)
    assert "worked_here" not in pa.posting_answers(conn, add(conn, "w4c"))


def test_applied_before_is_no_with_no_submitted_application(conn):
    assert pa.posting_answers(conn, add(conn, "a1"))["applied_before"] == "No"


def test_applied_before_is_no_after_an_unsubmitted_attempt(conn):
    earlier = add(conn, "a2a", company="Northwind Analytics")
    apply_to(conn, earlier)  # blocked or dry attempt, never submitted
    assert pa.posting_answers(conn, add(conn, "a2b", company="Northwind Analytics"))["applied_before"] == "No"


@pytest.mark.parametrize("company", ["Northwind Analytics", "Northwind Analytics, Inc.", "🔥 Northwind Analytics"])
def test_applied_before_is_yes_after_a_submitted_application_at_the_company(conn, company):
    earlier = add(conn, "a3a", company="Northwind Analytics")
    apply_to(conn, earlier, submitted_at="2026-10-02")
    assert pa.posting_answers(conn, add(conn, "a3b", company=company))["applied_before"] == "Yes"


def test_applied_before_ignores_other_companies(conn):
    earlier = add(conn, "a4a", company="Northwind Analytics")
    apply_to(conn, earlier, submitted_at="2026-10-02")
    assert pa.posting_answers(conn, add(conn, "a4b", company="Contoso Capital"))["applied_before"] == "No"


def test_applied_before_does_not_need_the_resume(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "MASTER_PATH", tmp_path / "missing.json")
    assert pa.posting_answers(conn, add(conn, "a5"))["applied_before"] == "No"


def test_resume_skills_lists_the_tools_the_resume_names_sorted(conn, master_path):
    _write_master(master_path, additional={"technicalSkills": ["Python", "Excel", "Bloomberg Terminal"]})
    assert pa.posting_answers(conn, add(conn, "s1"))["resume_skills"] == "bloomberg,excel,python"


@pytest.mark.parametrize("tool", ["Postgres", "PostgreSQL", "SQLite", "SQLAlchemy", "MySQL"])
def test_a_sql_database_or_orm_on_the_resume_is_sql(conn, master_path, tool):
    _write_master(master_path, summary=f"Built a mail-drafting agent on {tool}.")
    assert "sql" in pa.posting_answers(conn, add(conn, "s2"))["resume_skills"].split(",")


def test_postgres_on_the_resume_lists_both_postgres_and_sql(conn, master_path):
    _write_master(master_path, summary="Row-level security in Postgres.")
    assert pa.posting_answers(conn, add(conn, "s3"))["resume_skills"] == "postgres,sql"


def test_a_tool_the_resume_does_not_name_is_not_listed(conn, master_path):
    # Pandas and Tableau are no evidence of each other, and a prefix is not the word:
    # "JavaScript" is not Java, "Pythonic" is not Python.
    _write_master(master_path, summary="JavaScript and Pythonic style; charts by hand.")
    skills = pa.posting_answers(conn, add(conn, "s4"))["resume_skills"].split(",")
    assert skills == ["javascript"]


def test_a_resume_that_names_no_tool_gives_an_empty_skill_list(conn):
    assert pa.posting_answers(conn, add(conn, "s5"))["resume_skills"] == ""


def test_resume_skills_are_absent_when_the_resume_cannot_be_read(conn, tmp_path, monkeypatch):
    for path in _unreadable_masters(tmp_path):
        monkeypatch.setattr(pa, "MASTER_PATH", path)
        assert "resume_skills" not in pa.posting_answers(conn, add(conn, f"s6{path.stem}"))


def test_every_listed_skill_is_in_the_vocabulary_the_solver_reads(conn, master_path):
    from jobhunt.apply_assist.solve import SKILL_VOCAB

    _write_master(master_path, summary="Python, SQLAlchemy, Excel, Bloomberg, Jira, Postgres")
    skills = pa.posting_answers(conn, add(conn, "s7"))["resume_skills"].split(",")
    assert set(skills) <= set(SKILL_VOCAB) and "sql" in skills


def test_prop_trading_is_no_when_the_resume_names_none(conn):
    assert pa.posting_answers(conn, add(conn, "t1"))["prop_trading"] == "No"


@pytest.mark.parametrize("phrase", ["proprietary trading", "Prop Trading desk", "prop shop"])
def test_prop_trading_is_absent_when_the_resume_names_it(conn, master_path, phrase):
    _write_master(master_path, summary=f"Interned at a {phrase}.")
    assert "prop_trading" not in pa.posting_answers(conn, add(conn, "t2"))


def test_prop_trading_is_absent_when_the_resume_cannot_be_read(conn, tmp_path, monkeypatch):
    for path in _unreadable_masters(tmp_path):
        monkeypatch.setattr(pa, "MASTER_PATH", path)
        assert "prop_trading" not in pa.posting_answers(conn, add(conn, f"t3{path.stem}"))


FACTS = """# Facts

## Skills

Engineering: Python, SQLite, JavaScript.

Data and business tools (2026-10-27): Pandas, Tableau,
Salesforce, HubSpot.

## Work history

- Employer Co. Analyst. Looker and Snowflake are named here, not under Skills.
"""


def test_the_facts_skills_block_adds_the_tools_it_names(conn, facts_file):
    facts_file.write_text(FACTS, encoding="utf-8")
    skills = pa.posting_answers(conn, add(conn, "f1"))["resume_skills"].split(",")
    assert skills == ["hubspot", "javascript", "pandas", "python", "salesforce", "sql", "tableau"]


def test_a_tool_named_outside_the_facts_skills_block_is_not_listed(conn, facts_file):
    facts_file.write_text(FACTS, encoding="utf-8")
    skills = pa.posting_answers(conn, add(conn, "f2"))["resume_skills"].split(",")
    assert "looker" not in skills and "snowflake" not in skills


def test_the_facts_skills_add_to_the_resumes_not_replace_them(conn, master_path, facts_file):
    _write_master(master_path, summary="Bloomberg and Excel.")
    facts_file.write_text(FACTS, encoding="utf-8")
    skills = pa.posting_answers(conn, add(conn, "f3"))["resume_skills"].split(",")
    assert {"bloomberg", "excel", "pandas", "tableau"} <= set(skills)


def test_a_missing_facts_file_or_skills_block_leaves_the_resumes_skills(conn, master_path, facts_file):
    _write_master(master_path, summary="Python.")
    assert pa.posting_answers(conn, add(conn, "f4"))["resume_skills"] == "python"
    facts_file.write_text("# Facts\n\n## Education\n\nPandas everywhere.\n", encoding="utf-8")
    assert pa.posting_answers(conn, add(conn, "f5"))["resume_skills"] == "python"


def test_the_facts_skills_do_not_make_an_unreadable_resume_readable(conn, tmp_path, monkeypatch, facts_file):
    facts_file.write_text(FACTS, encoding="utf-8")
    monkeypatch.setattr(pa, "MASTER_PATH", tmp_path / "missing.json")
    assert "resume_skills" not in pa.posting_answers(conn, add(conn, "f6"))


ANSWERS_FILE = """# Standing answers
name: Pat Example
degree: B.S. Example, Dec 2026
education_start: August 2024
over_18: Yes
notes: education_start: ignored, the key is the text before the first colon
"""


def test_education_start_and_over_18_come_from_the_answers_file(conn, answers_file):
    answers_file.write_text(ANSWERS_FILE, encoding="utf-8")
    answers = pa.posting_answers(conn, add(conn, "e1"))
    assert answers["education_start"] == "August 2024"
    assert answers["over_18"] == "Yes"
    assert "name" not in answers and "degree" not in answers and "notes" not in answers


def test_the_answers_file_keys_are_absent_when_missing_or_empty(conn, answers_file):
    answers = pa.posting_answers(conn, add(conn, "e2"))  # no file at all
    assert "education_start" not in answers and "over_18" not in answers
    answers_file.write_text("education_start:\nover_18:   \nname: Pat\n", encoding="utf-8")
    answers = pa.posting_answers(conn, add(conn, "e3"))
    assert "education_start" not in answers and "over_18" not in answers


def test_one_answers_file_key_does_not_need_the_other(conn, answers_file):
    answers_file.write_text("over_18: No\n", encoding="utf-8")
    answers = pa.posting_answers(conn, add(conn, "e4"))
    assert answers["over_18"] == "No" and "education_start" not in answers


def test_answers_file_reads_both_keys_and_ignores_every_other_line(answers_file):
    answers_file.write_text(ANSWERS_FILE, encoding="utf-8")
    assert pa._answers_file() == {"education_start": "August 2024", "over_18": "Yes"}


def test_answers_file_trims_the_line_and_keeps_a_colon_inside_the_value(answers_file):
    answers_file.write_text("  education_start :  August 2024: fall term  \nover_18:Yes\n", encoding="utf-8")
    assert pa._answers_file() == {"education_start": "August 2024: fall term", "over_18": "Yes"}


def test_answers_file_omits_an_empty_value_and_a_missing_key(answers_file):
    answers_file.write_text("education_start:   \nname: Pat\nover_18 Yes\n", encoding="utf-8")
    assert pa._answers_file() == {}  # empty value; a name that is no key; a line with no colon


def test_answers_file_other_keys_are_never_returned(answers_file):
    answers_file.write_text("name: Pat\ndegree: B.S.\nover_18: No\n", encoding="utf-8")
    assert pa._answers_file() == {"over_18": "No"}


def test_answers_file_unreadable_path_gives_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "ANSWERS_PATH", tmp_path / "absent.local.md")
    assert pa._answers_file() == {}
    monkeypatch.setattr(pa, "ANSWERS_PATH", tmp_path)  # a directory reads as OSError
    assert pa._answers_file() == {}


def test_an_unknown_posting_still_gives_no_answers_even_with_an_answers_file(conn, answers_file):
    answers_file.write_text(ANSWERS_FILE, encoding="utf-8")
    assert pa.posting_answers(conn, 999) == {}


def test_the_real_answers_file_holds_readable_education_start_and_over_18(monkeypatch):
    if not OPERATOR_ANSWERS_PATH.exists():
        pytest.skip("operator check: set JOBHUNT_CHECK_LOCAL_FILES=1 with data/answers.local.md in place")
    monkeypatch.setattr(pa, "ANSWERS_PATH", OPERATOR_ANSWERS_PATH)
    found = pa._answers_file()
    assert re.fullmatch(r"[A-Z][a-z]+ 20\d\d", found.get("education_start", ""))
    assert found.get("over_18") in ("Yes", "No")


def test_the_real_resume_lists_sql_and_names_no_proprietary_trading(monkeypatch):
    if not OPERATOR_MASTER_PATH.exists():
        pytest.skip("operator check: set JOBHUNT_CHECK_LOCAL_FILES=1 with the master resume in place")
    monkeypatch.setattr(pa, "MASTER_PATH", OPERATOR_MASTER_PATH)
    skills = pa._resume_skills()
    assert skills is not None and "sql" in skills.split(",")
    assert pa._resume_prop_trading() == "No"


# --- referred ----------------------------------------------------------------------


def test_referred_no_when_nobody_is_known_there(conn):
    assert pa.posting_answers(conn, add(conn, "r1"))["referred"] == "No"


def test_referred_omitted_when_a_contact_works_there(conn):
    add_contact(conn, "Northwind Analytics, Inc.")
    assert "referred" not in pa.posting_answers(conn, add(conn, "r2", company="Northwind Analytics"))


def test_a_contact_elsewhere_does_not_count(conn):
    add_contact(conn, "Contoso Capital")
    assert pa.posting_answers(conn, add(conn, "r3"))["referred"] == "No"


def test_referred_omitted_when_a_referral_is_recorded_for_the_posting(conn):
    pid = add(conn, "r4")
    add_touch(conn, "referral", posting_id=pid, contact_id=add_contact(conn, "Contoso Capital"))
    assert "referred" not in pa.posting_answers(conn, pid)


def test_other_touch_kinds_are_not_referrals(conn):
    pid = add(conn, "r5")
    add_touch(conn, "follow_up", posting_id=pid, contact_id=add_contact(conn, "Contoso Capital"))
    assert pa.posting_answers(conn, pid)["referred"] == "No"


def test_a_referral_on_another_posting_is_not_this_ones(conn):
    other = add(conn, "r6a", company="Contoso Capital")
    add_touch(conn, "Referred by a friend", posting_id=other)
    assert pa.posting_answers(conn, add(conn, "r6b"))["referred"] == "No"


def test_a_referral_on_another_posting_at_the_same_company_counts(conn):
    other = add(conn, "r7a", company="NORTHWIND ANALYTICS")
    add_touch(conn, "Referred by a friend", posting_id=other)
    assert "referred" not in pa.posting_answers(conn, add(conn, "r7b"))


# --- company names -------------------------------------------------------------------


@pytest.mark.parametrize("left, right", [
    ("DoorDash", "🔥 DoorDash"),
    ("OneChronos", "*OneChronos"),
    ("Chronograph", "Chronograph (chronograph.pe)"),
    ("Susquehanna International Group", "Susquehanna International Group (SIG)"),
    ("Acme", "Acme, Inc."),
    ("Acme Labs", "ACME LABS LLC"),
    ("J.P. Morgan", "JPMorgan"),
    ("Example Exchange", "Example Exchange Group"),
])
def test_same_company(left, right):
    assert pa.same_company(left, right)
    assert pa.same_company(right, left)


@pytest.mark.parametrize("left, right", [
    ("Acme", "Apex"),
    ("Capital One", "Capital Group"),
    ("Two Sigma", "Sigma Two"),
    ("", "Acme"),
    (None, None),
    ("Acme", ""),
    ("🔥", "*"),
])
def test_not_the_same_company(left, right):
    assert not pa.same_company(left, right)
