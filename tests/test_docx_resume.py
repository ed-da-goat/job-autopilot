"""Resume .docx renderer tests. Synthetic template only; no personal data."""

import zipfile
from pathlib import Path

import pytest

from jobhunt.resume import docx_resume

HEAD = '<?xml version="1.0"?><w:document xmlns:w="w"><w:body>'
SECT = '<w:sectPr><w:pgSz w:w="12240"/></w:sectPr>'


def _para(*parts):
    return "<w:p>" + "".join(parts) + "</w:p>"


def _t(s):
    return f'<w:r><w:t xml:space="preserve">{s}</w:t></w:r>'


TAB = "<w:r><w:tab/></w:r>"


def _template(tmp_path: Path) -> Path:
    body = (
        _para(_t("SKILLS/INTERESTS"))
        + _para(_t("Tech:"), TAB, _t("Alpha (a, b), Beta, Gamma"))
        + _para(_t("Biz:"), TAB, _t("Delta, Epsilon"))
        + _para(_t("Honors:"), TAB, _t("Badge"))
        + _para(_t("Affiliations:"), TAB, _t("Club One, Club Two"))
        + _para(_t("Filler 12 and 3,000+ things 2024 2025 2026 3.5 555-0100 3"))
    )
    path = tmp_path / "tpl.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/styles.xml", "<styles/>")
        z.writestr("word/document.xml", HEAD + body + SECT + "</w:body></w:document>")
    return path


def _resume(**over):
    data = {
        "personalInfo": {"name": "Pat Doe", "email": "p@x.edu", "phone": "555-0100",
                         "location": "Town, State", "website": "", "linkedin": "in/pat"},
        "summary": "",
        "workExperience": [{"title": "Lead", "company": "Acme Co", "location": "City, ST",
                            "years": "March 2026 - Present",
                            "description": ["Did a thing for 12 people", "Ran 3,000+ checks"]}],
        "education": [{"institution": "Big University, School of Stuff",
                       "degree": "Bachelor of Arts, Concentration: Finance, GPA: 3.5",
                       "years": "May 2026",
                       "description": "Scholar, May 2024 - May 2026; Abroad, May 2025 - August 2025"}],
        "personalProjects": [],
        "additional": {"technicalSkills": [], "awards": []},
    }
    data.update(over)
    return data


def _texts(tmp_path, resume):
    out, texts = docx_resume.render(resume, _template(tmp_path), tmp_path / "o.docx")
    return out, texts


def test_roundtrip_paragraph_texts(tmp_path):
    out, texts = _texts(tmp_path, {"resume_data": _resume()})
    assert texts == [
        "PAT DOE",
        "Town, State | 555-0100 | p@x.edu | in/pat",
        "EDUCATION",
        "BIG UNIVERSITY, School of Stuff\tTown, State",
        "Bachelor of Arts, GPA: 3.5\tMay 2026",
        "Concentration: Finance",
        "Scholar, May 2024 – May 2026",
        "Abroad, May 2025 – August 2025",
        "",
        "EXPERIENCE",
        "ACME CO\tCity, ST",
        "Lead\tMarch 2026 – Present",
        "Did a thing for 12 people",
        "Ran 3,000+ checks",
        "",
        "SKILLS/INTERESTS",
        "Tech:\tAlpha (a, b), Beta, Gamma",
        "Biz:\tDelta, Epsilon",
        "Honors:\tBadge",
        "Affiliations:\tClub One, Club Two",
    ]
    with zipfile.ZipFile(out) as z:
        assert z.read("word/styles.xml") == b"<styles/>"
        assert b"<w:sectPr>" in z.read("word/document.xml")


def test_skill_reorder_within_category(tmp_path):
    r = _resume(additional={"technicalSkills": ["Gamma", "Alpha (a, b)", "Epsilon"], "awards": []})
    _, texts = _texts(tmp_path, r)
    assert "Tech:\tGamma, Alpha (a, b), Beta" in texts
    assert "Biz:\tEpsilon, Delta" in texts


def test_awards_replace_honors(tmp_path):
    r = _resume(additional={"technicalSkills": [], "awards": ["Badge"]})
    _, texts = _texts(tmp_path, r)
    assert "Honors:\tBadge" in texts


def test_unknown_skill_raises(tmp_path):
    r = _resume(additional={"technicalSkills": ["Nonexistent"], "awards": []})
    with pytest.raises(ValueError, match="Nonexistent"):
        _texts(tmp_path, r)


def test_no_hyphen_dates_or_em_dash(tmp_path):
    _, texts = _texts(tmp_path, _resume())
    joined = "\n".join(texts)
    assert " - " not in joined and "—" not in joined and "–" in joined


def test_summary_and_projects_raise(tmp_path):
    with pytest.raises(ValueError, match="summary"):
        _texts(tmp_path, _resume(summary="Hello"))
    with pytest.raises(ValueError, match="personalProjects"):
        _texts(tmp_path, _resume(personalProjects=[{"name": "x"}]))


def test_fact_gate_new_number_and_sold():
    assert docx_resume.fact_gate("ok 12 and 3,000+", "12 3,000+") == []
    assert any("99" in p for p in docx_resume.fact_gate("grew 99%", "grew 12"))
    assert any("sold" in p for p in docx_resume.fact_gate("built and sold it", "built it"))


def test_render_refuses_on_fact_gate(tmp_path):
    r = _resume()
    r["workExperience"][0]["description"] = ["Grew revenue 77%"]
    with pytest.raises(ValueError, match="fact gate"):
        _texts(tmp_path, r)
    assert not (tmp_path / "o.docx").exists()


