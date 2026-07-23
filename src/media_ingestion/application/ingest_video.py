"""The single MVP use case: ingest one YouTube video.

Flow:
  1. download audio + metadata (yt-dlp)
  2. try to fetch YouTube captions
  3. if none: use STT if wired, otherwise skip (status = unavailable)
  4. persist the result
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from ..domain.models import IngestionResult, TranscriptStatus
from ..domain.ports import (
    AudioDownloaderPort,
    SpeechToTextPort,
    StoragePort,
    TranscriptProviderPort,
)

log = structlog.get_logger(__name__)


@dataclass
class IngestVideoUseCase:
    downloader: AudioDownloaderPort
    transcript_provider: TranscriptProviderPort
    storage: StoragePort
    stt: SpeechToTextPort | None = None  # Phase 2: faster-whisper. None = skip.

    def execute(self, video_url: str, work_dir: Path, languages: list[str]) -> IngestionResult:
        metadata, audio = self.downloader.download(video_url, work_dir)
        log.info("audio.downloaded", video_id=metadata.video_id, path=str(audio.path))

        transcript = self.transcript_provider.fetch(metadata.video_id, languages)

        if transcript is not None:
            status = TranscriptStatus.AVAILABLE
            log.info(
                "transcript.fetched",
                video_id=metadata.video_id,
                source="youtube",
                language=transcript.language,
                segments=len(transcript.segments),
            )
        elif self.stt is not None:
            transcript = self.stt.transcribe(audio, metadata.language)
            status = TranscriptStatus.AVAILABLE
            log.info("transcript.generated", video_id=metadata.video_id, source="stt")
        else:
            status = TranscriptStatus.UNAVAILABLE
            log.info(
                "transcript.skipped",
                video_id=metadata.video_id,
                reason="no_youtube_captions_and_no_stt",
            )

        result = IngestionResult(
            metadata=metadata,
            audio=audio,
            transcript=transcript,
            transcript_status=status,
        )
        language = self._resolve_language(transcript, metadata, languages)
        out = self.storage.save(result, language)
        log.info("ingestion.saved", video_id=metadata.video_id, language=language, path=str(out))
        return result

    @staticmethod
    def _resolve_language(transcript, metadata, languages: list[str]) -> str:
        raw = (
            (transcript.language if transcript else None)
            or metadata.language
            or (languages[0] if languages else None)
            or "unknown"
        )
        # normalise "fr-FR" -> "fr"
        return raw.split("-")[0].lower()
