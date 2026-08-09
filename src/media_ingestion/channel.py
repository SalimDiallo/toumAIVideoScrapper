"""Normalize a YouTube channel reference into a canonical uploads URL + a key.

A channel reaches us in many shapes: a handle (``@handle`` or
``youtube.com/@handle``), a channel id (``UC…`` or ``youtube.com/channel/UC…``),
or a legacy vanity/user path (``youtube.com/c/Name``, ``youtube.com/user/Name``).

`normalize_channel_url` returns the canonical ``/videos`` tab URL that yt-dlp can
enumerate flat (newest uploads first). `channel_key` returns a stable identifier
used as the primary key in the watch registry. Both return ``None`` when no
channel can be recognized.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_HANDLE_RE = re.compile(r"^@[A-Za-z0-9._-]{1,60}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com"}


def _channel_ref(value: str) -> tuple[str, str] | None:
    """Return ``(key, path)`` or ``None``.

    ``path`` is the URL path prefix (e.g. ``@handle``, ``channel/UC…``,
    ``c/Name``); ``key`` is the stable identifier for the primary key.
    """
    if not value:
        return None
    value = value.strip()

    # Bare handle or channel id (no scheme, no slash).
    if "://" not in value and "/" not in value:
        if _HANDLE_RE.match(value):
            # Handles are case-insensitive: lowercase both key and path so the
            # canonical URL is stable whatever the input case.
            return value.lower(), value.lower()
        if _CHANNEL_ID_RE.match(value):
            return value, f"channel/{value}"
        return None

    candidate = value if "://" in value else f"https://{value}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host not in _HOSTS:
        return None

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    first = parts[0]
    if first.startswith("@") and _HANDLE_RE.match(first):
        return first.lower(), first.lower()
    if first == "channel" and len(parts) >= 2 and _CHANNEL_ID_RE.match(parts[1]):
        return parts[1], f"channel/{parts[1]}"
    if first in ("c", "user") and len(parts) >= 2:
        return f"{first}/{parts[1]}", f"{first}/{parts[1]}"
    return None


def normalize_channel_url(value: str) -> str | None:
    """Canonical uploads URL yt-dlp can enumerate, or None."""
    ref = _channel_ref(value)
    return f"https://www.youtube.com/{ref[1]}/videos" if ref else None


def channel_key(value: str) -> str | None:
    """Stable identifier for a channel (primary key), or None."""
    ref = _channel_ref(value)
    return ref[0] if ref else None
