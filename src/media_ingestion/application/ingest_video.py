"""Core use case: ingest one video (YouTube or any yt-dlp platform).

Flow:
  1. download audio + metadata (yt-dlp) — provider is detected here
  2. try to fetch captions (routed per provider by the transcript provider)
  3. if none: skip (status = unavailable)
  4. persist the result
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import structlog

from ..domain.models import IngestionResult, TranscriptStatus
from ..domain.ports import (
    AudioDownloaderPort,
    MetadataRepositoryPort,
    StoragePort,
    TranscriptProviderPort,
)

log = structlog.get_logger(__name__)


@dataclass
class IngestVideoUseCase:
    downloader: AudioDownloaderPort
    transcript_provider: TranscriptProviderPort
    storage: StoragePort
    metadata_repo: MetadataRepositoryPort | None = None  # Postgres catalog; None = skip indexing.

    def execute(self, video_url: str, work_dir: Path, languages: list[str]) -> IngestionResult:
        metadata, audio = self.downloader.download(video_url, work_dir)
        log.info("audio.downloaded", video_id=metadata.video_id, path=str(audio.path))

        transcript = self.transcript_provider.fetch(metadata, languages)

        if transcript is not None:
            status = TranscriptStatus.AVAILABLE
            log.info(
                "transcript.fetched",
                video_id=metadata.video_id,
                source=transcript.source.value,
                language=transcript.language,
                segments=len(transcript.segments),
            )
        else:
            status = TranscriptStatus.UNAVAILABLE
            log.info(
                "transcript.skipped",
                video_id=metadata.video_id,
                provider=metadata.provider,
                reason="no_captions",
            )

        result = IngestionResult(
            metadata=metadata,
            audio=audio,
            transcript=transcript,
            transcript_status=status,
        )
        language = self._resolve_language(transcript, metadata, languages)
        storage_uri = self.storage.save(result, language)
        log.info("ingestion.saved", video_id=metadata.video_id, language=language, uri=storage_uri)

        result = replace(result, language=language, storage_uri=storage_uri)

        if self.metadata_repo is not None:
            self.metadata_repo.upsert(result, language, storage_uri)
            log.info("metadata.indexed", video_id=metadata.video_id)

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
