from __future__ import annotations

import base64
import json

import pytest

from jobhunt.sources import alerts

from tests.conftest import MockResponse

# --------------------------------------------------------------------------
# Fixtures (inline — LinkedIn/Wellfound job-alert HTML is a moving target, so
# these are hand-built "realistic enough" digest-email snippets rather than
# captured real mail).
# --------------------------------------------------------------------------

LINKEDIN_ALERT_HTML = """
<table>
<tr>
  <td><a href="https://www.linkedin.com/comm/jobs/view/4111111111/?trackingId=abc123&refId=xyz"><img src="logo.png" alt="Acme Corp logo"/></a></td>
  <td>
    <a href="https://www.linkedin.com/comm/jobs/view/4111111111/?trackingId=abc123&refId=xyz">Backend Engineer, New Grad</a>
    <div>Acme Corp &middot; Atlanta, GA</div>
  </td>
</tr>
<tr>
  <td>
    <a href="https://www.linkedin.com/jobs/view/4222222222">Software Engineer</a>
    <div>Widget Inc &middot; Remote</div>
  </td>
</tr>
<tr>
  <td><a href="https://www.linkedin.com/comm/jobs/view/4333333333/?trackingId=zzz">Easy Apply</a></td>
</tr>
</table>
"""

WELLFOUND_ALERT_HTML = """
<table>
<tr>
  <td>
    <a href="https://wellfound.com/jobs/2831577-senior-software-engineer?utm_source=email&utm_campaign=alert">Senior Software Engineer</a>
    <div>Fintech Startup &middot; New York, NY</div>
  </td>
</tr>
<tr>
  <td>
    <a href="https://wellfound.com/l/9XyZ12ab?utm_source=email">Forward Deployed Engineer</a>
    <div>Agentic Labs &middot; Remote</div>
  </td>
</tr>
<tr>
  <td><a href="https://wellfound.com/l/deadlink000">View Job</a></td>
</tr>
</table>
"""


def _b64url_encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _message(message_id: str, html_body: str, sender: str, subject: str = "Your job alert") -> dict:
    return {
        "id": message_id,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Fri, 01 Aug 2026 12:00:00 +0000"},
            ],
            "mimeType": "text/html",
            "body": {"data": _b64url_encode(html_body)},
        },
    }


class FakeGmailSession:
    """Minimal stand-in for AuthorizedSession, keyed off the two Gmail REST
    endpoints alerts.py calls."""

    def __init__(self, list_responses: list[dict], message_payloads: dict[str, dict]):
        self._list_responses = list(list_responses)
        self._message_payloads = message_payloads
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("/messages"):
            return MockResponse(200, self._list_responses.pop(0))
        message_id = url.rsplit("/", 1)[-1]
        payload = self._message_payloads.get(message_id)
        if payload is None:
            return MockResponse(500)
        return MockResponse(200, payload)


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_is_configured_false_when_no_token(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "TOKEN_PATH", tmp_path / "gmail-token.json")
    assert alerts.is_configured() is False


def test_is_configured_true_when_token_file_exists(monkeypatch, tmp_path):
    token_path = tmp_path / "gmail-token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(alerts, "TOKEN_PATH", token_path)
    assert alerts.is_configured() is True


def test_load_credentials_returns_none_when_no_token(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "TOKEN_PATH", tmp_path / "gmail-token.json")
    assert alerts.load_credentials() is None


def test_fetch_raises_helpful_message_when_not_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "TOKEN_PATH", tmp_path / "gmail-token.json")
    monkeypatch.setattr(alerts, "CLIENT_PATH", tmp_path / "gmail-oauth-client.json")
    with pytest.raises(RuntimeError, match="gmail-auth"):
        alerts.fetch()


def test_fetch_raises_when_credentials_fail_to_load(monkeypatch, tmp_path):
    token_path = tmp_path / "gmail-token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(alerts, "TOKEN_PATH", token_path)
    monkeypatch.setattr(alerts, "load_credentials", lambda: None)
    with pytest.raises(RuntimeError, match="failed to load"):
        alerts.fetch()


