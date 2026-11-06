"""Dry-run submit guard: no page may submit an application until `lift`.

Why this exists (2026-10-24, Q7): in autoapply mode fill.py clicks
Ashby's Yes/No answer buttons, hidden radios and list options. Those Ashby
answer buttons are plain `<button>` elements with no `type` inside the
`<form>`, so the browser treats them as submit buttons. The candidate allowed the
clicks on one condition: a test must show that this guard stops any
submission first (tests/test_submit_guard.py).

The guard has two layers and one off switch:

  DOM layer      an init script in every frame. A capture-phase `submit`
                 listener on `window` calls preventDefault and
                 stopImmediatePropagation on EVERY submit event, and
                 `HTMLFormElement.prototype.submit` / `requestSubmit` are
                 replaced. A submit whose `event.submitter` carries the
                 answer-click mark (`mark_answer_click`) is expected: it is
                 still stopped, but nothing is recorded. Any other submit,
                 `form.submit()` or `form.requestSubmit()` is recorded.
  Network layer  `page.route` over every request to a Greenhouse, Lever or
                 Ashby host and every `/api/non-user-graphql` URL. It fails
                 closed: any non-GET/HEAD/OPTIONS request there is aborted and
                 recorded unless it is a recognised-harmless upload or
                 telemetry call (see `block_reason`). Requests the guard does
                 not watch go to `route.fallback()`, so later routes are
                 untouched.
  Context layer  a backstop for popups and new tabs: `install` also registers
                 the init script, a binding, a route and a `page` listener on
                 the page's browser context. Those only act on pages that were
                 never installed themselves, so the installed page is not
                 judged twice. Events from such a page are recorded against
                 the newest installed page in the context.
  lift(page)     turns the layers off so submit.py's real click works.

Events are recorded in Python (a WeakKeyDictionary keyed by page) through
`page.expose_binding`, so what was recorded survives navigation.
`submit.py::_evaluate_gate` turns every recorded event into a `blocked`
outcome; `submit_application` calls `lift` only when live and the gate passed.

Import direction: submit.py and fill.py import this module; this module
imports neither of them.
"""

from __future__ import annotations

import json
import re
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "ANSWER_MARK_ATTR",
    "block_reason",
    "blockers",
    "classify_request",
    "clear_answer_mark",
    "click_answer",
    "install",
    "installed",
    "lift",
    "mark_answer_click",
]

# Every entry `blockers` returns starts with this.
_PREFIX = "submit_guard: "

# The attribute `mark_answer_click` sets on the element about to be clicked.
# The DOM layer reads it from `event.submitter`, removes it, and honours it
# only for _MARK_TTL_MS after it was set (the value is Date.now()).
ANSWER_MARK_ATTR = "data-jh-answer-click"
_MARK_TTL_MS = 10_000
_MARK_TIMEOUT_MS = 3_000

# Page -> Python reports (the binding survives navigation).
_BINDING_NAME = "__jhSubmitGuardReport"
# The same report, exposed on the browser context for popups and new tabs
# (a context binding may not share a name with a page binding).
_CTX_BINDING_NAME = "__jhSubmitGuardCtxReport"
# Set on `window` by lift. The listeners read it at event time, so it does
# not matter whether the guard script or the off script runs first in a
# document loaded after lift.
_OFF_FLAG = "__jhSubmitGuardOff"

_MAX_EVENTS = 50  # a page that spams submits cannot grow the list without bound
_MAX_DETAIL = 200

# ---------------------------------------------------------------------------
# Network patterns. Evidence was gathered 2026-10-24 with read-only GETs of
# public posting pages and their JavaScript bundles (no POST, no browser, no
# click). Anything not seen in a page or bundle is marked UNCONFIRMED.
# ---------------------------------------------------------------------------

