"""Hacker News "Who is hiring?" monthly thread source, via Algolia's API.

1. Find the newest story whose title contains "Who is hiring".
2. Page through its top-level comments (cap 10 pages of 100).
3. Each top-level comment is one posting.
"""

from __future__ import annotations

import html
import re

from jobhunt.net import FetchError, polite_get

SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
SEARCH_URL = "https://hn.algolia.com/api/v1/search"
STORY_HITS_PER_PAGE = 5
COMMENT_HITS_PER_PAGE = 100
MAX_PAGES = 10
COMPANY_MAX_LEN = 60

_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub(" ", text)).strip()


def _first_line(comment_html: str) -> str:
    # HN comments separate paragraphs with <p>; the first paragraph is the "line".
    first_chunk = re.split(r"<p>", comment_html, maxsplit=1)[0]
    return _clean_html(first_chunk)


def find_latest_hiring_story() -> dict | None:
    resp = polite_get(
        SEARCH_BY_DATE_URL,
        params={"tags": "story,author_whoishiring", "hitsPerPage": STORY_HITS_PER_PAGE},
    )
    if resp.status_code != 200:
        raise FetchError(f"hn: story search HTTP {resp.status_code}", resp.status_code)
    payload = resp.json()
    hits = payload.get("hits", []) if isinstance(payload, dict) else []
    for hit in hits:
        title = hit.get("title") or ""
        if "who is hiring" in title.lower():
            return hit
    return None


def fetch_comments_for_story(story_id: str) -> list[dict]:
    comments: list[dict] = []
    for page in range(MAX_PAGES):
        resp = polite_get(
            SEARCH_URL,
            params={
                "tags": f"comment,story_{story_id}",
                "hitsPerPage": COMMENT_HITS_PER_PAGE,
                "page": page,
            },
        )
        if resp.status_code != 200:
            raise FetchError(f"hn: comments page {page} HTTP {resp.status_code}", resp.status_code)
        payload = resp.json()
        hits = payload.get("hits", []) if isinstance(payload, dict) else []
        if not hits:
            break
        comments.extend(hits)
    return comments


def normalize_comments(story_id: str, comments: list[dict]) -> list[dict]:
    out: list[dict] = []
    for comment in comments:
        parent_id = comment.get("parent_id")
        if parent_id is None or str(parent_id) != str(story_id):
            continue  # only top-level comments are postings
        object_id = comment.get("objectID")
        if not object_id:
            continue
        first_line = _first_line(comment.get("comment_text") or "")
        segments = [s.strip() for s in first_line.split("|")]
        company = segments[0][:COMPANY_MAX_LEN] if segments and segments[0] else None
        if not company:
            continue
        title = segments[1] if len(segments) > 1 and segments[1] else first_line
        out.append(
            {
                "source_posting_id": str(object_id),
                "company": company,
                "title": title,
                "location": None,
                "url": f"https://news.ycombinator.com/item?id={object_id}",
                "posted_at": comment.get("created_at"),
                "raw": comment,
            }
        )
    return out


def fetch() -> list[dict]:
    story = find_latest_hiring_story()
    if story is None:
        return []
    story_id = story.get("objectID")
    if not story_id:
        return []
    comments = fetch_comments_for_story(story_id)
    return normalize_comments(story_id, comments)
