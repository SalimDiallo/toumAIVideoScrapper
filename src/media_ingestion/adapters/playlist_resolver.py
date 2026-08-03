"""PlaylistResolverPort implementation backed by yt-dlp.

Enumerates a playlist *flat* (``extract_flat``): a single metadata request that
lists the videos without downloading any audio. The individual videos are then
queued as normal ingestion jobs and downloaded one by one by the workers.
"""

from __future__ import annotations

import structlog
import yt_dlp

from ..domain.ports import PlaylistEntry
from ..playlist import extract_playlist_id, playlist_url

log = structlog.get_logger(__name__)


class YtDlpPlaylistResolver:
    def resolve(self, playlist_url_or_id: str) -> list[PlaylistEntry]:
        playlist_id = extract_playlist_id(playlist_url_or_id)
        if not playlist_id:
            return []
        url = playlist_url(playlist_id)

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            # Flat = don't visit each video page, just list the playlist entries.
            "extract_flat": "in_playlist",
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - any failure means "empty playlist"
            log.info("playlist.unavailable", playlist_id=playlist_id, error=str(exc))
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
        log.info("playlist.resolved", playlist_id=playlist_id, videos=len(entries))
        return entries
