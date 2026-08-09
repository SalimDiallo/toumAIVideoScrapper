"""Veille use case: check watched channels and queue their new videos.

Runs one pass (typically once each morning, triggered by Airflow or the
`toumai-veille` CLI): for every active channel it lists the most recent uploads,
drops the ones already ingested/queued (de-dup by video id, exactly like the CSV
and playlist flows), and publishes the newcomers as ingestion jobs. The existing
Kafka worker does the actual download/transcription — this layer only enqueues.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog

from ..domain.models import Job
from ..domain.ports import (
    ChannelResolverPort,
    ChannelWatchStorePort,
    EventPublisherPort,
    JobStorePort,
    VeilleRunLogPort,
)

log = structlog.get_logger(__name__)


@dataclass
class WatchChannelsUseCase:
    channel_store: ChannelWatchStorePort
    resolver: ChannelResolverPort
    job_store: JobStorePort
    publisher: EventPublisherPort
    topic: str
    default_languages: list[str]
    recent_limit: int = 15
    # Optional monitoring log; when set, each pass is recorded for the history view.
    run_log: VeilleRunLogPort | None = None

    def run_once(self) -> dict:
        """Check every active channel once. Returns a summary:

        ``{"checked": int, "queued": int, "per_channel": [{...}]}``
        """
        now = datetime.now(timezone.utc)
        events: list[tuple[str, dict]] = []
        per_channel: list[dict] = []
        total_queued = 0

        for channel in self.channel_store.list_active():
            entries = self.resolver.recent_uploads(channel.url, limit=self.recent_limit)
            known = self.job_store.existing_video_ids(
                {e.video_id for e in entries if e.video_id}
            )
            seen: set[str] = set()
            queued = 0
            for entry in entries:
                vid = entry.video_id
                if vid and (vid in known or vid in seen):
                    continue
                job_id = uuid.uuid4().hex
                languages = list(self.default_languages)
                self.job_store.create(
                    Job(job_id=job_id, url=entry.url, languages=languages, video_id=vid)
                )
                events.append(
                    (job_id, {"job_id": job_id, "url": entry.url, "languages": languages})
                )
                if vid:
                    seen.add(vid)
                queued += 1

            self.channel_store.mark_checked(channel.channel_key, now)
            total_queued += queued
            per_channel.append(
                {
                    "channel_key": channel.channel_key,
                    "name": channel.name,
                    "url": channel.url,
                    "new": queued,
                }
            )

        if events:
            self.publisher.publish_batch(self.topic, events)
        if self.run_log is not None:
            self.run_log.record(len(per_channel), total_queued, per_channel)
        log.info("veille.run", channels=len(per_channel), queued=total_queued)
        return {"checked": len(per_channel), "queued": total_queued, "per_channel": per_channel}
