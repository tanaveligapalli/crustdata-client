"""Extract handle, caption and date fields from an Instagram post page
returned by Crustdata web fetch (/web/enrich/live, human_mode true).

Only uses fields seen in tests/fixtures/web_fetch.json. The post data
lives in a <script type="application/json"> block, in a media object with:
  code              post shortcode, e.g. "DTNUCNQCL_w"
  user.username     the handle
  caption.text      the caption
  taken_at          unix seconds
  caption.created_at

The fixture has no `datePublished` or `uploadDate` key. The parser still
looks for them, anywhere in the page's JSON, and returns None if absent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

_JSON_SCRIPT = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S
)
_SHORTCODE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel)/([^/?#]+)")


def _json_blocks(html: str) -> Iterator[Any]:
    for m in _JSON_SCRIPT.finditer(html):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            continue


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_media(d: dict[str, Any], shortcode: str | None) -> bool:
    if not isinstance(d.get("caption"), dict) or "taken_at" not in d:
        return False
    return shortcode is None or d.get("code") == shortcode


def parse_post(html: str, url: str | None = None) -> dict[str, Any]:
    """Return {handle, caption, taken_at, caption_created_at, datePublished,
    uploadDate, shortcode}. Values not found in the page are None."""
    shortcode = None
    if url and (m := _SHORTCODE.search(url)):
        shortcode = m.group(1)

    media: dict[str, Any] | None = None
    found: dict[str, Any] = {"datePublished": None, "uploadDate": None}
    for block in _json_blocks(html):
        for d in _walk(block):
            if media is None and _is_media(d, shortcode):
                media = d
            for key in ("datePublished", "uploadDate"):
                if found[key] is None and key in d:
                    found[key] = d[key]

    media = media or {}
    caption = media.get("caption") or {}
    user = media.get("user") or {}
    return {
        "shortcode": media.get("code"),
        "handle": user.get("username"),
        "caption": caption.get("text"),
        "taken_at": media.get("taken_at"),
        "caption_created_at": caption.get("created_at"),
        **found,
    }


def parse_web_fetch_response(response: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse each successful result of a /web/enrich/live response."""
    return [
        {"url": r.get("url"), **parse_post(r["content"], r.get("url"))}
        for r in response
        if r.get("success") and r.get("content")
    ]


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/web_fetch.json")
    print(json.dumps(parse_web_fetch_response(json.loads(path.read_text())), indent=2, ensure_ascii=False))
