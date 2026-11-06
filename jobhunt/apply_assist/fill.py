"""Playwright field scanning + resolution execution for apply-assist.

HARD SAFETY RULE: this module never presses Enter or Escape, never calls
form.submit() or dispatches submit, and never submits anything: the only
submitter is `apply_assist/submit.py` (specs/2026-10-23-autoapply.md). Without
`autoapply` it also never clicks anything and never auto-checks a checkbox.
Radio `.check()` stays allowed: it is a selection action, not a submission
action. NEW, the only key presses allowed are `Tab` and
`ArrowDown`, sent only to a detected react-select combobox, only to commit an
option whose text exactly equals the resolved answer (picking an option is a
selection action like radio `.check()`, not a submission). For a location or
country answer, "equals" also covers the one open-menu option that spells the
same place differently ("Atlanta, GA, USA" for "Atlanta, Georgia";
`solve.same_place`, `_spelling_options`): never when two options do.
One more keyboard action, for the react-select whose menu ignores `fill()`
(Greenhouse's Country opens and filters only on real key events): after
ArrowDown, the resolved answer's own characters are typed key by key
(`_type_like_a_person`, the only caller of `press_sequentially`). Only the
answer, and only if the stored text is plain printable text: a control,
format or line-break character anywhere in it (a typed newline is Enter, which
can submit the form) means nothing is typed at all (`_typeable`). A Space is only
ever sent into a box that already holds text (react-select picks the focused
option on a Space in an empty one). Tab is still pressed only while the
focused option is the answer.
The one narrow allowance, for the armed `jobhunt autoapply` lane only
(`autoapply=True`; 2026-10-24, spec questions Q5 and Q7): answer clicks
on the form's own answer controls, and no other click. They are an Ashby Yes/No
button, the label of a hidden radio or of a required acknowledgment box (action
"check"), a location box's list option, and a date picker's month button and day
cell. Each goes through `submit_guard.click_answer` (`_click_answer`, the only
click in this module), which is refused unless `submit_guard.installed(page)`:
the dry-run guard that stops any submit a click starts. A page without the guard
is never clicked and the field fails with that reason. The start date is typed,
not clicked, whenever the input takes typing (`autoapply_start_date`). Even then
fill never submits: the click on Submit is `apply_assist/submit.py`'s alone.
"""

from __future__ import annotations

import datetime
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jobhunt.apply_assist import cover_letter, solve, submit_guard
from jobhunt.apply_assist.detect import detect_ats
from jobhunt.apply_assist.keys import CHECKBOX_TITLE_SEP, classify
from jobhunt.apply_assist.mapper import MapperResult
from jobhunt.apply_assist.mapper import map_unknown as mapper_map_unknown
from jobhunt.apply_assist.schema import FieldSpec, FillOutcome, Resolution, TriageDigest
from jobhunt.apply_assist.tailor_answers import (
    TAILOR_MIN_SCORE,
    TailorResult,
    ai_policy_label,
    asks_for_prose,
    draft_for_posting,
)
from jobhunt.apply_assist.tailor_answers import fingerprint as tailor_fingerprint
from jobhunt.apply_assist.tailor_answers import question_text as tailor_question_text
from jobhunt.eligibility import user_preferences
from jobhunt.rubric import load_rubric
from jobhunt.spend import RunBudget

_SKIP_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset"}

_KIND_MAP = {
    "email": "email",
    "tel": "tel",
    "url": "url",
    "file": "file",
    "checkbox": "checkbox",
    "radio": "radio",
    # A number input is its own kind: playwright's fill() refuses text in one, and
    # solve.py answers a number box with digits only (a salary, a count of years).
    "number": "number",
}

# Fillable-field selector and visibility test, shared by the field-count, scan
# and hydration scripts so "fillable" and "visible" cannot drift apart.
_FILLABLE_SELECTOR = (
    "input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select"
)

_IS_VISIBLE_JS = """
  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    // Only a file input may be fully transparent: Lever lays its real input, at
    // opacity 0, over a styled "ATTACH RESUME/CV" button, and that overlay is the
    // field. Any other opacity-0 element is a honeypot and stays skipped.
    const isFile = el.tagName === 'INPUT' && el.type === 'file';
    if (style.opacity === '0' && !isFile) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
"""

_SCAN_JS = """
() => {
  const out = [];
  const usedNames = new Set();

  // The element's own label: label[for], a wrapping <label>, or aria-label. On a
  // radio or checkbox this is the OPTION's text ("Yes"), never the question.
  function ownLabel(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    const closest = el.closest('label');
    if (closest && closest.innerText.trim()) return closest.innerText.trim();
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    return '';
  }

  function squash(text) { return (text || '').replace(/\\s+/g, ' ').trim(); }

  function textOf(node) { return node ? squash(node.innerText || node.textContent) : ''; }

  function labelledByText(node) {
    const parts = [];
    for (const id of (node.getAttribute('aria-labelledby') || '').split(/\\s+/)) {
      const text = id ? textOf(document.getElementById(id)) : '';
      if (text) parts.push(text);
    }
    return parts.join(' ');
  }

  // Lever's custom questions and Ashby's radio groups carry no <label for>: the
  // question is a heading beside the control, inside the question's own box.
  const QUESTION_BOX = '.ashby-application-form-field-entry, li.application-question, ' +
    '.application-question, .field-wrapper, div.field';
  const QUESTION_HEADING = '.ashby-application-form-question-title, .application-label, ' +
    'legend, label';

  // The question's own heading, never an option's label, from the most explicit
  // markup to the least: aria-labelledby, a role=group's name, a fieldset's
  // legend, then the first heading in the question's box that holds no control.
  function questionLabel(el) {
    // A radio's or checkbox's own aria-labelledby names that option, not the question.
    if (!el.matches('input[type="radio"], input[type="checkbox"]')) {
      const named = labelledByText(el);
      if (named) return named;
    }
    const group = el.closest('[role=radiogroup], [role=group]');
    if (group) {
      const named = labelledByText(group) || squash(group.getAttribute('aria-label'));
      if (named) return named;
    }
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = textOf(fieldset.querySelector(':scope > legend'));
      if (legend) return legend;
    }
    const box = el.closest(QUESTION_BOX);
    if (box) {
      for (const node of box.querySelectorAll(QUESTION_HEADING)) {
        if (node.querySelector('input, select, textarea')) continue;
        const heading = textOf(node);
        if (heading) return heading;
      }
    }
    return '';
  }

  function labelFor(el) { return ownLabel(el) || questionLabel(el); }

__IS_VISIBLE__

  function isRequired(el) {
    return !!el.required || el.getAttribute('aria-required') === 'true';
  }

  function selectorFor(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) {
      const sameName = document.querySelectorAll(`[name="${CSS.escape(el.name)}"]`);
      if (sameName.length === 1) return `[name="${CSS.escape(el.name)}"]`;
      const idx = Array.prototype.indexOf.call(sameName, el);
      return `[name="${CSS.escape(el.name)}"]:nth-of-type(${idx + 1})`;
    }
    return null;
  }

  const radioGroups = {};

  const elements = Array.from(document.querySelectorAll('input, textarea, select'));
  for (const el of elements) {
    if (!isVisible(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;

    if (tag === 'input' && type === 'radio' && el.name) {
      const key = el.name;
      if (!radioGroups[key]) {
        radioGroups[key] = {
          tag: 'input', type: 'radio', id: '', name: key,
          autocomplete: el.getAttribute('autocomplete') || '',
          placeholder: '', required: isRequired(el),
          label: questionLabel(el), options: [], selector: `[name="${CSS.escape(key)}"]`,
        };
        out.push(radioGroups[key]);
      } else if (isRequired(el)) {
        radioGroups[key].required = true;  // any required member makes the group required
      }
      const optLabel = ownLabel(el) || el.value || '';
      if (optLabel) radioGroups[key].options.push(optLabel);
      continue;
    }

    const sel = selectorFor(el);
    if (!sel) continue;

    // A checkbox is its own option: its label stays the option text.
    const isCheckbox = tag === 'input' && type === 'checkbox';
    const entry = {
      tag,
      type: tag === 'input' ? type : tag,
      id: el.id || '',
      name: el.name || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      placeholder: el.getAttribute('placeholder') || '',
      required: isRequired(el),
      label: isCheckbox ? ownLabel(el) : labelFor(el),
      options: [],
      selector: sel,
    };

    if (tag === 'select') {
      entry.options = Array.from(el.options).map(o => (o.textContent || '').trim()).filter(Boolean);
    }

    out.push(entry);
  }

  return out;
}
""".replace("__IS_VISIBLE__", _IS_VISIBLE_JS)


# Ashby's own widgets, for the armed lane's second scan pass (`scan_fields(autoapply=True)`).
# The class names are Ashby's stable, unhashed ones (read from its live bundle, 2026-10-24).
# This is the one place that names them: the scan and the flows both read it, and a field's
# selector says which widget it came from (`_widget`).
_WIDGET_CSS = {
    "yesno": ".ashby-application-form-input-yesno",
    "radio": ".ashby-application-form-input-radio-group",
    "checkbox": ".ashby-application-form-input-checkbox-group",
    "autocomplete": "input.ashby-application-form-input-autocomplete",
    "date": "input.ashby-application-form-input-date",
}
# The widgets with no id or name to select them by: `<css> >> nth=<position>`.
_POSITIONAL = ("yesno", "autocomplete", "date")

