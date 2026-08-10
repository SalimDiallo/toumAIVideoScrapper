"""Identify which platform a video comes from.

Every download goes through yt-dlp, which already extracts audio + metadata from
YouTube, Vimeo, Dailymotion, TikTok, Rumble, Odysee (LBRY), PeerTube and Reddit.
This module just puts a stable *name* on the origin platform so we can:

  * route the transcript step (YouTube uses youtube-transcript-api, everything
    else pulls captions through yt-dlp);
  * store / display / filter the source platform in the catalog and dashboard.

Entry point:

  * :func:`provider_from_extractor` — authoritative, from yt-dlp's ``extractor_key``
    *after* download. This is what we persist.
"""

from __future__ import annotations

from enum import Enum


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

def provider_from_extractor(extractor: str | None) -> Provider:
    """Map a yt-dlp ``extractor_key`` / ``extractor`` to a provider (authoritative)."""
    if not extractor:
        return Provider.UNKNOWN
    return _EXTRACTOR_MAP.get(extractor.strip().lower(), Provider.UNKNOWN)
