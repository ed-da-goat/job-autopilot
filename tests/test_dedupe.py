from __future__ import annotations

from jobhunt import db as db_module
from jobhunt import dedupe as dedupe_module
from jobhunt import funnel as funnel_module


def _make_posting(
    conn,
    source: str,
    spid: str,
    company: str = "Acme",
    title: str = "Backend Engineer",
    location: str = "Remote",
    url: str = "https://boards.greenhouse.io/acme/jobs/123",
    status: str = "active",
) -> int:
    conn.execute(
        """INSERT INTO postings
           (source, source_posting_id, company, title, location, url, status,
            first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '', '')""",
        (source, spid, company, title, location, url, status),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM postings WHERE source=? AND source_posting_id=?", (source, spid)
    ).fetchone()["id"]


# --- URL canonicalization -------------------------------------------------


def test_canon_url_strips_tracking_params_only():
    a = dedupe_module._canon_url("https://boards.greenhouse.io/acme/jobs/123?utm_source=simplify&gh_src=abc")
    b = dedupe_module._canon_url("https://boards.greenhouse.io/acme/jobs/123")
    assert a == b == "boards.greenhouse.io/acme/jobs/123"


def test_canon_url_keeps_identifier_query_params_hn_id():
    """HN item URLs differ only by ?id= — that's an identifier, not tracking
    junk, and must NOT be stripped or every HN posting canonicalizes to the
    same URL."""
    a = dedupe_module._canon_url("https://news.ycombinator.com/item?id=44434576")
    b = dedupe_module._canon_url("https://news.ycombinator.com/item?id=44434577")
    assert a != b
    assert a == "news.ycombinator.com/item?id=44434576"
    assert b == "news.ycombinator.com/item?id=44434577"


def test_canon_url_keeps_identifier_query_params_greenhouse_embed():
    """Greenhouse embed URLs (?for=<company>&token=<job>) carry the actual
    job identity in the query string — for=/token= must survive."""
    a = dedupe_module._canon_url("https://boards.greenhouse.io/embed/job_app?for=acme&token=abc123")
    b = dedupe_module._canon_url("https://boards.greenhouse.io/embed/job_app?for=acme&token=xyz789")
    assert a != b


def test_canon_url_strips_tracking_but_keeps_identifier_on_same_posting():
    """A clean URL and its utm/gh_src-tagged twin must still canonicalize
    identically once the junk (but not the id) is removed."""
    clean = dedupe_module._canon_url("https://news.ycombinator.com/item?id=44434576")
    tagged = dedupe_module._canon_url(
        "https://news.ycombinator.com/item?id=44434576&utm_source=digest&gh_src=x"
    )
    assert clean == tagged == "news.ycombinator.com/item?id=44434576"


def test_canon_url_strips_trailing_slash():
    assert dedupe_module._canon_url("https://boards.greenhouse.io/acme/jobs/123/") == (
        dedupe_module._canon_url("https://boards.greenhouse.io/acme/jobs/123")
    )


def test_canon_url_ignores_scheme_and_lowercases_host():
    a = dedupe_module._canon_url("http://Boards.Greenhouse.io/acme/jobs/123")
    b = dedupe_module._canon_url("https://boards.greenhouse.io/acme/jobs/123")
    assert a == b


def test_canon_url_blank_returns_empty_string():
    assert dedupe_module._canon_url("") == ""
    assert dedupe_module._canon_url(None or "") == ""


# --- normalization + triple-key grouping ----------------------------------


def test_norm_casefolds_strips_punctuation_collapses_whitespace():
    assert dedupe_module._norm("Sr.  Software Engineer!!") == dedupe_module._norm("sr software engineer")


def test_triple_key_groups_postings_without_matching_url(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        direct = _make_posting(
            conn, "greenhouse", "gh-1", url="https://boards.greenhouse.io/acme/jobs/1", location="Remote"
        )
        agg = _make_posting(
            conn, "simplify", "sf-1", url="https://simplify.jobs/p/xyz", location="Remote!"
        )
        groups = dedupe_module.find_duplicate_groups(conn)
        assert len(groups) == 1
        ids = {r["id"] for r in groups[0]}
        assert ids == {direct, agg}
    finally:
        conn.close()


def test_overlapping_groups_merge_via_shared_member(tmp_path):
    """A~B share a URL, B~C share a triple key (different URL/company casing) —
    all three must end up in one merged group."""
    conn = db_module.connect(tmp_path / "t.db")
    try:
        a = _make_posting(conn, "greenhouse", "a", url="https://boards.greenhouse.io/acme/jobs/1")
        b = _make_posting(
            conn, "hn", "b", url="https://boards.greenhouse.io/acme/jobs/1?utm_source=hn",
            title="Backend Engineer", company="Acme",
        )
        c = _make_posting(
            conn, "simplify", "c", url="https://simplify.jobs/p/other",
            title="BACKEND ENGINEER!", company="acme", location="remote",
        )
        groups = dedupe_module.find_duplicate_groups(conn)
        assert len(groups) == 1
        assert {r["id"] for r in groups[0]} == {a, b, c}
    finally:
        conn.close()


# --- source-priority winner ------------------------------------------------


def test_direct_ats_beats_aggregator(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        direct = _make_posting(conn, "greenhouse", "gh-1", url="https://x.io/j/1")
        agg = _make_posting(conn, "simplify", "sf-1", url="https://x.io/j/1")
        result = dedupe_module.run_dedupe(conn)
        assert result.groups_found == 1
        assert result.marked_duplicate == 1
        assert conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (direct,)).fetchone()[
            :
        ] == ("active", None)
        row = conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (agg,)).fetchone()
        assert row["status"] == "duplicate"
        assert row["duplicate_of"] == direct
    finally:
        conn.close()


