from __future__ import annotations

from unittest.mock import patch

import pytest
from jobhunt import db as db_module
from jobhunt import expand as expand_module
from jobhunt.net import FetchError

from tests.conftest import MockResponse

README_TEXT = """\
| Company | Role | Location | Application | Date |
| --- | --- | --- | --- | --- |
| **Acme Corp** | SWE Intern | NYC | [Apply](https://boards.greenhouse.io/AcmeCorp/jobs/12345) | 2026-07-01 |
| **Beta Inc** | Data Intern | SF | [Apply](https://jobs.lever.co/betainc/abc-123) | 2026-07-02 |
| **Gamma LLC** | ML Intern | Remote | [Apply](https://jobs.ashbyhq.com/gammallc/xyz-789) | 2026-07-03 |
| **Delta Co** | PM Intern | Austin | [Apply](https://boards.greenhouse.io/embed/job_board?for=deltaco&token=abc) | 2026-07-04 |
| **Zeta Redirect** | SWE Intern | Remote | [Apply](https://simplify.jobs/p/abcdefgh) | 2026-07-05 |
| **Eta Other** | SWE Intern | Remote | [Apply](https://example.com/careers/eta) | 2026-07-06 |
| **Known Co** | SWE Intern | Remote | [Apply](https://boards.greenhouse.io/knownco/jobs/1) | 2026-07-07 |
| **Dead Co** | SWE Intern | Remote | [Apply](https://boards.greenhouse.io/deadco/jobs/1) | 2026-07-08 |
"""


def _connect(tmp_path):
    return db_module.connect(tmp_path / "test.db")


# --- extract_ats_refs -------------------------------------------------------


def test_extract_ats_refs_recognizes_all_platforms_and_ignores_others():
    texts = [
        "greenhouse: https://boards.greenhouse.io/acme/jobs/1",
        "job-boards: https://job-boards.greenhouse.io/acme2/jobs/2",
        "lever: https://jobs.lever.co/betainc/abc-123",
        "ashby: https://jobs.ashbyhq.com/gammallc/xyz-789",
        "embed job_board: https://boards.greenhouse.io/embed/job_board?for=deltaco&token=x",
        "embed job_app: https://boards.greenhouse.io/embed/job_app?for=epsilonco",
        "simplify redirect (ignored): https://simplify.jobs/p/abcdefgh",
        "other host (ignored): https://example.com/careers/eta",
    ]
    refs = expand_module.extract_ats_refs(texts)
    got = {(r["platform"], r["slug"]) for r in refs}
    assert got == {
        ("greenhouse", "acme"),
        ("greenhouse", "acme2"),
        ("lever", "betainc"),
        ("ashby", "gammallc"),
        ("greenhouse", "deltaco"),
        ("greenhouse", "epsilonco"),
    }
    assert all(r["company_hint"] is None for r in refs)


def test_extract_ats_refs_normalizes_case_and_dedupes():
    texts = [
        "https://boards.greenhouse.io/AcmeCorp/jobs/1",
        "https://boards.greenhouse.io/acmecorp/jobs/2",  # same slug, different job id -> dedupe
    ]
    refs = expand_module.extract_ats_refs(texts)
    assert refs == [{"platform": "greenhouse", "slug": "acmecorp", "company_hint": None}]


def test_extract_ats_refs_rejects_bare_embed_and_empty_path():
    texts = [
        "https://boards.greenhouse.io/embed",
        "https://boards.greenhouse.io/",
        "https://jobs.lever.co/",
    ]
    assert expand_module.extract_ats_refs(texts) == []


def test_extract_ats_refs_recognizes_smartrecruiters_and_preserves_case():
    texts = [
        "https://jobs.smartrecruiters.com/Visa/744000133907678-sr-manager",
        "https://careers.smartrecruiters.com/SmartRecruiters/744-a-job",
    ]
    refs = expand_module.extract_ats_refs(texts)
    got = {(r["platform"], r["slug"]) for r in refs}
    assert got == {("smartrecruiters", "Visa"), ("smartrecruiters", "SmartRecruiters")}


