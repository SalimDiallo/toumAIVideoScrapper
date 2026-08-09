"""FastAPI app: accept ingestion jobs (async) and expose their status.

POST /process  -> 202 + job_id, publishes `job.requested` to Kafka
GET  /jobs/{id} -> current job status (read from Postgres)
"""

from __future__ import annotations

import csv
import io
import uuid

from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from ..bootstrap import (
    build_catalog,
    build_channel_resolver,
    build_channel_store,
    build_job_store,
    build_playlist_resolver,
    build_publisher,
    build_veille_run_log,
)
from ..config import Settings
from ..domain.models import Job, JobStatus
from ..domain.ports import (
    ChannelResolverPort,
    ChannelWatchStorePort,
    EventPublisherPort,
    JobStorePort,
    MetadataRepositoryPort,
    PlaylistResolverPort,
    StoragePort,
    VeilleRunLogPort,
)
from .schemas import (
    BatchAccepted,
    BatchItem,
    JobResponse,
    PlaylistRequest,
    ProcessAccepted,
    ProcessRequest,
    VideoItem,
)


def _parse_languages(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return list(default)
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.replace(";", " ").split()]
    return [p for p in parts if p] or list(default)


def _job_to_response(job: Job) -> JobResponse:
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


def create_app(
    settings: Settings | None = None,
    *,
    store: JobStorePort | None = None,
    publisher: EventPublisherPort | None = None,
    storage: StoragePort | None = None,
    catalog: MetadataRepositoryPort | None = None,
    playlist_resolver: PlaylistResolverPort | None = None,
    channel_store: ChannelWatchStorePort | None = None,
    channel_resolver: ChannelResolverPort | None = None,
    veille_run_log: VeilleRunLogPort | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or build_job_store(settings)
    publisher = publisher or build_publisher(settings)

    app = FastAPI(title="Ingestion API", version="0.1.0")

    def _get_storage() -> StoragePort:
        from ..cli import _build_storage

        return storage or _build_storage(settings)

    def _get_catalog() -> MetadataRepositoryPort:
        return catalog or build_catalog(settings)

    def _get_playlist_resolver() -> PlaylistResolverPort:
        return playlist_resolver or build_playlist_resolver(settings)

    def _get_channel_store() -> ChannelWatchStorePort:
        return channel_store or build_channel_store(settings)

    def _get_channel_resolver() -> ChannelResolverPort:
        return channel_resolver or build_channel_resolver(settings)

    def _get_veille_run_log() -> VeilleRunLogPort:
        return veille_run_log or build_veille_run_log(settings)

    def _build_veille_use_case():
        from ..application.watch_channels import WatchChannelsUseCase

        return WatchChannelsUseCase(
            channel_store=_get_channel_store(),
            resolver=_get_channel_resolver(),
            job_store=store,
            publisher=publisher,
            topic=settings.topic_job_requested,
            default_languages=list(settings.languages),
            recent_limit=settings.veille_recent_limit,
            run_log=_get_veille_run_log(),
        )

    def _new_job_event(url: str, languages: list[str]) -> tuple[str, dict]:
        from ..video_id import extract_video_id

        job_id = uuid.uuid4().hex
        store.create(
            Job(job_id=job_id, url=url, languages=languages, video_id=extract_video_id(url))
        )
        return job_id, {"job_id": job_id, "url": url, "languages": languages}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/process", status_code=202, response_model=ProcessAccepted)
    def process(req: ProcessRequest) -> ProcessAccepted:
        languages = req.languages or list(settings.languages)
        job_id, event = _new_job_event(req.url, languages)
        publisher.publish(settings.topic_job_requested, key=job_id, event=event)
        return ProcessAccepted(job_id=job_id, status="pending")

    @app.post("/process/csv", status_code=202, response_model=BatchAccepted)
    async def process_csv(file: UploadFile = File(...)) -> BatchAccepted:
        raw = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames is None or "url" not in [f.strip().lower() for f in reader.fieldnames]:
            raise HTTPException(status_code=400, detail="CSV must have a 'url' column")

        jobs: list[BatchItem] = []
        errors: list[str] = []
        events: list[tuple[str, dict]] = []
        for i, row in enumerate(reader, start=2):  # row 1 = header
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            url = norm.get("url", "")
            if not url:
                errors.append(f"line {i}: empty url")
                continue
            languages = _parse_languages(
                norm.get("lang") or norm.get("languages"), settings.languages
            )
            job_id, event = _new_job_event(url, languages)
            events.append((job_id, event))
            jobs.append(BatchItem(url=url, job_id=job_id, languages=languages))

        # publish all at once (single flush) instead of blocking per message
        publisher.publish_batch(settings.topic_job_requested, events)
        return BatchAccepted(accepted=len(jobs), jobs=jobs, errors=errors)

    @app.post("/process/playlist", status_code=202, response_model=BatchAccepted)
    def process_playlist(req: PlaylistRequest) -> BatchAccepted:
        from ..playlist import extract_playlist_id

        if extract_playlist_id(req.playlist) is None:
            raise HTTPException(status_code=400, detail="no playlist id found in input")
        languages = req.languages or list(settings.languages)
        entries = _get_playlist_resolver().resolve(req.playlist)
        if not entries:
            raise HTTPException(status_code=404, detail="empty or unreadable playlist")

        jobs: list[BatchItem] = []
        events: list[tuple[str, dict]] = []
        for entry in entries:
            job_id, event = _new_job_event(entry.url, languages)
            events.append((job_id, event))
            jobs.append(BatchItem(url=entry.url, job_id=job_id, languages=languages))
        publisher.publish_batch(settings.topic_job_requested, events)
        return BatchAccepted(accepted=len(jobs), jobs=jobs)

    @app.get("/jobs", response_model=list[JobResponse])
    def list_jobs(
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[JobResponse]:
        status_enum = None
        if status is not None:
            try:
                status_enum = JobStatus(status)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"invalid status: {status}")
        jobs = store.list(status=status_enum, limit=limit, offset=offset)
        return [_job_to_response(j) for j in jobs]

    @app.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str) -> JobResponse:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_to_response(job)

    @app.get("/jobs/{job_id}/transcript")
    def get_job_transcript(job_id: str) -> dict:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if not job.result_uri:
            raise HTTPException(
                status_code=409, detail=f"job not finished (status={job.status.value})"
            )
        transcript = _get_storage().load_transcript(job.result_uri)
        if transcript is None:
            raise HTTPException(status_code=404, detail="no transcript for this video")
        return transcript

    @app.post("/jobs/{job_id}/retry", status_code=202, response_model=ProcessAccepted)
    def retry_job(job_id: str) -> ProcessAccepted:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        store.update_status(job_id, JobStatus.PENDING)
        publisher.publish(
            settings.topic_job_requested,
            key=job_id,
            event={"job_id": job_id, "url": job.url, "languages": job.languages},
        )
        return ProcessAccepted(job_id=job_id, status="pending")

    @app.post("/veille/run", status_code=202)
    def run_veille() -> dict:
        """Run one veille pass: scan watched channels, queue their new uploads.

        This is the endpoint the daily Airflow DAG triggers. Returns a summary
        ``{checked, queued, per_channel}``.
        """
        return _build_veille_use_case().run_once()

    @app.post("/veille/channels/csv", status_code=202)
    async def import_channels_csv(file: UploadFile = File(...)) -> dict:
        """Bulk-register watched channels from a CSV (column `channel`, optional `name`)."""
        from ..channel import channel_key, normalize_channel_url
        from ..domain.models import WatchedChannel

        raw = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        fields = [f.strip().lower() for f in (reader.fieldnames or [])]
        col = next((c for c in ("channel", "url", "handle") if c in fields), None)
        if col is None:
            raise HTTPException(status_code=400, detail="CSV must have a 'channel' (or 'url') column")

        cstore = _get_channel_store()
        existing = {c.channel_key for c in cstore.list_all()}
        seen: set[str] = set()
        added = duplicates = invalid = 0
        for row in reader:
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            value = norm.get(col, "")
            if not value:
                continue
            key = channel_key(value)
            url = normalize_channel_url(value)
            if not key or not url:
                invalid += 1
                continue
            if key in existing or key in seen:
                duplicates += 1
                continue
            cstore.add(WatchedChannel(channel_key=key, url=url, name=(norm.get("name") or None)))
            seen.add(key)
            added += 1
        return {"added": added, "duplicates": duplicates, "invalid": invalid}

    @app.get("/veille/runs")
    def veille_runs(limit: int = Query(default=20, ge=1, le=200)) -> list[dict]:
        """Recent veille passes (monitoring log), most recent first."""
        return [
            {
                "run_id": r.run_id,
                "ran_at": r.ran_at,
                "checked": r.checked,
                "queued": r.queued,
                "detail": r.detail,
            }
            for r in _get_veille_run_log().list_recent(limit)
        ]

    @app.get("/videos", response_model=list[VideoItem])
    def list_videos(
        language: str | None = Query(default=None),
        transcript: str | None = Query(default=None),
        provider: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[VideoItem]:
        rows = _get_catalog().list(
            language=language,
            transcript=transcript,
            provider=provider,
            limit=limit,
            offset=offset,
        )
        return [VideoItem(**row) for row in rows]

    from .ui import mount_ui

    mount_ui(
        app,
        settings=settings,
        store=store,
        publisher=publisher,
        get_storage=_get_storage,
        get_catalog=_get_catalog,
        get_playlist_resolver=_get_playlist_resolver,
        get_channel_store=_get_channel_store,
        get_veille_run_log=_get_veille_run_log,
        run_veille=lambda: _build_veille_use_case().run_once(),
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
