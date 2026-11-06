from __future__ import annotations

from unittest.mock import patch

from jobhunt import db as db_module
from jobhunt import discover as discover_module
from jobhunt import expand as expand_module

from tests.conftest import MockResponse


def _connect(tmp_path):
    return db_module.connect(tmp_path / "test.db")


def _isolate_dotenv(monkeypatch, tmp_path, contents: str | None = None) -> None:
    """Point discover.py's .env fallback at an isolated path so tests never
    read (or risk leaking) the real repo-root .env, and clear the real env
    vars so a developer's local environment can't leak in either."""
    monkeypatch.delenv("GOOGLE_CSE_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CSE_ID", raising=False)
    dotenv_path = tmp_path / ".env"
    if contents is not None:
        dotenv_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(discover_module, "DEFAULT_DOTENV_PATH", dotenv_path)


# --- yc_candidates -----------------------------------------------------------

YC_PAYLOAD = [
    {"name": "Hiring Co", "website": "https://hiringco.io", "isHiring": True},
    {"name": "Not Hiring Co", "website": "https://nothiring.io", "isHiring": False},
    {"name": "Unset Co", "website": "https://unset.io"},  # isHiring absent -> excluded
]


def test_yc_candidates_filters_by_real_hiring_field():
    with patch("jobhunt.discover.polite_get", return_value=MockResponse(200, YC_PAYLOAD)):
        groups = discover_module.yc_candidates()
    names = {g["name"] for g in groups}
    assert names == {"Hiring Co"}
    assert groups[0]["platforms"] == ["ashby", "greenhouse", "lever"]
    assert "hiringco" in groups[0]["slugs"]


def test_yc_candidates_raises_fetch_error_on_non_200():
    with patch("jobhunt.discover.polite_get", return_value=MockResponse(500)):
        try:
            discover_module.yc_candidates()
        except Exception as exc:
            assert "HTTP 500" in str(exc)
        else:
            raise AssertionError("expected FetchError")


# --- run_discovery: yc probe order, cap, cross-platform win -----------------


