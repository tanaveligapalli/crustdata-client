"""Extract handle, caption and date fields from an Instagram post or reel page
returned by Crustdata web fetch (/web/enrich/live, human_mode true).

Only uses fields seen in fetched pages (see tests/fixtures/web_fetch.json).
The post data lives in a <script type="application/json"> block under
``xig_polaris_media.if_not_gated_logged_out``: a media object with
  code              post shortcode, e.g. "DTNUCNQCL_w"
  user.username     the handle
  caption.text      the caption
  taken_at          unix seconds
  caption.created_at
  comment_count

parse_status:
  ok                  media object found and its code matches the URL
  gated               an ``if_not_gated_logged_out`` key is present but holds
                      no media object (the page withheld the post)
  shortcode_mismatch  a media object was found, but none has the URL's code
  no_post_object      none of the above

Date cross-check: the post's <time datetime="..."> is taken to be the last
<time> tag before the caption text in the rendered HTML. The other <time>
tags on the page belong to comments. If the caption text is not in the
HTML, no tag is chosen. A disagreement with taken_at of more than
TIME_TOLERANCE_SECONDS sets time_disagrees to True.

The fixture has no ``datePublished`` or ``uploadDate`` key. The parser still
looks for them anywhere in the page's JSON and returns None if absent.

Commenter names are never returned; only comment_count.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime
from typing import Any, Iterator

TIME_TOLERANCE_SECONDS = 60

_JSON_SCRIPT = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S
)
_URL = re.compile(r"instagram\.com/(?:[^/]+/)?(p|reel|reels|tv)/([^/?#]+)")
_TIME_TAG = re.compile(r'<time[^>]*\bdatetime="([^"]+)"')
_GATE_KEY = "if_not_gated_logged_out"


def parse_url(url: str | None) -> tuple[str | None, str | None]:
    """Return (kind, shortcode) from a post/reel URL, e.g. ("reel", "C1x...")."""
    m = _URL.search(url or "")
    if not m:
        return None, None
    kind = {"p": "post", "reels": "reel"}.get(m.group(1), m.group(1))
    return kind, m.group(2)


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


def _is_media(d: dict[str, Any]) -> bool:
    return isinstance(d.get("caption"), dict) and "taken_at" in d


def _iso_to_unix(value: str) -> int | None:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _post_time_tag(html: str, caption: str | None) -> str | None:
    if not caption:
        return None
    first_line = caption.strip().splitlines()[0].strip() if caption.strip() else ""
    if not first_line:
        return None
    # The caption also appears earlier in <meta> tags, before any <time>, so
    # only look for its rendered copy after the first <time> tag.
    first_tag = _TIME_TAG.search(html)
    if not first_tag:
        return None
    start = first_tag.start()
    pos = html.find(htmllib.escape(first_line, quote=False), start)
    if pos < 0:
        pos = html.find(first_line, start)
    if pos < 0:
        return None
    tags = list(_TIME_TAG.finditer(html, start, pos))
    return tags[-1].group(1) if tags else None


def parse_post(html: str, url: str | None = None) -> dict[str, Any]:
    kind, shortcode = parse_url(url)

    media_objects: list[dict[str, Any]] = []
    gate_seen = False
    gate_has_media = False
    extra: dict[str, Any] = {"datePublished": None, "uploadDate": None}
    for block in _json_blocks(html):
        for d in _walk(block):
            if _is_media(d):
                media_objects.append(d)
            if _GATE_KEY in d:
                gate_seen = True
                if isinstance(d[_GATE_KEY], dict) and _is_media(d[_GATE_KEY]):
                    gate_has_media = True
            for key in ("datePublished", "uploadDate"):
                if extra[key] is None and key in d:
                    extra[key] = d[key]

    media = next((m for m in media_objects if shortcode is None or m.get("code") == shortcode), None)
    if media is not None:
        status = "ok"
    elif gate_seen and not gate_has_media:
        status = "gated"
    elif media_objects:
        status = "shortcode_mismatch"
    else:
        status = "no_post_object"

    media = media or {}
    caption = media.get("caption") or {}
    user = media.get("user") or {}
    caption_text = caption.get("text")
    taken_at = media.get("taken_at")

    time_tag = _post_time_tag(html, caption_text) if status == "ok" else None
    time_tag_unix = _iso_to_unix(time_tag) if time_tag else None
    delta = (
        abs(time_tag_unix - taken_at)
        if isinstance(taken_at, int) and time_tag_unix is not None
        else None
    )

    return {
        "kind": kind,
        "parse_status": status,
        "shortcode": media.get("code"),
        "handle": user.get("username"),
        "caption": caption_text,
        "taken_at": taken_at,
        "caption_created_at": caption.get("created_at"),
        "comment_count": media.get("comment_count"),
        "time_tag": time_tag,
        "time_delta_seconds": delta,
        "time_disagrees": None if delta is None else delta > TIME_TOLERANCE_SECONDS,
        **extra,
    }


def parse_web_fetch_response(response: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse each result of a /web/enrich/live response. Results with
    success false or no content get parse_status "no_post_object"."""
    out = []
    for r in response:
        if r.get("success") and r.get("content"):
            out.append({"url": r.get("url"), **parse_post(r["content"], r.get("url"))})
        else:
            kind, _ = parse_url(r.get("url"))
            out.append({"url": r.get("url"), "kind": kind, "parse_status": "no_post_object"})
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/web_fetch.json")
    print(json.dumps(parse_web_fetch_response(json.loads(path.read_text(encoding="utf-8"))), indent=2, ensure_ascii=False))