# Greenhouse. The job-boards app is a Remix app whose submit handler is
# `fetch(submitPath, {method: "POST", headers: {"Content-Type":
# "application/json"}, body: JSON.stringify({job_application, ...})})` (the
# `sa({application, submitPath, ...})` function in its entry.client bundle).
# `submitPath` is a field of the page state in the served HTML:
#   job-boards.greenhouse.io/<board>/jobs/<id>
#       -> "submitPath":"https://boards.greenhouse.io/<board>/jobs/<id>"
#   embedded form (the same board served under /embed/)
#       -> "submitPath":"https://boards.greenhouse.io/embed/<board>/jobs/<id>"
#   job-boards.eu.greenhouse.io/<board>/jobs/<id> (EU region; 1,100+ active
#       postings in our own database)
#       -> "submitPath":"https://boards.eu.greenhouse.io/<board>/jobs/<id>"
# The form element itself is `<form method="get" action="/<board>/jobs/<id>">`
# (or `action="/embed/job_app?for=<board>&token=<id>"`), a no-JavaScript
# fallback; the DOM layer covers a native submit of it.
# Uploads are not matched: the bundle fetches
# `GET <base>/uncacheable_attributes/presigned_fields?fields[]=...` and then
# POSTs the file to the presigned storage URL on a storage host.
# UNCONFIRMED: a POST to the same paths on job-boards.greenhouse.io /
# job-boards.eu.greenhouse.io (listed because the lane may be served from
# them); a POST to the legacy `/embed/job_app` endpoint of the classic embed.
_GREENHOUSE_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)
_GREENHOUSE_SUBMIT_PATH = re.compile(r"^/(?:embed/)?[^/]+/jobs/\d+/?$|^/embed/job_app/?$")
# The Job Board API's application endpoint: POST /v1/boards/<board>/jobs/<id>
# on boards-api.greenhouse.io. The lane's own forms never use it, so any
# non-safe request to it is treated as a submit attempt. (The EU host is
# UNCONFIRMED.)
_GREENHOUSE_API_HOSTS = frozenset({"boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"})
_GREENHOUSE_API_SUBMIT_PATH = re.compile(r"^/v1/boards/[^/]+/jobs/[^/]+/?$")

# Lever. The apply page (https://jobs.lever.co/<company>/<uuid>/apply, its
# og:url) holds `<form id="application-form" enctype="multipart/form-data"
# method="POST">` with no `action`, so the submit POSTs to the page URL: path
# `/<company>/<uuid>/apply` (the page may add a query string such as
# `?lever-source=...`, which is not part of the match). The hidden
# `#btn-submit` (`type="submit"`, `data-qa="btn-submit"`) is clicked once
# hCaptcha passes. The resume parser is `POST /parseResume`
# (parseResume.js: `POST_PATH = '/parseResume'`), which is deliberately NOT
# matched. UNCONFIRMED: jobs.eu.lever.co serves the same app (the host exists
# and answers; the POST path shape there is assumed identical).
_LEVER_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})
_LEVER_SUBMIT_PATH = re.compile(r"^/[^/]+/[0-9a-fA-F-]{36}/apply/?$")

# Ashby. One GraphQL endpoint for the whole app: the Apollo HttpLink in the
# posting app's bundle (assets/index-*.js) builds
# `/api/non-user-graphql?op=${encodeURIComponent(operationName)}`, so the
# operation name is the `op` query parameter; the JSON body also carries
# `operationName` (matched too, in case a request leaves `op` off or lies
# about it). The same bundle defines these submission mutations:
#   ApiSubmitApplicationFormAction, ApiSubmitSingleApplicationFormAction,
#   ApiSubmitMultipleFormsAction, ApiSubmitRegistrationFormAction,
#   ApiSubmitSourcingFormAction, ApiSubmitSurveyFormAction,
#   ApiSubmitTakeHomeFormAction.
# The upload operations are ApiCreateFileUploadHandle (returns a presigned
# upload handle) and ApiSetFormValueToFile (attaches the uploaded file to the
# form value); neither starts with ApiSubmit, so they pass, and Ashby's own
# resume upload keeps working. The path is matched on any host because the
# path is Ashby-specific.
# UNCONFIRMED: which of the seven operations a given posting uses.
_ASHBY_GRAPHQL_PATH = "/api/non-user-graphql"
_ASHBY_SUBMIT_OP = re.compile(r"^ApiSubmit", re.IGNORECASE)