# What the second pass adds to `_SCAN_JS`, as raw fields plus one extra key, `replaces`:
# the selector of the base-scan entry this one stands in for (a Yes/No pair's hidden
# checkbox, a date or location box that has no id of its own), or null.
#   - A Yes/No pair of buttons is ONE radio field, options Yes and No, the question's
#     title as its label. Only a pair that reads Yes and No is one.
#   - A radio group and a checkbox group are re-read from the group, because the page may
#     hide the inputs (the base scan skips them) and because the star that makes the
#     question required sits on the group's TITLE, never on the inputs. A checkbox keeps
#     its OPTION's label ("I acknowledge ...") first, like the base scan's, then the
#     group's title after `CHECKBOX_TITLE_SEP`.
#   - A location box and a date box carry no id or name: they are found by position.
# A widget without a title is not reported: its question could not be told again later.
_ASHBY_SCAN_JS = r"""
([css, sep]) => {
  const out = [];

  function squash(text) { return (text || '').replace(/\s+/g, ' ').trim(); }

  function textOf(node) { return node ? squash(node.innerText || node.textContent) : ''; }

__IS_VISIBLE__

  function isRequired(el) {
    return !!el.required || el.getAttribute('aria-required') === 'true';
  }

  function selectorFor(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) {
      const sameName = document.querySelectorAll(`[name="${CSS.escape(el.name)}"]`);
      if (sameName.length === 1) return `[name="${CSS.escape(el.name)}"]`;
      const idx = Array.prototype.indexOf.call(sameName, el);
      return `[name="${CSS.escape(el.name)}"]:nth-of-type(${idx + 1})`;
    }
    return null;
  }

  function ownLabel(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    const closest = el.closest('label');
    if (closest && closest.innerText.trim()) return closest.innerText.trim();
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    return '';
  }

  // The question's title inside its own field entry, and whether it is starred: the
  // star is generated content on a class that starts with _required_. A radio or
  // checkbox group the page lays out as a bare fieldset sits in no field entry (OpenAI's
  // acknowledgments, Rogo's yes/no radios): its title is the fieldset's own heading.
  // Only a fieldset inside Ashby's own form container is read as a question group:
  // Greenhouse's and Lever's labels stay what the base scan made them.
  const ASHBY_FORM = '.ashby-application-form-container, .ashby-application-form-section-container';

  function titleOf(el) {
    const entry = el.closest('.ashby-application-form-field-entry');
    const named = entry ? entry.querySelector('.ashby-application-form-question-title') : null;
    if (named) return named;
    const set = el.closest('fieldset');
    if (!set || !set.closest(ASHBY_FORM)) return null;
    return set.querySelector('.ashby-application-form-question-title') ||
      set.querySelector(':scope > legend, :scope > label');
  }

  // The groups of one kind of input: the page's own class, plus any fieldset or ARIA
  // group that holds such inputs, each once.
  function groupsOf(selector, inputType) {
    const found = new Set(document.querySelectorAll(selector));
    for (const holder of document.querySelectorAll('fieldset, [role=radiogroup]')) {
      if (holder.closest(ASHBY_FORM) && holder.querySelector(`input[type="${inputType}"]`)) {
        found.add(holder);
      }
    }
    // The innermost: a fieldset around the page's own group stands aside for it.
    return Array.from(found).filter(
      (group) => !Array.from(found).some((other) => other !== group && group.contains(other)));
  }

  function titleRequired(title) {
    for (const name of title.classList) {
      if (name.startsWith('_required_')) return true;
    }
    return getComputedStyle(title, '::after').content.includes('*');
  }

  Array.from(document.querySelectorAll(css.yesno)).forEach((group, index) => {
    if (!isVisible(group)) return;
    const title = titleOf(group);
    if (!title) return;
    const words = Array.from(group.querySelectorAll('button'))
      .map((button) => squash(button.textContent).toLowerCase());
    if (words.length !== 2 || words.slice().sort().join() !== 'no,yes') return;
    const entry = group.closest('.ashby-application-form-field-entry') || group;
    const paired = group.querySelector('input[type="checkbox"]') ||
      entry.querySelector('input[type="checkbox"]');
    out.push({
      tag: 'input', type: 'radio', id: '', name: '', autocomplete: '', placeholder: '',
      required: titleRequired(title), label: textOf(title), options: ['Yes', 'No'],
      selector: `${css.yesno} >> nth=${index}`,
      replaces: paired ? selectorFor(paired) : null,
    });
  });

  const seenRadio = new Set();
  for (const group of groupsOf(css.radio, 'radio')) {
    if (!isVisible(group)) continue;
    const title = titleOf(group);
    const inputs = Array.from(group.querySelectorAll('input[type="radio"]'));
    if (!title || !inputs.length) continue;
    const name = inputs[0].name;
    if (!name || inputs.some((el) => el.name !== name) || seenRadio.has(name)) continue;
    seenRadio.add(name);
    out.push({
      tag: 'input', type: 'radio', id: '', name,
      autocomplete: inputs[0].getAttribute('autocomplete') || '', placeholder: '',
      required: titleRequired(title) || inputs.some(isRequired), label: textOf(title),
      options: inputs.map((el) => ownLabel(el) || el.value || '').filter(Boolean),
      selector: `[name="${CSS.escape(name)}"]`, replaces: null,
    });
  }

  for (const group of groupsOf(css.checkbox, 'checkbox')) {
    if (!isVisible(group)) continue;
    const title = titleOf(group);
    if (!title) continue;
    const starred = titleRequired(title);
    const titleText = textOf(title);
    for (const el of group.querySelectorAll('input[type="checkbox"]')) {
      if (el.closest(css.yesno)) continue;  // a Yes/No pair's own hidden box
      const selector = selectorFor(el);
      if (!selector) continue;
      const option = el.closest('[class*="checkbox-group-option"]');
      // The box's label is its own text, then the group's title: the title says what
      // is being agreed to ("Applicant Arbitration Agreement Acknowledgement"), the box
      // often only "I confirm I have read the above."
      const own = ownLabel(el) || textOf(option);
      out.push({
        tag: 'input', type: 'checkbox', id: el.id || '', name: el.name || '',
        autocomplete: el.getAttribute('autocomplete') || '', placeholder: '',
        required: starred || isRequired(el),
        label: titleText && own && !own.includes(titleText) ? own + sep + titleText : (own || titleText),
        options: [], selector, replaces: null,
      });
    }
  }

  for (const selector of [css.autocomplete, css.date]) {
    Array.from(document.querySelectorAll(selector)).forEach((el, index) => {
      if (!isVisible(el)) return;
      const title = titleOf(el);
      if (!title) return;
      out.push({
        tag: 'input', type: 'text', id: el.id || '', name: el.name || '',
        autocomplete: el.getAttribute('autocomplete') || '',
        placeholder: el.getAttribute('placeholder') || '',
        required: titleRequired(title) || isRequired(el), label: textOf(title), options: [],
        selector: `${selector} >> nth=${index}`, replaces: selectorFor(el),
      });
    });
  }

  return out;
}
""".replace("__IS_VISIBLE__", _IS_VISIBLE_JS)


_FIELD_COUNT_JS = (
    "() => document.querySelectorAll('__FILLABLE__').length"
).replace("__FILLABLE__", _FILLABLE_SELECTOR)

# `ssr`: the page was server-rendered by a React framework, so its fields exist
# before React owns them. `pending`: visible fillable fields React has not yet
# attached to. Once hydrated, React gives every field it manages an own property
# named `__reactFiber$...` / `__reactProps$...`; text typed before that is wiped
# by the first re-render.
_HYDRATION_JS = """
() => {
__IS_VISIBLE__
  const ssr = !!(window.__remixContext || window.__reactRouterContext ||
                 window.__NEXT_DATA__ || document.getElementById('__NEXT_DATA__'));
  let pending = 0;
  for (const el of document.querySelectorAll('__FILLABLE__')) {
    if (!isVisible(el)) continue;
    const hydrated = Object.getOwnPropertyNames(el).some(
      (k) => k.startsWith('__reactFiber$') || k.startsWith('__reactProps$'));
    if (!hydrated) pending += 1;
  }
  return { ssr, pending };
}
""".replace("__IS_VISIBLE__", _IS_VISIBLE_JS).replace("__FILLABLE__", _FILLABLE_SELECTOR)

_HYDRATION_SETTLE_MS = 300


def _page_wait(page: Any, ms: int) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception:  # noqa: BLE001 — page closing; a plain sleep still bounds the wait
        _pause(ms / 1000)


def _hydration_state(page: Any) -> tuple[bool, int]:
    """(ssr, pending). Anything unreadable counts as not-SSR so a fake page or
    a page mid-navigation never makes the wait hang or crash."""
    try:
        state = page.evaluate(_HYDRATION_JS)
    except Exception:  # noqa: BLE001
        return False, 0
    if not isinstance(state, dict) or not state.get("ssr"):
        return False, 0
    try:
        return True, max(0, int(state.get("pending") or 0))
    except (TypeError, ValueError):
        return True, 0


def _wait_for_hydration(page: Any, poll_ms: int, hydrate_timeout_ms: int) -> None:
    ssr, pending = _hydration_state(page)
    if not ssr:
        return
    deadline = time.monotonic() + hydrate_timeout_ms / 1000
    while pending > 0 and time.monotonic() < deadline:
        _page_wait(page, poll_ms)
        ssr, pending = _hydration_state(page)
        if not ssr:  # the page went away or changed under us: stop waiting
            return
    # Hydration attaches fields first; give the first re-render a beat to settle.
    _page_wait(page, _HYDRATION_SETTLE_MS)


