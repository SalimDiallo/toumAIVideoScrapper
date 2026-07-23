"""Factories for the Phase 2 infra pieces shared by the API and the worker."""

from __future__ import annotations

from .config import Settings
from .domain.ports import EventPublisherPort, JobStorePort


def build_publisher(settings: Settings) -> EventPublisherPort:
    from .adapters.kafka_producer import KafkaEventPublisher

    return KafkaEventPublisher(settings.kafka_bootstrap_servers)


def build_job_store(settings: Settings) -> JobStorePort:
    from .adapters.postgres_jobs import PostgresJobStore

    store = PostgresJobStore(settings.postgres_dsn)
    store.create_schema()
    return store