# Fail-closed network layer. Every request that is not GET, HEAD or OPTIONS to
# a Greenhouse, Lever or Ashby host (or to the Ashby GraphQL path on any host)
# is aborted unless it is on this allowlist:
#   Ashby      a GraphQL call with a readable JSON body and a named operation
#              that does not start with ApiSubmit. That is the upload pair
#              (ApiCreateFileUploadHandle, ApiSetFormValueToFile) and the
#              posting page's own queries (ApiFormRender and the like, which
#              the app POSTs). A missing or unreadable operation name, a
#              multipart body or an ApiSubmit* name is aborted.
#   Lever      POST /parseResume
#   Greenhouse .../uncacheable_attributes/presigned_fields; the presigned
#              upload POST goes to a storage host that is not an ATS host and
#              so is not watched
#   Greenhouse c.spl.greenhouse.io/com.snowplowanalytics.snowplow/<name> (the
#              Snowplow analytics beacon, tp2 in practice; it carries page
#              events and no form; seen in dry run 6)
#   all three  /seek/send_signal (the confirmation telemetry)
# A request aborted here that is not a recognised submit (`classify_request`)
# is an UNKNOWN abort: `is_unknown_abort` tells the two kinds of line apart.
# Nothing was sent either way, but only a recognised submit, or a catch by the
# DOM layer, says the form tried to submit. A request to a known submit path
# shape under another method (PUT, PATCH, ...), a Job Board API application
# POST and a multipart or unnamed Ashby GraphQL call all read as submit
# attempts, not as unknown aborts.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_GREENHOUSE_ALLOWED_PATH = re.compile(r"/uncacheable_attributes/presigned_fields/?$")
_GREENHOUSE_BEACON_HOST = "c.spl.greenhouse.io"
_GREENHOUSE_BEACON_PATH = re.compile(r"^/com\.snowplowanalytics\.snowplow/[A-Za-z0-9_-]+/?$")
# What only the network layer's catch-all emits (see block_reason): the whole line, not a
# fragment. `_ingest` keeps a page's own report from starting with "aborted ", so a page
# script cannot write one.
#
# The closing parenthesis is optional: `_clean` cuts every recorded line at _MAX_DETAIL
# characters, and a long path (a Cloudflare challenge URL runs past 200) loses its ")". Dry
# run 7 read that cut line as a stop instead of an unknown abort. The body still may not hold a
# space or a ")", so a line with anything after the parenthesis does not match.
_UNKNOWN_ABORT = re.compile(
    r"^aborted (?:greenhouse|lever|ashby) [A-Z]+ (?:not on the allowlist|bot challenge \(Cloudflare\))"
    r" \([^\s)]*\)?$"
)
# Cloudflare's challenge scripts post to this prefix on the ATS host itself. An interactive
# challenge is a bot check: the posting is blocked and never allowlisted (we do not solve or
# evade bot checks). The one exception is the passive JavaScript Detections beacon below.
_CHALLENGE_PREFIX = "/cdn-cgi/challenge-platform/"
# Cloudflare's passive "JavaScript Detections" beacon: every page load on a Bot-Management site
# posts it. Letting the site's own script report, as any browser does, neither solves nor evades
# a check, so this exact prefix (anchored) is allowed. Every other challenge-platform path
# (interactive challenge, Turnstile, /flow/, /cv/, orchestrate) stays a bot challenge.
# After the prefix only the beacon's own token characters are allowed; no "%" (an encoded dot or
# slash) and no ".." (a path climb), so nothing else rides on the allowed prefix.
_CHALLENGE_PASSIVE_PATH = re.compile(
    r"^/cdn-cgi/challenge-platform/h/[a-z]/jsd/oneshot/(?!.*\.\.)[A-Za-z0-9/_.:-]*$"
)
_PAGE_REPORT_LEAD = "page reported: "
_LEVER_ALLOWED_PATH = "/parseResume"
_SIGNAL_PATH = "/seek/send_signal"
_ATS_DOMAINS = {
    "greenhouse": "greenhouse.io",
    "lever": "lever.co",
    "ashby": "ashbyhq.com",
}


def _ats_family(host: str) -> str | None:
    """"greenhouse", "lever" or "ashby" when `host` is that ATS's domain or a
    subdomain of it, else None."""
    host = (host or "").lower().rstrip(".")
    for family, domain in _ATS_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return family
    return None


def classify_request(method: str, url: str, post_data: str | None = None) -> str | None:
    """Describe `method url` if it is a recognised application submit, else
    None. (The network layer is wider than this: see `block_reason`.)

    Pure (no browser), so every URL shape is unit-testable. The description
    carries the host and path only, never the query string.
    """
    if (method or "").upper() != "POST":
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    path = parts.path or "/"
    where = f"{host}{path}"
    if host in _GREENHOUSE_HOSTS and _GREENHOUSE_SUBMIT_PATH.match(path):
        return f"aborted greenhouse application POST ({where})"
    if host in _LEVER_HOSTS and _LEVER_SUBMIT_PATH.match(path):
        return f"aborted lever application POST ({where})"
    if path == _ASHBY_GRAPHQL_PATH:
        op = _ashby_submit_op(parts.query, post_data)
        if op:
            return f"aborted ashby submit operation {op} ({host})"
    return None