def test_extract_ats_refs_recognizes_workable_path_and_subdomain_forms():
    texts = [
        "https://apply.workable.com/typeform/j/AB1234CD/",
        "https://acme.workable.com/j/ZZ999/",
    ]
    refs = expand_module.extract_ats_refs(texts)
    got = {(r["platform"], r["slug"]) for r in refs}
    assert got == {("workable", "typeform"), ("workable", "acme")}


def test_extract_ats_refs_rejects_bare_workable_root_and_www():
    texts = [
        "https://apply.workable.com/",
        "https://www.workable.com/",
    ]
    assert expand_module.extract_ats_refs(texts) == []


def test_extract_ats_refs_recognizes_workday_with_and_without_language_segment():
    texts = [
        # bare site form, no language segment
        "https://adtran.wd3.myworkdayjobs.com/adtran/job/Atlanta-GA/Software-Engineer-I_R005655",
        # language segment before the site name
        "https://nxp.wd3.myworkdayjobs.com/en-US/careers/job/San-Jose/NFC-Engineer_R-10064298",
        "https://globalhr.wd5.myworkdayjobs.com/fr-CA/Private_Posting_No_TMP/job/US-TX-MCKINNEY/x_01853473",
    ]
    refs = expand_module.extract_ats_refs(texts)
    got = {(r["platform"], r["slug"]) for r in refs}
    assert got == {
        ("workday", "adtran:wd3:adtran"),
        ("workday", "nxp:wd3:careers"),
        ("workday", "globalhr:wd5:Private_Posting_No_TMP"),
    }


def test_extract_ats_refs_workday_preserves_tenant_and_site_case():
    texts = ["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/x/y_JR1"]
    refs = expand_module.extract_ats_refs(texts)
    assert refs == [
        {"platform": "workday", "slug": "nvidia:wd5:NVIDIAExternalCareerSite", "company_hint": None}
    ]


def test_extract_ats_refs_rejects_workday_bare_root_with_no_site_segment():
    texts = ["https://nvidia.wd5.myworkdayjobs.com/", "https://nvidia.wd5.myworkdayjobs.com/en-US/"]
    assert expand_module.extract_ats_refs(texts) == []


def test_extract_ats_refs_rejects_non_workday_myworkdayjobs_lookalike_host():
    # instance segment must look like "wdN" — anything else is not recognized
    texts = ["https://acme.notwd.myworkdayjobs.com/site/job/x"]
    assert expand_module.extract_ats_refs(texts) == []


def test_extract_ats_refs_recognizes_eu_greenhouse_hosts():
    texts = [
        "https://boards.eu.greenhouse.io/acmeeu/jobs/1",
        "https://job-boards.eu.greenhouse.io/embed/job_board?for=deltaeu&token=x",
    ]
    refs = expand_module.extract_ats_refs(texts)
    got = {(r["platform"], r["slug"]) for r in refs}
    assert got == {("greenhouse", "acmeeu"), ("greenhouse", "deltaeu")}


# --- extract_embed_signals ---------------------------------------------------


def test_extract_embed_signals_detects_gh_jid_and_gh_src_on_arbitrary_hosts():
    texts = [
        "https://careers.example-corp.com/jobs/123?gh_jid=98765",
        "https://jobs.another.io/roles?ref=x&gh_src=job_board",
        "https://plainsite.com/careers",  # no marker -> ignored
    ]
    signals = expand_module.extract_embed_signals(texts)
    got = {(s["platform"], s["domain_base"]) for s in signals}
    assert got == {("greenhouse", "example-corp"), ("greenhouse", "another")}


def test_extract_embed_signals_ignores_urls_already_classified_as_a_known_ats():
    # gh_src on a real boards.greenhouse.io URL is redundant with the certain ref --
    # it must not also produce a guess group.
    texts = ["https://boards.greenhouse.io/acme/jobs/1?gh_src=job_board"]
    assert expand_module.extract_embed_signals(texts) == []
    refs = expand_module.extract_ats_refs(texts)
    assert refs == [{"platform": "greenhouse", "slug": "acme", "company_hint": None}]


