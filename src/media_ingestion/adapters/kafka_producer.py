"""EventPublisherPort backed by Kafka (kafka-python-ng).

JSON-serialises events. Used by the API to publish `job.requested`, and by the
worker to publish `job.completed` / DLQ events.
"""

from __future__ import annotations

import json

import structlog
from kafka import KafkaProducer

log = structlog.get_logger(__name__)


class KafkaEventPublisher:
    def __init__(self, bootstrap_servers: str) -> None:
        self._producer = KafkaProducer(
            bootstrap_servers=bootstrap_servers.split(","),
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
            acks="all",
            retries=3,
        )

    def publish(self, topic: str, key: str, event: dict) -> None:
        future = self._producer.send(topic, key=key, value=event)
        future.get(timeout=10)  # block until acked so we surface broker errors
        log.info("event.published", topic=topic, key=key)

    def close(self) -> None:
        self._producer.flush()
        self._producer.close()
