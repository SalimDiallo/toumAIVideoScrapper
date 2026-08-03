"""Extract a YouTube playlist id from a URL or a bare id.

A playlist reaches us as a full URL (``youtube.com/playlist?list=<id>``,
``watch?v=<vid>&list=<id>``), a schemeless URL (``youtube.com/playlist?list=…``
pasted without ``https://``), or just the bare id (``PL…``, ``UU…``, ``RD…``).

Returns ``None`` when no playlist id can be found — the caller then reports that
no playlist was detected instead of queuing anything.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# Playlist ids are URL-safe base64-ish and longer/shorter than the 11-char video
# ids; since the value comes from a dedicated playlist field we stay lenient.
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,50}$")
_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def extract_playlist_id(value: str) -> str | None:
    if not value:
        return None
    value = value.strip()

    # Full or schemeless URL (a bare id has no '/' or '?').
    if "://" in value:
        candidate: str | None = value
    elif "/" in value or "?" in value:
        candidate = f"https://{value}"
    else:
        candidate = None

    if candidate is not None:
        try:
            parsed = urlparse(candidate)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in _HOSTS:
            return None
        values = parse_qs(parsed.query).get("list", [])
        return _clean(values[0]) if values else None

    # Bare id (came straight from the playlist field).
    return _clean(value)


def playlist_url(playlist_id: str) -> str:
    """Canonical playlist URL yt-dlp can enumerate."""
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _clean(candidate: str) -> str | None:
    return candidate if _PLAYLIST_ID_RE.match(candidate) else None