def test_extract_embed_signals_dedupes_same_url():
    texts = ["https://careers.acme.io/jobs?gh_jid=1 and again https://careers.acme.io/jobs?gh_jid=1"]
    assert len(expand_module.extract_embed_signals(texts)) == 1


# --- guess-slug generation ----------------------------------------------------


def test_guess_slugs_multiword_and_punctuation_hint_only():
    slugs = expand_module._guess_slugs("Example Inc.", None)
    assert slugs == ["exampleinc", "example-inc", "example"]


def test_guess_slugs_domain_derived_only():
    slugs = expand_module._guess_slugs(None, "other-domain")
    assert slugs == ["otherdomain", "other-domain", "other"]


def test_guess_slugs_single_word_hint_has_no_redundant_variants():
    assert expand_module._guess_slugs("Acme", None) == ["acme"]


def test_guess_slugs_combines_hint_and_domain_and_caps_at_four():
    slugs = expand_module._guess_slugs("Example Inc.", "other-domain")
    assert slugs == ["exampleinc", "example-inc", "example", "otherdomain"]
    assert len(slugs) == 4


def test_guess_slugs_no_bases_yields_nothing():
    assert expand_module._guess_slugs(None, None) == []
    assert expand_module._guess_slugs("", "") == []


# --- discover ----------------------------------------------------------------


def test_discover_dedupes_against_existing_companies_and_bad_slugs(tmp_path):
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Known Co', 'x', 'greenhouse', 'knownco', 'yes', NULL, 0, '')"
        )
        # dedupe-by-name case: same name as a README row, different platform/slug on file
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Acme Corp', 'x', 'lever', 'somethingelse', 'yes', NULL, 0, '')"
        )
        conn.commit()

        bad_slugs_path = tmp_path / "bad_slugs.md"
        bad_slugs_path.write_text(
            "# Boards that failed the 2026-07-17 sweep\n\n"
            "- greenhouse: Dead Co (slug `deadco`): 404 board not found\n",
            encoding="utf-8",
        )

        candidates = expand_module.discover(conn, README_TEXT, bad_slugs_path=bad_slugs_path)
        keys = {(c["platform"], c["slug"]) for c in candidates}

        assert ("greenhouse", "knownco") not in keys  # matched by (platform, slug)
        assert ("greenhouse", "acmecorp") not in keys  # matched by name, different platform/slug
        assert ("greenhouse", "deadco") not in keys  # matched by bad_slugs.md
        assert ("lever", "betainc") in keys
        assert ("ashby", "gammallc") in keys
        assert ("greenhouse", "deltaco") in keys
    finally:
        conn.close()


# --- probe ---------------------------------------------------------------


def test_probe_greenhouse_live():
    payload = {"jobs": [{"id": 1}]}
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, payload)):
        assert expand_module.probe("greenhouse", "acme") is True


def test_probe_lever_live_empty_board_is_still_live():
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, [])):
        assert expand_module.probe("lever", "betainc") is True


def test_probe_404_is_dead():
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(404)):
        assert expand_module.probe("greenhouse", "deadco") is False


def test_probe_200_wrong_shape_is_dead_not_error():
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, {"error": "no such board"})):
        assert expand_module.probe("ashby", "brokenco") is False


def test_probe_500_raises_fetcherror_not_dead():
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(500)):
        with pytest.raises(FetchError):
            expand_module.probe("greenhouse", "flakyco")


def test_probe_smartrecruiters_live():
    payload = {"offset": 0, "limit": 100, "totalFound": 1, "content": [{"id": "1"}]}
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, payload)):
        assert expand_module.probe("smartrecruiters", "Visa") is True


def test_probe_smartrecruiters_empty_board_is_still_live():
    payload = {"offset": 0, "limit": 100, "totalFound": 0, "content": []}
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, payload)):
        assert expand_module.probe("smartrecruiters", "Empty") is True


