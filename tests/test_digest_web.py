"""httpx TestClient tests for the /digest web page (jobhunt/web/app.py).
Every test points app.dependency_overrides[get_digest_dir] at a tmp_path
directory — never the real data/digest.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobhunt.web.app import app, get_digest_dir


@pytest.fixture
def client(tmp_path):
    digest_dir = tmp_path / "digest"
    app.dependency_overrides[get_digest_dir] = lambda: digest_dir
    try:
        yield TestClient(app), digest_dir
    finally:
        app.dependency_overrides.clear()


def test_digest_empty_state(client):
    http, digest_dir = client
    resp = http.get("/digest")
    assert resp.status_code == 200
    assert "No digests yet" in resp.text


def test_digest_renders_latest(client):
    http, digest_dir = client
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / "2026-07-16.md").write_text("# Job Hunt Digest — 2026-07-16\n\nold\n")
    (digest_dir / "2026-07-17.md").write_text("# Job Hunt Digest — 2026-07-17\n\nnewest\n")

    resp = http.get("/digest")
    assert resp.status_code == 200
    assert "newest" in resp.text
    assert "2026-07-17" in resp.text
    assert "2026-07-16" in resp.text  # date picker lists both


def test_digest_by_date(client):
    http, digest_dir = client
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / "2026-07-16.md").write_text("# Job Hunt Digest — 2026-07-16\n\nold content\n")

    resp = http.get("/digest/2026-07-16")
    assert resp.status_code == 200
    assert "old content" in resp.text


def test_digest_bad_date_404(client):
    http, digest_dir = client
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / "2026-07-16.md").write_text("content\n")

    resp = http.get("/digest/bad-date")
    assert resp.status_code == 404