def wait_for_fields(
    page: Any,
    timeout_ms: int = 15000,
    poll_ms: int = 250,
    hydrate_timeout_ms: int = 10000,
) -> int:
    """Block until the application form has mounted AND hydrated, or time out.

    `driver.open_page` navigates with wait_until="domcontentloaded", which
    fires before a client-rendered form exists. Ashby and Lever mount theirs
    after hydration and scanned as empty pages with no error raised.
    Greenhouse server-renders its form, so the fields exist at once, but React
    hydrates them 0.2-1 s later and the next re-render wipes any value typed in
    between (fill's own read-back still saw it). So once the fields exist, a
    server-rendered React page waits (up to `hydrate_timeout_ms`) until every
    visible field carries React's own-property marker, then settles briefly.
    Pages that are not server-rendered React pages skip that wait.

    Returns the field count last observed, so callers can tell "form never
    appeared" (0) from a page that genuinely has no fields.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    count = 0
    while True:
        try:
            count = int(page.evaluate(_FIELD_COUNT_JS))
        except Exception:  # noqa: BLE001 — page mid-navigation; retry until deadline
            count = 0
        if count:
            _wait_for_hydration(page, poll_ms, hydrate_timeout_ms)
            return count
        if time.monotonic() >= deadline:
            return count
        page.wait_for_timeout(poll_ms)


def scan_fields(page: Any, *, autoapply: bool = False) -> list[FieldSpec]:
    """Scan the page for visible fillable fields via a single evaluate() pass.

    `autoapply` (the armed lane only) adds a second pass for Ashby's own widgets
    (`_ASHBY_SCAN_JS`): a Yes/No pair of buttons, radio and checkbox groups the page
    hides behind a label, a location box, a date picker. Without it the scan is this
    one pass, exactly."""
    wait_for_fields(page)
    raw = page.evaluate(_SCAN_JS)
    specs: list[FieldSpec] = []
    for entry in raw:
        specs.append(_field_from_raw(entry))
    if autoapply:
        raw_widgets = page.evaluate(_ASHBY_SCAN_JS, [_WIDGET_CSS, CHECKBOX_TITLE_SEP])
        specs = _merge_ashby_scan(specs, raw_widgets)
    return specs


def _field_from_raw(entry: dict) -> FieldSpec:
    tag = entry.get("tag", "")
    el_type = entry.get("type", "text")
    if tag == "textarea":
        kind = "textarea"
    elif tag == "select":
        kind = "select"
    else:
        kind = _KIND_MAP.get(el_type, "text")

    return FieldSpec(
        selector=entry.get("selector") or "",
        kind=kind,
        label=entry.get("label", ""),
        name=entry.get("name", ""),
        elem_id=entry.get("id", ""),
        autocomplete=entry.get("autocomplete", ""),
        placeholder=entry.get("placeholder", ""),
        required=bool(entry.get("required", False)),
        options=list(entry.get("options") or []),
    )


def _merge_ashby_scan(base: list[FieldSpec], raw: list[dict]) -> list[FieldSpec]:
    """Put the second pass's fields (`_ASHBY_SCAN_JS`) into the base scan's list.
    A field replaces the base entry for the same selector in place, and one with a
    `replaces` selector also removes the base entry it stands in for; any other is
    appended after the base fields."""
    fields = list(base)
    for entry in raw:
        spec = _field_from_raw(entry)
        replaced = entry.get("replaces")
        if replaced:
            fields = [f for f in fields if f.selector != replaced]
        for index, existing in enumerate(fields):
            if existing.selector == spec.selector:
                fields[index] = spec
                break
        else:
            fields.append(spec)
    return fields


# Per-action Playwright waits. Playwright's own default is 30 s, which turns one
# missing or re-rendered element into a half-minute stall per field.
_ACTION_TIMEOUT_MS = 10_000
_READBACK_TIMEOUT_MS = 3_000

# The gate (apply_assist/submit.py) reads window[UPLOAD_RECORD_KEY][selector],
# a {name, size, count} record written when a file input receives files. Greenhouse
# removes `input#resume` from the DOM on the change event and shows a chip, so
# the record is the only evidence the upload landed.
UPLOAD_RECORD_KEY = "__jobhuntUploads"

_VERIFY_DELAY_S = 0.5
_OPTION_POLL_STEPS = 30  # x _OPTION_POLL_S = 3 s to see the first focused option
_OPTION_POLL_S = 0.1
_MAX_ARROW_PRESSES = 25
_KEY_DELAY_MS = 40  # between typed keys, a person's pace: react-select re-renders on each
_MAX_TYPED_CHARS = 150  # no option label is longer; never type a paragraph into a menu
_TEXT_KINDS = ("text", "email", "tel", "url", "number", "textarea")


def _pause(seconds: float) -> None:
    time.sleep(seconds)


# --- the armed lane's answer clicks (autoapply only) ---------------------------------
# Everything from here to `apply_resolutions` runs only when `autoapply=True` reached
# the scan or the fill. The module docstring states the allowance; the rule in code is
# `_click_answer`: the only click, refused unless the dry-run guard is installed.


def autoapply_start_date() -> datetime.date | None:
    """The start date the lane puts into a start-date picker (spec question Q7): the
    candidate's standing `earliest_start`, read at call time. None when it is unset:
    fail closed, the picker is then left to resolve() and is not given a guessed date."""
    return user_preferences().standing_answers.earliest_start


_CHECKED = "checked"  # what a ticked box reads back as; an unticked one reads back ""
_DATE_FORMAT = "%m/%d/%Y"  # react-datepicker's default, which Ashby's date box keeps
_DATE_INPUT_FORMATS = ("%Y-%m-%d", _DATE_FORMAT, "%B %d, %Y")
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MAX_MONTH_CLICKS = 36  # three years of month buttons: the start date is never farther
_DATE_POPUP = ".ashby-application-form-input-date-popup"
# The location box's list is drawn in a floating portal, outside the box's own entry.
_AUTOCOMPLETE_OPTIONS = '.ashby-application-form-input-autocomplete-popup [role="option"]'

# A widget's question title, read the way the scan's `titleOf` reads it: in its field
# entry, else (a question the page lays out as a bare fieldset, Spade's "Where did you
# hear about Spade?") in the fieldset inside Ashby's own form container.
_WIDGET_TITLE_JS = r"""
(el) => {
  const entry = el.closest('.ashby-application-form-field-entry');
  let title = entry ? entry.querySelector('.ashby-application-form-question-title') : null;
  if (!title) {
    const set = el.closest('fieldset');
    if (set && set.closest(
        '.ashby-application-form-container, .ashby-application-form-section-container')) {
      title = set.querySelector('.ashby-application-form-question-title') ||
        set.querySelector(':scope > legend, :scope > label');
    }
  }
  return title ? (title.innerText || title.textContent || '') : '';
}
"""

_IS_SHOWN_JS = "(el) => {\n" + _IS_VISIBLE_JS + "  return isVisible(el);\n}\n"

_YESNO_PRESSED_JS = r"""
(group) => {
  const pressed = group.querySelectorAll('button[aria-pressed="true"]');
  return pressed.length === 1 ? (pressed[0].textContent || '').trim() : '';
}
"""

_COMBOBOX_STATE_JS = r"""
(el) => ({ value: el.value || '', expanded: el.getAttribute('aria-expanded') === 'true' })
"""

_AUTOCOMPLETE_OPTIONS_JS = r"""
() => {
  const options = [];
  let loading = false;
  const popups = document.querySelectorAll('.ashby-application-form-input-autocomplete-popup');
  for (const popup of popups) {
    if (popup.matches('[class*="popup-loading"]') || popup.querySelector('[class*="popup-loading"]')) {
      loading = true;
    }
    for (const node of popup.querySelectorAll('[role="option"]')) {
      options.push((node.textContent || '').trim());
    }
  }
  return { options, loading };
}
"""

# A spot on a label that a press is safe on, as {x, y} from the label's padding box: the
# label's text, clear of any link in it (a press on "Arbitration Agreement" would open the
# agreement, not tick the box). Null when no such spot exists.
_LABEL_POINT_JS = r"""
(label) => {
  const style = getComputedStyle(label);
  const borderLeft = parseInt(style.borderLeftWidth, 10) || 0;
  const borderTop = parseInt(style.borderTopWidth, 10) || 0;
  const box = label.getBoundingClientRect();
  const lines = Array.from(label.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
  const avoid = [];
  for (const node of label.querySelectorAll('a, button, [role="link"], [role="button"]')) {
    for (const r of node.getClientRects()) {
      if (r.width > 0 && r.height > 0) {
        avoid.push({ left: r.left - 1, top: r.top - 1, right: r.right + 1, bottom: r.bottom + 1 });
      }
    }
  }
  const free = (x, y) =>
    !avoid.some((r) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom);
  for (const line of lines) {
    const middle = line.left + line.width / 2;
    const columns = [];
    for (let x = line.left + 2; x < line.right - 1; x += 4) columns.push(x);
    columns.sort((a, b) => Math.abs(a - middle) - Math.abs(b - middle));
    for (const fraction of [0.5, 0.25, 0.75]) {
      const y = line.top + line.height * fraction;
      for (const x of columns) {
        if (free(x, y)) return { x: x - box.left - borderLeft, y: y - box.top - borderTop };
      }
    }
  }
  return null;
}
"""


def _css_string(value: str) -> str:
    """`value` as the inside of a double-quoted CSS attribute value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_date(text: Any) -> datetime.date | None:
    """The date `text` spells as 2027-01-04, 01/04/2027 or "January 4, 2027", else None."""
    shown = " ".join(str(text or "").split())
    for form in _DATE_INPUT_FORMATS:
        try:
            return datetime.datetime.strptime(shown, form).date()
        except ValueError:
            continue
    return None


def _ordinal(day: int) -> str:
    """4 -> "4th", 21 -> "21st": how a calendar's day cell spells the day."""
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _widget(field: FieldSpec) -> str:
    """Which Ashby widget the autoapply scan found `field` in ("" for any other)."""
    for name in _POSITIONAL:
        if field.selector.startswith(f"{_WIDGET_CSS[name]} >> nth="):
            return name
    return ""


def _require_guard(page: Any) -> None:
    """Refuse to touch an answer control on a page without the dry-run submit guard."""
    if not submit_guard.installed(page):
        raise RuntimeError(
            "the dry-run submit guard is not installed on this page; refusing to click"
        )


def _click_answer(page: Any, locator: Any, *, position: dict[str, float] | None = None) -> None:
    """The one click in this module: one answer control, behind the submit guard. The
    guard marks the click as an answer's, so a submit it starts is still prevented."""
    _require_guard(page)
    submit_guard.click_answer(page, locator, timeout=_ACTION_TIMEOUT_MS, position=position)


def _entry_title(loc: Any) -> str | None:
    """The normalized question title of the one widget `loc` finds, None for no or many."""
    if loc.count() != 1:
        return None
    return _norm(loc.evaluate(_WIDGET_TITLE_JS, timeout=_READBACK_TIMEOUT_MS))


def _widget_locator(page: Any, field: FieldSpec) -> Any:
    """The widget the scan saw as `field`: found again by its position, and trusted only
    if it still carries the scan's question. If the form has moved on since, the one
    widget now carrying that question; never a guess."""
    want = _norm(field.label)
    found = page.locator(field.selector)
    if _entry_title(found) == want:
        return found
    every = page.locator(_WIDGET_CSS[_widget(field)])
    same = [every.nth(i) for i in range(every.count()) if _entry_title(every.nth(i)) == want]
    if len(same) == 1:
        return same[0]
    raise ValueError(f"the question {field.label!r} is no longer where the scan saw it")


def _fill_yes_no(page: Any, field: FieldSpec, expected: str) -> None:
    """Press the Yes or No button that is the answer. One that is already pressed is
    left alone: Ashby turns a pressed button back off."""
    _require_guard(page)
    group = _widget_locator(page, field)
    buttons = group.locator("button")
    want = _norm(expected)
    for i in range(buttons.count()):
        button = buttons.nth(i)
        if _norm(button.inner_text(timeout=_READBACK_TIMEOUT_MS)) != want:
            continue
        if button.get_attribute("aria-pressed", timeout=_READBACK_TIMEOUT_MS) == "true":
            return
        _click_answer(page, button)
        return
    raise ValueError(f"no Yes/No button matched {expected!r}")


def _label_of(page: Any, control: Any) -> Any:
    """The label that toggles `control`: its `label[for]`, else a label around it, else
    the control itself."""
    elem_id = control.get_attribute("id", timeout=_READBACK_TIMEOUT_MS)
    if elem_id:
        by_for = page.locator(f'label[for="{_css_string(elem_id)}"]')
        if by_for.count() == 1:
            return by_for
    around = control.locator("xpath=ancestor::label[1]")
    if around.count() == 1:
        return around
    return control


def _click_control(page: Any, control: Any) -> None:
    """Toggle a radio or checkbox by pressing its label, never the control. A label
    press cannot submit, and it works on a control the page hides. It aims clear of any
    link in the label."""
    label = _label_of(page, control)
    spot = label.evaluate(_LABEL_POINT_JS, timeout=_READBACK_TIMEOUT_MS)
    if not spot:
        raise ValueError("no spot to press on the label is clear of its links; not clicking")
    _click_answer(page, label, position=spot)


def _pick_hidden_radio(page: Any, radio: Any) -> None:
    """Select a radio the page hides behind its label. One that is already selected is
    left alone: Ashby unchecks a radio that is pressed while checked."""
    _require_guard(page)
    if radio.is_checked(timeout=_READBACK_TIMEOUT_MS):
        return
    _click_control(page, radio)


def _check_box(page: Any, field: FieldSpec) -> None:
    """Tick the box of a required acknowledgment (action "check"), by pressing its label.
    Not `.check()`, which this module keeps for radios (`_check_radio`)."""
    _require_guard(page)
    box = page.locator(field.selector).first
    if box.is_checked(timeout=_READBACK_TIMEOUT_MS):
        return
    _click_control(page, box)


def _autocomplete_queries(expected: str) -> list[str]:
    """What to type into a location box, in turn: the first part of the answer ("Atlanta"
    of "Atlanta, Georgia", which its list answers most often), then the whole answer."""
    whole = " ".join(expected.split())
    first = whole.split(",")[0].strip()
    return [first, whole] if first and first != whole else [whole]


def _autocomplete_hits(page: Any, want: str, place: bool) -> tuple[list[str], list[int]]:
    """Wait for the location box's list to settle: its option texts, and the positions
    of the ones that are the answer (`want` is `_norm(answer)`)."""
    texts: list[str] = []
    hits: list[int] = []
    for _ in range(_OPTION_POLL_STEPS):
        state = page.evaluate(_AUTOCOMPLETE_OPTIONS_JS)
        texts = [str(text) for text in state["options"]]
        hits = [i for i, text in enumerate(texts) if _option_matches(want, text, place=place)]
        if hits and not state["loading"]:
            break
        _pause(_OPTION_POLL_S)
    return texts, hits


def _clear_autocomplete(box: Any) -> None:
    """Best effort after a failed pick: empty the box, so typed text never passes for an
    answer (leaving it resets the box to its last pick)."""
    try:
        box.fill("", timeout=_ACTION_TIMEOUT_MS)
        box.press("Tab", timeout=_ACTION_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — the pick's own error is the one to report
        return


def _fill_autocomplete(page: Any, field: FieldSpec, expected: str, *, place: bool) -> None:
    """Fill Ashby's location box: type, then press the one list option that is the answer.
    Typing alone sets nothing (the box falls back to its last pick when it loses focus),
    and Enter, which would pick, is never pressed. Two options spelling the answer, or
    none, is a failed fill."""
    _require_guard(page)
    box = _widget_locator(page, field)
    want = _norm(expected)
    state = box.evaluate(_COMBOBOX_STATE_JS, timeout=_READBACK_TIMEOUT_MS)
    if not state["expanded"] and _option_matches(want, state["value"], place=place):
        return  # already the answer
    try:
        for typed in _autocomplete_queries(expected):
            box.fill(typed, timeout=_ACTION_TIMEOUT_MS)
            texts, hits = _autocomplete_hits(page, want, place)
            if hits:
                break
        else:
            raise ValueError(_no_option_text(expected))
        if len(hits) > 1:
            raise ValueError(_no_option_text(expected) + _CROWDED_SUFFIX)
        target = page.locator(_AUTOCOMPLETE_OPTIONS).nth(hits[0])
        if _norm(target.text_content(timeout=_READBACK_TIMEOUT_MS)) != _norm(texts[hits[0]]):
            raise ValueError("the list changed under the pick; not clicking")
        _click_answer(page, target)
        box.press("Tab", timeout=_ACTION_TIMEOUT_MS)  # leaving the box proves the pick held
    except Exception:
        _clear_autocomplete(box)
        raise


def _input_text(box: Any) -> str:
    return box.input_value(timeout=_READBACK_TIMEOUT_MS)


def _date_readback(box: Any) -> str:
    """The date the box shows, as ISO text (its own text if that is no date)."""
    text = _input_text(box)
    shown = _parse_date(text)
    return shown.isoformat() if shown else text


def _popup_month(popup: Any) -> tuple[int, int]:
    """(year, month) the open calendar shows, read from its heading ("January 2027")."""
    heading = popup.locator("h4").first.inner_text(timeout=_READBACK_TIMEOUT_MS)
    found = re.search(r"([A-Za-z]+)\s+(\d{4})", heading)
    if not found or found.group(1).casefold() not in _MONTHS:
        raise ValueError(f"cannot read the month the calendar shows: {heading!r}")
    return int(found.group(2)), _MONTHS.index(found.group(1).casefold()) + 1


def _pick_date(page: Any, box: Any, want: datetime.date) -> None:
    """Choose `want` in the date box's calendar: its month buttons to the month, then
    the day's cell. Used only when typing the date did not take."""
    box.focus(timeout=_ACTION_TIMEOUT_MS)  # the calendar opens when the box takes focus
    popup = page.locator(_DATE_POPUP).first
    popup.wait_for(state="visible", timeout=_ACTION_TIMEOUT_MS)
    target = (want.year, want.month)
    clicks = 0
    while (shown := _popup_month(popup)) != target:
        if clicks >= _MAX_MONTH_CLICKS:
            raise ValueError(f"the calendar did not reach {want.strftime('%B %Y')}")
        direction = "next" if shown < target else "previous"
        _click_answer(page, popup.locator(f'button[data-direction="{direction}"]'))
        clicks += 1
    name = f"{want.strftime('%B')} {_ordinal(want.day)}, {want.year}"
    cell = popup.locator(
        f'[aria-label*="{_css_string(name)}"]:not(.react-datepicker__day--outside-month)'
    )
    if cell.count() != 1:
        raise ValueError(f"no single day cell for {name!r} in the calendar")
    if "react-datepicker__day--selected" in (
        cell.get_attribute("class", timeout=_READBACK_TIMEOUT_MS) or ""
    ):
        return  # already chosen: pressing the chosen day clears it
    _click_answer(page, cell)


def _fill_date(page: Any, field: FieldSpec, expected: str) -> str:
    """Put `expected` (a date) into Ashby's date box: typed in the box's own format and
    committed with Tab, which closes the calendar. The calendar's buttons are pressed
    only if typing did not take. Returns the date as ISO text, what the read-back shows."""
    _require_guard(page)
    want = _parse_date(expected)
    if want is None:
        raise ValueError(f"not a date: {expected!r}")
    box = _widget_locator(page, field)
    if _parse_date(_input_text(box)) == want:
        return want.isoformat()  # already the answer
    box.fill(want.strftime(_DATE_FORMAT), timeout=_ACTION_TIMEOUT_MS)
    box.press("Tab", timeout=_ACTION_TIMEOUT_MS)
    if _parse_date(_input_text(box)) != want:
        _pick_date(page, box, want)
    return want.isoformat()


def _adapt_to_date_picker(resolutions: list[Resolution]) -> list[Resolution]:
    """A start-date picker takes a date, not text like "ASAP": it gets
    `autoapply_start_date()` (Q7) whatever answer is stored. With no earliest start set,
    or for any other date picker, the resolution is left to resolve()."""
    start = autoapply_start_date()
    if start is None:
        return list(resolutions)
    adapted: list[Resolution] = []
    for res in resolutions:
        if (
            _widget(res.field) == "date"
            and res.semantic_key == "start_date"
            and res.action in ("fill", "ask")
            and not res.draft
        ):
            res = replace(
                res,
                action="fill",
                value=start.isoformat(),
                reason="start date picker: the standing earliest start",
            )
        adapted.append(res)
    return adapted


def _check_as_ask(res: Resolution) -> Resolution:
    """Outside autoapply a "check" is only a question: nothing here ticks a box."""
    if res.action != "check":
        return res
    return replace(res, action="ask", reason=res.reason or "checkbox — never auto-checked")


def apply_resolutions(
    page: Any,
    resolutions: list[Resolution],
    settle_ms: int = 150,
    *,
    autoapply: bool = False,
    type_drafts: bool = False,
) -> list[FillOutcome]:
    """Execute each fill/upload Resolution against the live page, then read
    the value back to verify it stuck. Never touches ask/skip resolutions.

    After the first pass, one verify pass re-reads every non-upload outcome
    that had no error (React can hydrate or re-render after the first
    read-back and wipe a value) and re-applies a field whose value is gone,
    exactly once.

    `autoapply` (the armed lane) also executes "check" resolutions, and lets a
    fill click the form's answer controls behind the submit guard (module docstring).

    `type_drafts` (the handoff lane only) also types the AI-drafted answers
    (`draft=True`) through the same apply-and-verify path as any other fill.
    The candidate's own Submit click in that tab is his approval of the text (spec
    2026-10-23-autoapply.md, 2026-10-27 amendment). Without it a draft is never
    typed here.
    """
    actions = ("fill", "upload", "check") if autoapply else ("fill", "upload")
    lane = {"autoapply": True} if autoapply else {}
    applied: list[tuple[Resolution, FillOutcome]] = []
    for res in resolutions:
        if res.action not in actions:
            continue
        # Drafted answers execute only via apply_one, after explicit
        # per-draft human approval in the CLI review loop, never here. The one
        # exception is the handoff lane (`type_drafts`), where the candidate's Submit
        # click in the tab is the approval.
        if res.draft and not type_drafts:
            continue
        applied.append((res, _apply_one(page, res, settle_ms, **lane)))
    if not applied:
        return []
    _pause(_VERIFY_DELAY_S)
    return [_verify_one(page, res, outcome, settle_ms, **lane) for res, outcome in applied]


def _verify_one(
    page: Any,
    res: Resolution,
    outcome: FillOutcome,
    settle_ms: int,
    *,
    autoapply: bool = False,
) -> FillOutcome:
    if res.action == "upload" or outcome.error:
        return outcome
    try:
        readback, react_select = _read_back_state(page, outcome.field)
    except Exception as exc:  # noqa: BLE001 — a field that cannot be re-read is not verified
        return replace(outcome, committed=False, error=f"verify read failed: {exc}")
    place = solve.is_place_key(res.semantic_key)
    if not _matches(outcome.value, readback, react_select, outcome.picked, place=place):
        lane = {"autoapply": True} if autoapply else {}
        return _apply_one(page, res, settle_ms, **lane)  # the one and only retry
    if outcome.committed and outcome.readback == readback:
        return outcome
    return replace(outcome, readback=readback, committed=True)


def apply_one(
    page: Any, res: Resolution, settle_ms: int = 150, *, autoapply: bool = False
) -> FillOutcome:
    """Apply a single Resolution (including drafts) after explicit human
    approval. Thin public wrapper around _apply_one for the CLI review loop."""
    lane = {"autoapply": True} if autoapply else {}
    return _apply_one(page, res, settle_ms, **lane)


def _apply_one(
    page: Any, res: Resolution, settle_ms: int, *, autoapply: bool = False
) -> FillOutcome:
    field = res.field
    expected = res.value or ""
    place = solve.is_place_key(res.semantic_key)  # location / country / the posting's city: see solve.same_place
    picks: list[str] = []  # the option text a Tab committed in this call, if one did
    widget = _widget(field)  # "" unless the autoapply scan found it in an Ashby widget

    def outcome(readback: str, committed: bool, error: str = "") -> FillOutcome:
        return FillOutcome(
            field=field,
            semantic_key=res.semantic_key,
            value=expected,
            readback=readback,
            committed=committed,
            error=error,
            picked=picks[0] if picks else "",
        )

    try:
        if res.action == "check":
            if not autoapply or field.kind != "checkbox":
                return outcome("", False, "check is only done by the autoapply lane, on a checkbox")
            expected = _CHECKED  # the value the read-back, outcome and verify check
            _check_box(page, field)
        elif res.action == "upload":
            loc = page.locator(field.selector).first
            _install_upload_record(loc, field)
            loc.set_input_files(expected, timeout=_ACTION_TIMEOUT_MS)
        elif widget and not autoapply:
            return outcome("", False, "an Ashby widget is only filled by the autoapply lane")
        elif widget == "yesno":
            _fill_yes_no(page, field, expected)
        elif widget == "autocomplete":
            _fill_autocomplete(page, field, expected, place=place)
        elif widget == "date":
            expected = _fill_date(page, field, expected)  # ISO text, as the read-back shows it
        elif field.kind == "select":
            page.locator(field.selector).first.select_option(
                label=expected, timeout=_ACTION_TIMEOUT_MS
            )
        elif field.kind == "radio":
            _check_radio(page, field, expected, autoapply=autoapply)
        elif field.kind in _TEXT_KINDS:
            loc = page.locator(field.selector).first
            combobox = _detect_react_select(loc)
            if combobox is None and res.option_rule in solve.MENU_ONLY_RULES:
                # A demographic or acknowledgment answer is an option of a menu; a
                # plain box has none, and typing the placeholder into it would be wrong.
                return outcome("", False, "not a menu: answer by hand")
            if combobox is None:
                loc.fill(expected, timeout=_ACTION_TIMEOUT_MS)
            elif res.option_rule:
                problem, expected = _commit_rule_pick(loc, res, expected, combobox, picks)
                if problem:
                    return outcome("", False, problem)
            else:
                problem = _commit_react_select(loc, expected, combobox, picks, place=place)
                if problem:
                    country = (
                        _commit_country_fallback(loc, res, expected, problem, picks)
                        if place
                        else ""
                    )
                    if not country:
                        return outcome("", False, problem)
                    expected = country  # the value the read-back, outcome and verify check
        else:
            return outcome("", False, f"unsupported kind for fill: {field.kind}")
    except Exception as exc:  # noqa: BLE001 — surface any Playwright error per-field
        return outcome("", False, str(exc))

    if settle_ms:
        time.sleep(settle_ms / 1000)

    try:
        readback, react_select = _read_back_state(page, field)
    except Exception as exc:  # noqa: BLE001
        return outcome("", False, str(exc))

    if res.action == "upload":
        # The input may be gone by now; the read-back came from the upload record.
        committed = readback == Path(expected).name
    else:
        committed = _matches(
            expected, readback, react_select, picks[0] if picks else "", place=place
        )
    return outcome(readback, committed)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


# Greenhouse's phone block has its own Country react-select whose options carry
# the dial code: "United States +1", "United Kingdom +44", sometimes with no
# space ("Afghanistan+93"). The stored answer is the bare country name, so one
# trailing dial code is the single decoration an option may wear and still be
# the answer. Nothing else is tolerated: no prefix, substring or fuzzy match,
# so "United States Minor Outlying Islands +1" and "United Kingdom +44" are
# never mistaken for "United States", nor "United States +1" for "United".
_DIAL_CODE = re.compile(r"\s*\+\d{1,4}$")


def _option_matches(want_norm: str, option_text: Any, *, place: bool = False) -> bool:
    """Whether a react-select option (or the committed value it left behind) is
    the answer. `want_norm` is `_norm(answer)`. True when the normalized option
    equals it, or equals it once ONE trailing dial code is removed from the
    option (never from the answer). A bare "+1" names no one, so it is never the
    answer to a non-empty one.

    `place` (the answer is a location or country) adds one more equality: the
    option, dial code removed, spells the same place (solve.same_place), so
    "Atlanta, GA, USA" is "Atlanta, Georgia" and "United States of America" is
    "United States". Still equality: "United States Minor Outlying Islands" and
    "Georgia" (the country) are not "United States"."""
    option = _norm(option_text)
    if option == want_norm:
        return True
    if not want_norm:
        return False
    bare = _DIAL_CODE.sub("", option, count=1)
    return bare == want_norm or (place and solve.same_place(want_norm, bare))


def _matches(
    expected: str, readback: str, react_select: bool, picked: str = "", *, place: bool = False
) -> bool:
    """Whether `readback` shows that the field holds `expected`. `picked` is the
    option text this fill committed with Tab ("" when it pressed none).

    A combobox offers fixed options: only the exact option counts, bar a trailing
    dial code (see _option_matches). One page shows the pick differently. On
    Greenhouse's phone-block Country the committed option reads back as a flag
    plus its dial code ("+1"), never its name, so no read-back of it can equal
    "United States". The option's name is verified at the Tab, which only goes to
    a focused option that _option_matches accepts (that option's text is
    `picked`); the read-back then confirms the pick held, through that option's
    code. So the read-back also counts when `picked` is such an option, ends in a
    dial code, and the read-back is exactly that code. An empty read-back, another
    code, a `picked` without a code, and a code with nothing picked all still
    fail. `place` is as for _option_matches."""
    if not react_select:
        return solve.values_match(expected, readback)
    want = _norm(expected)
    if _option_matches(want, readback, place=place):
        return True
    code = _DIAL_CODE.search(_norm(picked))
    if code is None or not _option_matches(want, picked, place=place):
        return False
    return _norm(readback) == _norm(code.group())


def _install_upload_record(loc: Any, field: FieldSpec) -> None:
    """Best effort: have the page record the files the input receives, before
    the page can remove the input. Installs listeners only; dispatches nothing."""
    try:
        loc.evaluate(
            _RECORD_UPLOAD_JS,
            [UPLOAD_RECORD_KEY, field.selector],
            timeout=_READBACK_TIMEOUT_MS,
        )
    except Exception:  # noqa: BLE001 — the read-back falls back to el.files
        return


def _detect_react_select(loc: Any) -> dict | None:
    """The combobox state {multi, readonly, value, expanded} when `loc` is a
    react-select input, else None. Any failure means "not a react-select", so the plain fill
    path (and any test fake without evaluate) is unaffected."""
    try:
        state = loc.evaluate(_REACT_SELECT_JS, timeout=_READBACK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return None
    return state if isinstance(state, dict) else None


def _focused_option(loc: Any) -> str:
    try:
        text = loc.evaluate(_FOCUSED_OPTION_JS, timeout=_READBACK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — treated as "no focused option yet"
        return ""
    return text if isinstance(text, str) else ""


def _wait_for_focused_option(loc: Any) -> str:
    for _ in range(_OPTION_POLL_STEPS):
        text = _focused_option(loc)
        if text:
            return text
        _pause(_OPTION_POLL_S)
    return ""


def _arrow_down(loc: Any, previous: str) -> str:
    loc.press("ArrowDown", timeout=_ACTION_TIMEOUT_MS)
    text = _focused_option(loc)
    if text == previous:  # the menu may not have re-rendered yet: look once more
        _pause(_OPTION_POLL_S)
        text = _focused_option(loc)
    return text


def _menu_is_closed(loc: Any) -> bool:
    """Whether the combobox itself reports a closed menu (aria-expanded="false").
    Only that positive report counts: an open menu that lists nothing ("No
    options"), or an input that reports nothing, is not a closed menu."""
    state = _detect_react_select(loc)
    return state is not None and state.get("expanded") is False


def _typeable(expected: str) -> str:
    """`expected` as plain words one space apart, or "" when it must not be
    typed at all. Typing is key presses, so this fails closed on the stored text
    itself, before anything is collapsed or trimmed: a newline would be Enter
    (which can submit the form), a tab character Tab, an escape character
    Escape. A control character (Cc), a format character (Cf: zero-width and
    direction marks), a line or paragraph separator, any space that is not an
    ASCII space, or any other unprintable character anywhere in it means
    nothing is typed. Only then are runs of spaces collapsed, as _norm does. No
    option label is long enough to be worth typing a paragraph."""
    raw = str(expected or "")
    if not raw.isprintable():
        return ""
    text = " ".join(raw.split())
    return text if len(text) <= _MAX_TYPED_CHARS else ""


def _menu_options(loc: Any) -> list[str] | None:
    """The texts of the options in the open menu, or None when they cannot be read."""
    try:
        texts = loc.evaluate(_MENU_OPTIONS_JS, timeout=_READBACK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — unreadable means "not shown to be the only one"
        return None
    if isinstance(texts, list) and all(isinstance(text, str) for text in texts):
        return texts
    return None


def _menu_is_partial(loc: Any) -> bool:
    """Whether the open menu shows signs of a partial list: it says it has more options
    than it renders (aria-setsize or aria-rowcount above the rendered count), or it offers
    to load more. A single match in such a list proves nothing about the options not
    rendered, so a rule pick is not trusted. False when it cannot be read."""
    try:
        return bool(loc.evaluate(_MENU_PARTIAL_JS, timeout=_READBACK_TIMEOUT_MS))
    except Exception:  # noqa: BLE001 — unreadable: the other checks stand
        return False


def _spelling_options(loc: Any, want: str, focused: str) -> int:
    """How many options in the open menu spell the answer's place (`want` is
    `_norm(answer)`; _option_matches with place). 0 when that cannot be shown:
    the menu cannot be read, it does not list `focused` (the option the keys act
    on), or an option equals the answer outright (that one wins, and the arrow
    scan reaches it)."""
    options = _menu_options(loc)
    if not options or _norm(focused) not in {_norm(text) for text in options}:
        return 0
    if any(_option_matches(want, text) for text in options):
        return 0
    return sum(_option_matches(want, text, place=True) for text in options)


def _offer(loc: Any, accept: Callable[[str], bool]) -> str:
    """The option the menu has focused when `accept` takes it, else ""."""
    seen = _focused_option(loc)
    if not seen:  # the menu may not have re-rendered yet: look once more
        _pause(_OPTION_POLL_S)
        seen = _focused_option(loc)
    return seen if seen and accept(seen) else ""


def _type_like_a_person(
    loc: Any, expected: str, accept: Callable[[str], bool] | None = None
) -> str:
    """Open the menu with ArrowDown and type `expected` key by key, for a
    react-select that ignores `fill()` because it opens and filters only on real
    key events. Returns the text of the option left focused, or "" when nothing
    was typed or nothing is focused.

    Only the answer's own characters are typed (_typeable), at a person's pace.
    A Space is never sent into an empty box: react-select reads it as "select
    the focused option", which would commit whatever the menu opened on. The
    words therefore go in one at a time, and the box has to hold text before each
    following Space; a first word that did not land stops the typing.

    `accept`, when given, is asked about the option focused when the menu first
    opens and after each word but the last, and typing stops at the first one it
    accepts: a later word can filter the answer's place away ("Georgia" drops
    "Atlanta, GA"). It adds no key; the option returned is the focused one, as
    always.
    """
    text = _typeable(expected)
    if not text:
        return ""
    loc.fill("", timeout=_ACTION_TIMEOUT_MS)  # drop what the ignored fill() left behind
    loc.focus(timeout=_ACTION_TIMEOUT_MS)
    loc.press("ArrowDown", timeout=_ACTION_TIMEOUT_MS)  # opens the menu
    if accept is not None:  # nothing typed yet: the menu lists every option
        taken = _offer(loc, accept)
        if taken:
            return taken
    words = text.split(" ")
    for index, word in enumerate(words):
        if index:
            if not loc.input_value(timeout=_READBACK_TIMEOUT_MS):
                return ""
            word = " " + word
        loc.press_sequentially(
            word, delay=_KEY_DELAY_MS, timeout=_ACTION_TIMEOUT_MS + len(word) * _KEY_DELAY_MS
        )
        if accept is not None and index < len(words) - 1:
            taken = _offer(loc, accept)
            if taken:
                return taken
    return _wait_for_focused_option(loc)


_CROWDED_SUFFIX = " (two or more options spell it)"
_NO_RULE_OPTION = "the menu has no single option the rule names"
_PARTIAL_MENU = "the menu looks like a partial list (more options than shown)"


def _no_option_text(expected: str) -> str:
    """What _commit_react_select returns when the open menu has no option for
    `expected` at all. A menu that spells the place twice returns this plus
    _CROWDED_SUFFIX, so it starts the same way but is not equal to it."""
    return f"no option exactly matching {expected!r}"


def _commit_country_fallback(
    loc: Any, res: Resolution, expected: str, problem: str, picks: list[str]
) -> str:
    """For a residence question (a location Resolution that carries
    fallback_value, the stored country; no other key does), commit the country in
    the same react-select when `problem` says its menu has no option for the
    location answer `expected`, and nothing else. Returns the country that was
    committed, or "" when nothing was (the caller then reports `problem`, the
    location answer's own).

    Only on that exact text: not when two options spelled the place (the answer
    was there, twice), not for a multi-select, not for any other failure. The
    country goes through the same commit as any answer, with `place` on, so the
    same keys are sent (ArrowDown, the answer's own characters, Tab) and Tab only
    while the focused option is the country, spelled as the list spells it
    ("United States of America"). The city's try can leave the menu open on every
    option, and a box that ignores fill() would then be scanned from its top rather
    than typed into, so an open menu is closed first by taking focus off the box (no
    key: react-select drops the typed text on blur and selects nothing). Any error
    leaves the location problem."""
    country = res.fallback_value
    if (
        res.semantic_key != "location"
        or not isinstance(country, str)
        or not _norm(country)
        or not _norm(expected)
        or problem != _no_option_text(expected)
    ):
        return ""
    try:
        combobox = _detect_react_select(loc)
        if combobox is not None and combobox.get("expanded"):
            loc.blur(timeout=_ACTION_TIMEOUT_MS)
            combobox = _detect_react_select(loc)
        if combobox is None:
            return ""
        if _commit_react_select(loc, country, combobox, picks, place=True):
            return ""
    except Exception:  # noqa: BLE001 — the location answer's problem stands
        return ""
    return country


def _commit_rule_pick(
    loc: Any, res: Resolution, expected: str, combobox: dict, picks: list[str]
) -> tuple[str, str]:
    """For a react-select whose options solve.py could not see (res.option_rule): open
    the menu, read every option, and commit the one solve.pick_option names, through the
    same exact-text commit as any answer. Returns (problem, answer): "" and the option
    committed, or the reason nothing was and `expected` unchanged. A rule that names no
    option, or two, leaves the field untouched (the menu is closed by taking focus off
    the box, which selects nothing)."""
    if combobox.get("multi"):
        return "multi-select combobox not supported", expected
    chosen = None
    try:
        loc.focus(timeout=_ACTION_TIMEOUT_MS)
        loc.press("ArrowDown", timeout=_ACTION_TIMEOUT_MS)  # opens the menu
        _wait_for_focused_option(loc)
        options = _menu_options(loc)
        if options:
            chosen = solve.pick_option(res.option_rule, res.option_args, options)
            # A partial (virtualized or load-more) list may hold a second match that
            # is not rendered: the field is asked rather than trusting one match.
            if chosen is not None and _menu_is_partial(loc):
                loc.blur(timeout=_ACTION_TIMEOUT_MS)
                return _PARTIAL_MENU, expected
        # Closed again either way (blur selects nothing), so the commit starts from the
        # state any answer does: a menu that renders late must not show its old list.
        loc.blur(timeout=_ACTION_TIMEOUT_MS)
        if chosen is None:
            return _NO_RULE_OPTION, expected
    except Exception as exc:  # noqa: BLE001 — surface any Playwright error per-field
        return str(exc), expected
    problem = _commit_react_select(loc, chosen, combobox, picks)
    return problem, (expected if problem else chosen)


def _commit_react_select(
    loc: Any,
    expected: str,
    combobox: dict,
    picked: list[str] | None = None,
    *,
    place: bool = False,
) -> str:
    """Pick, in a react-select combobox, the option whose text exactly equals
    `expected`, or equals it plus one trailing dial code ("United States +1" for
    "United States"; see _option_matches). Returns "" on success, else the
    reason nothing was picked. When `picked` is a list, the text of the option
    this call committed with Tab is appended to it (nothing is, when the value
    already was the answer and no key was sent), for _matches to check the
    read-back against.

    `place` (`expected` is a location or country) also lets Tab commit the one
    option in the open menu that spells the same place ("Atlanta, GA, USA" for
    "Atlanta, Georgia"). Never when any read of the menu showed two options that
    do (typing narrows the menu, so a later read must not forget an earlier one),
    nor while an option equals the answer outright.

    Typing alone commits nothing: react-select clears typed text on blur. The
    option has to be the focused one in the open menu and be committed with Tab.
    fill() types `expected` itself: the filter matches the bare name, not the
    name with its dial code. Some comboboxes ignore fill() and leave the menu
    closed (Greenhouse's Country); only then is the answer typed key by key
    instead (_type_like_a_person).

    The keys ever sent are ArrowDown (open the menu, move focus), the answer's
    own printable characters (fallback only) and Tab, and Tab only while the
    focused option's text is the answer (_option_matches), so no other option
    can be picked. Never Enter.
    """
    want = _norm(expected)
    crowded = False  # some read of the menu showed two options that spell the place

    def acceptable(option: str) -> bool:
        """Whether Tab may commit the focused `option`."""
        nonlocal crowded
        if _option_matches(want, option):
            return True
        if not place:
            return False
        spelled = _spelling_options(loc, want, option)
        crowded = crowded or spelled > 1
        return spelled == 1 and not crowded and _option_matches(want, option, place=True)

    if combobox.get("multi"):
        return "multi-select combobox not supported"
    if _option_matches(want, combobox.get("value"), place=place):
        return ""
    no_match = _no_option_text(expected)
    if not want:
        return no_match
    searchable = not combobox.get("readonly")
    if searchable:
        loc.fill(expected, timeout=_ACTION_TIMEOUT_MS)
    else:
        loc.focus(timeout=_ACTION_TIMEOUT_MS)
        loc.press("ArrowDown", timeout=_ACTION_TIMEOUT_MS)  # opens the menu
    focused = _wait_for_focused_option(loc)
    if searchable and not focused and _menu_is_closed(loc):
        focused = _type_like_a_person(loc, expected, acceptable if place else None)
    first = _norm(focused)
    for presses in range(_MAX_ARROW_PRESSES + 1):
        if focused and acceptable(focused):
            loc.press("Tab", timeout=_ACTION_TIMEOUT_MS)
            if picked is not None:
                picked.append(focused)
            return ""
        if not focused or presses == _MAX_ARROW_PRESSES:
            break
        focused = _arrow_down(loc, focused)
        if _norm(focused) == first:  # wrapped around: every option has been seen
            break
    if searchable:
        loc.fill("", timeout=_ACTION_TIMEOUT_MS)  # clear the filter; blur selects nothing
    # Two options spelling the place is not "no option": the place is in the
    # menu, twice, and _commit_country_fallback must not read that as a miss.
    return (no_match + _CROWDED_SUFFIX) if crowded else no_match


def _check_radio(page: Any, field: FieldSpec, expected: str, *, autoapply: bool = False) -> None:
    """Check the radio input in this group whose associated label text
    equals `expected`. .check() selects a radio — it is not a submit. A radio the
    page hides behind its label (autoapply only) is selected by pressing that label
    behind the submit guard instead (`_pick_hidden_radio`)."""
    inputs = page.locator(field.selector)
    count = inputs.count()
    expected_cf = expected.strip().casefold()
    for i in range(count):
        radio = inputs.nth(i)
        label_text = (
            radio.evaluate(_RADIO_LABEL_JS, timeout=_READBACK_TIMEOUT_MS).strip().casefold()
        )
        if label_text == expected_cf:
            if autoapply and not radio.evaluate(_IS_SHOWN_JS, timeout=_READBACK_TIMEOUT_MS):
                _pick_hidden_radio(page, radio)
            else:
                radio.check(timeout=_ACTION_TIMEOUT_MS)
            return
    raise ValueError(f"no radio option matched {expected!r}")


_RADIO_LABEL_JS = """
(el) => {
  if (el.id) {
    const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
  }
  const closest = el.closest('label');
  if (closest && closest.innerText.trim()) return closest.innerText.trim();
  return el.value || '';
}
"""

_SELECT_READBACK_JS = """
(el) => {
  const opt = el.options[el.selectedIndex];
  return opt ? (opt.textContent || '').trim() : '';
}
"""

_RADIO_CHECKED_LABEL_JS = """
(name) => {
  const checked = document.querySelector(`input[type="radio"][name="${CSS.escape(name)}"]:checked`);
  if (!checked) return '';
  if (checked.id) {
    const lbl = document.querySelector(`label[for="${CSS.escape(checked.id)}"]`);
    if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
  }
  const closest = checked.closest('label');
  if (closest && closest.innerText.trim()) return closest.innerText.trim();
  return checked.value || '';
}
"""


# Installed on a file input just before set_input_files. Capture-phase listeners
# on the input itself run before any handler the page registered, so the record
# is written before the page can remove the input. Listeners only: this never
# dispatches an event.
_RECORD_UPLOAD_JS = """
(el, [key, selector]) => {
  const store = (window[key] = window[key] || {});
  const record = () => {
    const files = el.files ? Array.from(el.files) : [];
    store[selector] = {
      name: files.length ? files[0].name : '',
      size: files.length ? files[0].size : 0,
      count: files.length,
    };
  };
  el.addEventListener('input', record, true);
  el.addEventListener('change', record, true);
}
"""

# Page-level (not locator-level) so a removed input does not make Playwright
# wait out its auto-wait: the live input's files, else the upload record.
_FILE_READBACK_JS = """
([key, selector]) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (e) { el = null; }
  if (el && el.files && el.files.length) return el.files[0].name;
  const store = window[key];
  const rec = store && store[selector];
  if (rec && rec.count >= 1) return rec.name || '';
  return '';
}
"""

# react-select (v5) renders its search box as input[role=combobox] inside
# div.select__control. multi: several values allowed. readonly: not searchable.
# value: the committed single-value text ('' when nothing is selected).
# expanded: aria-expanded as a boolean (the menu is open), null when the input
# does not say, so that "closed" is only ever a positive report.
_REACT_SELECT_JS = """
(el) => {
  if (el.getAttribute('role') !== 'combobox') return null;
  const control = el.closest('[class*="select__control"]');
  if (!control) return null;
  const single = control.querySelector('[class*="single-value"], [class*="singleValue"]');
  const expanded = el.getAttribute('aria-expanded');
  return {
    multi: !!control.querySelector('[class*="value-container--is-multi"]'),
    readonly: !!el.readOnly || el.getAttribute('aria-readonly') === 'true',
    value: single ? (single.textContent || '').trim() : '',
    expanded: expanded === null ? null : expanded === 'true',
  };
}
"""

# Text of the option react-select currently has focused: the combobox's
# aria-activedescendant, else the first option-is-focused class. Never a
# document-wide [role=option] search (pages hold hundreds of hidden ones).
_FOCUSED_OPTION_JS = """
(el) => {
  const active = el.getAttribute('aria-activedescendant');
  let opt = active ? document.getElementById(active) : null;
  if (!opt) opt = document.querySelector('[class*="option--is-focused"]');
  return opt ? (opt.textContent || '').trim() : '';
}
"""

# Texts of the options in the menu that holds the focused option, read only for
# _spelling_options (how many options spell the answer's place?).
# null when there is no focused option or no menu around it. A node inside
# another matched node is dropped so one option is not counted twice; anything
# unclear is over-read, never under-read, because a second option can only
# block a pick.
_MENU_OPTIONS_JS = """
(el) => {
  const active = el.getAttribute('aria-activedescendant');
  let focused = active ? document.getElementById(active) : null;
  if (!focused) focused = document.querySelector('[class*="option--is-focused"]');
  if (!focused) return null;
  const menu = focused.closest(
    '[role="listbox"], [class*="menu-list"], [class*="MenuList"], [class*="select__menu"]'
  );
  if (!menu) return null;
  const found = Array.from(menu.querySelectorAll('[role="option"], [class*="option"]'));
  const outer = found.filter(
    (node) => !found.some((other) => other !== node && other.contains(node))
  );
  return outer.map((node) => (node.textContent || '').trim());
}
"""


_MENU_PARTIAL_JS = """
(el) => {
  const active = el.getAttribute('aria-activedescendant');
  let focused = active ? document.getElementById(active) : null;
  if (!focused) focused = document.querySelector('[class*="option--is-focused"]');
  if (!focused) return false;
  const menu = focused.closest(
    '[role="listbox"], [class*="menu-list"], [class*="MenuList"], [class*="select__menu"]'
  );
  if (!menu) return false;
  const options = Array.from(menu.querySelectorAll('[role="option"]'));
  const claimed = Math.max(
    0,
    ...options.map((o) => parseInt(o.getAttribute('aria-setsize') || '0', 10) || 0),
    parseInt(menu.getAttribute('aria-rowcount') || '0', 10) || 0
  );
  if (claimed > options.length) return true;
  return /load\\s+more|show\\s+more|more\\s+(results|options)|loading|scroll\\s+(down\\s+)?to\\s+(load|see)/i
    .test(menu.textContent || '');
}
"""


def _read_back_state(page: Any, field: FieldSpec) -> tuple[str, bool]:
    """(read-back text, whether the field is a react-select combobox)."""
    widget = _widget(field)
    if widget == "yesno":
        group = _widget_locator(page, field)
        return group.evaluate(_YESNO_PRESSED_JS, timeout=_READBACK_TIMEOUT_MS), False
    if widget == "autocomplete":
        # Read as a combobox, so the pick counts by its option text (_option_matches). A
        # list still open means nothing was picked: typed text is no answer.
        state = _widget_locator(page, field).evaluate(
            _COMBOBOX_STATE_JS, timeout=_READBACK_TIMEOUT_MS
        )
        return ("" if state["expanded"] else str(state["value"])), True
    if widget == "date":
        return _date_readback(_widget_locator(page, field)), False
    if field.kind == "checkbox":
        box = page.locator(field.selector).first
        return (_CHECKED if box.is_checked(timeout=_READBACK_TIMEOUT_MS) else ""), False
    if field.kind == "select":
        loc = page.locator(field.selector).first
        return loc.evaluate(_SELECT_READBACK_JS, timeout=_READBACK_TIMEOUT_MS), False
    if field.kind == "radio":
        return page.evaluate(_RADIO_CHECKED_LABEL_JS, field.name or field.selector), False
    if field.kind == "file":
        return page.evaluate(_FILE_READBACK_JS, [UPLOAD_RECORD_KEY, field.selector]), False
    loc = page.locator(field.selector).first
    if field.kind in _TEXT_KINDS:
        combobox = _detect_react_select(loc)
        if combobox is not None:
            return str(combobox.get("value") or ""), True
    return loc.input_value(timeout=_READBACK_TIMEOUT_MS), False


def _read_back(page: Any, field: FieldSpec) -> str:
    return _read_back_state(page, field)[0]


# Why an essay stays `ask` when the form has an AI policy: it reaches needs-you
# with this reason, so the candidate sees why nothing was drafted.
AI_POLICY_REASON = "AI policy on form — answer by hand"


def _is_essay_ask(res: Resolution, *, autoapply: bool) -> bool:
    """A textarea left to the human that the drafter may take: one no semantic
    key fits ("unrecognized field", solve.resolve's reason for every essay in
    both lanes). The unattended lane also takes a cover-letter textarea, which
    solve.resolve holds back as a "cover_letter" key on a non-file field, and a
    single-line text input whose label asks for prose (asks_for_prose)."""
    if res.action != "ask":
        return False
    if res.field.kind == "text":
        # A short-prose question rendered as a one-line input ("In 1-2 sentences,
        # what's the most complex thing you've done with SQL?"). Unattended lane only.
        return (
            autoapply
            and res.reason == "unrecognized field"
            and asks_for_prose(tailor_question_text(res.field))
        )
    if res.field.kind != "textarea":
        return False
    if res.reason == "unrecognized field":
        return True
    return autoapply and res.semantic_key == "cover_letter"


def _apply_tailoring(
    conn: sqlite3.Connection,
    posting_id: int,
    url: str,
    resolutions: list[Resolution],
    budget: RunBudget | None,
    tailor_sink: list[TailorResult] | None,
    *,
    autoapply: bool = False,
) -> tuple[list[Resolution], list[Resolution]]:
    """Draft answers for eligible textarea fields left `ask` by resolve().
    Returns the (possibly updated) resolutions and a freshly recomputed
    residual list.

    The assisted lane drafts only for a posting whose current-rubric score
    reaches TAILOR_MIN_SCORE. The auto-apply lane (`autoapply`) has no score
    gate: its pool already decided, and its essays are required or optional
    alike.

    A form with any AI policy on it (a label about AI use or addressed to an AI
    or language model) gets no drafts in either lane: its essays stay `ask`
    with AI_POLICY_REASON."""
    if not autoapply:
        rubric_version = load_rubric()["version"]
        score_row = conn.execute(
            "SELECT total FROM scores WHERE posting_id = ? AND rubric_version = ?",
            (posting_id, rubric_version),
        ).fetchone()
        posting_score = score_row["total"] if score_row is not None else None

        if posting_score is None or posting_score < TAILOR_MIN_SCORE:
            return resolutions, [r for r in resolutions if r.action == "ask"]

    essay_idxs = [i for i, r in enumerate(resolutions) if _is_essay_ask(r, autoapply=autoapply)]

    if essay_idxs and ai_policy_label([r.field for r in resolutions]):
        for idx in essay_idxs:
            resolutions[idx] = replace(resolutions[idx], reason=AI_POLICY_REASON)
        return resolutions, [r for r in resolutions if r.action == "ask"]

    by_fp: dict[str, int] = {}
    for idx in essay_idxs:
        fp = tailor_fingerprint(tailor_question_text(resolutions[idx].field))
        by_fp.setdefault(fp, idx)

    eligible_fields = [resolutions[idx].field for idx in by_fp.values()]
    if eligible_fields:
        tailor_result = draft_for_posting(
            conn, posting_id, eligible_fields, budget, host=urlparse(url).netloc or None
        )
        if tailor_sink is not None:
            tailor_sink.append(tailor_result)
        for fp, (_question, answer) in tailor_result.drafts.items():
            idx = by_fp.get(fp)
            if idx is None:
                continue
            old = resolutions[idx]
            resolutions[idx] = Resolution(
                field=old.field,
                semantic_key=old.semantic_key,
                action="fill",
                value=answer,
                reason="drafted by tailor-answers — review before submit",
                draft=True,
            )

    return resolutions, [r for r in resolutions if r.action == "ask"]


def _attach_cover_letters(
    conn: sqlite3.Connection,
    posting_id: int,
    fields: list[FieldSpec],
    resolutions: list[Resolution],
    budget: RunBudget | None,
    audit_root: Path | None,
) -> list[Resolution]:
    """Draft the posting's cover letter once and turn each cover-letter FILE input
    that solve.resolve left `ask` ("attach by hand") into an upload of the PDF.

    The upload is `draft=True`, so apply_resolutions runs it only under
    `type_drafts` (the handoff lane): click mode can never upload it. Without a
    PDF (AI policy on the form, a rejected or failed draft, an exception) the
    input stays `ask`, with the draft status in the reason, and the run goes on.
    draft_cover_letter appends the letter text to drafts.md itself."""
    idxs = [
        i
        for i, r in enumerate(resolutions)
        if r.semantic_key == "cover_letter" and r.field.kind == "file" and r.action == "ask"
    ]
    if not idxs:
        return resolutions
    try:
        result = cover_letter.draft_cover_letter(
            conn, posting_id, fields=fields, budget=budget, audit_root=audit_root
        )
    except Exception as exc:  # noqa: BLE001 — a letter failure must never cost the run
        status, pdf_path = f"{type(exc).__name__}", None
    else:
        status, pdf_path = result.status, result.pdf_path
    for idx in idxs:
        old = resolutions[idx]
        if pdf_path:
            resolutions[idx] = Resolution(
                old.field,
                "cover_letter",
                "upload",
                value=str(pdf_path),
                reason="cover letter drafted, review before submit",
                draft=True,
            )
        elif status == "ai_policy":
            resolutions[idx] = replace(old, reason=AI_POLICY_REASON)
        else:
            resolutions[idx] = replace(old, reason=f"cover letter not drafted ({status}), attach by hand")
    return resolutions


def run_assist(
    page: Any,
    posting_id: int,
    url: str,
    answers: dict,
    resume_path: str | None,
    scan_only: bool = False,
    conn: sqlite3.Connection | None = None,
    budget: RunBudget | None = None,
    mapper_sink: list[MapperResult] | None = None,
    tailor: bool = False,
    tailor_sink: list[TailorResult] | None = None,
    company: str | None = None,
    autoapply: bool = False,
    type_drafts: bool = False,
    audit_root: Path | None = None,
) -> TriageDigest:
    """Scan the page, resolve fields against `answers`, optionally apply the
    fill/upload resolutions, and assemble a TriageDigest of the outcome.

    When `conn` is given, any field classify() can't place is handed to the
    LLM field-mapper (jobhunt.apply_assist.mapper) before resolve() runs, so a
    cached — or freshly named — semantic key can stand in for it. `budget`
    gates the network call the mapper may make; with `conn=None` the mapper
    never runs and behavior is identical to before this parameter existed.
    `mapper_sink`, if given, gets one MapperResult appended whenever the
    mapper ran — how a caller collects cost/coverage stats across a batch.

    `tailor` (default False) additionally hands any textarea field left
    `ask`/"unrecognized field" to tailor_answers.draft_for_posting when
    `conn` is given and the posting's current-rubric score qualifies
    (TAILOR_MIN_SCORE). A drafted answer converts that Resolution to
    action="fill", draft=True — collected into `TriageDigest.drafts` instead
    of being typed, since apply_resolutions skips draft=True Resolutions.
    Typing a draft requires per-draft human approval via apply_one (see the
    CLI review loop). `tailor_sink`, if given, gets one TailorResult
    appended whenever the drafter ran.

    `type_drafts` (default False; the handoff lane only) types each drafted
    answer into its field through the same apply-and-verify path as any other
    fill, instead of leaving it in `TriageDigest.drafts` untyped. The candidate's own
    Submit click in the handed-off tab is his approval of the text. It takes
    effect only with `autoapply` (and `tailor`, which makes the drafts exist).
    Under `autoapply` the drafter runs without the TAILOR_MIN_SCORE gate (see
    _apply_tailoring). Drafts stay listed in `TriageDigest.drafts` either way,
    so the submit gate and the drafts file know about them. In that same lane
    (`autoapply`, `type_drafts`, `tailor`, `conn`) a cover-letter FILE input is
    filled with the PDF cover_letter.draft_cover_letter builds, as a draft upload
    the same way; with no PDF it stays `ask`. `audit_root` (default None, the
    repo root) is where that module appends the letter to `runs/<date>/drafts.md`.

    `company` (default None) is the company the posting is for. With it,
    classify() names a how-heard question that spells the company out ("How did
    you first learn about Veeva?"), and resolve() takes it from there. A field
    classify() leaves unnamed only for want of the company is still not handed
    to the mapper, so the mapper's own check (classify() is None, no company)
    holds; if the company does not name it either, it is asked about.

    `autoapply` (default False) is the armed lane's mode. It scans Ashby's own
    widgets too (`scan_fields(autoapply=True)`), resolves with
    `solve.resolve(..., autoapply=True)`, gives a start-date picker the fixed
    `autoapply_start_date()`, and executes "check" resolutions (required
    acknowledgment boxes) and the answer clicks the module docstring allows, all
    behind the dry-run submit guard: a page without it is never clicked. Without
    it nothing changes: the scan is one pass, a "check" is only a question, and
    fill never clicks."""
    errors: list[str] = []
    ats = detect_ats(url)

    try:
        fields = scan_fields(page, autoapply=True) if autoapply else scan_fields(page)
    except Exception as exc:  # noqa: BLE001
        return TriageDigest(
            posting_id=posting_id,
            url=url,
            ats=ats,
            filled=[],
            residual=[],
            skipped=[],
            errors=[f"scan failed: {exc}"],
        )

    if ats and not fields:
        # A recognised ATS always has a form. Zero fields means we scanned the
        # wrong page or the form never mounted — historically this reported as
        # a clean run with filled=0 and nobody noticed for two weeks.
        errors.append(
            f"scanned 0 fields on a known ATS ({ats}) — wrong page or form never mounted"
        )

    key_overrides: dict[str, str] = {}
    if conn is not None:
        unknown_fields = [
            f for f in fields if classify(f) is None and classify(f, company) is None
        ]
        mapper_result = mapper_map_unknown(
            conn, unknown_fields, budget, host=urlparse(url).netloc or None
        )
        key_overrides = mapper_result.overrides
        if mapper_sink is not None:
            mapper_sink.append(mapper_result)

    if autoapply:
        resolutions = _adapt_to_date_picker(
            solve.resolve(fields, answers, resume_path, key_overrides, company, autoapply=True)
        )
    else:
        resolutions = [
            _check_as_ask(res)
            for res in solve.resolve(fields, answers, resume_path, key_overrides, company)
        ]

    skipped = [r for r in resolutions if r.action == "skip"]
    residual = [r for r in resolutions if r.action == "ask"]

    if conn is not None and tailor:
        # The score gate lifts only for the unattended lane; the extra argument
        # is passed only then, so the assisted lane's call is exactly as before.
        tailor_lane = {"autoapply": True} if autoapply else {}
        resolutions, residual = _apply_tailoring(
            conn, posting_id, url, resolutions, budget, tailor_sink, **tailor_lane
        )
        if autoapply and type_drafts:
            # Handoff lane only: solve.resolve and click mode keep "attach by hand".
            resolutions = _attach_cover_letters(
                conn, posting_id, fields, resolutions, budget, audit_root
            )
            residual = [r for r in resolutions if r.action == "ask"]

    draft_resolutions = [r for r in resolutions if r.draft]

    if scan_only:
        fill_and_upload = [
            r for r in resolutions if r.action in ("fill", "upload", "check") and not r.draft
        ]
        residual = residual + [
            Resolution(
                field=r.field,
                semantic_key=r.semantic_key,
                action=r.action,
                value=r.value,
                reason="scan-only run",
                fallback_value=r.fallback_value,
                option_rule=r.option_rule,
                option_args=list(r.option_args),
            )
            for r in fill_and_upload
        ]
        return TriageDigest(
            posting_id=posting_id,
            url=url,
            ats=ats,
            filled=[],
            residual=residual,
            skipped=skipped,
            errors=errors,
            drafts=draft_resolutions,
        )

    if autoapply:
        # `type_drafts` goes in only when set: no other caller's call changes.
        draft_lane = {"type_drafts": True} if type_drafts else {}
        filled = apply_resolutions(page, resolutions, autoapply=True, **draft_lane)
    else:
        filled = apply_resolutions(page, resolutions)
    return TriageDigest(
        posting_id=posting_id,
        url=url,
        ats=ats,
        filled=filled,
        residual=residual,
        skipped=skipped,
        errors=errors,
        drafts=draft_resolutions,
    )