def test_fetch_never_invokes_run_auth_flow(monkeypatch, tmp_path):
    """fetch() must never trigger the interactive/browser-launching consent
    flow itself — it only loads an already-saved token."""
    monkeypatch.setattr(alerts, "TOKEN_PATH", tmp_path / "gmail-token.json")

    def boom():
        raise AssertionError("run_auth_flow must never be called by fetch()")

    monkeypatch.setattr(alerts, "run_auth_flow", boom)
    with pytest.raises(RuntimeError):
        alerts.fetch()  # not configured -> RuntimeError, never touches run_auth_flow


def test_run_auth_flow_raises_when_client_secret_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "CLIENT_PATH", tmp_path / "gmail-oauth-client.json")
    monkeypatch.setattr(alerts, "TOKEN_PATH", tmp_path / "gmail-token.json")
    with pytest.raises(RuntimeError, match="gmail-oauth-client.json"):
        alerts.run_auth_flow()


def test_run_auth_flow_never_prints_token_material(monkeypatch, tmp_path, capsys):
    client_path = tmp_path / "gmail-oauth-client.json"
    client_path.write_text("{}", encoding="utf-8")
    token_path = tmp_path / "gmail-token.json"
    monkeypatch.setattr(alerts, "CLIENT_PATH", client_path)
    monkeypatch.setattr(alerts, "TOKEN_PATH", token_path)

    secret = "FAKE-REFRESH-TOKEN-FOR-TESTS-0000000000000"

    class FakeCreds:
        def to_json(self):
            return json.dumps({"refresh_token": secret, "token": "ya29.fake-access-token"})

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            assert path == str(client_path)
            return cls()

        def run_local_server(self, port=0):
            return FakeCreds()

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", FakeFlow)

    alerts.run_auth_flow()

    out = capsys.readouterr().out
    assert secret not in out
    assert "token saved to" in out
    assert token_path.read_text(encoding="utf-8") == FakeCreds().to_json()


# --------------------------------------------------------------------------
# LinkedIn parser
# --------------------------------------------------------------------------


def test_parse_linkedin_alert_multiple_jobs_and_skips_malformed_block():
    postings = alerts.parse_linkedin_alert(LINKEDIN_ALERT_HTML)

    # Third block ("Easy Apply" only, no resolvable title/company) is skipped.
    assert len(postings) == 2

    acme = next(p for p in postings if p["source_posting_id"] == "li-4111111111")
    assert acme["title"] == "Backend Engineer, New Grad"
    assert acme["company"] == "Acme Corp"
    assert acme["location"] == "Atlanta, GA"
    assert acme["url"] == "https://www.linkedin.com/jobs/view/4111111111/"

    widget = next(p for p in postings if p["source_posting_id"] == "li-4222222222")
    assert widget["title"] == "Software Engineer"
    assert widget["company"] == "Widget Inc"
    assert widget["location"] == "Remote"
    assert widget["url"] == "https://www.linkedin.com/jobs/view/4222222222/"


def test_parse_linkedin_alert_normalizes_comm_and_plain_paths_to_same_id():
    comm_html = (
        '<tr><td><a href="https://www.linkedin.com/comm/jobs/view/999888777/?trk=xyz">'
        "Data Engineer</a><div>Acme &middot; Remote</div></td></tr>"
    )
    plain_html = (
        '<tr><td><a href="https://www.linkedin.com/jobs/view/999888777">'
        "Data Engineer</a><div>Acme &middot; Remote</div></td></tr>"
    )

    comm_result = alerts.parse_linkedin_alert(comm_html)
    plain_result = alerts.parse_linkedin_alert(plain_html)

    assert len(comm_result) == len(plain_result) == 1
    assert comm_result[0]["source_posting_id"] == plain_result[0]["source_posting_id"] == "li-999888777"
    assert comm_result[0]["url"] == plain_result[0]["url"] == "https://www.linkedin.com/jobs/view/999888777/"


