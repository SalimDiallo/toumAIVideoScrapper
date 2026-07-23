"""TranscriptProviderPort backed by youtube-transcript-api.

Returns None when the video has no captions (transcripts disabled, none found,
or a network error). The use case decides what to do next (skip, or STT in Phase 2).
Supports both the 1.x instance API and the legacy 0.6.x static API.
"""

from __future__ import annotations
from pydoc import text

import structlog
from youtube_transcript_api import YouTubeTranscriptApi

from ..domain.models import Transcript, TranscriptSegment, TranscriptSource

log = structlog.get_logger(__name__)


class YouTubeTranscriptProvider:
    def fetch(self, video_id: str, languages: list[str]) -> Transcript | None:
        try:
            raw, language = self._fetch_raw(video_id, languages)
        except Exception as exc:  # noqa: BLE001 - any failure means "no transcript"
            log.info("transcript.unavailable", video_id=video_id, error=str(exc))
            return None

        segments = [
            TranscriptSegment(
                start_s=float(item["start"]),
                duration_s=float(item.get("duration", 0.0)),
                text=item["text"],
            )
            for item in raw
        ]
        if not segments:
            return None
        return Transcript(language=language, source=TranscriptSource.YOUTUBE, segments=segments)

    @staticmethod
    def _fetch_raw(video_id: str, languages: list[str]) -> tuple[list[dict], str]:
        api = YouTubeTranscriptApi()
        # youtube-transcript-api >= 1.0 (instance API)
        if hasattr(api, "fetch"):
            fetched = api.fetch(video_id, languages=languages)
            raw = [
                {"text": s.text, "start": s.start, "duration": s.duration} for s in fetched
            ]
            return raw, getattr(fetched, "language_code", languages[0])
        # legacy 0.6.x static API
        raw = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)  # type: ignore[attr-defined]
        return raw, languages[0]