def _ashby_op_names(query: str, post_data: str | None) -> tuple[list[str], bool]:
    """Operation names named by the query and the body, and whether the body
    was readable (a JSON object, or a list of JSON objects, whose
    `operationName` values are strings when present)."""
    names: list[str] = [n for n in parse_qs(query).get("op", []) if isinstance(n, str)]
    readable = False
    if post_data:
        try:
            body = json.loads(post_data)
        except ValueError:
            body = None
        items = body if isinstance(body, list) else [body]
        readable = bool(items) and all(isinstance(item, dict) for item in items)
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("operationName")
            if isinstance(name, str):
                names.append(name)
            elif name is not None:
                readable = False
    return names, readable


def _ashby_submit_op(query: str, post_data: str | None) -> str | None:
    """The first Ashby submit operation named by the query or the body."""
    for name in _ashby_op_names(query, post_data)[0]:
        if _ASHBY_SUBMIT_OP.match(name):
            return name
    return None


def _ashby_graphql_block(host: str, query: str, post_data: str | None, content_type: str | None) -> str | None:
    names, readable = _ashby_op_names(query, post_data)
    for name in names:
        if _ASHBY_SUBMIT_OP.match(name):
            return f"aborted ashby submit operation {name} ({host})"
    if "multipart" in (content_type or "").lower():
        return f"aborted ashby multipart graphql call ({host})"
    if not names or not readable:
        return f"aborted ashby graphql call without a readable operation name ({host})"
    return None


def _submit_shape(host: str, path: str) -> bool:
    """True when `host` and `path` have the shape of an application submit
    endpoint, whatever the method."""
    return bool(
        (host in _GREENHOUSE_HOSTS and _GREENHOUSE_SUBMIT_PATH.match(path))
        or (host in _GREENHOUSE_API_HOSTS and _GREENHOUSE_API_SUBMIT_PATH.match(path))
        or (host in _LEVER_HOSTS and _LEVER_SUBMIT_PATH.match(path))
    )


def block_reason(method: str, url: str, post_data: str | None = None, content_type: str | None = None) -> str | None:
    """Why the network layer aborts `method url`, else None (let it through).

    Fail closed: a request that is not GET, HEAD or OPTIONS to a Greenhouse,
    Lever or Ashby host, or to the Ashby GraphQL path, is aborted unless it is
    on the allowlist described above. Pure (no browser). The description
    carries the host and path only, never the query string."""
    verb = (method or "").upper()
    if verb in _SAFE_METHODS:
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None  # not a URL the browser can send
    path = parts.path or "/"
    where = f"{host}{path}"
    recognised = classify_request(verb, url, post_data)
    if recognised:
        return recognised
    if path == _ASHBY_GRAPHQL_PATH:
        return _ashby_graphql_block(host, parts.query, post_data, content_type)
    family = _ats_family(host)
    if family is None:
        return None
    if _submit_shape(host, path):
        return f"aborted {family} application {verb or 'unknown method'} ({where})"
    if verb == "POST" and _CHALLENGE_PASSIVE_PATH.match(path):
        return None
    if path.startswith(_CHALLENGE_PREFIX):
        # Host and fixed prefix only: the rest is a long token, and `_clean` would cut the line.
        return f"aborted {family} {verb or 'unknown method'} bot challenge (Cloudflare) ({host}{_CHALLENGE_PREFIX})"
    if path == _SIGNAL_PATH:
        return None
    if family == "greenhouse" and _GREENHOUSE_ALLOWED_PATH.search(path):
        return None
    if host == _GREENHOUSE_BEACON_HOST and _GREENHOUSE_BEACON_PATH.match(path):
        return None
    if family == "lever" and verb == "POST" and path == _LEVER_ALLOWED_PATH:
        return None
    return f"aborted {family} {verb or 'unknown method'} not on the allowlist ({where})"


def is_unknown_abort(blocker: str) -> bool:
    """True when `blocker` (a line from `blockers`, prefix or not) is the network
    layer's catch-all: an aborted request that matched no submit shape and no
    allowlist entry, so nothing was sent and nothing says a form tried to
    submit. False for everything else: a recognised submit (any method), a
    multipart or unnamed Ashby GraphQL call, a DOM-layer catch and any line
    about the guard's own state. The whole line must match what `block_reason`
    writes, so a page's own report cannot pass for one."""
    text = (blocker or "").strip()
    if text.startswith(_PREFIX):
        text = text[len(_PREFIX):]
    return _UNKNOWN_ABORT.match(text) is not None