def test_parse_linkedin_alert_empty_html_returns_empty_list():
    assert alerts.parse_linkedin_alert("") == []
    assert alerts.parse_linkedin_alert("<html><body>no jobs here</body></html>") == []


# --------------------------------------------------------------------------
# Wellfound parser
# --------------------------------------------------------------------------


def test_parse_wellfound_alert_multiple_jobs_and_skips_malformed_block():
    postings = alerts.parse_wellfound_alert(WELLFOUND_ALERT_HTML)

    # Third block ("View Job" only, no resolvable title/company) is skipped.
    assert len(postings) == 2

    senior = next(p for p in postings if p["source_posting_id"] == "wf-2831577-senior-software-engineer")
    assert senior["title"] == "Senior Software Engineer"
    assert senior["company"] == "Fintech Startup"
    assert senior["location"] == "New York, NY"
    assert senior["url"] == "https://wellfound.com/jobs/2831577-senior-software-engineer"

    redirect = next(p for p in postings if p["source_posting_id"] == "wf-9XyZ12ab")
    assert redirect["title"] == "Forward Deployed Engineer"
    assert redirect["company"] == "Agentic Labs"
    assert redirect["location"] == "Remote"
    assert redirect["url"] == "https://wellfound.com/l/9XyZ12ab"


def test_parse_wellfound_alert_empty_html_returns_empty_list():
    assert alerts.parse_wellfound_alert("") == []


# --------------------------------------------------------------------------
# fetch() end to end (Gmail REST layer mocked; no live calls)
# --------------------------------------------------------------------------


def _configure_token(monkeypatch, tmp_path) -> None:
    token_path = tmp_path / "gmail-token.json"
    token_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(alerts, "TOKEN_PATH", token_path)
    monkeypatch.setattr(alerts, "load_credentials", lambda: object())


def test_fetch_happy_path_normalizes_and_dedupes_across_messages(monkeypatch, tmp_path):
    _configure_token(monkeypatch, tmp_path)

    messages = {
        "msg1": _message("msg1", LINKEDIN_ALERT_HTML, "LinkedIn Job Alerts <jobs-noreply@linkedin.com>"),
        # Same alert content repeated a day later — must dedupe by source_posting_id.
        "msg2": _message("msg2", LINKEDIN_ALERT_HTML, "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"),
    }
    session = FakeGmailSession(
        list_responses=[{"messages": [{"id": "msg1"}, {"id": "msg2"}]}],
        message_payloads=messages,
    )
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", lambda creds: session)

    postings = alerts.fetch()

    ids = sorted(p["source_posting_id"] for p in postings)
    assert ids == ["li-4111111111", "li-4222222222"]  # 2 unique jobs, not 4

    posting = next(p for p in postings if p["source_posting_id"] == "li-4111111111")
    assert posting["company"] == "Acme Corp"
    assert posting["title"] == "Backend Engineer, New Grad"
    assert posting["location"] == "Atlanta, GA"
    assert posting["url"] == "https://www.linkedin.com/jobs/view/4111111111/"
    assert posting["posted_at"] is not None and posting["posted_at"].startswith("2026-08-01")
    assert posting["raw"]["subject"] == "Your job alert"
    assert posting["raw"]["message_id"] == "msg1"  # first occurrence wins


def test_fetch_handles_wellfound_sender_too(monkeypatch, tmp_path):
    _configure_token(monkeypatch, tmp_path)

    messages = {"msg1": _message("msg1", WELLFOUND_ALERT_HTML, "Wellfound <notifications@wellfound.com>")}
    session = FakeGmailSession(
        list_responses=[{"messages": [{"id": "msg1"}]}],
        message_payloads=messages,
    )
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", lambda creds: session)

    postings = alerts.fetch()
    ids = {p["source_posting_id"] for p in postings}
    assert ids == {"wf-2831577-senior-software-engineer", "wf-9XyZ12ab"}


