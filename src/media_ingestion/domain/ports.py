"""Ports (interfaces). Adapters implement these; the use case depends only on them.

swapping local disk for MinIO means writing a
new adapter — the use case never changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .models import (
    AudioAsset,
    IngestionResult,
    Job,
    JobStatus,
    Transcript,
    VeilleRun,
    VideoMetadata,
    WatchedChannel,
)


@dataclass(frozen=True)
class AudioHandle:
    """A playable audio artifact, returned by StoragePort.open_audio().

    Exactly one of `path` / `url` is set. Local storage returns a `path` (the
    web layer serves it with HTTP range support, so the player can seek). Remote
    storage (MinIO) returns a presigned `url` the browser fetches directly —
    S3 range requests give full seeking without proxying bytes through the API.
    """

    media_type: str
    path: Path | None = None
    url: str | None = None
    size: int | None = None


class AudioDownloaderPort(Protocol):
    def download(self, video_url: str, dest_dir: Path) -> tuple[VideoMetadata, AudioAsset]:
        """Download the audio track and return it alongside video metadata."""
        ...


class TranscriptProviderPort(Protocol):
    def fetch(self, metadata: VideoMetadata, languages: list[str]) -> Transcript | None:
        """Return a transcript if one exists, else None (no captions available).

        Takes the whole `metadata` (not just the id) so an implementation can route
        on `metadata.provider` and reach the source `metadata.url` when it needs to
        re-query the platform (e.g. pulling subtitles through yt-dlp).
        """
        ...


@dataclass(frozen=True)
class PlaylistEntry:
    """One video listed in a playlist (metadata only, nothing downloaded yet)."""

    video_id: str
    url: str
    title: str | None = None


class PlaylistResolverPort(Protocol):
    def resolve(self, playlist_url_or_id: str) -> list[PlaylistEntry]:
        """Expand a playlist URL/id into its video entries (no download).

        Returns an empty list when the playlist is empty or cannot be read.
        """
        ...


class ChannelResolverPort(Protocol):
    def recent_uploads(self, channel_url: str, limit: int = 15) -> list[PlaylistEntry]:
        """List a channel's most recent uploads (flat, no download), newest first.

        Returns an empty list when the channel is empty or cannot be read.
        """
        ...


class ChannelWatchStorePort(Protocol):
    """Registry of channels under daily surveillance (the "veille")."""

    def add(self, channel: WatchedChannel) -> None:
        """Register a channel. Idempotent per channel_key (re-add is a no-op)."""
        ...

    def list_active(self) -> list[WatchedChannel]:
        """Channels the veille should check (active only)."""
        ...

    def list_all(self) -> list[WatchedChannel]:
        """Every registered channel (for the management UI)."""
        ...

    def remove(self, channel_key: str) -> None:
        """Unregister a channel. Idempotent."""
        ...

    def set_active(self, channel_key: str, active: bool) -> None:
        """Pause (active=False) or resume (active=True) surveillance of a channel."""
        ...

    def mark_checked(self, channel_key: str, when: datetime) -> None:
        """Record the timestamp of the last veille pass for this channel."""
        ...


class VeilleRunLogPort(Protocol):
    """Append-only history of veille passes (the monitoring log)."""

    def record(self, checked: int, queued: int, detail: list[dict]) -> None:
        """Log one completed veille pass."""
        ...

    def list_recent(self, limit: int = 20) -> list[VeilleRun]:
        """Most recent passes first (for the UI history)."""
        ...

    def stats(self) -> dict:
        """All-time aggregates for the dashboard KPIs.

        Returns ``{"runs": int, "queued_total": int, "last_ran_at": datetime | None}``.
        """
        ...


class StoragePort(Protocol):
    def save(self, result: IngestionResult, language: str) -> str:
        """Persist the result (audio + json), grouped by language.

        Returns a storage location (local path or an s3:// URI) — the caller
        treats it as an opaque string, so local disk and MinIO are interchangeable.
        """
        ...

    def load_transcript(self, storage_uri: str) -> dict | None:
        """Read back the transcript.json at a storage location, or None if absent."""
        ...

    def load_metadata(self, storage_uri: str) -> dict | None:
        """Read back the metadata.json at a storage location, or None if absent."""
        ...

    def open_audio(self, storage_uri: str) -> AudioHandle | None:
        """Return a playable handle to the audio artifact, or None if absent."""
        ...

    def read_audio(self, storage_uri: str) -> tuple[str, Iterator[bytes]] | None:
        """Return (filename, byte-chunk iterator) for the audio (for zipping)."""
        ...

    def delete(self, storage_uri: str) -> None:
        """Delete every artifact stored at this location (audio + json). Idempotent."""
        ...


class MetadataRepositoryPort(Protocol):
    def upsert(self, result: IngestionResult, language: str, storage_uri: str) -> None:
        """Index the ingested video in a catalog (e.g. Postgres). Idempotent per video_id."""
        ...

    def list(
        self,
        *,
        language: str | None = None,
        transcript: str | None = None,
        provider: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List catalog rows, most recent first.

        Optionally filtered by `language`, `transcript` status
        ("available" / "unavailable"), origin `provider`, and/or a free-text
        query `q` (matched against title / channel / URL).
        """
        ...

    def get(self, video_id: str) -> dict | None:
        """Fetch a single catalog row by video_id, or None if unknown."""
        ...

    def delete(self, video_id: str) -> None:
        """Remove a catalog row by video_id. Idempotent."""
        ...

    def stats(self) -> dict:
        """Catalog-wide aggregates for the analytics page.

        Returns totals plus breakdowns::

            {
              "videos": int,
              "duration_s": int,               # total audio collected
              "with_transcript": int,
              "without_transcript": int,
              "by_language": [{"language", "videos", "duration_s"}],
              "by_provider": [{"provider", "videos", "duration_s"}],
              "by_source":   [{"source", "videos"}],
            }
        """
        ...


class EventPublisherPort(Protocol):
    def publish(self, topic: str, key: str, event: dict) -> None:
        """Publish one event (e.g. to Kafka). Decouples the API from the workers."""
        ...

    def publish_batch(self, topic: str, items: list[tuple[str, dict]]) -> None:
        """Publish many (key, event) pairs efficiently (single flush). Non per-message blocking."""
        ...


class JobStorePort(Protocol):
    def create(self, job: Job) -> None:
        """Persist a freshly accepted job (status PENDING)."""
        ...

    def get(self, job_id: str) -> Job | None: ...

    def list(
        self,
        *,
        status: JobStatus | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """List jobs, most recent first, optionally filtered by status and/or a
        free-text query `q` (matched against the URL)."""
        ...

    def counts_by_status(self) -> dict[str, int]:
        """Total number of jobs per status (for the dashboard KPIs)."""
        ...

    def existing_video_ids(self, video_ids: Iterable[str]) -> set[str]:
        """Subset of `video_ids` already tied to a non-failed job (de-duplication)."""
        ...

    def timeseries(self, *, days: int = 14) -> list[dict]:
        """Per-day job counts over the last `days` (for the evolution chart)."""
        ...

    def delete(self, job_id: str) -> None:
        """Remove a job record by id. Idempotent."""
        ...

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result_uri: str | None = None,
        error: str | None = None,
    ) -> None: ...
