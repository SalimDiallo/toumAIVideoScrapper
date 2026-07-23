"""FastAPI app: accept ingestion jobs (async) and expose their status.

POST /process  -> 202 + job_id, publishes `job.requested` to Kafka
GET  /jobs/{id} -> current job status (read from Postgres)
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException

from ..bootstrap import build_job_store, build_publisher
from ..config import Settings
from ..domain.models import Job
from ..domain.ports import EventPublisherPort, JobStorePort
from .schemas import JobResponse, ProcessAccepted, ProcessRequest


def create_app(
    settings: Settings | None = None,
    *,
    store: JobStorePort | None = None,
    publisher: EventPublisherPort | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or build_job_store(settings)
    publisher = publisher or build_publisher(settings)

    app = FastAPI(title="TOUMAI Ingestion API", version="0.1.0")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/process", status_code=202, response_model=ProcessAccepted)
    def process(req: ProcessRequest) -> ProcessAccepted:
        job_id = uuid.uuid4().hex
        languages = req.languages or list(settings.languages)
        job = Job(job_id=job_id, url=req.url, languages=languages)
        store.create(job)
        publisher.publish(
            settings.topic_job_requested,
            key=job_id,
            event={"job_id": job_id, "url": req.url, "languages": languages},
        )
        return ProcessAccepted(job_id=job_id, status=job.status.value)

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str) -> JobResponse:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return JobResponse(
            job_id=job.job_id,
            url=job.url,
            languages=job.languages,
            status=job.status.value,
            result_uri=job.result_uri,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    return app


def build_asgi() -> FastAPI:
    """Factory used by uvicorn (`--factory`)."""
    from ..logging_setup import configure_logging

    configure_logging()
    return create_app()


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "media_ingestion.api.app:build_asgi",
        factory=True,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    _ = settings  # placeholder for future host/port config