def test_run_discovery_yc_probe_order_cap_and_cross_platform_win(tmp_path):
    """A 3-platform x 4-variant guess group has 12 possible combos; the
    6-probe cap must stop probing there, in strict platform-major order
    (all of ashby's variants before greenhouse's), and a hit on a LATER
    platform (greenhouse, the 6th combo probed) must still win and be the
    one inserted — proving the winner search spans all probed platforms,
    not just the first."""
    conn = _connect(tmp_path)
    try:
        payload = [{"name": "Multi Word Co", "website": "https://multiword.io", "isHiring": True}]
        calls = []

        def fake_probe(platform, slug):
            calls.append((platform, slug))
            return platform == "greenhouse" and slug == "multi-word-co"

        with (
            patch("jobhunt.discover.polite_get", return_value=MockResponse(200, payload)),
            patch("jobhunt.expand.probe", side_effect=fake_probe),
        ):
            results = discover_module.run_discovery(
                conn, source="yc", limit=300, dry_run=False, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        assert calls == [
            ("ashby", "multiwordco"),
            ("ashby", "multi-word-co"),
            ("ashby", "multi"),
            ("ashby", "multiword"),
            ("greenhouse", "multiwordco"),
            ("greenhouse", "multi-word-co"),
        ]
        yc_summary = results["yc"]
        assert yc_summary["probed"] == 6
        assert yc_summary["inserted"] == 1
        row = conn.execute(
            "SELECT platform, slug FROM companies WHERE name='Multi Word Co'"
        ).fetchone()
        assert (row["platform"], row["slug"]) == ("greenhouse", "multi-word-co")
    finally:
        conn.close()


def test_run_discovery_yc_respects_limit_cap(tmp_path):
    """`limit` caps TOTAL raw probe calls, not group count (a single group
    can cost up to YC_MAX_PROBES_PER_COMPANY=6 probes). "First Co" and
    "Second Co" each guess to 3 slug variants x 3 platforms = 9 raw combos,
    capped to 6 by max_probes_per_group. limit=6 is exactly one company's
    full budgeted combo set, so the whole first group is probed and the
    second is excluded entirely — not partially probed."""
    conn = _connect(tmp_path)
    try:
        payload = [
            {"name": "First Co", "website": "https://firstco.io", "isHiring": True},
            {"name": "Second Co", "website": "https://secondco.io", "isHiring": True},
        ]
        calls = []

        def fake_probe(platform, slug):
            calls.append((platform, slug))
            return False

        with (
            patch("jobhunt.discover.polite_get", return_value=MockResponse(200, payload)),
            patch("jobhunt.expand.probe", side_effect=fake_probe),
        ):
            results = discover_module.run_discovery(
                conn, source="yc", limit=6, dry_run=False, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        assert results["yc"]["probed"] == 6
        probed_names = {slug for _platform, slug in calls}
        assert "firstco" in probed_names
        assert not any("secondco" in s for s in probed_names)
    finally:
        conn.close()


def test_run_discovery_yc_dry_run_makes_zero_probes(tmp_path):
    conn = _connect(tmp_path)
    try:
        payload = [{"name": "Preview Co", "website": "https://previewco.io", "isHiring": True}]
        with (
            patch("jobhunt.discover.polite_get", return_value=MockResponse(200, payload)),
            patch("jobhunt.expand.probe") as mock_probe,
        ):
            results = discover_module.run_discovery(
                conn, source="yc", dry_run=True, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        mock_probe.assert_not_called()
        assert results["yc"]["probed"] == 0
        assert results["yc"]["inserted"] == 0
        row = conn.execute("SELECT COUNT(*) c FROM companies").fetchone()
        assert row["c"] == 0
    finally:
        conn.close()


# --- cse: env gating -----------------------------------------------------


def test_cse_is_configured_false_when_neither_env_nor_dotenv_set(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    assert discover_module.is_configured() is False


def test_cse_is_configured_true_from_env(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    monkeypatch.setenv("GOOGLE_CSE_KEY", "env-key-value")
    monkeypatch.setenv("GOOGLE_CSE_ID", "env-cx-value")
    assert discover_module.is_configured() is True


def test_cse_is_configured_true_from_dotenv_fallback(tmp_path, monkeypatch):
    _isolate_dotenv(
        monkeypatch,
        tmp_path,
        contents="# a comment\n\nGOOGLE_CSE_KEY=dotenv-key-value\nGOOGLE_CSE_ID=dotenv-cx-value\n",
    )
    assert discover_module.is_configured() is True


def test_cse_dotenv_fallback_ignores_comments_and_blank_lines(tmp_path, monkeypatch):
    _isolate_dotenv(
        monkeypatch,
        tmp_path,
        contents="\n# GOOGLE_CSE_KEY=commented-out-should-not-count\n\nGOOGLE_CSE_KEY=real-key\nGOOGLE_CSE_ID=real-id\n",
    )
    assert discover_module.is_configured() is True


def test_cse_candidates_raises_when_unconfigured(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    try:
        discover_module.cse_candidates()
    except RuntimeError as exc:
        assert "GOOGLE_CSE_KEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_cse_key_value_never_appears_in_output(tmp_path, monkeypatch, capsys):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    monkeypatch.setenv("GOOGLE_CSE_KEY", "TOTALLY-SECRET-KEY-VALUE")
    monkeypatch.setenv("GOOGLE_CSE_ID", "TOTALLY-SECRET-CX-VALUE")

    with patch("jobhunt.discover.polite_get", return_value=MockResponse(500)):
        try:
            discover_module.cse_candidates(queries=["one query"])
        except Exception:
            pass

    out = capsys.readouterr().out
    assert "TOTALLY-SECRET-KEY-VALUE" not in out
    assert "TOTALLY-SECRET-CX-VALUE" not in out


# --- cse: query construction + URL->extract_ats_refs handoff ----------------


def test_cse_query_construction_site_filter_and_request_count(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    monkeypatch.setenv("GOOGLE_CSE_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")

    seen_calls = []

    def fake_polite_get(url, params=None, **kwargs):
        seen_calls.append(params)
        return MockResponse(200, {"items": []})

    with patch("jobhunt.discover.polite_get", side_effect=fake_polite_get):
        discover_module.cse_candidates()

    assert len(seen_calls) == len(discover_module.CSE_QUERIES) * 2
    assert len(seen_calls) <= 10
    for params in seen_calls:
        assert params["key"] == "k"
        assert params["cx"] == "cx"
        assert "site:boards.greenhouse.io" in params["q"]
        assert "site:jobs.ashbyhq.com" in params["q"]
        assert params["start"] in (1, 11)


def test_cse_candidates_extracts_ats_refs_from_result_links(tmp_path, monkeypatch):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    monkeypatch.setenv("GOOGLE_CSE_KEY", "k")
    monkeypatch.setenv("GOOGLE_CSE_ID", "cx")

    def fake_polite_get(url, params=None, **kwargs):
        return MockResponse(
            200,
            {
                "items": [
                    {"link": "https://boards.greenhouse.io/foundco/jobs/1"},
                    {"link": "https://jobs.lever.co/leverco/abc-123"},
                    {"link": "https://example.com/careers/not-an-ats"},
                ]
            },
        )

    with patch("jobhunt.discover.polite_get", side_effect=fake_polite_get):
        candidates = discover_module.cse_candidates(queries=["one query"])

    got = {(c["platform"], c["slug"]) for c in candidates}
    assert got == {("greenhouse", "foundco"), ("lever", "leverco")}
    assert all(c["name"] for c in candidates)


# --- run_discovery: cse unconfigured falls back to yc ------------------------


def test_run_discovery_cse_unconfigured_falls_back_to_yc(tmp_path, monkeypatch, capsys):
    _isolate_dotenv(monkeypatch, tmp_path, contents=None)
    conn = _connect(tmp_path)
    try:
        with (
            patch("jobhunt.discover.polite_get", return_value=MockResponse(200, [])),
            patch("jobhunt.expand.probe") as mock_probe,
        ):
            results = discover_module.run_discovery(
                conn, source="cse", dry_run=True, bad_slugs_path=tmp_path / "bad_slugs.md"
            )

        mock_probe.assert_not_called()
        out = capsys.readouterr().out
        assert "NOTE" in out
        assert results["yc"] is not None
        assert results["cse"] is None
    finally:
        conn.close()


def test_run_discovery_rejects_unknown_source(tmp_path):
    conn = _connect(tmp_path)
    try:
        try:
            discover_module.run_discovery(conn, source="bogus")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    finally:
        conn.close()


# --- expand.dedupe_and_probe_candidates (used by the cse feeder) ------------


def test_dedupe_and_probe_candidates_skips_known_and_dead(tmp_path):
    conn = _connect(tmp_path)
    try:
        bad_slugs_path = tmp_path / "bad_slugs.md"
        bad_slugs_path.write_text(
            "## Auto-discovered dead slugs (2026-01-01, seed-expand)\n\n"
            "- greenhouse: Dead Co (slug `deadco`): 404 board not found\n",
            encoding="utf-8",
        )
        conn.execute(
            "INSERT INTO companies (name, category, platform, slug, verified, why_fit, watchlist, added_at) "
            "VALUES ('Known Co', 'x', 'lever', 'knownco', 'yes', NULL, 0, '')"
        )
        conn.commit()

        candidates = [
            {"platform": "lever", "slug": "knownco", "name": "Known Co"},
            {"platform": "greenhouse", "slug": "deadco", "name": "Dead Co"},
            {"platform": "greenhouse", "slug": "newco", "name": "New Co"},
        ]

        with patch("jobhunt.expand.probe", return_value=True):
            summary = expand_module.dedupe_and_probe_candidates(
                conn, candidates, bad_slugs_path=bad_slugs_path
            )

        assert summary["already_known"] == 2
        assert summary["probed"] == 1
        assert summary["inserted"] == 1
        row = conn.execute("SELECT platform, slug FROM companies WHERE name='New Co'").fetchone()
        assert (row["platform"], row["slug"]) == ("greenhouse", "newco")
    finally:
        conn.close()
