"""FastAPI app: accept ingestion jobs (async) and expose their status.

POST /process  -> 202 + job_id, publishes `job.requested` to Kafka
GET  /jobs/{id} -> current job status (read from Postgres)
"""

from __future__ import annotations

import csv
import io
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile

from ..bootstrap import build_job_store, build_publisher
from ..config import Settings
from ..domain.models import Job
from ..domain.ports import EventPublisherPort, JobStorePort
from .schemas import BatchAccepted, BatchItem, JobResponse, ProcessAccepted, ProcessRequest


def _parse_languages(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return list(default)
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.replace(";", " ").split()]
    return [p for p in parts if p] or list(default)


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

    def _submit(url: str, languages: list[str]) -> str:
        """Create a PENDING job and publish job.requested. Returns the job_id."""
        job_id = uuid.uuid4().hex
        job = Job(job_id=job_id, url=url, languages=languages)
        store.create(job)
        publisher.publish(
            settings.topic_job_requested,
            key=job_id,
            event={"job_id": job_id, "url": url, "languages": languages},
        )
        return job_id

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/process", status_code=202, response_model=ProcessAccepted)
    def process(req: ProcessRequest) -> ProcessAccepted:
        languages = req.languages or list(settings.languages)
        job_id = _submit(req.url, languages)
        return ProcessAccepted(job_id=job_id, status="pending")

    @app.post("/process/csv", status_code=202, response_model=BatchAccepted)
    async def process_csv(file: UploadFile = File(...)) -> BatchAccepted:
        raw = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames is None or "url" not in [f.strip().lower() for f in reader.fieldnames]:
            raise HTTPException(status_code=400, detail="CSV must have a 'url' column")

        jobs: list[BatchItem] = []
        errors: list[str] = []
        for i, row in enumerate(reader, start=2):  # row 1 = header
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            url = norm.get("url", "")
            if not url:
                errors.append(f"line {i}: empty url")
                continue
            languages = _parse_languages(norm.get("lang") or norm.get("languages"), settings.languages)
            job_id = _submit(url, languages)
            jobs.append(BatchItem(url=url, job_id=job_id, languages=languages))

        return BatchAccepted(accepted=len(jobs), jobs=jobs, errors=errors)

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
