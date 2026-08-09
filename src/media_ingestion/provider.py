"""Identify which platform a video comes from.

Every download goes through yt-dlp, which already extracts audio + metadata from
YouTube, Vimeo, Dailymotion, TikTok, Rumble, Odysee (LBRY), PeerTube and Reddit.
This module just puts a stable *name* on the origin platform so we can:

  * route the transcript step (YouTube uses youtube-transcript-api, everything
    else pulls captions through yt-dlp);
  * store / display / filter the source platform in the catalog and dashboard.

Two entry points:

  * :func:`provider_from_extractor` — authoritative, from yt-dlp's ``extractor_key``
    *after* download. This is what we persist.
  * :func:`detect_provider` — best-effort, from a raw URL *before* download (host
    based). Federated hosts (PeerTube) and unknown ones fall back to ``UNKNOWN``.
"""

from __future__ import annotations

from enum import Enum
from urllib.parse import urlparse


class Provider(str, Enum):
    YOUTUBE = "youtube"
    VIMEO = "vimeo"
    DAILYMOTION = "dailymotion"
    TIKTOK = "tiktok"
    RUMBLE = "rumble"
    ODYSEE = "odysee"
    PEERTUBE = "peertube"
    REDDIT = "reddit"
    UNKNOWN = "unknown"


# yt-dlp `extractor_key` (or its lowercase `extractor`) -> our provider name.
# yt-dlp uses one extractor per platform family; we only map the ones we target
# and treat the id case-insensitively (e.g. "Youtube", "TikTok", "PeerTube").
_EXTRACTOR_MAP: dict[str, Provider] = {
    "youtube": Provider.YOUTUBE,
    "vimeo": Provider.VIMEO,
    "dailymotion": Provider.DAILYMOTION,
    "tiktok": Provider.TIKTOK,
    "rumble": Provider.RUMBLE,
    "rumbleembed": Provider.RUMBLE,
    "lbry": Provider.ODYSEE,  # Odysee is the web front-end for the LBRY network
    "odysee": Provider.ODYSEE,
    "peertube": Provider.PEERTUBE,
    "reddit": Provider.REDDIT,
}

# Hostname (without leading "www.") -> provider, for pre-download URL detection.
_HOST_MAP: dict[str, Provider] = {
    "youtube.com": Provider.YOUTUBE,
    "m.youtube.com": Provider.YOUTUBE,
    "music.youtube.com": Provider.YOUTUBE,
    "youtu.be": Provider.YOUTUBE,
    "vimeo.com": Provider.VIMEO,
    "player.vimeo.com": Provider.VIMEO,
    "dailymotion.com": Provider.DAILYMOTION,
    "dai.ly": Provider.DAILYMOTION,
    "tiktok.com": Provider.TIKTOK,
    "vm.tiktok.com": Provider.TIKTOK,
    "rumble.com": Provider.RUMBLE,
    "odysee.com": Provider.ODYSEE,
    "reddit.com": Provider.REDDIT,
    "old.reddit.com": Provider.REDDIT,
    "redd.it": Provider.REDDIT,
    "v.redd.it": Provider.REDDIT,
}


def provider_from_extractor(extractor: str | None) -> Provider:
    """Map a yt-dlp ``extractor_key`` / ``extractor`` to a provider (authoritative)."""
    if not extractor:
        return Provider.UNKNOWN
    return _EXTRACTOR_MAP.get(extractor.strip().lower(), Provider.UNKNOWN)


def detect_provider(url: str) -> Provider:
    """Best-effort provider from a raw URL, by hostname. UNKNOWN if unrecognised.

    Note: PeerTube is federated across arbitrary domains, so it can't be matched
    here — its provider is only known once yt-dlp reports the extractor.
    """
    if not url:
        return Provider.UNKNOWN
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return Provider.UNKNOWN
    host = host.removeprefix("www.")
    return _HOST_MAP.get(host, Provider.UNKNOWN)