def _watched(url: str) -> bool:
    """Cheap URL-only test that decides whether the route handler sees a
    request. The method and the body are checked in the handler."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.path or "/") == _ASHBY_GRAPHQL_PATH or _ats_family(parts.hostname or "") is not None


# ---------------------------------------------------------------------------
# The DOM layer. One source, used twice: as an init script (every document
# loaded from now on, in every frame) and as an evaluate (documents already
# loaded). It is idempotent per document.
# ---------------------------------------------------------------------------

_GUARD_FN = (
    r"""() => {
  if (window.__jhSubmitGuard) return;
  const MARK = "%(mark)s";
  const TTL = %(ttl)d;
  const pending = [];
  const off = () => window[%(off)s] === true;
  const clip = (s, n) => String(s == null ? "" : s).replace(/\s+/g, " ").trim().slice(0, n);
  const report = (kind, detail) => {
    const payload = { kind: kind, detail: clip(detail, %(detail)d) };
    try {
      (window[%(binding)s] || window[%(ctxbinding)s])(payload);
    } catch (e) {
      pending.push(payload);  // binding unavailable: Python drains this instead
    }
  };
  const describe = (form, submitter) => {
    let out = "form";
    if (form && form.tagName === "FORM" && form.id) out += "#" + form.id;
    if (submitter && submitter.tagName) {
      const text = submitter.innerText || submitter.value || submitter.getAttribute("aria-label") || "";
      out += ', submitter ' + submitter.tagName.toLowerCase() + ' "' + clip(text, 40) + '"';
    } else {
      out += ", no submitter";
    }
    return out;
  };
  window.addEventListener("submit", (ev) => {
    if (off()) return;
    ev.preventDefault();
    ev.stopImmediatePropagation();
    const sub = ev.submitter || null;
    if (sub && sub.hasAttribute && sub.hasAttribute(MARK)) {
      const stamp = Number(sub.getAttribute(MARK));
      sub.removeAttribute(MARK);
      if (stamp > 0 && Date.now() - stamp < TTL) return;  // expected answer click
    }
    report("submit", "submit event stopped (" + describe(ev.target, sub) + ")");
  }, true);
  const proto = window.HTMLFormElement && window.HTMLFormElement.prototype;
  if (proto) {
    const origSubmit = proto.submit;
    const origRequestSubmit = proto.requestSubmit;
    Object.defineProperty(proto, "submit", {
      configurable: true, writable: true,
      value: function submit() {
        if (off()) return origSubmit.apply(this, arguments);
        report("form.submit", "form.submit() stopped (" + describe(this, null) + ")");
      },
    });
    Object.defineProperty(proto, "requestSubmit", {
      configurable: true, writable: true,
      value: function requestSubmit() {
        // The original fires a (cancelable) submit event, which the listener
        // above stops and records; with the guard off it submits for real.
        return origRequestSubmit.apply(this, arguments);
      },
    });
  }
  Object.defineProperty(window, "__jhSubmitGuard", {
    configurable: false, writable: false,
    value: Object.freeze({
      ping: () => {
        try { return (window[%(binding)s] || window[%(ctxbinding)s])({ kind: "ping" }); } catch (e) { return null; }
      },
      drain: () => pending.splice(0),
    }),
  });
}"""
    % {
        "mark": ANSWER_MARK_ATTR,
        "ttl": _MARK_TTL_MS,
        "off": json.dumps(_OFF_FLAG),
        "binding": json.dumps(_BINDING_NAME),
        "ctxbinding": json.dumps(_CTX_BINDING_NAME),
        "detail": _MAX_DETAIL,
    }
)
_GUARD_INIT_JS = f"({_GUARD_FN})();"

# Flush: a binding call returns only after every earlier call from the same
# frame was handled in Python (calls are handled in order), so awaiting a ping
# makes the Python-side list current. `null` means no guard in this document.
_FLUSH_FN = (
    "async () => { const g = window.__jhSubmitGuard; if (!g) return null;"
    " await g.ping(); return g.drain(); }"
)

_OFF_INIT_JS = f"window[{json.dumps(_OFF_FLAG)}] = true;"
_OFF_FN = f"() => {{ window[{json.dumps(_OFF_FLAG)}] = true; }}"

# Runs in the locator's own frame: the DOM layer is there and not switched off.
_DOM_LAYER_FN = f"() => !!window.__jhSubmitGuard && window[{json.dumps(_OFF_FLAG)}] !== true"

_MARK_FN = f"el => el.setAttribute({json.dumps(ANSWER_MARK_ATTR)}, String(Date.now()))"
_UNMARK_FN = f"el => el.removeAttribute({json.dumps(ANSWER_MARK_ATTR)})"


@dataclass
class _State:
    """Per-page guard state. Holds no reference to the page itself (the
    registry is weak-keyed by page and must not keep it alive)."""

    events: list[str] = field(default_factory=list)
    dropped: int = 0
    # install progress, so a failed install can be retried step by step
    bound: bool = False
    scripted: bool = False
    routed: bool = False
    in_context: bool = False  # registered as a sink of its context guard
    active: bool = False  # install finished and lift not started
    lifting: bool = False  # lift started; install must never run again
    lifted: bool = False  # lift finished
    match: Callable[[str], bool] | None = None
    handler: Callable[[Any], None] | None = None
    context: Any = None  # weakref.ref to the page's browser context, or None


@dataclass
class _CtxGuard:
    """The backstop on one browser context: guards popups and new tabs, that
    is, pages that were never installed themselves. Holds no reference to the
    context or to any page."""

    sinks: list[_State] = field(default_factory=list)  # installed, not lifted; newest last
    tracked: "weakref.WeakSet[Any]" = field(default_factory=weakref.WeakSet)  # pages created after install
    bound: bool = False
    scripted: bool = False
    listening: bool = False
    routed: bool = False
    match: Callable[[str], bool] | None = None
    handler: Callable[[Any], None] | None = None


_STATES: "weakref.WeakKeyDictionary[Any, _State]" = weakref.WeakKeyDictionary()
_CONTEXTS: "weakref.WeakKeyDictionary[Any, _CtxGuard]" = weakref.WeakKeyDictionary()


def _get(page: Any) -> _State | None:
    try:
        return _STATES.get(page)
    except TypeError:  # not hashable / not weak-referenceable (a test double)
        return None


def _context_guard(context: Any) -> _CtxGuard | None:
    try:
        return _CONTEXTS.get(context)
    except TypeError:
        return None


def _clean(text: Any) -> str:
    out = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return " ".join(out.split())[:_MAX_DETAIL]


def _record(state: _State, text: Any) -> None:
    if state.lifted:
        return
    if len(state.events) < _MAX_EVENTS:
        state.events.append(_clean(text))
    else:
        state.dropped += 1


def _ingest(state: _State, payload: Any) -> None:
    """Record one report from the page. Anything else a page may send (a
    ping, junk) records nothing."""
    if not isinstance(payload, dict):
        return
    if payload.get("kind") not in ("submit", "form.submit"):
        return
    detail = _clean(payload.get("detail") or payload.get("kind"))
    if detail.lower().startswith("aborted "):
        # Only the network layer writes "aborted ..." lines; a page must not forge one.
        detail = _PAGE_REPORT_LEAD + detail
    _record(state, detail)


def _binding(state: _State) -> Callable[..., None]:
    def on_report(_source: Any, payload: Any = None) -> None:
        _ingest(state, payload)

    return on_report


def _sink(guard: _CtxGuard, page: Any) -> _State | None:
    """The state a popup or new tab reports into: its own if it has one, else
    the newest installed page in the context. (No browser call here: this
    runs inside route and binding callbacks, where a sync Playwright call
    would deadlock.)"""
    own = _get(page)
    if own is not None:
        return own
    return guard.sinks[-1] if guard.sinks else None


def _page_of(source: Any) -> Any:
    try:
        return source["page"]
    except Exception:
        return None


def _ctx_binding(guard: _CtxGuard) -> Callable[..., None]:
    def on_report(source: Any, payload: Any = None) -> None:
        sink = _sink(guard, _page_of(source))
        if sink is not None:
            _ingest(sink, payload)

    return on_report


def _decide(route: Any) -> str | None:
    request = route.request
    body: str | None = None
    try:
        body = request.post_data
    except Exception:  # binary body: not a JSON request
        body = None
    content_type: str | None = None
    try:
        content_type = request.headers.get("content-type")
    except Exception:
        content_type = None
    return block_reason(request.method, request.url, body, content_type)


def _abort(route: Any) -> None:
    try:
        route.abort("blockedbyclient")
    except Exception:  # page closed while we were deciding: nothing left to stop
        pass


def _route_handler(state: _State) -> Callable[[Any], None]:
    def handler(route: Any) -> None:
        reason = _decide(route)
        if reason is None:
            route.fallback()
            return
        _record(state, reason)
        _abort(route)

    return handler


def _ctx_route_handler(guard: _CtxGuard) -> Callable[[Any], None]:
    def handler(route: Any) -> None:
        # An installed page (even a lifted one) is judged by its own route.
        try:
            requesting = route.request.frame.page
        except Exception:
            requesting = None
        if requesting is not None and _get(requesting) is not None:
            route.fallback()
            return
        reason = _decide(route)
        if reason is None:
            route.fallback()
            return
        sink = _sink(guard, requesting)
        if sink is not None:
            _record(sink, reason)
        _abort(route)

    return handler


def _install_context(context: Any, state: _State) -> None:
    """Guard popups and new tabs through the page's browser context. Each
    step is done once per context, so a failed install can be retried."""
    guard = _context_guard(context)
    if guard is None:
        guard = _CtxGuard()
        _CONTEXTS[context] = guard
    if not guard.bound:
        context.expose_binding(_CTX_BINDING_NAME, _ctx_binding(guard))
        guard.bound = True
    if not guard.scripted:
        context.add_init_script(script=_GUARD_INIT_JS)
        guard.scripted = True
    if not guard.listening:
        context.on("page", guard.tracked.add)
        guard.listening = True
    if not guard.routed:
        guard.match = _watched
        guard.handler = _ctx_route_handler(guard)
        context.route(guard.match, guard.handler)
        guard.routed = True
    if not state.in_context:
        guard.sinks.append(state)
        state.in_context = True
    state.context = weakref.ref(context)


def install(page: Any) -> None:
    """Arm the guard on `page`. Call right after `new_page()` and before the
    first `goto`. Idempotent. Raises after `lift` (use a fresh page). Popups
    and new tabs the page opens are guarded too (through its context)."""
    state = _STATES.get(page)
    if state is None:
        state = _State()
        _STATES[page] = state
    if state.lifting:
        raise RuntimeError("submit guard was lifted for this page; use a fresh page")
    if state.active:
        return
    if not state.bound:
        page.expose_binding(_BINDING_NAME, _binding(state))
        state.bound = True
    if not state.scripted:
        page.add_init_script(script=_GUARD_INIT_JS)
        state.scripted = True
    for frame in page.frames:  # documents that are already loaded
        frame.evaluate(_GUARD_FN)
    if not state.routed:
        state.match = _watched
        state.handler = _route_handler(state)
        page.route(state.match, state.handler)
        state.routed = True
    context = getattr(page, "context", None)  # a test double has none
    if context is not None:
        _install_context(context, state)
    state.active = True


def installed(page: Any) -> bool:
    """True while the guard is in force on `page` (False before `install`
    and after `lift`). A popup or new tab opened after `install` counts as
    guarded while any installed page in its context is."""
    state = _get(page)
    if state is not None:
        return bool(state.active and not state.lifted)
    try:
        guard = _context_guard(getattr(page, "context", None))
        return bool(guard and guard.sinks and page in guard.tracked)
    except TypeError:
        return False


def _frame_ats_host(frame: Any) -> str | None:
    """The host of `frame`'s URL when it is a Greenhouse, Lever or Ashby host."""
    try:
        host = (urlsplit(frame.url).hostname or "").lower()
    except Exception:
        return None
    return host if _ats_family(host) else None