def test_probe_smartrecruiters_404_is_dead():
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(404)):
        assert expand_module.probe("smartrecruiters", "badco") is False


def test_probe_workable_live():
    payload = {"name": "Typeform", "jobs": [{"shortcode": "AB1"}]}
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, payload)):
        assert expand_module.probe("workable", "typeform") is True


def test_probe_workable_empty_board_is_still_live():
    payload = {"name": "Empty Co", "jobs": []}
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(200, payload)):
        assert expand_module.probe("workable", "emptyco") is True


def test_probe_workable_404_is_dead():
    with patch("jobhunt.expand.polite_get", return_value=MockResponse(404)):
        assert expand_module.probe("workable", "badco") is False


def test_probe_workday_live_posts_to_jobs_endpoint():
    payload = {"total": 2, "jobPostings": [{"title": "Job", "externalPath": "/job/x"}]}
    with patch("jobhunt.expand.polite_post", return_value=MockResponse(200, payload)) as mock_post:
        assert expand_module.probe("workday", "nvidia:wd5:NVIDIAExternalCareerSite") is True
    called_url = mock_post.call_args[0][0]
    assert called_url == (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    )
    assert mock_post.call_args.kwargs["json_body"]["offset"] == 0


def test_probe_workday_empty_board_is_still_live():
    payload = {"total": 0, "jobPostings": []}
    with patch("jobhunt.expand.polite_post", return_value=MockResponse(200, payload)):
        assert expand_module.probe("workday", "acme:wd1:acme") is True


def test_probe_workday_404_is_dead():
    with patch("jobhunt.expand.polite_post", return_value=MockResponse(404)):
        assert expand_module.probe("workday", "badtenant:wd1:badsite") is False


def test_probe_workday_malformed_slug_is_dead_not_error():
    with patch("jobhunt.expand.polite_post") as mock_post:
        assert expand_module.probe("workday", "not-composite") is False
    mock_post.assert_not_called()


def test_probe_workday_500_raises_fetcherror_not_dead():
    with patch("jobhunt.expand.polite_post", return_value=MockResponse(500)):
        with pytest.raises(FetchError):
            expand_module.probe("workday", "flakyco:wd1:flakyco")


# --- run_expand ----------------------------------------------------------


def test_run_expand_inserts_live_and_records_dead_and_skips_conflicting_name(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        bad_slugs_path.write_text("# header\n\n", encoding="utf-8")

        # 'Delta Co' already exists as a hand-curated row -> ON CONFLICT DO NOTHING
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Delta Co', 'hand-curated', 'lever', 'someother', 'yes', 'curated', 0, '')"
        )
        # 'Acme Corp' already known too -> excluded from discovery entirely (not even probed)
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Acme Corp', 'hand-curated', 'greenhouse', 'acmecorp', 'yes', 'curated', 0, '')"
        )
        conn.commit()

        def fake_polite_get(url, *args, **kwargs):
            if "lever.co/v0/postings/betainc" in url:
                return MockResponse(200, [{"id": "1"}])
            if "ashbyhq.com/posting-api/job-board/gammallc" in url:
                return MockResponse(200, {"jobs": [{"id": "1"}]})
            if "greenhouse.io/v1/boards/deltaco" in url:
                return MockResponse(200, {"jobs": []})
            if "greenhouse.io/v1/boards/deadco" in url:
                return MockResponse(404)
            if "greenhouse.io/v1/boards/knownco" in url:
                return MockResponse(200, {"jobs": [{"id": "1"}]})
            raise AssertionError(f"unexpected probe url: {url}")

        with patch("jobhunt.expand.polite_get", side_effect=fake_polite_get):
            summary = expand_module.run_expand(
                conn, limit=None, dry_run=False, readme_text=README_TEXT, bad_slugs_path=bad_slugs_path
            )

        # deltaco is live (empty board still counts) but its derived name "Delta Co" collides
        # with the pre-existing hand-curated row -> ON CONFLICT(name) DO NOTHING, not counted.
        assert summary["live_inserted"] == 3  # betainc, gammallc, knownco
        assert summary["dead"] == 1  # deadco
        assert summary["errors"] == 0

        rows = {
            r["name"]: (r["platform"], r["slug"], r["category"], r["verified"], r["watchlist"])
            for r in conn.execute("SELECT * FROM companies").fetchall()
        }
        assert rows["Beta Inc"] == ("lever", "betainc", "auto-discovered", "auto-yes", 0)
        assert rows["Gamma LLC"] == ("ashby", "gammallc", "auto-discovered", "auto-yes", 0)
        assert rows["Known Co"] == ("greenhouse", "knownco", "auto-discovered", "auto-yes", 0)
        # the hand-curated "Delta Co" row was never touched by the conflicting auto-candidate
        assert rows["Delta Co"] == ("lever", "someother", "hand-curated", "yes", 0)

        bad_slugs_text = bad_slugs_path.read_text(encoding="utf-8")
        assert "deadco" in bad_slugs_text
        assert "Auto-discovered dead slugs" in bad_slugs_text
    finally:
        conn.close()


