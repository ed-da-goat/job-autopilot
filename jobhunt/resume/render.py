"""Render `resume/resume.yaml` to a one-page PDF. No LLM call, no spend.

The variant's `order` list is a flat sequence of bullet ids. Experience blocks
are emitted in order of their first appearing bullet and bullets within a block
follow that sequence, so reordering a variant is a data edit rather than a
template edit. A tailoring overlay (see `jobhunt.resume.tailor`) is the same
shape: it may replace the summary, re-order, drop, and re-word bullets, and
nothing else.

PDF generation goes through Playwright's Chromium `page.pdf()`, which is
headless-only. `uv run playwright install chromium` is a one-time prereq.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobhunt.resume.validate import ResumeValidationError, assert_clean

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESUME_YAML_PATH = REPO_ROOT / "resume" / "resume.yaml"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "resume.html"
OUTPUT_DIR = REPO_ROOT / "data" / "apply_packs" / "resume"


def load_resume(path: Path | str = RESUME_YAML_PATH) -> dict[str, Any]:
    """Load the structured resume source."""
    resume_path = Path(path)
    if not resume_path.exists():
        raise FileNotFoundError(
            f"no structured resume at {resume_path}. This file is gitignored by design "
            "(it carries the phone number), so a fresh clone has to recreate it. "
            "FIXTURE_YAML in tests/test_resume_render.py is the committed schema example."
        )
    with resume_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{resume_path} did not parse to a mapping")
    return data


def known_bullet_ids(data: dict[str, Any]) -> set[str]:
    """Every bullet id declared in the resume source."""
    ids: set[str] = set()
    for job in data.get("experience") or []:
        for bullet in job.get("bullets") or []:
            ids.add(bullet["id"])
    for project in data.get("projects") or []:
        for bullet in project.get("bullets") or []:
            ids.add(bullet["id"])
    return ids


def available_variants(data: dict[str, Any]) -> list[str]:
    return sorted((data.get("variants") or {}).keys())


def build_context(
    data: dict[str, Any],
    variant: str,
    overlay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve a variant (plus an optional tailoring overlay) into template context.

    `overlay` is the validated tailoring payload: `summary`, `bullet_order`,
    `bullet_rewrites`, `dropped`. Anything it omits falls back to the variant.
    """
    variants = data.get("variants") or {}
    if variant not in variants:
        raise KeyError(
            f"unknown variant {variant!r}. Available: {', '.join(available_variants(data)) or '(none)'}"
        )
    spec = variants[variant]
    overlay = overlay or {}

    summary = overlay.get("summary") or spec.get("summary", "")
    order: list[str] = list(overlay.get("bullet_order") or spec.get("order") or [])
    dropped = {*(overlay.get("dropped") or []), *(spec.get("drop") or [])}
    rewrites: dict[str, str] = overlay.get("bullet_rewrites") or {}

    known = known_bullet_ids(data)
    unknown = sorted({*order, *dropped} - known)
    if unknown:
        raise ResumeValidationError(
            f"variant {variant!r} references bullet id(s) absent from the resume source: "
            f"{', '.join(unknown)}"
        )

    selected = [bullet_id for bullet_id in order if bullet_id not in dropped]
    position = {bullet_id: index for index, bullet_id in enumerate(selected)}

    def blocks(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in entries:
            kept = [
                {**bullet, "text": rewrites.get(bullet["id"], bullet["text"])}
                for bullet in (entry.get("bullets") or [])
                if bullet["id"] in position
            ]
            if not kept:
                continue
            kept.sort(key=lambda bullet: position[bullet["id"]])
            block = copy.deepcopy(entry)
            block["bullets"] = kept
            out.append(block)
        # Experience blocks follow the order of their first selected bullet.
        out.sort(key=lambda block: position[block["bullets"][0]["id"]])
        return out

    return {
        "contact": data.get("contact") or {},
        "summary": summary,
        "experience": blocks(data.get("experience") or []),
        "education": data.get("education") or [],
        "skills": data.get("skills") or [],
        "projects": blocks(data.get("projects") or []),
        "variant": variant,
        "variant_label": spec.get("label", variant),
    }


def render_html(context: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(TEMPLATE_NAME).render(**context)


def context_text(context: dict[str, Any]) -> str:
    """Flatten the rendered facts for the banned-phrase scan.

    Scanning this rather than the HTML keeps CSS and markup out of the match
    surface while still covering every string the reader will see.
    """
    parts: list[str] = [str(context.get("summary", ""))]
    for block in [*context.get("experience", []), *context.get("projects", [])]:
        parts.extend(
            [str(block.get("company", "")), str(block.get("name", "")), str(block.get("title", ""))]
        )
        parts.extend(str(bullet["text"]) for bullet in block.get("bullets", []))
    for school in context.get("education", []):
        parts.extend([str(school.get("school", "")), str(school.get("degree", ""))])
    for row in context.get("skills", []):
        parts.append(str(row.get("text", "")))
    return "\n".join(part for part in parts if part)


def html_to_pdf(html: str, out_path: Path) -> Path:
    """Print `html` to a Letter PDF through headless Chromium."""
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.pdf(
                path=str(out_path),
                format="Letter",
                print_background=True,
                prefer_css_page_size=True,
            )
        finally:
            browser.close()
    return out_path


def output_path(variant: str, posting_id: int | None = None) -> Path:
    stem = f"{posting_id}-{variant}" if posting_id is not None else variant
    return OUTPUT_DIR / f"{stem}.pdf"


def render_variant(
    variant: str,
    *,
    posting_id: int | None = None,
    overlay: dict[str, Any] | None = None,
    out_path: Path | None = None,
    resume_path: Path | str = RESUME_YAML_PATH,
) -> Path:
    """Render one variant to PDF. Fails closed on any banned claim."""
    data = load_resume(resume_path)
    context = build_context(data, variant, overlay)
    assert_clean(context_text(context), where=f"rendered resume ({variant})")
    html = render_html(context)
    destination = out_path or output_path(variant, posting_id)
    return html_to_pdf(html, destination)
