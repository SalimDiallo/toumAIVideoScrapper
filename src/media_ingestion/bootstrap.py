"""Factories for the infrastructure pieces shared by the API and the worker."""

from __future__ import annotations

from . import _kafka_compat
from .config import Settings
from .domain.ports import EventPublisherPort, JobStorePort

# Rend kafka-python-ng tolérant aux sockets fermées sous Python 3.12+ (rebalance).
_kafka_compat.apply()


def build_publisher(settings: Settings) -> EventPublisherPort:
    from .adapters.kafka_producer import KafkaEventPublisher

    return KafkaEventPublisher(settings.kafka_bootstrap_servers, settings.kafka_api_version)


def build_job_store(settings: Settings) -> JobStorePort:
    from .adapters.postgres_jobs import PostgresJobStore

    store = PostgresJobStore(settings.postgres_dsn)
    store.create_schema()
    return store


def build_playlist_resolver(settings: Settings):
    """yt-dlp playlist enumerator (expands a playlist into video jobs)."""
    from .adapters.playlist_resolver import YtDlpPlaylistResolver

    return YtDlpPlaylistResolver()


def build_catalog(settings: Settings):
    """The Postgres videos catalog (read side for GET /videos). Always Postgres."""
    from .adapters.postgres_repo import PostgresMetadataRepository

    repo = PostgresMetadataRepository(settings.postgres_dsn)
    repo.create_schema()
    return repo
