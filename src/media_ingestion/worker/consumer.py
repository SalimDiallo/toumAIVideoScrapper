"""Kafka consumer loop: reads `job.requested` and runs the ingestion use case."""

from __future__ import annotations

import json

import structlog
from kafka import KafkaConsumer

from ..bootstrap import build_job_store, build_publisher
from ..cli import build_use_case
from ..config import Settings
from ..logging_setup import configure_logging
from .handler import JobHandler

log = structlog.get_logger(__name__)


def main() -> None:
    configure_logging()
    settings = Settings()

    handler = JobHandler(
        use_case=build_use_case(settings),
        job_store=build_job_store(settings),
        publisher=build_publisher(settings),
        settings=settings,
    )

    consumer = KafkaConsumer(
        settings.topic_job_requested,
        bootstrap_servers=settings.kafka_bootstrap_servers.split(","),
        group_id=settings.kafka_consumer_group,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    log.info("worker.started", topic=settings.topic_job_requested, group=settings.kafka_consumer_group)

    try:
        for message in consumer:
            try:
                handler.handle(message.value)
            except Exception as exc:  # noqa: BLE001 - malformed event etc.; don't poison-pill
                log.error("worker.handle_error", error=str(exc))
            finally:
                consumer.commit()
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