def test_fact_gate_extra_corpus_allows_facts_only_numbers():
    assert any("250" in p for p in docx_resume.fact_gate("processed 250 invoices", "12 3,000+"))
    assert docx_resume.fact_gate("processed 250 invoices", "12 3,000+", "verified: 250 invoices a week") == []
    # the corpus only widens the number check: banned phrases still fail, other numbers still fail
    assert any("sold" in p for p in docx_resume.fact_gate("sold 250 invoices", "12", "250"))
    assert any("99" in p for p in docx_resume.fact_gate("processed 99 invoices", "12", "250"))
    # no corpus behaves exactly as before
    assert docx_resume.fact_gate("ok 12", "12", "") == docx_resume.fact_gate("ok 12", "12") == []
    assert docx_resume.fact_gate("grew 99%", "12") == docx_resume.fact_gate("grew 99%", "12", "")


def test_render_extra_corpus_allows_facts_only_numbers(tmp_path):
    r = _resume()
    r["workExperience"][0]["description"] = ["Processed 250 invoices per week"]
    with pytest.raises(ValueError, match="250"):
        _texts(tmp_path, r)
    assert not (tmp_path / "o.docx").exists()
    out, texts = docx_resume.render(r, _template(tmp_path), tmp_path / "o2.docx", extra_corpus="250 invoices a week")
    assert out.exists() and any("250 invoices" in t for t in texts)
    # the corpus does not whitelist numbers it does not contain
    r["workExperience"][0]["description"] = ["Processed 251 invoices per week"]
    with pytest.raises(ValueError, match="251"):
        docx_resume.render(r, _template(tmp_path), tmp_path / "o3.docx", extra_corpus="250 invoices a week")
    assert not (tmp_path / "o3.docx").exists()


def test_split_items_respects_parens():
    assert docx_resume.split_items("A (x, y), B, C (z)") == ["A (x, y)", "B", "C (z)"]


def test_pdf_page_count(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"/Type /Pages /Type /Page /Type/Page")
    assert docx_resume.pdf_page_count(f) == 2


def test_docx_to_pdf_falls_back_to_word(tmp_path, monkeypatch):
    word_tmp = tmp_path / "word"
    monkeypatch.setattr(docx_resume, "WORD_TMP", word_tmp)
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"docx")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == docx_resume.SOFFICE:
            raise FileNotFoundError(cmd[0])
        src, dst = Path(cmd[3]), Path(cmd[4])
        assert src.parent == word_tmp and src.read_bytes() == b"docx"
        dst.write_bytes(b"%PDF /Type /Page")

    monkeypatch.setattr(docx_resume.subprocess, "run", fake_run)
    pdf = docx_resume.docx_to_pdf(docx, tmp_path / "out")
    assert calls == [docx_resume.SOFFICE, "osascript"]
    assert pdf == tmp_path / "out" / "r.pdf"
    assert pdf.read_bytes() == b"%PDF /Type /Page"
    assert list(word_tmp.iterdir()) == []  # container copies cleaned up


def test_docx_to_pdf_word_no_output_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_resume, "WORD_TMP", tmp_path / "word")
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"docx")

    def fake_run(cmd, **kw):
        if cmd[0] == docx_resume.SOFFICE:
            raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(docx_resume.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        docx_resume.docx_to_pdf(docx, tmp_path / "out")


# --- the shipped Alex Rivera example ---------------------------------------------------

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_DOCX = REPO / "examples" / "resume.example.docx"
EXAMPLE_MASTER = REPO / "examples" / "master.example.rm.json"
EXAMPLE_YAML = REPO / "examples" / "resume.example.yaml"


def test_example_docx_parses_as_a_template():
    texts = docx_resume._paragraph_texts(docx_resume._read_document_xml(EXAMPLE_DOCX))
    assert "SKILLS/INTERESTS" in texts
    labels = [label for label, _items in docx_resume.template_skills(texts)]
    assert labels == ["Technical", "Business", "Honors", "Interests"]
    assert texts[0] == "ALEX RIVERA"


def test_example_docx_is_well_formed_and_has_the_bullet_lists():
    import xml.dom.minidom

    with zipfile.ZipFile(EXAMPLE_DOCX) as z:
        for name in z.namelist():
            xml.dom.minidom.parseString(z.read(name))
        numbering = z.read("word/numbering.xml").decode()
    assert 'w:numId="21"' in numbering and 'w:numId="26"' in numbering


def test_example_master_renders_into_the_example_template(tmp_path):
    import json

    master = json.loads(EXAMPLE_MASTER.read_text(encoding="utf-8"))
    out, texts = docx_resume.render(master, EXAMPLE_DOCX, tmp_path / "out.docx")
    assert out.is_file()
    assert any("EXAMPLE CO" in t for t in texts)


def test_example_generator_is_byte_stable(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("make_example_resume", REPO / "scripts" / "make_example_resume.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build(tmp_path / "again.docx").read_bytes() == EXAMPLE_DOCX.read_bytes()


def test_example_yaml_loads_and_builds_every_variant():
    from jobhunt.resume import render as render_module

    data = render_module.load_resume(EXAMPLE_YAML)
    ids = render_module.known_bullet_ids(data)
    assert ids == {"exco-1", "exco-2", "slabs-1", "slabs-2"}
    for variant in render_module.available_variants(data):
        context = render_module.build_context(data, variant)
        assert context["experience"]