def test_fetch_counts_message_failures_as_one_aggregate_warning(monkeypatch, tmp_path, capsys):
    _configure_token(monkeypatch, tmp_path)

    session = FakeGmailSession(
        list_responses=[{"messages": [{"id": "bad1"}, {"id": "bad2"}]}],
        message_payloads={},  # both messages.get calls come back HTTP 500
    )
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", lambda creds: session)

    postings = alerts.fetch()

    assert postings == []
    out = capsys.readouterr().out
    assert "WARNING: alerts: skipped 2 unparseable message(s)" in out


def test_fetch_ignores_messages_from_unrecognized_senders(monkeypatch, tmp_path):
    _configure_token(monkeypatch, tmp_path)

    messages = {"msg1": _message("msg1", LINKEDIN_ALERT_HTML, "Random Recruiter <someone@example.com>")}
    session = FakeGmailSession(
        list_responses=[{"messages": [{"id": "msg1"}]}],
        message_payloads=messages,
    )
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", lambda creds: session)

    assert alerts.fetch() == []


# --------------------------------------------------------------------------
# Provider registry (generic-parser providers: indeed, dice, ziprecruiter,
# monster, glassdoor, otta, builtin, workatastartup)
# --------------------------------------------------------------------------


def _generic_fixture_html(jobs: list[tuple[str, str, str, str]]) -> str:
    """Build a 2-job (or N-job) digest-email-shaped fixture: one <tr> per
    job, a link wrapping the title, and a company/location line below it —
    same shape the bespoke LinkedIn/Wellfound fixtures use."""
    rows = []
    for url, title, company, location in jobs:
        rows.append(f'<tr><td><a href="{url}">{title}</a><div>{company} &middot; {location}</div></td></tr>')
    return "<table>" + "".join(rows) + "</table>"


