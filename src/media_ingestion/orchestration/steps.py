"""Pipeline steps as standalone functions (no Airflow import here).

Each step takes/returns a JSON-friendly `ctx` dict so it can run as an Airflow
task (state passed via XCom) — but the functions are plain Python and unit-testable
without Airflow. They reuse the very same ports/adapters as the CLI and the worker.
"""

from __future__ import annotations

import structlog

from ..adapters.youtube_transcript import YouTubeTranscriptProvider
from ..adapters.ytdlp_downloader import YtDlpDownloader
from ..application.ingest_video import IngestVideoUseCase
from ..cli import _build_metadata_repo, _build_storage
from ..config import Settings
from ..domain.models import IngestionResult, TranscriptStatus
from .serde import (
    audio_from_dict,
    audio_to_dict,
    metadata_from_dict,
    metadata_to_dict,
    transcript_from_dict,
    transcript_to_dict,
)

log = structlog.get_logger(__name__)


def download_step(url: str, languages: list[str], settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    downloader = YtDlpDownloader(
        audio_format=settings.audio_format, ffmpeg_location=settings.ffmpeg_location
    )
    metadata, audio = downloader.download(url, settings.data_dir)
    log.info("step.download.done", video_id=metadata.video_id)
    return {
        "url": url,
        "languages": languages,
        "metadata": metadata_to_dict(metadata),
        "audio": audio_to_dict(audio),
    }


def transcript_step(ctx: dict, settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    provider = YouTubeTranscriptProvider(
        accept_asr=settings.accept_youtube_asr,
        enable_translation=settings.enable_transcript_translation,
    )
    meta = metadata_from_dict(ctx["metadata"])
    transcript = provider.fetch(meta.video_id, ctx["languages"])
    log.info("step.transcript.done", video_id=meta.video_id, found=transcript is not None)
    return {**ctx, "transcript": transcript_to_dict(transcript) if transcript else None}


def store_step(ctx: dict, settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    result, language = _rebuild_result(ctx)
    storage_uri = _build_storage(settings).save(result, language)
    log.info("step.store.done", video_id=result.metadata.video_id, uri=storage_uri)
    return {**ctx, "language": language, "storage_uri": storage_uri}


def index_step(ctx: dict, settings: Settings | None = None) -> dict:
    settings = settings or Settings()
    repo = _build_metadata_repo(settings)
    if repo is not None:
        result, _ = _rebuild_result(ctx)
        repo.upsert(result, ctx["language"], ctx["storage_uri"])
        log.info("step.index.done", video_id=result.metadata.video_id)
    return ctx


def _rebuild_result(ctx: dict) -> tuple[IngestionResult, str]:
    metadata = metadata_from_dict(ctx["metadata"])
    audio = audio_from_dict(ctx["audio"])
    transcript = transcript_from_dict(ctx.get("transcript"))
    status = TranscriptStatus.AVAILABLE if transcript else TranscriptStatus.UNAVAILABLE
    result = IngestionResult(
        metadata=metadata,
        audio=audio,
        transcript=transcript,
        transcript_status=status,
        language=ctx.get("language"),
        storage_uri=ctx.get("storage_uri"),
    )
    language = ctx.get("language") or IngestVideoUseCase._resolve_language(
        transcript, metadata, ctx["languages"]
    )
    return result, language