def _flush_frames(frames: list[Any], sink: _State, *, require_first: bool) -> list[str]:
    """Drain each frame into `sink`. A frame that cannot be read, or that has
    no DOM layer, is a problem when it is the required main frame or when it
    is on an ATS host; analytics and other frames are ignored."""
    problems: list[str] = []
    for index, frame in enumerate(frames):
        required = require_first and index == 0
        try:
            drained = frame.evaluate(_FLUSH_FN)
        except Exception as exc:
            if required:
                problems.append(f"could not read guard state in the main frame ({type(exc).__name__})")
            else:
                host = _frame_ats_host(frame)
                if host:
                    problems.append(f"could not read guard state in a frame on {host} ({type(exc).__name__})")
            continue
        if drained is None:
            if required:
                problems.append("DOM layer missing in the main frame")
            else:
                host = _frame_ats_host(frame)
                if host:
                    problems.append(f"DOM layer missing in a frame on {host}")
            continue
        for payload in drained:
            _ingest(sink, payload)
    return problems


def _flush(page: Any, state: _State) -> list[str]:
    """Pull anything still in flight from the page (and from the popups and
    new tabs reporting into it) into `state`. Returns reasons the guard state
    could not be trusted (fail closed)."""
    try:
        frames = list(page.frames)
    except Exception as exc:
        return [f"could not list frames ({type(exc).__name__})"]
    problems = _flush_frames(frames, state, require_first=True)
    context = state.context() if state.context is not None else None
    guard = _context_guard(context) if context is not None else None
    if guard is None:
        return problems
    try:
        others = [p for p in list(context.pages) if p is not page and _get(p) is None and p in guard.tracked]
    except Exception as exc:
        return problems + [f"could not list the other pages ({type(exc).__name__})"]
    for other in others:
        if _sink(guard, other) is not state:
            continue
        try:
            other_frames = list(other.frames)
        except Exception:  # closed meanwhile: its reports already arrived
            continue
        problems.extend(_flush_frames(other_frames, state, require_first=False))
    return problems


