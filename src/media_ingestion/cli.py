"""CLI entry point: `toumai-ingest <youtube_url> [--lang fr en] ...`

This wires the concrete adapters into the use case. In Phase 2 the same
use case is driven by a Kafka worker instead of this CLI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters.local_storage import LocalJsonStorage
from .adapters.rate_limited_downloader import RateLimitedDownloader
from .adapters.youtube_transcript import YouTubeTranscriptProvider
from .adapters.ytdlp_downloader import YtDlpDownloader
from .application.ingest_video import IngestVideoUseCase
from .config import Settings
from .domain.ports import MetadataRepositoryPort, StoragePort
from .logging_setup import configure_logging


def _build_storage(settings: Settings) -> StoragePort:
    if settings.storage_backend == "minio":
        from .adapters.minio_storage import MinioStorage

        return MinioStorage(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
            secure=settings.minio_secure,
        )
    return LocalJsonStorage(root=settings.data_dir)


def _build_metadata_repo(settings: Settings) -> MetadataRepositoryPort | None:
    if settings.metadata_backend == "postgres":
        from .adapters.postgres_repo import PostgresMetadataRepository

        repo = PostgresMetadataRepository(dsn=settings.postgres_dsn)
        repo.create_schema()
        return repo
    return None


def _build_downloader(settings: Settings) -> RateLimitedDownloader:
    """yt-dlp enveloppé de la couche throttling (délais + backoff 429 + proxies)."""
    inner = YtDlpDownloader(
        audio_format=settings.audio_format, ffmpeg_location=settings.ffmpeg_location
    )
    return RateLimitedDownloader(
        inner,
        delay_min_s=settings.download_delay_min_s,
        delay_max_s=settings.download_delay_max_s,
        max_retries=settings.download_max_retries,
        backoff_base_s=settings.download_backoff_base_s,
        backoff_max_s=settings.download_backoff_max_s,
        proxies=settings.download_proxies,
    )


def build_use_case(settings: Settings) -> IngestVideoUseCase:
    return IngestVideoUseCase(
        downloader=_build_downloader(settings),
        transcript_provider=YouTubeTranscriptProvider(
            accept_asr=settings.accept_youtube_asr,
            enable_translation=settings.enable_transcript_translation,
        ),
        storage=_build_storage(settings),
        metadata_repo=_build_metadata_repo(settings),
    )


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(prog="toumai-ingest", description=__doc__)
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--lang", nargs="+", default=settings.languages, help="Preferred caption languages"
    )
    parser.add_argument("--data-dir", default=str(settings.data_dir), help="Output root directory")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)
    settings.data_dir = Path(args.data_dir)
    use_case = build_use_case(settings)

    result = use_case.execute(args.url, work_dir=settings.data_dir, languages=args.lang)
    print(
        f"[{result.transcript_status.value}] {result.metadata.title} -> {settings.data_dir / result.metadata.video_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
