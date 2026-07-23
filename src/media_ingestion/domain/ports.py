"""Ports (interfaces). Adapters implement these; the use case depends only on them.

This is what makes Phase 2 cheap: swapping local disk for MinIO, or plugging a
faster-whisper STT engine, means writing a new adapter — the use case never changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import AudioAsset, IngestionResult, Transcript, VideoMetadata


class AudioDownloaderPort(Protocol):
    def download(self, video_url: str, dest_dir: Path) -> tuple[VideoMetadata, AudioAsset]:
        """Download the audio track and return it alongside video metadata."""
        ...


class TranscriptProviderPort(Protocol):
    def fetch(self, video_id: str, languages: list[str]) -> Transcript | None:
        """Return a transcript if one exists, else None (no captions available)."""
        ...


class SpeechToTextPort(Protocol):
    def transcribe(self, audio: AudioAsset, language: str | None = None) -> Transcript:
        """Generate a transcript from audio (Phase 2, e.g. faster-whisper)."""
        ...


class StoragePort(Protocol):
    def save(self, result: IngestionResult, language: str) -> str:
        """Persist the result (audio + json), grouped by language.

        Returns a storage location (local path or an s3:// URI) — the caller
        treats it as an opaque string, so local disk and MinIO are interchangeable.
        """
        ...


class MetadataRepositoryPort(Protocol):
    def upsert(self, result: IngestionResult, language: str, storage_uri: str) -> None:
        """Index the ingested video in a catalog (e.g. Postgres). Idempotent per video_id."""
        ...
