"""Kafka consumer loop: reads `job.requested` and runs the ingestion use case."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait

import structlog
from kafka import KafkaConsumer
from kafka.errors import CommitFailedError

from ..bootstrap import build_job_store, build_publisher
from ..cli import build_use_case
from ..config import Settings
from ..logging_setup import configure_logging
from .handler import JobHandler

log = structlog.get_logger(__name__)


def _safe_handle(handler: JobHandler, event: dict) -> None:
    try:
        handler.handle(event)
    except Exception as exc:  # noqa: BLE001 - malformed event etc.; don't poison-pill
        log.error("worker.handle_error", error=str(exc))


def main() -> None:
    configure_logging()
    settings = Settings()

    handler = JobHandler(
        use_case=build_use_case(settings),
        job_store=build_job_store(settings),
        publisher=build_publisher(settings),
        settings=settings,
    )

    # Plafond de téléchargements simultanés sur ce serveur : on lit au plus N
    # messages par poll et on les traite dans un pool de N threads, puis on commit
    # le lot (at-least-once). yt-dlp est I/O bound -> les threads suffisent.
    n = max(1, settings.max_concurrent_downloads)
    consumer = KafkaConsumer(
        settings.topic_job_requested,
        bootstrap_servers=settings.kafka_bootstrap_servers.split(","),
        group_id=settings.kafka_consumer_group,
        # Version fixée -> pas de probing check_version() (plante sous Windows).
        api_version=tuple(int(p) for p in settings.kafka_api_version.split(".")),
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        max_poll_records=n,
        # Assez large pour un lot lent (gros téléchargements + backoff 429) sans que
        # le broker éjecte le worker et fasse échouer le commit.
        max_poll_interval_ms=settings.kafka_max_poll_interval_ms,
    )
    log.info(
        "worker.started",
        topic=settings.topic_job_requested,
        group=settings.kafka_consumer_group,
        max_concurrent=n,
    )

    try:
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="dl") as pool:
            while True:
                batch = consumer.poll(timeout_ms=1000)
                if not batch:
                    continue
                messages = [m for records in batch.values() for m in records]
                futures = [pool.submit(_safe_handle, handler, m.value) for m in messages]
                wait(futures)
                try:
                    consumer.commit()
                except CommitFailedError as exc:
                    # Éjecté du groupe (lot trop lent) : ne pas planter. Le lot sera
                    # re-consommé après rebalance ; les étapes sont idempotentes.
                    log.warning("worker.commit_failed", error=str(exc))
    except KeyboardInterrupt:
        log.info("worker.stopping")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
