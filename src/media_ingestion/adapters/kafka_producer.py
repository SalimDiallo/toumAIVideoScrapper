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
    def __init__(self, bootstrap_servers: str, api_version: str = "2.5.0") -> None:
        self._producer = KafkaProducer(
            bootstrap_servers=bootstrap_servers.split(","),
            # Version fixée -> pas de probing check_version() (plante sous Windows).
            api_version=tuple(int(p) for p in api_version.split(".")),
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
            acks="all",
            retries=3,
        )

    def publish(self, topic: str, key: str, event: dict) -> None:
        future = self._producer.send(topic, key=key, value=event)
        future.get(timeout=10)  # block until acked so we surface broker errors
        log.info("event.published", topic=topic, key=key)

    def publish_batch(self, topic: str, items: list[tuple[str, dict]]) -> None:
        """Send all then flush once — avoids blocking on each message (big CSV)."""
        for key, event in items:
            self._producer.send(topic, key=key, value=event)
        self._producer.flush()
        log.info("event.published_batch", topic=topic, count=len(items))

    def close(self) -> None:
        self._producer.flush()
        self._producer.close()
