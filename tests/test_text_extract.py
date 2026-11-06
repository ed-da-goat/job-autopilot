from __future__ import annotations

import json

from jobhunt.text_extract import extract_text


def make_posting(*, source: str, raw: dict, title: str = "Software Engineer") -> dict:
    return {"source": source, "title": title, "raw_json": json.dumps(raw)}


def test_extract_text_greenhouse_unescapes_double_encoded_html():
    raw = {"content": "&lt;p&gt;Requires &lt;b&gt;Python&lt;/b&gt; and it&#39;s a great job.&lt;/p&gt;"}
    posting = make_posting(source="greenhouse", raw=raw)
    _title, description, has_description = extract_text(posting)
    assert has_description is True
    assert "python" in description
    assert "<" not in description
    assert "it's a great job" in description


def test_extract_text_lever_combines_description_plain_and_lists():
    raw = {
        "descriptionPlain": "We build great software.",
        "lists": [{"text": "Requirements:", "content": "<li>Python</li><li>FastAPI</li>"}],
    }
    posting = make_posting(source="lever", raw=raw)
    _title, description, has_description = extract_text(posting)
    assert has_description is True
    assert "great software" in description
    assert "python" in description
    assert "fastapi" in description


def test_extract_text_ashby_prefers_description_plain_over_html():
    raw = {"descriptionPlain": "Plain text wins.", "descriptionHtml": "<p>HTML fallback</p>"}
    posting = make_posting(source="ashby", raw=raw)
    _title, description, _has = extract_text(posting)
    assert "plain text wins" in description
    assert "html fallback" not in description


def test_extract_text_ashby_falls_back_to_html_when_no_plain():
    raw = {"descriptionHtml": "<p>Only HTML here.</p>"}
    posting = make_posting(source="ashby", raw=raw)
    _title, description, has_description = extract_text(posting)
    assert has_description is True
    assert "only html here" in description


def test_extract_text_hn_uses_comment_text():
    raw = {"comment_text": "Acme Corp | Backend Engineer | Remote | Python and Postgres"}
    posting = make_posting(source="hn", raw=raw)
    _title, description, has_description = extract_text(posting)
    assert has_description is True
    assert "python and postgres" in description


def test_extract_text_simplify_has_no_description():
    raw = {"company": "Acme", "role": "SWE", "location": "NYC", "link": "https://x"}
    posting = make_posting(source="simplify", raw=raw, title="Software Engineer New Grad")
    _title, description, has_description = extract_text(posting)
    assert has_description is False
    assert description == ""


def test_extract_text_lowercases_title():
    posting = make_posting(source="simplify", raw={}, title="Backend Engineer")
    title, _description, _has = extract_text(posting)
    assert title == "backend engineer"