GENERIC_PROVIDER_FIXTURES: dict[str, tuple[str, list[str]]] = {
    "indeed": (
        _generic_fixture_html(
            [
                (
                    "https://www.indeed.com/viewjob?jk=abc123def456&tk=xyz",
                    "Software Engineer",
                    "Acme Corp",
                    "Atlanta, GA",
                ),
                (
                    "https://www.indeed.com/rc/clk?jk=789fed321cba&utm_source=email",
                    "Data Analyst",
                    "Beta LLC",
                    "Remote",
                ),
            ]
        ),
        ["in-abc123def456", "in-789fed321cba"],
    ),
    "dice": (
        _generic_fixture_html(
            [
                (
                    "https://www.dice.com/job-detail/aaaa1111-bbbb-2222-cccc-3333dddd4444",
                    "Backend Engineer",
                    "Gamma Inc",
                    "Remote",
                ),
                (
                    "https://www.dice.com/jobs/detail/bbbb2222-cccc-3333-dddd-4444eeee5555",
                    "Platform Engineer",
                    "Delta Co",
                    "Boston, MA",
                ),
            ]
        ),
        ["dice-aaaa1111-bbbb-2222-cccc-3333dddd4444", "dice-bbbb2222-cccc-3333-dddd-4444eeee5555"],
    ),
    "ziprecruiter": (
        _generic_fixture_html(
            [
                (
                    "https://www.ziprecruiter.com/c/Acme-Corp/Job/Software-Engineer/-in-Atlanta,GA?jid=zr111",
                    "Software Engineer",
                    "Acme Corp",
                    "Atlanta, GA",
                ),
                (
                    "https://www.ziprecruiter.com/jobs/beta-llc-1234567?jid=zr222",
                    "Data Analyst",
                    "Beta LLC",
                    "Remote",
                ),
            ]
        ),
        ["zr-mail-zr111", "zr-mail-zr222"],
    ),
    "monster": (
        _generic_fixture_html(
            [
                (
                    "https://www.monster.com/job-openings/software-engineer-atlanta-ga",
                    "Software Engineer",
                    "Epsilon LLC",
                    "Atlanta, GA",
                ),
                (
                    "https://www.monster.com/job-openings/data-engineer-remote?jobid=mon2x",
                    "Data Engineer",
                    "Zeta Corp",
                    "Remote",
                ),
            ]
        ),
        ["mon-software-engineer-atlanta-ga", "mon-mon2x"],
    ),
    "glassdoor": (
        _generic_fixture_html(
            [
                (
                    "https://www.glassdoor.com/job-listing/software-engineer-acme-JV_gd1.htm?jl=gd1",
                    "Software Engineer",
                    "Eta Corp",
                    "Remote",
                ),
                (
                    "https://www.glassdoor.com/partner/jobListing.htm?jl=gd2&pos=1",
                    "Data Engineer",
                    "Theta LLC",
                    "New York, NY",
                ),
            ]
        ),
        ["gd-gd1", "gd-gd2"],
    ),
    "otta": (
        _generic_fixture_html(
            [
                (
                    "https://otta.com/jobs/software-engineer-iota-abc",
                    "Software Engineer",
                    "Iota Inc",
                    "Remote",
                ),
                (
                    "https://www.welcometothejungle.com/en/companies/kappa/jobs/data-engineer-xyz",
                    "Data Engineer",
                    "Kappa SAS",
                    "Paris",
                ),
            ]
        ),
        ["otta-software-engineer-iota-abc", "otta-data-engineer-xyz"],
    ),
    "builtin": (
        _generic_fixture_html(
            [
                (
                    "https://builtin.com/job/software-engineer-lambda-123",
                    "Software Engineer",
                    "Lambda Corp",
                    "Chicago, IL",
                ),
                (
                    "https://builtinnyc.com/job/data-engineer-mu-456",
                    "Data Engineer",
                    "Mu LLC",
                    "New York, NY",
                ),
            ]
        ),
        ["bi-mail-software-engineer-lambda-123", "bi-mail-data-engineer-mu-456"],
    ),
    "workatastartup": (
        _generic_fixture_html(
            [
                (
                    "https://www.workatastartup.com/jobs/987654",
                    "Software Engineer",
                    "Nu Startup",
                    "Remote",
                ),
                (
                    "https://www.workatastartup.com/companies/xi-labs/jobs/123456",
                    "Data Engineer",
                    "Xi Labs",
                    "San Francisco, CA",
                ),
            ]
        ),
        ["waas-987654", "waas-123456"],
    ),
}


@pytest.mark.parametrize("slug", sorted(GENERIC_PROVIDER_FIXTURES))
def test_generic_provider_parses_two_jobs_with_correct_prefix(slug):
    html_text, expected_ids = GENERIC_PROVIDER_FIXTURES[slug]
    parser = alerts.PROVIDERS[slug]["parser"]

    postings = parser(html_text)

    assert [p["source_posting_id"] for p in postings] == expected_ids
    for posting in postings:
        assert posting["source_posting_id"].startswith(alerts.PROVIDERS[slug]["id_prefix"])
        assert posting["title"]
        assert posting["company"]
        assert posting["url"]


def test_generic_parser_skips_malformed_block():
    html_text = _generic_fixture_html(
        [("https://www.indeed.com/viewjob?jk=goodjob0001", "Software Engineer", "Acme Corp", "Remote")]
    )
    # A block whose only anchor text is boilerplate ("Apply") never resolves
    # a title, so the block is dropped rather than raising.
    html_text += '<tr><td><a href="https://www.indeed.com/viewjob?jk=deadbeef0000">Apply</a></td></tr>'

    postings = alerts.PROVIDERS["indeed"]["parser"](html_text)

    assert [p["source_posting_id"] for p in postings] == ["in-goodjob0001"]