def blockers(page: Any) -> list[str]:
    """Everything the guard stopped on `page` so far, each entry starting
    with "submit_guard: ". [] when the guard was never installed."""
    state = _get(page)
    if state is None:
        return []
    problems = _flush(page, state) if state.active and not state.lifted else []
    out = [_PREFIX + event for event in state.events]
    if state.dropped:
        out.append(f"{_PREFIX}{state.dropped} further stopped attempts not listed")
    out.extend(_PREFIX + problem for problem in problems)
    return out


def lift(page: Any) -> None:
    """Turn the layers off so the real submit click works. A no-op when the
    guard is not installed. Raises if a layer cannot be turned off; the
    caller must then not click."""
    state = _get(page)
    if state is None or state.lifted:
        return
    if state.active:
        _flush(page, state)  # keep what was stopped before the lift
    # From here the guard is no longer in force, even if a step below raises:
    # `installed` must not claim protection that the off script already removed.
    state.lifting = True
    state.active = False
    page.add_init_script(script=_OFF_INIT_JS)  # documents loaded from now on
    for frame in page.frames:  # documents already loaded
        frame.evaluate(_OFF_FN)
    if state.routed and state.match is not None and state.handler is not None:
        page.unroute(state.match, state.handler)
        state.routed = False
    _release_context(state)
    state.lifted = True


