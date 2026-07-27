"""Extract a YouTube video id from a URL, for de-duplicating ingestion jobs.

The same video reaches us under many URL shapes (``youtu.be/<id>``,
``watch?v=<id>&list=…``, ``/shorts/<id>``, mobile/music hosts, extra query
params). yt-dlp only hands back the canonical id *after* downloading, so to
avoid queuing/downloading the same video twice we derive the id up front.

Returns ``None`` when the URL is not a recognizable YouTube video (non-YouTube
links are simply not de-duplicated by id).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# A YouTube video id is exactly 11 URL-safe base64 chars.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")
_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com"}


def extract_video_id(url: str) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None

    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "youtu.be":
        return _clean(parsed.path.lstrip("/").split("/", 1)[0])
    if host in _HOSTS:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            return _clean(values[0]) if values else None
        for prefix in _PATH_PREFIXES:
            if parsed.path.startswith(prefix):
                return _clean(parsed.path[len(prefix) :].split("/", 1)[0])
    return None


def _clean(candidate: str) -> str | None:
    return candidate if _ID_RE.match(candidate) else None