def test_run_expand_dry_run_makes_zero_network_calls(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"

        def boom(*args, **kwargs):
            raise AssertionError("dry-run must not call polite_get")

        with patch("jobhunt.expand.polite_get", side_effect=boom):
            summary = expand_module.run_expand(
                conn, limit=None, dry_run=True, readme_text=README_TEXT, bad_slugs_path=bad_slugs_path
            )

        assert summary["probed"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM companies").fetchone()["c"] == 0
        assert not bad_slugs_path.exists()
    finally:
        conn.close()


def test_run_expand_respects_limit(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        calls = []

        def fake_polite_get(url, *args, **kwargs):
            calls.append(url)
            return MockResponse(200, {"jobs": [{"id": "1"}]} if "boards-api" in url else [{"id": "1"}])

        with patch("jobhunt.expand.polite_get", side_effect=fake_polite_get):
            summary = expand_module.run_expand(
                conn, limit=1, dry_run=False, readme_text=README_TEXT, bad_slugs_path=bad_slugs_path
            )

        assert summary["probed"] == 1
        assert len(calls) == 1
    finally:
        conn.close()


# --- run_expand: embed guesses ------------------------------------------------

EMBED_README_TEXT = (
    "| Company | Role | Location | Application | Date |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| **Widget Co** | SWE Intern | Remote | "
    "[Apply](https://careers.widgetplatform.io/jobs/1?gh_jid=111) | 2026-07-10 |\n"
)


def test_run_expand_embed_guess_first_hit_wins_and_short_circuits(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        calls = []

        def fake_probe(platform, slug):
            calls.append((platform, slug))
            return slug == "widget"  # third variant is the only live one

        with patch("jobhunt.expand.probe", side_effect=fake_probe):
            summary = expand_module.run_expand(
                conn, dry_run=False, readme_text=EMBED_README_TEXT, bad_slugs_path=bad_slugs_path
            )

        # "widgetplatform" (the 4th guess) is never probed -- stopped after the hit
        assert calls == [
            ("greenhouse", "widgetco"),
            ("greenhouse", "widget-co"),
            ("greenhouse", "widget"),
        ]
        assert summary["embed_guess_groups"] == 1
        assert summary["embed_guess_confirmed"] == 1
        assert summary["dead"] == 2  # widgetco, widget-co recorded dead

        row = conn.execute("SELECT platform, slug FROM companies WHERE name='Widget Co'").fetchone()
        assert (row["platform"], row["slug"]) == ("greenhouse", "widget")

        bad_slugs_text = bad_slugs_path.read_text(encoding="utf-8")
        assert "widgetco" in bad_slugs_text
        assert "widget-co" in bad_slugs_text
        assert "widgetplatform" not in bad_slugs_text  # never probed -> never confirmed dead
    finally:
        conn.close()


def test_run_expand_dry_run_labels_guessed_vs_parsed(tmp_path, capsys):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        readme = (
            "| Company | Role | Location | Application | Date |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| **Acme Corp** | SWE Intern | NYC | "
            "[Apply](https://boards.greenhouse.io/AcmeCorp/jobs/12345) | 2026-07-01 |\n"
        ) + EMBED_README_TEXT

        with patch("jobhunt.expand.polite_get") as mock_get:
            summary = expand_module.run_expand(
                conn, dry_run=True, readme_text=readme, bad_slugs_path=bad_slugs_path
            )
        mock_get.assert_not_called()

        out = capsys.readouterr().out
        assert "Acme Corp" in out and "parsed from URL" in out
        assert "Widget Co" in out and "guessed from embed URL" in out
        assert summary["embed_guess_groups"] == 1
    finally:
        conn.close()


def test_run_expand_include_embed_guesses_false_is_noop(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        with patch("jobhunt.expand.probe") as mock_probe:
            summary = expand_module.run_expand(
                conn,
                dry_run=False,
                readme_text=EMBED_README_TEXT,
                bad_slugs_path=bad_slugs_path,
                include_embed_guesses=False,
            )
        mock_probe.assert_not_called()
        assert summary["embed_guess_groups"] == 0
        assert summary["embed_guess_confirmed"] == 0
    finally:
        conn.close()


def test_run_expand_embed_guess_group_skipped_when_name_already_a_company(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Widget Co', 'hand-curated', 'lever', 'somethingelse', 'yes', NULL, 0, '')"
        )
        conn.commit()

        with patch("jobhunt.expand.probe") as mock_probe:
            summary = expand_module.run_expand(
                conn, dry_run=False, readme_text=EMBED_README_TEXT, bad_slugs_path=bad_slugs_path
            )
        mock_probe.assert_not_called()
        assert summary["embed_guess_groups"] == 0
    finally:
        conn.close()


def test_run_expand_embed_guess_filters_variant_already_claimed_or_dead(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        bad_slugs_path.write_text(
            "# header\n\n- greenhouse: Other (slug `widgetco`): 404 board not found\n", encoding="utf-8"
        )
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Other Co', 'x', 'greenhouse', 'widget-co', 'yes', NULL, 0, '')"
        )
        conn.commit()

        calls = []

        def fake_probe(platform, slug):
            calls.append(slug)
            return slug == "widget"

        with patch("jobhunt.expand.probe", side_effect=fake_probe):
            summary = expand_module.run_expand(
                conn, dry_run=False, readme_text=EMBED_README_TEXT, bad_slugs_path=bad_slugs_path
            )

        # "widgetco" (bad_slugs.md) and "widget-co" (claimed by Other Co) are filtered out
        # before probing -- first remaining variant "embed" hits immediately.
        assert calls == ["widget"]
        assert summary["embed_guess_confirmed"] == 1
    finally:
        conn.close()


# --- run_expand: watchlist guesses --------------------------------------------

EMPTY_README_TEXT = "| Company | Role | Location | Application | Date |\n| --- | --- | --- | --- | --- |\n"


def test_run_expand_guess_watchlist_default_false_is_strict_noop(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Watch Co', 'x', NULL, NULL, 'no', NULL, 1, '')"
        )
        conn.commit()

        with patch("jobhunt.expand.probe") as mock_probe:
            summary = expand_module.run_expand(
                conn, dry_run=False, readme_text=EMPTY_README_TEXT, bad_slugs_path=bad_slugs_path
            )

        mock_probe.assert_not_called()
        assert summary["watchlist_guess_groups"] == 0
        assert summary["watchlist_guess_confirmed"] == 0
        row = conn.execute(
            "SELECT platform, slug, watchlist FROM companies WHERE name='Watch Co'"
        ).fetchone()
        assert (row["platform"], row["slug"], row["watchlist"]) == (None, None, 1)
    finally:
        conn.close()


def test_run_expand_guess_watchlist_true_promotes_existing_row(tmp_path, capsys):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Watch Co', 'x', NULL, NULL, 'no', NULL, 1, '')"
        )
        conn.commit()

        def fake_probe(platform, slug):
            return slug == "watchco"

        with patch("jobhunt.expand.probe", side_effect=fake_probe):
            summary = expand_module.run_expand(
                conn,
                dry_run=False,
                readme_text=EMPTY_README_TEXT,
                bad_slugs_path=bad_slugs_path,
                guess_watchlist=True,
            )

        assert summary["watchlist_guess_groups"] == 1
        assert summary["watchlist_guess_confirmed"] == 1
        # promoted in place: watchlist cleared, platform/slug filled, verified
        # flipped, pre-existing category left alone (COALESCE only backfills)
        row = conn.execute(
            "SELECT platform, slug, verified, category, watchlist FROM companies WHERE name='Watch Co'"
        ).fetchone()
        assert (row["platform"], row["slug"], row["verified"], row["category"], row["watchlist"]) == (
            "greenhouse",
            "watchco",
            "auto-yes",
            "x",
            0,
        )

        out = capsys.readouterr().out
        assert "PROMOTED Watch Co -> greenhouse:watchco" in out
    finally:
        conn.close()


def test_run_expand_guess_watchlist_never_touches_already_placed_row(tmp_path, capsys):
    """A watchlist=1 row that already has platform+slug set (e.g. starred
    while also placed) must never be overwritten by promotion, even though
    _gather_watchlist_guess_groups still includes any watchlist=1 row."""
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Watch Co', 'x', 'lever', 'oldslug', 'yes', NULL, 1, '')"
        )
        conn.commit()

        def fake_probe(platform, slug):
            return slug == "watchco"

        with patch("jobhunt.expand.probe", side_effect=fake_probe):
            summary = expand_module.run_expand(
                conn,
                dry_run=False,
                readme_text=EMPTY_README_TEXT,
                bad_slugs_path=bad_slugs_path,
                guess_watchlist=True,
            )

        # a guess group is still generated and probed (watchlist=1 is enough)...
        assert summary["watchlist_guess_groups"] == 1
        assert summary["watchlist_guess_confirmed"] == 1
        # ...but the pre-existing platform+slug must survive untouched.
        row = conn.execute(
            "SELECT platform, slug, watchlist FROM companies WHERE name='Watch Co'"
        ).fetchone()
        assert (row["platform"], row["slug"], row["watchlist"]) == ("lever", "oldslug", 1)

        out = capsys.readouterr().out
        assert "PROMOTED" not in out
    finally:
        conn.close()


def test_run_expand_guess_watchlist_only_applies_to_watchlist_or_platformless_rows(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        # a normal, already-placed company (platform set, watchlist=0) must NOT be re-guessed
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Placed Co', 'x', 'lever', 'placedco', 'yes', NULL, 0, '')"
        )
        conn.commit()

        with patch("jobhunt.expand.probe") as mock_probe:
            summary = expand_module.run_expand(
                conn,
                dry_run=False,
                readme_text=EMPTY_README_TEXT,
                bad_slugs_path=bad_slugs_path,
                guess_watchlist=True,
            )

        mock_probe.assert_not_called()
        assert summary["watchlist_guess_groups"] == 0
    finally:
        conn.close()


def test_probe_and_insert_guess_groups_limit_caps_total_probes_not_group_count(tmp_path):
    """`limit` on probe_and_insert_guess_groups must cap the TOTAL number of
    raw probe calls across all groups, not the number of groups. Three
    groups each need 2 probes (max_probes_per_group=2); limit=4 must admit
    exactly the first two groups whole (2 + 2 = 4) and exclude the third
    entirely — group count alone (3) would wrongly fit under a naive
    groups[:limit] slice if limit were interpreted as a group cap of 4."""
    conn = _connect(tmp_path)
    try:
        groups = [
            {"name": "Alpha Co", "platforms": ["ashby"], "slugs": ["alpha1", "alpha2"]},
            {"name": "Bravo Co", "platforms": ["ashby"], "slugs": ["bravo1", "bravo2"]},
            {"name": "Charlie Co", "platforms": ["ashby"], "slugs": ["charlie1", "charlie2"]},
        ]
        calls = []

        def fake_probe(platform, slug):
            calls.append((platform, slug))
            return False

        with patch("jobhunt.expand.probe", side_effect=fake_probe):
            summary = expand_module.probe_and_insert_guess_groups(
                conn,
                groups,
                max_probes_per_group=2,
                limit=4,
                bad_slugs_path=tmp_path / "bad_slugs.md",
            )

        assert summary["probed"] == 4
        probed_slugs = {slug for _platform, slug in calls}
        assert probed_slugs == {"alpha1", "alpha2", "bravo1", "bravo2"}
    finally:
        conn.close()


def test_probe_and_insert_guess_groups_promotes_existing_watchlist_row_on_live_hit(tmp_path):
    """A guess group whose name matches an existing watchlist/platformless
    company row must NOT be dropped as already-known — it's probed, and a
    live hit PROMOTES that row (platform/slug/watchlist=0) instead of
    inserting a duplicate second row for the same name."""
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Watch Co', 'x', NULL, NULL, NULL, NULL, 1, '')"
        )
        conn.commit()

        groups = [{"name": "Watch Co", "platforms": ["ashby", "greenhouse"], "slugs": ["watchco"]}]

        def fake_probe(platform, slug):
            return platform == "greenhouse" and slug == "watchco"

        with patch("jobhunt.expand.probe", side_effect=fake_probe):
            summary = expand_module.probe_and_insert_guess_groups(
                conn, groups, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        assert summary["already_known"] == 0
        assert summary["promoted"] == 1
        assert summary["inserted"] == 0
        rows = conn.execute("SELECT platform, slug, watchlist FROM companies WHERE name='Watch Co'").fetchall()
        assert len(rows) == 1
        assert (rows[0]["platform"], rows[0]["slug"], rows[0]["watchlist"]) == ("greenhouse", "watchco", 0)
    finally:
        conn.close()


def test_probe_and_insert_guess_groups_skips_fully_placed_company_name(tmp_path):
    """A guess group whose name matches a company that already has BOTH
    platform and slug set must be dropped entirely (already_known), not
    probed and not promoted — promotion only applies to watchlist/
    platformless rows."""
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Placed Co', 'x', 'lever', 'placedco', 'yes', NULL, 0, '')"
        )
        conn.commit()

        groups = [{"name": "Placed Co", "platforms": ["ashby"], "slugs": ["placedco"]}]

        with patch("jobhunt.expand.probe") as mock_probe:
            summary = expand_module.probe_and_insert_guess_groups(
                conn, groups, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        mock_probe.assert_not_called()
        assert summary["already_known"] == 1
        assert summary["probed"] == 0
        assert summary["promoted"] == 0
    finally:
        conn.close()


def test_dedupe_and_probe_candidates_promotes_existing_watchlist_row_on_live_hit(tmp_path):
    """Same promotion behavior as probe_and_insert_guess_groups, for the
    certain-slug (CSE) entrypoint: a candidate whose name matches an
    existing watchlist/platformless row promotes that row on a live hit."""
    conn = _connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Watch Co', 'x', NULL, NULL, NULL, NULL, 1, '')"
        )
        conn.commit()

        candidates = [{"platform": "greenhouse", "slug": "watchco", "name": "Watch Co"}]

        with patch("jobhunt.expand.probe", return_value=True):
            summary = expand_module.dedupe_and_probe_candidates(
                conn, candidates, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        assert summary["already_known"] == 0
        assert summary["promoted"] == 1
        assert summary["inserted"] == 0
        rows = conn.execute("SELECT platform, slug, watchlist FROM companies WHERE name='Watch Co'").fetchall()
        assert len(rows) == 1
        assert (rows[0]["platform"], rows[0]["slug"], rows[0]["watchlist"]) == ("greenhouse", "watchco", 0)
    finally:
        conn.close()
