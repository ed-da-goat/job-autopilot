"""Structured-resume rendering and per-job tailoring.

`render` turns `resume/resume.yaml` plus a variant into a one-page PDF with no
LLM call and no spend. `validate` holds the fact-drift guardrails shared by the
renderer and the (spend-gated) tailoring path.
"""

from jobhunt.resume import render, tailor, validate

__all__ = ["render", "tailor", "validate"]
