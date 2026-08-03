"""Server-rendered web UI (HTMX + Jinja2 + Tailwind) for the ingestion platform.

Mounted onto the FastAPI app by :func:`create_app`. Reuses the very same
`store` / `publisher` / `catalog` / `storage` dependencies as the JSON API — the
UI is just another read/write client, it holds no business logic of its own.

Live updates are done the simple way: HTMX polls small fragments
(`/ui/partials/...`) every few seconds. No websockets, no build step.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from ..config import Settings
from ..domain.models import Job, JobStatus
from ..playlist import extract_playlist_id
from ..video_id import extract_video_id
from ..domain.ports import (
    EventPublisherPort,
    JobStorePort,
    MetadataRepositoryPort,
    PlaylistResolverPort,
    StoragePort,
)

_BASE = Path(__file__).parent

# Human label + Tailwind classes per status (sober slate palette + one accent).
STATUS_META: dict[str, dict[str, str]] = {
    "pending": {
        "label": "En attente",
        "dot": "bg-amber-400",
        "badge": "bg-amber-50 text-amber-700 ring-amber-600/20",
    },
    "running": {
        "label": "En cours",
        "dot": "bg-indigo-500",
        "badge": "bg-indigo-50 text-indigo-700 ring-indigo-600/20",
    },
    "completed": {
        "label": "Terminé",
        "dot": "bg-emerald-500",
        "badge": "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
    },
    "failed": {
        "label": "Échec",
        "dot": "bg-rose-500",
        "badge": "bg-rose-50 text-rose-700 ring-rose-600/20",
    },
}
STATUS_ORDER = ["pending", "running", "completed", "failed"]


def _parse_languages(raw: str | None, default: list[str]) -> list[str]:
    """Split a free-form 'fr, en; ar' string into a clean language list."""
    if not raw:
        return list(default)
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.replace(";", " ").split()]
    return [p for p in parts if p] or list(default)


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%d/%m %H:%M")


def _job_vm(job: Job) -> dict:
    """View-model: everything a template needs, pre-formatted."""
    meta = STATUS_META.get(job.status.value, STATUS_META["pending"])
    return {
        "id": job.job_id,
        "short_id": job.job_id[:8],
        "url": job.url,
        "languages": job.languages,
        "status": job.status.value,
        "status_label": meta["label"],
        "status_dot": meta["dot"],
        "status_badge": meta["badge"],
        "result_uri": job.result_uri,
        "error": job.error,
        "created_at": _fmt_dt(job.created_at),
        "updated_at": _fmt_dt(job.updated_at),
        "can_retry": job.status is JobStatus.FAILED,
        "has_transcript": job.status is JobStatus.COMPLETED and bool(job.result_uri),
    }


def _fill_timeseries(rows: list[dict], days: int) -> dict:
    """Backfill missing days with zeros so the chart has a continuous X axis."""
    by_day = {r["day"]: r for r in rows}
    today = datetime.now(timezone.utc).date()
    labels: list[str] = []
    completed: list[int] = []
    failed: list[int] = []
    total: list[int] = []
    for i in range(days - 1, -1, -1):
        d: date = today - timedelta(days=i)
        key = d.isoformat()
        row = by_day.get(key, {})
        labels.append(d.strftime("%d/%m"))
        completed.append(int(row.get("completed", 0)))
        failed.append(int(row.get("failed", 0)))
        total.append(int(row.get("total", 0)))
    return {"labels": labels, "completed": completed, "failed": failed, "total": total}


def _fmt_ts(seconds: float) -> str:
    """Format a segment offset as H:MM:SS (or M:SS under an hour)."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _segments_vm(transcript: dict | None) -> list[dict]:
    """Turn transcript segments into rows aligned to their audio time range."""
    if not transcript:
        return []
    out: list[dict] = []
    for seg in transcript.get("segments") or []:
        start = float(seg.get("start_s", 0) or 0)
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({"start_s": round(start, 2), "ts": _fmt_ts(start), "text": text})
    return out


