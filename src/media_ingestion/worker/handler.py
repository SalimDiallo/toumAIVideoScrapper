"""Business logic for one `job.requested` event.

Kept transport-agnostic (no Kafka here) so it can be unit-tested with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ..application.ingest_video import IngestVideoUseCase
from ..config import Settings
from ..domain.models import JobStatus
from ..domain.ports import EventPublisherPort, JobStorePort

log = structlog.get_logger(__name__)


@dataclass
class JobHandler:
    use_case: IngestVideoUseCase
    job_store: JobStorePort
    publisher: EventPublisherPort
    settings: Settings
    # Pipeline de nettoyage silver, injecté quand storage=minio + silver_auto_process.
    # None = pas d'enchaînement automatique après l'ingestion.
    silver_pipeline: object | None = None

    def handle(self, event: dict) -> None:
        job_id = event["job_id"]
        url = event["url"]
        languages = event.get("languages") or list(self.settings.languages)

        self.job_store.update_status(job_id, JobStatus.RUNNING)
        log.info("job.started", job_id=job_id, url=url)

        try:
            result = self.use_case.execute(url, self.settings.data_dir, languages)
        except Exception as exc:  # noqa: BLE001 - any failure -> DLQ
            self.job_store.update_status(job_id, JobStatus.FAILED, error=str(exc))
            self.publisher.publish(
                self.settings.topic_job_dlq,
                key=job_id,
                event={"job_id": job_id, "url": url, "error": str(exc)},
            )
            log.error("job.failed", job_id=job_id, error=str(exc))
            return

        self.job_store.update_status(job_id, JobStatus.COMPLETED, result_uri=result.storage_uri)
        self.publisher.publish(
            self.settings.topic_job_completed,
            key=job_id,
            event={
                "job_id": job_id,
                "video_id": result.metadata.video_id,
                "storage_uri": result.storage_uri,
                "transcript_status": result.transcript_status.value,
            },
        )
        log.info("job.completed", job_id=job_id, uri=result.storage_uri)

        # Enchaînement automatique du nettoyage silver, juste après l'ingestion.
        # Best-effort : un échec ici ne remet PAS le job en erreur (il est déjà
        # terminé et le bronze est bien persisté) — on se contente de journaliser.
        if self.silver_pipeline is not None and result.language:
            try:
                done = self.silver_pipeline.process_video(result.language, result.metadata.video_id)
                log.info(
                    "job.silver_auto",
                    job_id=job_id,
                    video_id=result.metadata.video_id,
                    processed=done,
                )
            except Exception as exc:  # noqa: BLE001 - nettoyage best-effort
                log.warning(
                    "job.silver_auto_failed",
                    job_id=job_id,
                    video_id=result.metadata.video_id,
                    error=str(exc),
                )