def _release_context(state: _State) -> None:
    """Stop guarding popups and new tabs for `state`; when it was the last
    installed page in its context, take the context route off too. (Popups
    already open keep their DOM layer: the init script cannot be removed.)"""
    context = state.context() if state.context is not None else None
    guard = _context_guard(context) if context is not None else None
    if guard is None:
        return
    if state in guard.sinks:
        guard.sinks.remove(state)
    if not guard.sinks and guard.routed and guard.match is not None and guard.handler is not None:
        context.unroute(guard.match, guard.handler)
        guard.routed = False


def mark_answer_click(locator: Any) -> None:
    """Mark the element `locator` points at as the submitter of an expected
    answer click: the submit event it triggers is stopped but not recorded.
    The mark is removed on use, expires after ten seconds, and fill.py also
    clears it in a `finally` (`clear_answer_mark`)."""
    locator.evaluate(_MARK_FN, timeout=_MARK_TIMEOUT_MS)


def clear_answer_mark(locator: Any) -> None:
    """Remove the mark again. Best effort: never raises. An element that is
    gone (a list option the click closed) took its mark with it, so there is
    nothing to wait for."""
    try:
        if locator.count() == 0:
            return
        locator.evaluate(_UNMARK_FN, timeout=_MARK_TIMEOUT_MS)
    except Exception:
        pass


def click_answer(
    page: Any,
    locator: Any,
    *,
    timeout: int = 10_000,
    position: dict[str, float] | None = None,
) -> None:
    """Click `locator` once as a deliberate answer click (an Ashby Yes/No
    button, a radio's label, a list option) and nothing else.

    This is the only click fill.py makes (submit.py's own click is the real
    submit). It refuses when the guard is not installed on `page` (raises
    RuntimeError), and when the DOM layer is not present in the element's own
    frame (a frame the init script did not reach), so no click can ever reach
    an unguarded page or frame. The element is marked first, so the submit
    event a type-less button fires is stopped without being recorded as an
    attempt, and the mark is cleared again whatever the click does. fill.py
    calls this instead of `.click()`, so its source holds no click of its own
    (tests/test_fill_ashby_clicks.py checks that).

    `position` ({"x": .., "y": ..}, relative to the element's top-left corner)
    aims the click at a spot that is safe to hit, e.g. on a label's own text
    rather than on a link inside it."""
    if not installed(page):
        raise RuntimeError("submit guard is not installed on this page; refusing to click")
    try:
        present = locator.evaluate(_DOM_LAYER_FN, timeout=_MARK_TIMEOUT_MS)
    except Exception as exc:
        raise RuntimeError(
            f"could not confirm the submit guard in the element's frame ({type(exc).__name__}); refusing to click"
        ) from exc
    if present is not True:
        raise RuntimeError("submit guard DOM layer is missing in the element's frame; refusing to click")
    options: dict[str, Any] = {"timeout": timeout}
    if position is not None:
        options["position"] = position
    mark_answer_click(locator)
    try:
        locator.click(**options)
    finally:
        clear_answer_mark(locator)