def _audio_response(handle) -> FileResponse | RedirectResponse:
    """Serve an AudioHandle: FileResponse (range/seek) locally, redirect to the
    presigned MinIO URL remotely (the browser then seeks against S3 directly)."""
    if handle.path is not None:
        return FileResponse(handle.path, media_type=handle.media_type)
    return RedirectResponse(handle.url)


def mount_ui(
    app: FastAPI,
    *,
    settings: Settings,
    store: JobStorePort,
    publisher: EventPublisherPort,
    get_storage: Callable[[], StoragePort],
    get_catalog: Callable[[], MetadataRepositoryPort],
    get_playlist_resolver: Callable[[], PlaylistResolverPort],
) -> None:
    """Attach static files + the /ui router to an existing FastAPI app."""
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
    templates = Jinja2Templates(directory=str(_BASE / "templates"))
    templates.env.globals["STATUS_META"] = STATUS_META
    templates.env.globals["STATUS_ORDER"] = STATUS_ORDER

    router = APIRouter(prefix="/ui", tags=["ui"])

    def _kpis() -> dict:
        counts = store.counts_by_status()
        return {
            "total": sum(counts.values()),
            "counts": {s: counts.get(s, 0) for s in STATUS_ORDER},
        }

    # ------------------------------------------------------------------ pages
    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        chart = _fill_timeseries(store.timeseries(days=14), days=14)
        recent = [_job_vm(j) for j in store.list(limit=8)]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"active": "dashboard", "kpis": _kpis(), "chart": chart, "recent": recent},
        )

    @router.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, status: str | None = None) -> HTMLResponse:
        active_status = status if status in STATUS_META else None
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {"active": "jobs", "status": active_status, "kpis": _kpis()},
        )

    @router.get("/upload", response_class=HTMLResponse)
    def upload_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "upload.html",
            {"active": "upload", "default_langs": ", ".join(settings.languages)},
        )

    @router.get("/videos", response_class=HTMLResponse)
    def videos_page(
        request: Request, language: str | None = None, transcript: str | None = None
    ) -> HTMLResponse:
        transcript = transcript if transcript in ("available", "unavailable") else None
        rows = get_catalog().list(
            language=language or None, transcript=transcript, limit=200
        )
        # Language chips are built from a transcript-unfiltered scan so they stay
        # stable whatever the transcript filter is.
        langs = sorted(
            {r.get("language") for r in get_catalog().list(limit=10000) if r.get("language")}
        )
        return templates.TemplateResponse(
            request,
            "videos.html",
            {
                "active": "videos",
                "videos": rows,
                "languages": langs,
                "language": language,
                "transcript": transcript,
            },
        )

    def _media_ctx(
        request: Request,
        *,
        active: str,
        title: str,
        url: str,
        storage_uri: str | None,
        audio_url: str,
        badges: list[str],
        message: str | None,
    ) -> HTMLResponse:
        transcript = get_storage().load_transcript(storage_uri) if storage_uri else None
        segments = _segments_vm(transcript)
        handle = get_storage().open_audio(storage_uri) if storage_uri else None
        # MinIO -> play straight from the presigned URL (seekable); local -> our
        # own /audio route (FileResponse, also seekable).
        audio_src = handle.url if (handle and handle.url) else (audio_url if handle else None)
        return templates.TemplateResponse(
            request,
            "media.html",
            {
                "active": active,
                "title": title,
                "url": url,
                "badges": [b for b in badges if b],
                "audio_url": audio_src,
                "segments": segments,
                "plain_text": (transcript or {}).get("text") if not segments else None,
                "message": message,
            },
        )

    @router.get("/jobs/{job_id}/transcript", response_class=HTMLResponse)
    def transcript_page(request: Request, job_id: str) -> HTMLResponse:
        job = store.get(job_id)
        message = None
        if job is None:
            message = "Job introuvable."
        elif not job.result_uri:
            message = f"Job non terminé (statut : {job.status.value})."
        return _media_ctx(
            request,
            active="jobs",
            title=job.url if job else "Transcript",
            url=job.url if job else "",
            storage_uri=job.result_uri if job else None,
            audio_url=f"/ui/jobs/{job_id}/audio",
            badges=list(job.languages) if job else [],
            message=message,
        )

    @router.get("/jobs/{job_id}/audio")
    def job_audio(job_id: str):
        job = store.get(job_id)
        handle = get_storage().open_audio(job.result_uri) if job and job.result_uri else None
        if handle is None:
            raise HTTPException(status_code=404, detail="audio not found")
        return _audio_response(handle)

    @router.get("/videos/{video_id}", response_class=HTMLResponse)
    def video_detail(request: Request, video_id: str) -> HTMLResponse:
        video = get_catalog().get(video_id)
        if video is None:
            return _media_ctx(
                request,
                active="videos",
                title="Vidéo",
                url="",
                storage_uri=None,
                audio_url="",
                badges=[],
                message="Vidéo introuvable dans le catalogue.",
            )
        return _media_ctx(
            request,
            active="videos",
            title=video.get("title") or video.get("url") or video_id,
            url=video.get("url") or "",
            storage_uri=video.get("storage_uri"),
            audio_url=f"/ui/videos/{video_id}/audio",
            badges=[video.get("language"), video.get("channel")],
            message=None,
        )

    @router.get("/videos/{video_id}/audio")
    def video_audio(video_id: str):
        video = get_catalog().get(video_id)
        uri = video.get("storage_uri") if video else None
        handle = get_storage().open_audio(uri) if uri else None
        if handle is None:
            raise HTTPException(status_code=404, detail="audio not found")
        return _audio_response(handle)

    @router.post("/videos/zip")
    def videos_zip(
        video_ids: list[str] = Form(default=[]), scope: str = Form(default="")
    ) -> FileResponse:
        """Bundle the audio (+ transcript.json) of the selected videos into a ZIP.

        `scope=all` zips the whole catalog; otherwise only the checked `video_ids`.
        The zip is streamed from a temp file that is removed once the response ends.
        """
        catalog, storage = get_catalog(), get_storage()
        ids = (
            [v["video_id"] for v in catalog.list(limit=10_000)]
            if scope == "all"
            else [i for i in video_ids if i]
        )
        if not ids:
            raise HTTPException(status_code=400, detail="aucune vidéo sélectionnée")

        tmp = tempfile.NamedTemporaryFile(prefix="toumai_", suffix=".zip", delete=False)
        tmp.close()
        added = 0
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for vid in ids:
                video = catalog.get(vid)
                uri = video.get("storage_uri") if video else None
                if not uri:
                    continue
                audio = storage.read_audio(uri)
                if audio is not None:
                    filename, chunks = audio
                    with zf.open(f"{vid}/{filename}", "w") as dest:
                        for chunk in chunks:
                            dest.write(chunk)
                    added += 1
                transcript = storage.load_transcript(uri)
                if transcript is not None:
                    zf.writestr(
                        f"{vid}/transcript.json",
                        json.dumps(transcript, ensure_ascii=False, indent=2),
                    )
                # metadata.json from storage, falling back to the catalog row
                metadata = storage.load_metadata(uri) or video
                zf.writestr(
                    f"{vid}/metadata.json",
                    json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                )
        if added == 0:
            os.unlink(tmp.name)
            raise HTTPException(status_code=404, detail="aucun audio pour cette sélection")
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename="toumai_audios.zip",
            background=BackgroundTask(os.unlink, tmp.name),
        )

    def _delete_video(video_id: str) -> None:
        video = get_catalog().get(video_id)
        if video and video.get("storage_uri"):
            get_storage().delete(video["storage_uri"])  # drop the audio + json
        get_catalog().delete(video_id)

    @router.post("/videos/{video_id}/delete", response_class=HTMLResponse)
    def delete_video(video_id: str) -> HTMLResponse:
        _delete_video(video_id)
        return HTMLResponse("")  # HTMX swaps the row out

    @router.post("/videos/bulk-delete", response_class=HTMLResponse)
    def bulk_delete_videos(
        request: Request,
        video_ids: list[str] = Form(default=[]),
        language: str | None = None,
        transcript: str | None = None,
    ) -> HTMLResponse:
        transcript = transcript if transcript in ("available", "unavailable") else None
        for vid in video_ids:
            if vid:
                _delete_video(vid)
        rows = get_catalog().list(language=language or None, transcript=transcript, limit=200)
        return templates.TemplateResponse(request, "partials/videos_rows.html", {"videos": rows})

    # --------------------------------------------------------------- fragments
    @router.get("/partials/kpis", response_class=HTMLResponse)
    def partial_kpis(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "partials/kpis.html", {"kpis": _kpis()})

    def _jobs_rows(request: Request, status: str | None, selectable: bool) -> HTMLResponse:
        status_enum = JobStatus(status) if status in STATUS_META else None
        jobs = [_job_vm(j) for j in store.list(status=status_enum, limit=100)]
        return templates.TemplateResponse(
            request,
            "partials/jobs_rows.html",
            {"jobs": jobs, "status": status, "selectable": selectable},
        )

    @router.get("/partials/jobs-rows", response_class=HTMLResponse)
    def partial_jobs_rows(
        request: Request, status: str | None = None, limit: int = 100, selectable: bool = False
    ) -> HTMLResponse:
        status_enum = JobStatus(status) if status in STATUS_META else None
        jobs = [_job_vm(j) for j in store.list(status=status_enum, limit=limit)]
        return templates.TemplateResponse(
            request,
            "partials/jobs_rows.html",
            {"jobs": jobs, "status": status, "selectable": selectable},
        )

    @router.get("/api/timeseries")
    def api_timeseries(days: int = 14) -> dict:
        return _fill_timeseries(store.timeseries(days=days), days=days)

    # ----------------------------------------------------------------- actions
    @router.post("/jobs/{job_id}/retry", response_class=HTMLResponse)
    def retry(
        request: Request,
        job_id: str,
        status: str | None = Form(default=None),
        selectable: bool = Form(default=False),
    ) -> HTMLResponse:
        job = store.get(job_id)
        if job is not None:
            store.update_status(job_id, JobStatus.PENDING)
            publisher.publish(
                settings.topic_job_requested,
                key=job_id,
                event={"job_id": job_id, "url": job.url, "languages": job.languages},
            )
        return _jobs_rows(request, status, selectable)

    @router.post("/jobs/{job_id}/delete", response_class=HTMLResponse)
    def delete_job(
        request: Request,
        job_id: str,
        status: str | None = Form(default=None),
        selectable: bool = Form(default=False),
    ) -> HTMLResponse:
        store.delete(job_id)
        return _jobs_rows(request, status, selectable)

    @router.post("/jobs/bulk-delete", response_class=HTMLResponse)
    def bulk_delete_jobs(
        request: Request,
        job_ids: list[str] = Form(default=[]),
        status: str | None = None,
        selectable: bool = True,
    ) -> HTMLResponse:
        for jid in job_ids:
            if jid:
                store.delete(jid)
        return _jobs_rows(request, status, selectable)

    @router.post("/process", response_class=HTMLResponse)
    def submit_url(
        request: Request, url: str = Form(...), languages: str = Form(default="")
    ) -> HTMLResponse:
        url = url.strip()
        if not url:
            return templates.TemplateResponse(
                request, "partials/flash.html", {"ok": False, "message": "URL vide."}
            )
        vid = extract_video_id(url)
        # Skip if this video is already queued/running/ingested (dedup by video id).
        if vid and store.existing_video_ids([vid]):
            return templates.TemplateResponse(
                request,
                "partials/flash.html",
                {"warn": True, "message": f"Vidéo déjà présente (id {vid}) — job non créé."},
            )
        langs = _parse_languages(languages, settings.languages)
        job_id = uuid.uuid4().hex
        store.create(Job(job_id=job_id, url=url, languages=langs, video_id=vid))
        publisher.publish(
            settings.topic_job_requested,
            key=job_id,
            event={"job_id": job_id, "url": url, "languages": langs},
        )
        return templates.TemplateResponse(
            request,
            "partials/flash.html",
            {"ok": True, "message": f"Job créé : {job_id[:8]} ({', '.join(langs)})"},
        )

    def _queue_dedup(rows: list[tuple[str, list[str], str | None]]) -> tuple[int, int]:
        """Create + publish a job per row, skipping videos already known or repeated.

        Dedup is by YouTube video id, against existing non-failed jobs *and* within
        this same batch. Returns (accepted, duplicates).
        """
        already = store.existing_video_ids({vid for _, _, vid in rows if vid})
        seen: set[str] = set()
        accepted = 0
        duplicates = 0
        events: list[tuple[str, dict]] = []
        for url, langs, vid in rows:
            if vid and (vid in already or vid in seen):
                duplicates += 1
                continue
            job_id = uuid.uuid4().hex
            store.create(Job(job_id=job_id, url=url, languages=langs, video_id=vid))
            events.append((job_id, {"job_id": job_id, "url": url, "languages": langs}))
            if vid:
                seen.add(vid)
            accepted += 1
        publisher.publish_batch(settings.topic_job_requested, events)
        return accepted, duplicates

    @router.post("/process/playlist", response_class=HTMLResponse)
    def submit_playlist(
        request: Request, playlist: str = Form(...), languages: str = Form(default="")
    ) -> HTMLResponse:
        playlist = playlist.strip()
        if extract_playlist_id(playlist) is None:
            return templates.TemplateResponse(
                request,
                "partials/flash.html",
                {"ok": False, "message": "Aucune playlist détectée (lien ou ID invalide)."},
            )
        langs = _parse_languages(languages, settings.languages)
        entries = get_playlist_resolver().resolve(playlist)
        if not entries:
            return templates.TemplateResponse(
                request,
                "partials/flash.html",
                {"warn": True, "message": "Playlist vide ou illisible — aucun job créé."},
            )
        accepted, duplicates = _queue_dedup([(e.url, langs, e.video_id) for e in entries])
        return templates.TemplateResponse(
            request,
            "partials/flash.html",
            {
                "ok": True,
                "message": (
                    f"Playlist : {accepted} job(s) créé(s)"
                    + (f", {duplicates} doublon(s) ignoré(s)" if duplicates else "")
                    + f" ({', '.join(langs)})"
                ),
            },
        )

    @router.post("/process/csv", response_class=HTMLResponse)
    async def submit_csv(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
        raw = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        if reader.fieldnames is None or "url" not in [f.strip().lower() for f in reader.fieldnames]:
            return templates.TemplateResponse(
                request,
                "partials/csv_result.html",
                {"ok": False, "message": "Le CSV doit contenir une colonne 'url'."},
            )

        errors: list[str] = []
        rows: list[tuple[str, list[str], str | None]] = []
        for i, row in enumerate(reader, start=2):
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            url = norm.get("url", "")
            if not url:
                errors.append(f"ligne {i} : url vide")
                continue
            langs = _parse_languages(norm.get("lang") or norm.get("languages"), settings.languages)
            rows.append((url, langs, extract_video_id(url)))

        # Dedup against already-known videos + within this same file.
        accepted, duplicates = _queue_dedup(rows)
        return templates.TemplateResponse(
            request,
            "partials/csv_result.html",
            {
                "ok": True,
                "accepted": accepted,
                "errors": errors,
                "duplicates": duplicates,
                "filename": file.filename,
            },
        )

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")