def test_provider_for_sender_matches_domain_suffix_including_subdomains():
    assert alerts._provider_for_sender("LinkedIn Job Alerts <jobs@em.linkedin.com>") == "linkedin"
    assert alerts._provider_for_sender("Dice Alerts <alerts@email.dice.com>") == "dice"
    assert alerts._provider_for_sender("Wellfound <team@hi.wellfound.com>") == "wellfound"
    assert alerts._provider_for_sender("Random Recruiter <someone@example.com>") is None


def test_id_prefix_collision_guard_for_mail_ingested_providers():
    """zr-mail-/bi-mail- must stay distinct from the existing API-source
    prefixes zr-/bi- (jobhunt/sources/ziprecruiter.py uses "zr-{id}"; a
    future builtin.py API source would use "bi-{id}")."""
    assert alerts.PROVIDERS["ziprecruiter"]["id_prefix"] == "zr-mail-"
    assert alerts.PROVIDERS["builtin"]["id_prefix"] == "bi-mail-"

    zr_html = _generic_fixture_html([("https://www.ziprecruiter.com/jobs/acme-1?jid=42", "Engineer", "Acme", "Remote")])
    posting = alerts.PROVIDERS["ziprecruiter"]["parser"](zr_html)[0]
    assert posting["source_posting_id"] == "zr-mail-42"
    assert not posting["source_posting_id"].startswith("zr-4")  # not "zr-42" or any bare zr- id


def test_gmail_query_contains_every_registry_domain_exactly_once():
    for slug, config in alerts.PROVIDERS.items():
        for domain in config["sender_domains"]:
            # Exact-address entries (contain "@") go into the query as-is;
            # plain domains are prefixed with "@".
            needle = domain if "@" in domain else f"@{domain}"
            assert alerts.QUERY.count(needle) == 1, f"{slug}: {needle!r} should appear exactly once in QUERY"


GOOGLE_CAREERS_ALERT_HTML = _generic_fixture_html(
    [
        (
            "https://www.google.com/about/careers/applications/jobs/results/123456789-software-engineer",
            "Software Engineer",
            "Google",
            "Mountain View, CA",
        ),
        (
            "https://careers.google.com/jobs/results/987654321-data-scientist",
            "Data Scientist",
            "Google",
            "New York, NY",
        ),
    ]
)


def test_google_careers_parses_two_jobs_with_trailing_numeric_id():
    postings = alerts.PROVIDERS["googlecareers"]["parser"](GOOGLE_CAREERS_ALERT_HTML)

    assert [p["source_posting_id"] for p in postings] == ["goog-mail-123456789", "goog-mail-987654321"]
    for posting in postings:
        assert posting["title"]
        assert posting["company"] == "Google"


def test_provider_for_sender_exact_address_match_for_google_careers():
    assert alerts._provider_for_sender("Google Careers <careers-noreply@google.com>") == "googlecareers"
    # Case-insensitive.
    assert alerts._provider_for_sender("Google Careers <Careers-NoReply@Google.com>") == "googlecareers"


def test_provider_for_sender_other_google_senders_do_not_match():
    # A bare "google.com" domain entry would suffix-match every Google
    # service email; only the exact careers-noreply address should match.
    assert alerts._provider_for_sender("Google Calendar <calendar-notification@google.com>") is None
    assert alerts._provider_for_sender("Google Drive <drive-shares-noreply@google.com>") is None


def test_fetch_handles_a_generic_registry_provider_end_to_end(monkeypatch, tmp_path):
    _configure_token(monkeypatch, tmp_path)

    html_text, expected_ids = GENERIC_PROVIDER_FIXTURES["indeed"]
    messages = {"msg1": _message("msg1", html_text, "Indeed Alerts <alert@indeed.com>")}
    session = FakeGmailSession(
        list_responses=[{"messages": [{"id": "msg1"}]}],
        message_payloads=messages,
    )
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", lambda creds: session)

    postings = alerts.fetch()

    assert sorted(p["source_posting_id"] for p in postings) == sorted(expected_ids)
    assert all(p["raw"]["provider"] == "indeed" for p in postings)