def test_hn_beats_simplify_beats_adzuna(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        hn = _make_posting(conn, "hn", "h1", url="https://x.io/j/2")
        simplify = _make_posting(conn, "simplify", "s1", url="https://x.io/j/2")
        adzuna = _make_posting(conn, "adzuna", "z1", url="https://x.io/j/2")
        dedupe_module.run_dedupe(conn)
        assert conn.execute("SELECT status FROM postings WHERE id=?", (hn,)).fetchone()["status"] == "active"
        for pid in (simplify, adzuna):
            row = conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (pid,)).fetchone()
            assert row["status"] == "duplicate"
            assert row["duplicate_of"] == hn
    finally:
        conn.close()


# --- funnel protection -------------------------------------------------


def test_qualified_posting_never_demoted_even_if_lower_priority_source(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        direct = _make_posting(conn, "greenhouse", "gh-1", url="https://x.io/j/3")
        agg = _make_posting(conn, "simplify", "sf-1", url="https://x.io/j/3")
        funnel_module.move(conn, agg, "qualified")

        result = dedupe_module.run_dedupe(conn)

        agg_row = conn.execute("SELECT status FROM postings WHERE id=?", (agg,)).fetchone()
        assert agg_row["status"] == "active"
        direct_row = conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (direct,)).fetchone()
        assert direct_row["status"] == "duplicate"
        assert direct_row["duplicate_of"] == agg
        assert result.protected_by_funnel == 1
    finally:
        conn.close()


def test_multiple_funnel_protected_postings_all_stay_active(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        gh = _make_posting(conn, "greenhouse", "gh-1", url="https://x.io/j/4")
        hn = _make_posting(conn, "hn", "hn-1", url="https://x.io/j/4")
        simplify = _make_posting(conn, "simplify", "sf-1", url="https://x.io/j/4")
        funnel_module.move(conn, hn, "qualified")
        funnel_module.move(conn, simplify, "applied")

        dedupe_module.run_dedupe(conn)

        assert conn.execute("SELECT status FROM postings WHERE id=?", (hn,)).fetchone()["status"] == "active"
        assert conn.execute("SELECT status FROM postings WHERE id=?", (simplify,)).fetchone()["status"] == "active"
        gh_row = conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (gh,)).fetchone()
        assert gh_row["status"] == "duplicate"
        assert gh_row["duplicate_of"] == hn  # best-priority among the protected set
    finally:
        conn.close()


# --- idempotence + closed postings -----------------------------------------


def test_idempotent_running_twice_is_stable(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        direct = _make_posting(conn, "greenhouse", "gh-1", url="https://x.io/j/5")
        agg = _make_posting(conn, "simplify", "sf-1", url="https://x.io/j/5")

        first = dedupe_module.run_dedupe(conn)
        second = dedupe_module.run_dedupe(conn)

        assert first == second
        assert conn.execute("SELECT status FROM postings WHERE id=?", (direct,)).fetchone()["status"] == "active"
        row = conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (agg,)).fetchone()
        assert row["status"] == "duplicate"
        assert row["duplicate_of"] == direct
    finally:
        conn.close()


def test_closed_postings_are_never_touched(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        closed = _make_posting(conn, "greenhouse", "gh-1", url="https://x.io/j/6", status="closed")
        agg = _make_posting(conn, "simplify", "sf-1", url="https://x.io/j/6")

        result = dedupe_module.run_dedupe(conn)

        assert result.groups_found == 0
        closed_row = conn.execute("SELECT status, duplicate_of FROM postings WHERE id=?", (closed,)).fetchone()
        assert closed_row["status"] == "closed"
        assert closed_row["duplicate_of"] is None
        agg_row = conn.execute("SELECT status FROM postings WHERE id=?", (agg,)).fetchone()
        assert agg_row["status"] == "active"
    finally:
        conn.close()


def test_preview_duplicate_groups_never_writes(tmp_path):
    conn = db_module.connect(tmp_path / "t.db")
    try:
        direct = _make_posting(conn, "greenhouse", "gh-1", url="https://x.io/j/7")
        agg = _make_posting(conn, "simplify", "sf-1", url="https://x.io/j/7")

        result, plan = dedupe_module.preview_duplicate_groups(conn)

        assert result.groups_found == 1
        assert result.marked_duplicate == 1
        assert len(plan) == 1
        winner, losers = plan[0]
        assert winner["id"] == direct
        assert [r["id"] for r in losers] == [agg]
        # Nothing was actually written.
        assert conn.execute("SELECT status FROM postings WHERE id=?", (agg,)).fetchone()["status"] == "active"
    finally:
        conn.close()


# --- migration idempotence --------------------------------------------------


def test_migration_adds_duplicate_of_column_idempotently(tmp_path):
    path = tmp_path / "mig.db"
    conn = db_module.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(postings)")}
    assert "duplicate_of" in columns
    conn.close()

    # Reopening an already-migrated DB must not error or duplicate the column.
    conn2 = db_module.connect(path)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
        columns2 = [row[1] for row in conn2.execute("PRAGMA table_info(postings)")]
        assert columns2.count("duplicate_of") == 1
    finally:
        conn2.close()
