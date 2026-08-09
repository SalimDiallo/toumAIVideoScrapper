"""ChannelResolverPort implementation backed by yt-dlp.

Lists a channel's most recent uploads *flat* (``extract_flat``) — a single
metadata request that yields the videos (newest first) without downloading any
audio. Only the ``playlistend`` most recent are fetched: the veille then
de-duplicates them against already-ingested videos and queues the newcomers.
"""

from __future__ import annotations

import structlog
import yt_dlp

from ..channel import normalize_channel_url
from ..domain.ports import PlaylistEntry

log = structlog.get_logger(__name__)


class YtDlpChannelResolver:
    def recent_uploads(self, channel_url: str, limit: int = 15) -> list[PlaylistEntry]:
        url = normalize_channel_url(channel_url)
        if not url:
            return []

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            # Flat = list the channel's videos without visiting each page.
            "extract_flat": "in_playlist",
            # Only the N most recent uploads (the channel /videos tab is newest-first).
            "playlistend": limit,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - any failure means "no uploads"
            log.info("channel.unavailable", channel=url, error=str(exc))
            return []

        entries: list[PlaylistEntry] = []
        for entry in info.get("entries") or []:
            if not entry:
                continue  # yt-dlp yields None for deleted/private videos
            video_id = entry.get("id")
            if not video_id:
                continue
            entry_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            entries.append(
                PlaylistEntry(video_id=video_id, url=entry_url, title=entry.get("title"))
            )
        log.info("channel.resolved", channel=url, videos=len(entries))
        return entries
