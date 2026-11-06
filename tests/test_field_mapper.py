"""LLM field-mapper (jobhunt/apply_assist/mapper.py): write-through cache,
output-side refusal, and its threading into solve.resolve.

No real OpenRouter calls: every client passed to map_unknown here is a
stub. SPEND_OK stays unset throughout, per repo convention (see
tests/test_openrouter.py / tests/test_resume_tailor.py) — the gate_closed
test relies on that being the default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunt import db as db_module
from jobhunt import spend as spend_module
from jobhunt.apply_assist import queue as queue_module
from jobhunt.apply_assist import solve as solve_module
from jobhunt.apply_assist.keys import classify
from jobhunt.apply_assist.mapper import MapperResult, fingerprint, map_unknown
from jobhunt.apply_assist.schema import FieldSpec
from tests.conftest import load_fixture_json


@pytest.fixture
def conn(tmp_path: Path):
    connection = db_module.connect(tmp_path / "mapper.db")
    yield connection
    connection.close()


def _field(**overrides) -> FieldSpec:
    base = dict(selector="#f", kind="text", label="Some Field", name="", elem_id="")
    base.update(overrides)
    return FieldSpec(**base)


def _fixture_fields() -> list[dict]:
    return load_fixture_json("unrecognized_fields.json")


def _stub_client(mapping: dict[str, tuple[str | None, float]]):
    """Returns a map_unknown(client=...) stub that answers from `mapping`
    (fingerprint -> (semantic_key, confidence)) and counts its own calls."""
    calls = {"n": 0}

    def client(prompt: str, **kwargs) -> dict:
        calls["n"] += 1
        import json

        payload = [
            {"fingerprint": fp, "semantic_key": key, "confidence": conf}
            for fp, (key, conf) in mapping.items()
        ]
        return {"text": json.dumps(payload), "cost_usd": 0.001}

    client.calls = calls
    return client


# --- fixture-driven resolution via overrides -----------------------------


def test_fixture_field_resolves_via_override_when_answer_present() -> None:
    entry = next(e for e in _fixture_fields() if e["expected_key"] == "full_name")
    field = FieldSpec(**entry["field"])
    overrides = {fingerprint(field): "full_name"}

    resolutions = solve_module.resolve([field], {"full_name": "Ada Lovelace"}, None, overrides)

    assert resolutions[0].action == "fill"
    assert resolutions[0].semantic_key == "full_name"
    assert resolutions[0].value == "Ada Lovelace"


def test_fixture_field_classify_hit_is_never_second_guessed_by_override() -> None:
    # Confirm the *other* direction: a field classify() DOES resolve must
    # ignore any override present for its fingerprint.
    field = FieldSpec(
        selector="#email", kind="text", label="Email*", name="", elem_id="", autocomplete="email"
    )
    bogus_overrides = {fingerprint(field): "full_name"}

    resolutions = solve_module.resolve(
        [field], {"email": "ada@example.com"}, None, bogus_overrides
    )

    assert resolutions[0].semantic_key == "email"
    assert resolutions[0].value == "ada@example.com"


def test_overridden_key_with_no_stored_answer_still_asks() -> None:
    entry = next(e for e in _fixture_fields() if e["expected_key"] == "full_name")
    field = FieldSpec(**entry["field"])
    overrides = {fingerprint(field): "full_name"}

    resolutions = solve_module.resolve([field], {}, None, overrides)

    assert resolutions[0].action == "ask"
    assert "no stored answer for 'full_name'" in resolutions[0].reason


def test_override_to_a_yes_no_key_is_refused_though_the_shape_allows_it() -> None:
    # "Do you have a valid visa?" names no key, yet its yes/no shape allows all
    # three yes/no keys. Only classify() settles a yes/no key's polarity, so the
    # mapper's pick is dropped: a stored "Yes" for work_authorization would claim
    # authorization for someone who needs no visa.
    field = _field(label="Do you have a valid visa?")
    assert classify(field) is None
    overrides = {fingerprint(field): "work_authorization"}

    resolutions = solve_module.resolve(
        [field], {"work_authorization": "Yes"}, None, overrides
    )

    assert resolutions[0].action == "ask"
    assert resolutions[0].semantic_key is None
    assert resolutions[0].reason == "unrecognized field"


# --- write-through cache ---------------------------------------------------


def test_immediate_rerun_makes_zero_client_calls(conn) -> None:
    field = _field(label="X Profile", kind="text")
    fp = fingerprint(field)
    client = _stub_client({fp: ("website", 0.9)})
    budget = spend_module.RunBudget(conn, job="test")

    first = map_unknown(conn, [field], budget, client=client)
    assert first.status == "ok"
    assert first.overrides == {fp: "website"}
    assert client.calls["n"] == 1

    second = map_unknown(conn, [field], budget, client=client)
    assert second.status == "all_cached"
    assert second.overrides == {fp: "website"}
    assert client.calls["n"] == 1  # decisive: no second network call


def test_cached_null_short_circuits_second_call(conn) -> None:
    field = _field(label="Please describe your employment history essay", kind="textarea")
    fp = fingerprint(field)
    client = _stub_client({fp: (None, 0.9)})
    budget = spend_module.RunBudget(conn, job="test")

    first = map_unknown(conn, [field], budget, client=client)
    assert first.status == "ok"
    assert first.overrides == {}
    row = conn.execute(
        "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fp,)
    ).fetchone()
    assert row is not None
    assert row["semantic_key"] is None

    second = map_unknown(conn, [field], budget, client=client)
    assert second.status == "all_cached"
    assert second.overrides == {}
    assert client.calls["n"] == 1


# --- output-side refusal ---------------------------------------------------


def test_refuses_sensitive_key_even_if_model_returns_one(conn) -> None:
    field = _field(label="Anything else you'd like to share?", kind="text")
    fp = fingerprint(field)
    client = _stub_client({fp: ("gender", 0.99)})  # adversarial/misbehaving model
    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.overrides == {}
    row = conn.execute(
        "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fp,)
    ).fetchone()
    assert row["semantic_key"] is None  # refused on the cached row, not just the override


def test_refuses_checkbox_kind_regardless_of_model_response(conn) -> None:
    field = _field(label="I agree to the terms", kind="checkbox")
    fp = fingerprint(field)
    client = _stub_client({fp: ("email", 0.9)})
    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.overrides == {}


def test_refuses_file_kind_regardless_of_model_response(conn) -> None:
    field = _field(label="Attach a writing sample", kind="file")
    fp = fingerprint(field)
    client = _stub_client({fp: ("resume", 0.9)})
    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.overrides == {}


# --- system prompt is actually sent, and the response is parsed robustly --


def test_system_prompt_is_sent_to_client(conn) -> None:
    field = _field(label="X Profile", kind="text")
    fp = fingerprint(field)
    seen = {}

    def client(prompt: str, **kwargs) -> dict:
        seen["system"] = kwargs.get("system")
        payload = [{"fingerprint": fp, "semantic_key": "website", "confidence": 0.9}]
        return {"text": json.dumps(payload), "cost_usd": 0.001}

    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.status == "ok"
    assert seen["system"] is not None
    assert "JSON array" in seen["system"]


def test_prose_response_yields_error_status_not_crash(conn) -> None:
    field = _field(label="Some unmapped question")

    def client(prompt: str, **kwargs) -> dict:
        return {"text": "Sure! Here are the mappings...", "cost_usd": 0.0}

    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.status == "error"
    assert result.overrides == {}
    assert result.cost_usd == 0.0
    row = conn.execute(
        "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fingerprint(field),)
    ).fetchone()
    assert row is None
    assert result.detail is not None
    assert "Sure! Here are the mappings" in result.detail


def test_fenced_json_is_parsed(conn) -> None:
    field = _field(label="X Profile", kind="text")
    fp = fingerprint(field)

    def client(prompt: str, **kwargs) -> dict:
        payload = [{"fingerprint": fp, "semantic_key": "website", "confidence": 0.9}]
        fenced = "```json\n" + json.dumps(payload) + "\n```"
        return {"text": fenced, "cost_usd": 0.001}

    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.status == "ok"
    assert result.overrides == {fp: "website"}


def test_empty_response_yields_error_status(conn) -> None:
    field = _field(label="Some unmapped question")

    def client(prompt: str, **kwargs) -> dict:
        return {"text": "  ", "cost_usd": 0.0}

    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget, client=client)

    assert result.status == "error"
    assert result.detail is not None
    assert "empty" in result.detail


# --- non-silent failure statuses -------------------------------------------


def test_gate_closed_status_and_loud_summary_line(conn, monkeypatch) -> None:
    monkeypatch.delenv("SPEND_OK", raising=False)
    field = _field(label="Some unmapped question")
    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field], budget)  # client=None -> real openrouter.complete

    assert result.status == "gate_closed"
    assert result.detail is not None

    summary = queue_module.render_queue_summary([], [result])
    assert f"MAPPER ALERT: status=gate_closed — {result.detail}" in summary


def test_over_cap_status_and_loud_summary_line(conn, monkeypatch) -> None:
    monkeypatch.setenv("JOBHUNT_RUN_CAP_USD", "0.0000001")
    field = _field(label="Some unmapped question")
    budget = spend_module.RunBudget(conn, job="test")
    client = _stub_client({fingerprint(field): ("website", 0.9)})

    result = map_unknown(conn, [field], budget, client=client)

    assert result.status == "over_cap"
    assert client.calls["n"] == 0  # reserve() must fail before any call goes out

    summary = queue_module.render_queue_summary([], [result])
    assert f"MAPPER ALERT: status=over_cap — {result.detail}" in summary


def test_no_budget_makes_no_network_call(conn) -> None:
    field = _field(label="Some unmapped question")
    client = _stub_client({fingerprint(field): ("website", 0.9)})

    result = map_unknown(conn, [field], None, client=client)

    assert result.status == "no_budget"
    assert client.calls["n"] == 0


# --- fingerprint stability --------------------------------------------------


def test_fingerprint_stable_across_differing_numeric_elem_ids() -> None:
    a = FieldSpec(
        selector="#question_12198097007",
        kind="text",
        label="Full Legal Name*",
        elem_id="question_12198097007",
    )
    b = FieldSpec(
        selector="#question_98765432001",
        kind="text",
        label="Full Legal Name*",
        elem_id="question_98765432001",
    )
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_for_different_labels() -> None:
    a = _field(label="Full Legal Name*")
    b = _field(label="Country*")
    assert fingerprint(a) != fingerprint(b)


def test_map_unknown_returns_no_unknown_fields_for_empty_input(conn) -> None:
    budget = spend_module.RunBudget(conn, job="test")
    result = map_unknown(conn, [], budget)
    assert result.status == "no_unknown_fields"
    assert isinstance(result, MapperResult)


# --- country handled deterministically, never reaches the mapper -----------


def test_country_field_resolved_by_classify_not_mapper() -> None:
    entry = next(e for e in _fixture_fields() if e["field"]["label"] == "Country*")
    field = FieldSpec(**entry["field"])

    assert classify(field) == "country"
    assert entry["expected_key"] == "country"


# --- ambiguity guard --------------------------------------------------------


def test_ambiguous_key_in_batch_is_dropped_and_not_cached(conn) -> None:
    # Model assigns the same key ("website") to two different fields in one
    # batch — one stored answer can't be right for both, so neither may be
    # overridden or cached. A third, unambiguous field in the same batch must
    # still resolve and cache normally.
    field_a = _field(label="X Profile", kind="text")
    field_b = _field(label="Google Scholar", kind="text")
    field_c = _field(label="Public Profile Link", kind="text")
    fp_a, fp_b, fp_c = fingerprint(field_a), fingerprint(field_b), fingerprint(field_c)
    client = _stub_client(
        {
            fp_a: ("website", 0.8),
            fp_b: ("website", 0.8),
            fp_c: ("linkedin", 0.95),
        }
    )
    budget = spend_module.RunBudget(conn, job="test")

    result = map_unknown(conn, [field_a, field_b, field_c], budget, client=client)

    assert result.status == "ok"
    assert fp_a not in result.overrides
    assert fp_b not in result.overrides
    assert result.overrides[fp_c] == "linkedin"

    row_a = conn.execute(
        "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fp_a,)
    ).fetchone()
    row_b = conn.execute(
        "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fp_b,)
    ).fetchone()
    row_c = conn.execute(
        "SELECT semantic_key FROM field_key_map WHERE fingerprint = ?", (fp_c,)
    ).fetchone()

    assert row_a is None  # not cached at all — ambiguous only in this batch
    assert row_b is None
    assert row_c is not None
    assert row_c["semantic_key"] == "linkedin"
