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
import threading
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from ..channel import channel_key as extract_channel_key, normalize_channel_url
from ..env_file import update_env
from ..transcript_quality import assess as assess_quality
from ..config import Settings, reload_into
from ..domain.models import Job, JobStatus, WatchedChannel
from ..playlist import extract_playlist_id
from ..silver.storage import BronzeEntry
from ..video_id import extract_video_id
from ..domain.ports import (
    ChannelWatchStorePort,
    EventPublisherPort,
    JobStorePort,
    MetadataRepositoryPort,
    PlaylistResolverPort,
    StoragePort,
    VeilleRunLogPort,
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

# Rows per page for the paginated tables (videos, jobs).
_PAGE_SIZE = 50


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


def _fmt_relative(value: datetime | None) -> str:
    """Human 'time ago' label (French), for the veille monitoring log."""
    if value is None:
        return "jamais"
    secs = int((datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())
    if secs < 0:
        return "à l'instant"
    if secs < 60:
        return "à l'instant"
    mins = secs // 60
    if mins < 60:
        return f"il y a {mins} min"
    hours = mins // 60
    if hours < 24:
        return f"il y a {hours} h"
    days = hours // 24
    return f"il y a {days} j"


def _job_vm(job: Job, *, video_exists: bool = True) -> dict:
    """View-model: everything a template needs, pre-formatted.

    ``video_exists`` says whether this job's ingested video is still in the
    catalog. A completed job whose video was deleted can no longer be opened, so
    we offer a *replay* instead; and deleting such a job needs no "also delete
    the video?" prompt.
    """
    meta = STATUS_META.get(job.status.value, STATUS_META["pending"])
    has_video = bool(job.video_id) and video_exists
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
        "has_video": has_video,
        "can_replay": job.status is JobStatus.COMPLETED and bool(job.video_id) and not video_exists,
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


def _fmt_duration(seconds: float | int | None) -> str:
    """Readable audio length: '2 h 05', '43 min', or '18 s'."""
    secs = int(seconds or 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d}"
    if m:
        return f"{m} min"
    return f"{s} s"


# Human label + Tailwind classes per estimated transcription-quality level.
QUALITY_META: dict[str, dict[str, str]] = {
    "Excellente": {
        "badge": "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
        "bar": "bg-emerald-500",
    },
    "Bonne": {"badge": "bg-teal-50 text-teal-700 ring-teal-600/20", "bar": "bg-teal-500"},
    "Moyenne": {"badge": "bg-amber-50 text-amber-700 ring-amber-600/20", "bar": "bg-amber-500"},
    "Faible": {"badge": "bg-rose-50 text-rose-700 ring-rose-600/20", "bar": "bg-rose-500"},
    "Indisponible": {
        "badge": "bg-slate-100 text-slate-500 ring-slate-500/20",
        "bar": "bg-slate-300",
    },
}

# Human label per source provenance bucket.
SOURCE_KIND_LABEL = {
    "human": "Sous-titres auteur (humain)",
    "asr": "Reconnaissance vocale (ASR)",
    "translated": "Traduction automatique",
    "none": "Aucune source",
}


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


def _quality_vm(q: dict, duration_s) -> dict:
    """Pre-format the assessed quality metrics for the media template."""
    meta = QUALITY_META.get(q["label"], QUALITY_META["Indisponible"])
    cov = q["coverage"]
    return {
        "available": q["available"],
        "label": q["label"],
        "badge": meta["badge"],
        "bar": meta["bar"],
        "score": q["score"],
        "coverage_pct": round(cov * 100) if cov is not None else None,
        "source_kind": q["source_kind"],
        "source_label": SOURCE_KIND_LABEL.get(q["source_kind"], "—"),
        "n_segments": q["n_segments"],
        "words": q["words"],
        "wpm": q["wpm"],
        "start_offset": _fmt_duration(q["start_offset_s"]) if q["start_offset_s"] else "0 s",
        "max_gap": _fmt_duration(q["max_gap_s"]) if q["max_gap_s"] else "0 s",
        "duration": _fmt_duration(duration_s) if duration_s else "—",
    }


def _cleaning_vm(metadata: dict | None) -> dict | None:
    """Métriques *pertinentes* du nettoyage silver, pour le détail audio.

    Lues depuis la metadata.json silver (moteur vad : champs ``silver_*`` + bloc
    ``silver_metrics`` ; moteur ffmpeg : durées + ``silver_refined_at``). Retourne
    ``None`` quand il ne s'agit pas d'une entrée nettoyée (couche bronze, ou entrée
    trop ancienne sans traçabilité) — le panneau est alors masqué.
    """
    if not metadata:
        return None
    orig = metadata.get("original_duration_s")
    new = metadata.get("duration_s")
    # Seule une entrée réellement raffinée porte ces champs.
    if (
        orig is None
        or new is None
        or not (metadata.get("silver_refined_at") or metadata.get("silver_engine"))
    ):
        return None

    orig = float(orig)
    new = float(new)
    removed = max(orig - new, 0.0)
    engine = metadata.get("silver_engine", "ffmpeg")

    # Répartition de ce qui a été retiré (moteur vad ; on n'affiche que le non-nul).
    breakdown: list[dict] = []
    for key, label in (
        ("silver_music_removed_s", "Musique"),
        ("silver_applause_removed_s", "Applaudissements"),
        ("silver_noise_removed_s", "Bruit"),
        ("silver_silence_removed_s", "Silence"),
    ):
        val = metadata.get(key)
        if val:
            breakdown.append({"label": label, "value": _fmt_duration(val)})

    vm = {
        "engine": engine,
        "engine_label": "VAD · Silero + YAMNet" if engine == "vad" else "ffmpeg · demucs",
        "backends": (
            f"{metadata.get('silver_vad_backend', '?')} + "
            f"{metadata.get('silver_classifier_backend', '?')}"
            if engine == "vad"
            else None
        ),
        "original": _fmt_duration(orig),
        "cleaned": _fmt_duration(new),
        "removed": _fmt_duration(removed),
        "removed_pct": round(100 * removed / orig) if orig else 0,
        "breakdown": breakdown,
        "speech_ratio_pct": None,
        "speech_segments": None,
    }
    # Détails VAD (part de parole conservée, nb de segments de parole).
    vad_metrics = (metadata.get("silver_metrics") or {}).get("vad") or {}
    if vad_metrics:
        ratio = vad_metrics.get("speech_ratio")
        vm["speech_ratio_pct"] = round(ratio * 100) if ratio is not None else None
        vm["speech_segments"] = vad_metrics.get("num_speech_segments")
    return vm


def _stats_vm(s: dict) -> dict:
    """Pre-format catalog aggregates for the analytics page."""
    videos = s.get("videos", 0)
    total_dur = s.get("duration_s", 0)
    avail = s.get("with_transcript", 0)

    def _rows(items: list[dict], key: str) -> list[dict]:
        out = []
        for r in items:
            d = r.get("duration_s", 0)
            out.append(
                {
                    "name": r.get(key) or "?",
                    "videos": r.get("videos", 0),
                    "duration": _fmt_duration(d),
                    "hours": round(d / 3600, 1),
                    "pct": round(100 * d / total_dur) if total_dur else 0,
                }
            )
        return out

    return {
        "videos": videos,
        "duration": _fmt_duration(total_dur),
        "hours": round(total_dur / 3600, 1),
        "avg_duration": _fmt_duration(total_dur / videos) if videos else "—",
        "with_transcript": avail,
        "without_transcript": s.get("without_transcript", 0),
        "transcript_pct": round(100 * avail / videos) if videos else 0,
        "by_language": _rows(s.get("by_language", []), "language"),
        "by_provider": _rows(s.get("by_provider", []), "provider"),
        "by_source": s.get("by_source", []),
    }


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
    get_channel_store: Callable[[], ChannelWatchStorePort],
    get_veille_run_log: Callable[[], VeilleRunLogPort],
    run_veille: Callable[[], dict],
    get_silver_store: Callable[[], object] | None = None,
    get_silver_pipeline: Callable[[], object] | None = None,
    get_silver_storage: Callable[[], StoragePort] | None = None,
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
        audio = _stats_vm(get_catalog().stats())
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "active": "dashboard",
                "kpis": _kpis(),
                "chart": chart,
                "recent": recent,
                "audio": audio,
            },
        )

    @router.get("/jobs", response_class=HTMLResponse)
    def jobs_page(
        request: Request, status: str | None = None, q: str | None = None
    ) -> HTMLResponse:
        active_status = status if status in STATUS_META else None
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {
                "active": "jobs",
                "status": active_status,
                "q": (q or "").strip() or None,
                "kpis": _kpis(),
            },
        )

    @router.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "stats.html",
            {"active": "stats", "stats": _stats_vm(get_catalog().stats())},
        )

    def _env_path() -> Path:
        return Path(os.environ.get("TOUMAI_ENV_FILE", ".env"))

    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        cfg = {
            "languages": ", ".join(settings.languages),
            "proxies": "\n".join(settings.download_proxies),
            "webshare_username": settings.webshare_proxy_username or "",
            "webshare_password": settings.webshare_proxy_password or "",
            "cookies_from_browser": settings.ytdlp_cookies_from_browser or "",
            "veille_recent_limit": settings.veille_recent_limit,
            "accept_youtube_asr": settings.accept_youtube_asr,
            "enable_transcript_translation": settings.enable_transcript_translation,
            "max_concurrent_downloads": settings.max_concurrent_downloads,
            "download_delay_min_s": settings.download_delay_min_s,
            "download_delay_max_s": settings.download_delay_max_s,
            # silver (nettoyage audio bronze -> silver)
            "silver_auto_process": settings.silver_auto_process,
            "silver_remove_music": settings.silver_remove_music,
            "silver_remove_nonspeech": settings.silver_remove_nonspeech,
            "silver_nonspeech_keywords": ", ".join(settings.silver_nonspeech_keywords),
            "silver_demucs_model": settings.silver_demucs_model,
            "silver_silence_threshold_db": settings.silver_silence_threshold_db,
            "silver_min_silence_s": settings.silver_min_silence_s,
            "silver_padding_s": settings.silver_padding_s,
            # read-only infra (informational)
            "env_path": str(_env_path()),
            "postgres_dsn": settings.postgres_dsn,
            "kafka": settings.kafka_bootstrap_servers,
            "storage_backend": settings.storage_backend,
        }
        return templates.TemplateResponse(
            request, "config.html", {"active": "settings", "cfg": cfg}
        )

    @router.post("/settings", response_class=HTMLResponse)
    def settings_save(
        request: Request,
        languages: str = Form(default=""),
        proxies: str = Form(default=""),
        webshare_username: str = Form(default=""),
        webshare_password: str = Form(default=""),
        cookies_from_browser: str = Form(default=""),
        veille_recent_limit: int = Form(default=15),
        max_concurrent_downloads: int = Form(default=3),
        download_delay_min_s: float = Form(default=1.0),
        download_delay_max_s: float = Form(default=4.0),
        accept_youtube_asr: bool = Form(default=False),
        enable_transcript_translation: bool = Form(default=False),
        silver_auto_process: bool = Form(default=False),
        silver_remove_music: bool = Form(default=False),
        silver_remove_nonspeech: bool = Form(default=False),
        silver_nonspeech_keywords: str = Form(default=""),
        silver_demucs_model: str = Form(default="htdemucs"),
        silver_silence_threshold_db: float = Form(default=-35.0),
        silver_min_silence_s: float = Form(default=0.5),
        silver_padding_s: float = Form(default=0.1),
    ) -> HTMLResponse:
        proxy_list = [p.strip() for p in proxies.replace(",", "\n").splitlines() if p.strip()]
        lang_list = _parse_languages(languages, settings.languages)
        # Marqueurs non-parlés : séparés par virgule ou retour ligne ; on garde les
        # expressions multi-mots (ex. "background noise"), donc on ne coupe PAS sur l'espace.
        kw_list = [
            k.strip() for k in silver_nonspeech_keywords.replace("\n", ",").split(",") if k.strip()
        ]
        updates: dict[str, str | None] = {
            "TOUMAI_LANGUAGES": json.dumps(lang_list),
            "TOUMAI_DOWNLOAD_PROXIES": json.dumps(proxy_list) if proxy_list else None,
            "TOUMAI_WEBSHARE_PROXY_USERNAME": webshare_username.strip() or None,
            "TOUMAI_WEBSHARE_PROXY_PASSWORD": webshare_password.strip() or None,
            "TOUMAI_YTDLP_COOKIES_FROM_BROWSER": cookies_from_browser.strip() or None,
            "TOUMAI_VEILLE_RECENT_LIMIT": str(int(veille_recent_limit)),
            "TOUMAI_MAX_CONCURRENT_DOWNLOADS": str(int(max_concurrent_downloads)),
            "TOUMAI_DOWNLOAD_DELAY_MIN_S": str(float(download_delay_min_s)),
            "TOUMAI_DOWNLOAD_DELAY_MAX_S": str(float(download_delay_max_s)),
            "TOUMAI_ACCEPT_YOUTUBE_ASR": "true" if accept_youtube_asr else "false",
            "TOUMAI_ENABLE_TRANSCRIPT_TRANSLATION": (
                "true" if enable_transcript_translation else "false"
            ),
            "TOUMAI_SILVER_AUTO_PROCESS": "true" if silver_auto_process else "false",
            "TOUMAI_SILVER_REMOVE_MUSIC": "true" if silver_remove_music else "false",
            "TOUMAI_SILVER_REMOVE_NONSPEECH": "true" if silver_remove_nonspeech else "false",
            "TOUMAI_SILVER_NONSPEECH_KEYWORDS": json.dumps(kw_list) if kw_list else None,
            "TOUMAI_SILVER_DEMUCS_MODEL": silver_demucs_model.strip() or "htdemucs",
            "TOUMAI_SILVER_SILENCE_THRESHOLD_DB": str(float(silver_silence_threshold_db)),
            "TOUMAI_SILVER_MIN_SILENCE_S": str(float(silver_min_silence_s)),
            "TOUMAI_SILVER_PADDING_S": str(float(silver_padding_s)),
        }
        update_env(_env_path(), updates)
        # Apply live: re-read .env into the shared Settings instance so every
        # request (UI + API) uses the new values immediately — a refresh is enough.
        reload_into(settings, _env_path())
        return templates.TemplateResponse(
            request,
            "partials/flash.html",
            {
                "ok": True,
                "message": "Configuration enregistrée et appliquée. Le worker se mettra à jour au prochain job.",
            },
        )

    @router.post("/settings/reload", response_class=HTMLResponse)
    def settings_reload(request: Request) -> HTMLResponse:
        """Re-read .env into the live Settings (e.g. after editing the file by hand)."""
        reload_into(settings, _env_path())
        return templates.TemplateResponse(
            request,
            "partials/flash.html",
            {"ok": True, "message": "Configuration rechargée depuis .env."},
        )

    # ---------------------------------------------------------------- silver
    # Nettoyage bronze -> silver (demucs + ffmpeg). Lourd : on le lance dans un
    # thread pour ne pas bloquer la requête, avec un garde anti-concurrence et le
    # dernier bilan gardé en mémoire pour l'affichage.
    silver_state: dict = {"running": False, "last": None}
    silver_lock = threading.Lock()

    def _silver_counts() -> dict:
        if get_silver_store is None:
            return {"available": False}
        try:
            bronze, silver = get_silver_store().counts()
        except Exception as exc:  # noqa: BLE001 - MinIO injoignable / non configuré
            return {"available": False, "error": str(exc)}
        return {
            "available": True,
            "bronze": bronze,
            "silver": silver,
            "pending": max(bronze - silver, 0),
            "running": silver_state["running"],
            "last": silver_state["last"],
        }

    def _silver_worker(force: bool) -> None:
        try:
            summary = get_silver_pipeline().run(force=force)  # type: ignore[union-attr]
            silver_state["last"] = summary.as_dict()
        except Exception as exc:  # noqa: BLE001 - on remonte l'erreur dans l'état
            silver_state["last"] = {"error": str(exc)}
        finally:
            silver_state["running"] = False

    def _silver_listing() -> dict:
        """Sépare les entrées bronze en « traitées » (silver) et « en attente ».

        Les titres viennent du catalogue (une seule requête), joints par video_id ;
        une entrée sans ligne catalogue reste listée avec son id.
        """
        if get_silver_store is None:
            return {"available": False, "done": [], "pending": []}
        try:
            store = get_silver_store()
            entries = store.iter_bronze_entries()
            done_pairs = store.silver_pairs()
        except Exception as exc:  # noqa: BLE001 - MinIO injoignable / non configuré
            return {"available": False, "error": str(exc), "done": [], "pending": []}

        catalog = {r["video_id"]: r for r in get_catalog().list(limit=10_000)}
        done: list[dict] = []
        pending: list[dict] = []
        for entry in entries:
            row = catalog.get(entry.video_id) or {}
            item = {
                "video_id": entry.video_id,
                "language": entry.language,
                "title": row.get("title") or row.get("url") or entry.video_id,
                "channel": row.get("channel"),
                "in_catalog": entry.video_id in catalog,
            }
            (done if (entry.language, entry.video_id) in done_pairs else pending).append(item)
        # Ordre stable et lisible : par titre.
        done.sort(key=lambda i: (i["title"] or "").lower())
        pending.sort(key=lambda i: (i["title"] or "").lower())
        return {
            "available": True,
            "done": done,
            "pending": pending,
            "done_count": len(done),
            "pending_count": len(pending),
        }

    @router.get("/silver", response_class=HTMLResponse)
    def silver_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "silver.html", {"active": "silver"})

    @router.get("/partials/silver", response_class=HTMLResponse)
    def partial_silver(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/silver_card.html", {"silver": _silver_counts()}
        )

    @router.get("/partials/silver-list", response_class=HTMLResponse)
    def partial_silver_list(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/silver_list.html", {"listing": _silver_listing()}
        )

    @router.post("/silver/zip")
    def silver_zip(
        video_ids: list[str] = Form(default=[]), scope: str = Form(default="")
    ) -> FileResponse:
        """Bundle l'audio nettoyé (+ transcript/metadata) des vidéos silver dans un ZIP.

        `scope=all` prend toutes les entrées déjà en silver ; sinon uniquement la
        sélection. Chaque case porte un identifiant composite ``<language>/<video_id>``
        car l'URI silver dépend de la langue. Lu depuis le bucket silver.
        """
        if get_silver_store is None or get_silver_storage is None:
            raise HTTPException(status_code=400, detail="couche silver indisponible")
        store = get_silver_store()
        silver_storage = get_silver_storage()

        if scope == "all":
            pairs = sorted(store.silver_pairs())
        else:
            pairs = []
            for raw in video_ids:
                language, _, vid = (raw or "").partition("/")
                if language and vid:
                    pairs.append((language, vid))
        if not pairs:
            raise HTTPException(status_code=400, detail="aucune vidéo nettoyée sélectionnée")

        tmp = tempfile.NamedTemporaryFile(prefix="toumai_silver_", suffix=".zip", delete=False)
        tmp.close()
        added = 0
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for language, vid in pairs:
                uri = store.silver_uri(BronzeEntry(language=language, video_id=vid))
                audio = silver_storage.read_audio(uri)
                if audio is not None:
                    filename, chunks = audio
                    with zf.open(f"{vid}/{filename}", "w") as dest:
                        for chunk in chunks:
                            dest.write(chunk)
                    added += 1
                transcript = silver_storage.load_transcript(uri)
                if transcript is not None:
                    zf.writestr(
                        f"{vid}/transcript.json",
                        json.dumps(transcript, ensure_ascii=False, indent=2),
                    )
                metadata = silver_storage.load_metadata(uri)
                if metadata is not None:
                    zf.writestr(
                        f"{vid}/metadata.json",
                        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
                    )
        if added == 0:
            os.unlink(tmp.name)
            raise HTTPException(status_code=404, detail="aucun audio nettoyé pour cette sélection")
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename="toumai_silver.zip",
            background=BackgroundTask(os.unlink, tmp.name),
        )

    @router.post("/silver/run", response_class=HTMLResponse)
    def silver_run(request: Request, force: bool = Form(default=False)) -> HTMLResponse:
        if get_silver_pipeline is None:
            return templates.TemplateResponse(
                request,
                "partials/flash.html",
                {"ok": False, "message": "Couche silver non disponible (MinIO requis)."},
            )
        with silver_lock:
            if silver_state["running"]:
                return templates.TemplateResponse(
                    request,
                    "partials/flash.html",
                    {"warn": True, "message": "Un nettoyage silver est déjà en cours."},
                )
            silver_state["running"] = True
        threading.Thread(target=_silver_worker, args=(force,), daemon=True).start()
        return templates.TemplateResponse(
            request,
            "partials/flash.html",
            {"ok": True, "message": "Nettoyage silver lancé en arrière-plan."},
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
        request: Request,
        language: str | None = None,
        transcript: str | None = None,
        provider: str | None = None,
        q: str | None = None,
        silver: str | None = None,
        page: int = 1,
    ) -> HTMLResponse:
        transcript = transcript if transcript in ("available", "unavailable") else None
        silver = silver if silver in ("done", "pending") else None
        q = (q or "").strip() or None
        page = max(page, 1)
        offset = (page - 1) * _PAGE_SIZE
        silver_ids = _silver_pairs()

        if silver is None:
            # Cas courant : pagination DB (une ligne de plus pour savoir s'il y a une suite).
            rows = get_catalog().list(
                language=language or None,
                transcript=transcript,
                provider=provider or None,
                q=q,
                limit=_PAGE_SIZE + 1,
                offset=offset,
            )
            has_next = len(rows) > _PAGE_SIZE
            rows = rows[:_PAGE_SIZE]
        else:
            # Filtre silver : l'appartenance vient de MinIO (hors DB). On récupère le
            # lot filtré côté DB puis on filtre par présence silver, et on pagine à la main.
            want_done = silver == "done"
            all_rows = get_catalog().list(
                language=language or None,
                transcript=transcript,
                provider=provider or None,
                q=q,
                limit=10_000,
            )
            filtered = [
                r
                for r in all_rows
                if ((r.get("language"), r["video_id"]) in silver_ids) == want_done
            ]
            has_next = offset + _PAGE_SIZE < len(filtered)
            rows = filtered[offset : offset + _PAGE_SIZE]

        # Chips come from the cheap aggregate (one grouped query) instead of a full
        # table scan — stable regardless of the active filters.
        st = get_catalog().stats()
        langs = sorted(r["language"] for r in st["by_language"] if r["language"] not in (None, "?"))
        providers = sorted(
            r["provider"] for r in st["by_provider"] if r["provider"] not in (None, "?")
        )
        return templates.TemplateResponse(
            request,
            "videos.html",
            {
                "active": "videos",
                "videos": rows,
                "languages": langs,
                "providers": providers,
                "language": language,
                "transcript": transcript,
                "provider": provider,
                "q": q,
                "silver": silver,
                "page": page,
                "has_next": has_next,
                "has_prev": page > 1,
                "silver_ids": silver_ids,
            },
        )

    def _bronze_to_language_video(bronze_uri: str | None) -> tuple[str, str] | None:
        """Extrait ``(language, video_id)`` d'une URI bronze ``s3://…/bronze/<lang>/<vid>``."""
        if not bronze_uri or not bronze_uri.startswith("s3://"):
            return None
        parts = bronze_uri[len("s3://") :].split("/")
        # ['<bucket>', 'bronze', '<lang>', '<vid>']
        if len(parts) >= 4 and parts[1] == "bronze":
            return parts[2], parts[3]
        return None

    def _silver_view(bronze_uri: str | None) -> dict:
        """Dispo + URI de la version silver correspondant à une entrée bronze."""
        parsed = _bronze_to_language_video(bronze_uri)
        if parsed is None or get_silver_store is None or get_silver_storage is None:
            return {"available": False, "uri": None}
        language, video_id = parsed
        entry = BronzeEntry(language=language, video_id=video_id)
        try:
            store = get_silver_store()
            return {"available": store.silver_exists(entry), "uri": store.silver_uri(entry)}
        except Exception:  # noqa: BLE001 - MinIO injoignable / silver non configuré
            return {"available": False, "uri": None}

    def _silver_pairs() -> set[tuple[str, str]]:
        """Ensemble ``(language, video_id)`` déjà en silver, pour badger la liste."""
        if get_silver_store is None:
            return set()
        try:
            return get_silver_store().silver_pairs()
        except Exception:  # noqa: BLE001 - MinIO injoignable / silver non configuré
            return set()

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
        layer: str = "bronze",
        silver_available: bool = False,
        bronze_href: str | None = None,
        silver_href: str | None = None,
    ) -> HTMLResponse:
        # On lit dans la couche demandée : bronze via le stockage principal, silver
        # via l'adaptateur bindé au bucket silver (mêmes méthodes de lecture).
        is_silver = layer == "silver"
        storage = get_silver_storage() if (is_silver and get_silver_storage) else get_storage()

        transcript = storage.load_transcript(storage_uri) if storage_uri else None
        segments = _segments_vm(transcript)
        handle = storage.open_audio(storage_uri) if storage_uri else None
        # MinIO -> play straight from the presigned URL (seekable); local -> our
        # own /audio route (FileResponse, also seekable).
        audio_src = handle.url if (handle and handle.url) else (audio_url if handle else None)
        # Transcription quality (coverage / desync / provenance) — estimated.
        metadata = storage.load_metadata(storage_uri) if storage_uri else None
        duration_s = (metadata or {}).get("duration_s")
        quality = _quality_vm(
            assess_quality(
                (transcript or {}).get("segments"),
                duration_s,
                (transcript or {}).get("source"),
            ),
            duration_s,
        )
        # Métriques de transformation : uniquement sur la couche silver (nettoyée).
        cleaning = _cleaning_vm(metadata) if layer == "silver" else None
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
                "quality": quality,
                "cleaning": cleaning,
                "message": message,
                # Bascule bronze/silver
                "layer": layer,
                "silver_available": silver_available,
                "bronze_href": bronze_href,
                "silver_href": silver_href,
            },
        )

    @router.get("/jobs/{job_id}/transcript", response_class=HTMLResponse)
    def transcript_page(request: Request, job_id: str, layer: str = "bronze") -> HTMLResponse:
        job = store.get(job_id)
        message = None
        if job is None:
            message = "Job introuvable."
        elif not job.result_uri:
            message = f"Job non terminé (statut : {job.status.value})."
        bronze_uri = job.result_uri if job else None
        silver = _silver_view(bronze_uri)
        is_silver = layer == "silver" and silver["available"]
        return _media_ctx(
            request,
            active="jobs",
            title=job.url if job else "Transcript",
            url=job.url if job else "",
            storage_uri=silver["uri"] if is_silver else bronze_uri,
            audio_url=f"/ui/jobs/{job_id}/audio?layer={'silver' if is_silver else 'bronze'}",
            badges=list(job.languages) if job else [],
            message=message,
            layer="silver" if is_silver else "bronze",
            silver_available=silver["available"],
            bronze_href=f"/ui/jobs/{job_id}/transcript",
            silver_href=(
                f"/ui/jobs/{job_id}/transcript?layer=silver" if silver["available"] else None
            ),
        )

    @router.get("/jobs/{job_id}/audio")
    def job_audio(job_id: str, layer: str = "bronze"):
        job = store.get(job_id)
        bronze_uri = job.result_uri if job else None
        if layer == "silver":
            silver = _silver_view(bronze_uri)
            handle = (
                get_silver_storage().open_audio(silver["uri"])
                if silver["available"] and get_silver_storage
                else None
            )
        else:
            handle = get_storage().open_audio(bronze_uri) if bronze_uri else None
        if handle is None:
            raise HTTPException(status_code=404, detail="audio not found")
        return _audio_response(handle)

    @router.get("/videos/{video_id}", response_class=HTMLResponse)
    def video_detail(request: Request, video_id: str, layer: str = "bronze") -> HTMLResponse:
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
        bronze_uri = video.get("storage_uri")
        silver = _silver_view(bronze_uri)
        is_silver = layer == "silver" and silver["available"]
        return _media_ctx(
            request,
            active="videos",
            title=video.get("title") or video.get("url") or video_id,
            url=video.get("url") or "",
            storage_uri=silver["uri"] if is_silver else bronze_uri,
            audio_url=f"/ui/videos/{video_id}/audio?layer={'silver' if is_silver else 'bronze'}",
            badges=[video.get("provider"), video.get("language"), video.get("channel")],
            message=None,
            layer="silver" if is_silver else "bronze",
            silver_available=silver["available"],
            bronze_href=f"/ui/videos/{video_id}",
            silver_href=f"/ui/videos/{video_id}?layer=silver" if silver["available"] else None,
        )

    @router.get("/videos/{video_id}/audio")
    def video_audio(video_id: str, layer: str = "bronze"):
        video = get_catalog().get(video_id)
        bronze_uri = video.get("storage_uri") if video else None
        if layer == "silver":
            silver = _silver_view(bronze_uri)
            handle = (
                get_silver_storage().open_audio(silver["uri"])
                if silver["available"] and get_silver_storage
                else None
            )
        else:
            handle = get_storage().open_audio(bronze_uri) if bronze_uri else None
        if handle is None:
            raise HTTPException(status_code=404, detail="audio not found")
        return _audio_response(handle)

    @router.post("/videos/zip")
    def videos_zip(
        video_ids: list[str] = Form(default=[]),
        scope: str = Form(default=""),
        layer: str = Form(default="bronze"),
    ) -> FileResponse:
        """Bundle the audio (+ transcript.json) of the selected videos into a ZIP.

        `scope=all` zips the whole catalog; otherwise only the checked `video_ids`.
        `layer=silver` bundles the cleaned version (audio + resynced transcript) read
        from the silver bucket, skipping videos that have no silver yet; `bronze`
        (default) bundles the raw ingested version. Streamed from a temp file that is
        removed once the response ends.
        """
        catalog = get_catalog()
        is_silver = (
            layer == "silver" and get_silver_store is not None and get_silver_storage is not None
        )
        storage = get_silver_storage() if is_silver else get_storage()
        ids = (
            [v["video_id"] for v in catalog.list(limit=10_000)]
            if scope == "all"
            else [i for i in video_ids if i]
        )
        if not ids:
            raise HTTPException(status_code=400, detail="aucune vidéo sélectionnée")

        prefix = "toumai_silver_" if is_silver else "toumai_"
        tmp = tempfile.NamedTemporaryFile(prefix=prefix, suffix=".zip", delete=False)
        tmp.close()
        added = 0
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
            for vid in ids:
                video = catalog.get(vid)
                bronze_uri = video.get("storage_uri") if video else None
                if not bronze_uri:
                    continue
                # En silver, on résout l'URI nettoyée et on saute les vidéos sans
                # version silver ; en bronze, on lit directement l'URI d'origine.
                if is_silver:
                    sv = _silver_view(bronze_uri)
                    uri = sv["uri"] if sv["available"] else None
                    if not uri:
                        continue
                else:
                    uri = bronze_uri
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
            detail = (
                "aucune version nettoyée pour cette sélection"
                if is_silver
                else "aucun audio pour cette sélection"
            )
            raise HTTPException(status_code=404, detail=detail)
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename="toumai_silver.zip" if is_silver else "toumai_audios.zip",
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
        provider: str | None = None,
        q: str | None = None,
    ) -> HTMLResponse:
        transcript = transcript if transcript in ("available", "unavailable") else None
        for vid in video_ids:
            if vid:
                _delete_video(vid)
        rows = get_catalog().list(
            language=language or None,
            transcript=transcript,
            provider=provider or None,
            q=(q or "").strip() or None,
            limit=_PAGE_SIZE,
        )
        return templates.TemplateResponse(
            request,
            "partials/videos_rows.html",
            {"videos": rows, "silver_ids": _silver_pairs()},
        )

    # --------------------------------------------------------------- fragments
    @router.get("/partials/kpis", response_class=HTMLResponse)
    def partial_kpis(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "partials/kpis.html", {"kpis": _kpis()})

    def _job_vms(jobs: list[Job]) -> list[dict]:
        """Build job view-models, resolving in one query which jobs still have
        their video in the catalog (drives the replay button / delete prompt)."""
        wanted = {j.video_id for j in jobs if j.video_id and j.status is JobStatus.COMPLETED}
        existing = get_catalog().existing_video_ids(wanted) if wanted else set()
        return [_job_vm(j, video_exists=j.video_id in existing) for j in jobs]

    def _jobs_rows(
        request: Request, status: str | None, selectable: bool, q: str | None = None
    ) -> HTMLResponse:
        status_enum = JobStatus(status) if status in STATUS_META else None
        jobs = _job_vms(store.list(status=status_enum, q=q or None, limit=100))
        return templates.TemplateResponse(
            request,
            "partials/jobs_rows.html",
            {"jobs": jobs, "status": status, "selectable": selectable, "q": q},
        )

    @router.get("/partials/jobs-rows", response_class=HTMLResponse)
    def partial_jobs_rows(
        request: Request,
        status: str | None = None,
        q: str | None = None,
        limit: int = 100,
        selectable: bool = False,
    ) -> HTMLResponse:
        status_enum = JobStatus(status) if status in STATUS_META else None
        jobs = _job_vms(store.list(status=status_enum, q=(q or "").strip() or None, limit=limit))
        return templates.TemplateResponse(
            request,
            "partials/jobs_rows.html",
            {"jobs": jobs, "status": status, "selectable": selectable, "q": q},
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
        q: str | None = Form(default=None),
    ) -> HTMLResponse:
        job = store.get(job_id)
        if job is not None:
            store.update_status(job_id, JobStatus.PENDING)
            publisher.publish(
                settings.topic_job_requested,
                key=job_id,
                event={"job_id": job_id, "url": job.url, "languages": job.languages},
            )
        return _jobs_rows(request, status, selectable, q)

    @router.get("/jobs/{job_id}/delete-dialog", response_class=HTMLResponse)
    def delete_job_dialog(
        request: Request,
        job_id: str,
        status: str | None = None,
        selectable: bool = False,
        q: str | None = None,
    ) -> HTMLResponse:
        """Modal asking whether to also delete the job's associated video.

        Only opened for jobs whose video is still in the catalog; jobs without a
        (surviving) video are deleted directly from the row (no prompt).
        """
        job = store.get(job_id)
        if job is None:
            return HTMLResponse("")
        video = get_catalog().get(job.video_id) if job.video_id else None
        return templates.TemplateResponse(
            request,
            "partials/job_delete_modal.html",
            {
                "job": _job_vm(job, video_exists=video is not None),
                "video": video,
                "status": status,
                "selectable": selectable,
                "q": q,
            },
        )

    @router.post("/jobs/{job_id}/delete", response_class=HTMLResponse)
    def delete_job(
        request: Request,
        job_id: str,
        status: str | None = Form(default=None),
        selectable: bool = Form(default=False),
        q: str | None = Form(default=None),
        delete_video: bool = Form(default=False),
    ) -> HTMLResponse:
        job = store.get(job_id)
        if job is not None and delete_video and job.video_id:
            _delete_video(job.video_id)  # drop the ingested audio + catalog row too
        store.delete(job_id)
        return _jobs_rows(request, status, selectable, q)

    @router.get("/jobs/bulk-delete-dialog", response_class=HTMLResponse)
    def bulk_delete_jobs_dialog(
        request: Request,
        job_ids: list[str] = Query(default=[]),
        status: str | None = None,
        selectable: bool = True,
        q: str | None = None,
    ) -> HTMLResponse:
        """Modal for a bulk job delete: how many of the selected jobs still have
        a video, so we can offer to drop those videos too."""
        ids = [j for j in job_ids if j]
        jobs = [j for j in (store.get(i) for i in ids) if j is not None]
        wanted = {j.video_id for j in jobs if j.video_id and j.status is JobStatus.COMPLETED}
        video_count = len(get_catalog().existing_video_ids(wanted)) if wanted else 0
        return templates.TemplateResponse(
            request,
            "partials/jobs_bulk_delete_modal.html",
            {
                "job_ids": ids,
                "job_count": len(ids),
                "video_count": video_count,
                "status": status,
                "selectable": selectable,
                "q": q,
            },
        )

    @router.post("/jobs/bulk-delete", response_class=HTMLResponse)
    def bulk_delete_jobs(
        request: Request,
        job_ids: list[str] = Form(default=[]),
        status: str | None = None,
        selectable: bool = True,
        q: str | None = None,
        delete_video: bool = Form(default=False),
    ) -> HTMLResponse:
        for jid in job_ids:
            if not jid:
                continue
            if delete_video:
                job = store.get(jid)
                if job is not None and job.video_id:
                    _delete_video(job.video_id)  # drop the ingested audio + catalog row too
            store.delete(jid)
        return _jobs_rows(request, status, selectable, q)

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

    # ------------------------------------------------------------------ veille
    def _channel_vm(channel: WatchedChannel) -> dict:
        return {
            "channel_key": channel.channel_key,
            "url": channel.url,
            "name": channel.name or channel.channel_key,
            "active": channel.active,
            "added_at": _fmt_dt(channel.added_at),
            "last_checked_at": _fmt_dt(channel.last_checked_at),
            "last_checked_ago": _fmt_relative(channel.last_checked_at),
            "never_checked": channel.last_checked_at is None,
        }

    def _run_vm(run) -> dict:
        hits = [d for d in (run.detail or []) if d.get("new")]
        return {
            "ran_at": _fmt_dt(run.ran_at),
            "ago": _fmt_relative(run.ran_at),
            "checked": run.checked,
            "queued": run.queued,
            # per-channel breakdown: only channels that produced something
            "hits": hits,
            "empty": run.checked == 0,
        }

    def _veille_kpis() -> dict:
        channels = get_channel_store().list_all()
        active = sum(1 for c in channels if c.active)
        stats = get_veille_run_log().stats()
        recent = get_veille_run_log().list_recent(limit=1)
        last = recent[0] if recent else None
        return {
            "channels_total": len(channels),
            "channels_active": active,
            "channels_inactive": len(channels) - active,
            "runs": stats.get("runs", 0),
            "queued_total": stats.get("queued_total", 0),
            "last_ago": _fmt_relative(stats.get("last_ran_at")),
            "last_at": _fmt_dt(stats.get("last_ran_at")),
            "last_queued": last.queued if last else 0,
            "last_checked": last.checked if last else 0,
        }

    def _channels_rows(request: Request) -> HTMLResponse:
        channels = [_channel_vm(c) for c in get_channel_store().list_all()]
        return templates.TemplateResponse(
            request, "partials/channels_rows.html", {"channels": channels}
        )

    def _runs_rows(request: Request) -> HTMLResponse:
        runs = [_run_vm(r) for r in get_veille_run_log().list_recent(limit=20)]
        return templates.TemplateResponse(request, "partials/veille_runs.html", {"runs": runs})

    @router.get("/veille", response_class=HTMLResponse)
    def veille_page(request: Request) -> HTMLResponse:
        channels = [_channel_vm(c) for c in get_channel_store().list_all()]
        runs = [_run_vm(r) for r in get_veille_run_log().list_recent(limit=20)]
        return templates.TemplateResponse(
            request,
            "veille.html",
            {"active": "veille", "channels": channels, "runs": runs, "kpis": _veille_kpis()},
        )

    @router.get("/veille/partials/rows", response_class=HTMLResponse)
    def veille_rows(request: Request) -> HTMLResponse:
        return _channels_rows(request)

    @router.get("/veille/partials/runs", response_class=HTMLResponse)
    def veille_runs_rows(request: Request) -> HTMLResponse:
        return _runs_rows(request)

    @router.get("/veille/partials/kpis", response_class=HTMLResponse)
    def veille_kpis_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/veille_kpis.html", {"kpis": _veille_kpis()}
        )

    @router.post("/veille/add", response_class=HTMLResponse)
    def veille_add(
        request: Request, channel: str = Form(...), name: str = Form(default="")
    ) -> HTMLResponse:
        raw = channel.strip()
        key = extract_channel_key(raw)
        url = normalize_channel_url(raw)
        if not key or not url:
            return templates.TemplateResponse(
                request,
                "partials/flash.html",
                {"ok": False, "message": "Chaîne non reconnue (handle @nom, URL ou id UC…)."},
            )
        get_channel_store().add(
            WatchedChannel(channel_key=key, url=url, name=(name.strip() or None))
        )
        resp = templates.TemplateResponse(
            request,
            "partials/flash.html",
            {"ok": True, "message": f"Chaîne ajoutée : {name.strip() or key}"},
        )
        resp.headers["HX-Trigger"] = "channelsChanged"
        return resp

    @router.post("/veille/import-csv", response_class=HTMLResponse)
    async def veille_import_csv(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
        """Bulk-add watched channels from a CSV (column `channel`, optional `name`)."""
        raw = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        fields = [f.strip().lower() for f in (reader.fieldnames or [])]
        # Accept `channel`, or fall back to `url` / `handle` as the channel column.
        col = next((c for c in ("channel", "url", "handle") if c in fields), None)
        if col is None:
            resp = templates.TemplateResponse(
                request,
                "partials/flash.html",
                {"ok": False, "message": "Le CSV doit contenir une colonne 'channel' (ou 'url')."},
            )
            return resp

        store = get_channel_store()
        existing = {c.channel_key for c in store.list_all()}
        seen: set[str] = set()
        added = duplicates = invalid = 0
        for row in reader:
            norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            raw_channel = norm.get(col, "")
            if not raw_channel:
                continue
            key = extract_channel_key(raw_channel)
            url = normalize_channel_url(raw_channel)
            if not key or not url:
                invalid += 1
                continue
            if key in existing or key in seen:
                duplicates += 1
                continue
            store.add(WatchedChannel(channel_key=key, url=url, name=(norm.get("name") or None)))
            seen.add(key)
            added += 1

        parts = [f"{added} chaîne(s) ajoutée(s)"]
        if duplicates:
            parts.append(f"{duplicates} doublon(s) ignoré(s)")
        if invalid:
            parts.append(f"{invalid} ligne(s) invalide(s)")
        resp = templates.TemplateResponse(
            request,
            "partials/flash.html",
            {"ok": added > 0, "warn": added == 0, "message": " · ".join(parts)},
        )
        if added:
            resp.headers["HX-Trigger"] = "channelsChanged"
        return resp

    @router.post("/veille/toggle", response_class=HTMLResponse)
    def veille_toggle(
        request: Request, channel_key: str = Form(...), active: bool = Form(...)
    ) -> HTMLResponse:
        get_channel_store().set_active(channel_key, active)
        resp = _channels_rows(request)
        resp.headers["HX-Trigger"] = "channelsChanged"  # refresh the KPI cards
        return resp

    @router.post("/veille/delete", response_class=HTMLResponse)
    def veille_delete(request: Request, channel_key: str = Form(...)) -> HTMLResponse:
        get_channel_store().remove(channel_key)
        resp = _channels_rows(request)
        resp.headers["HX-Trigger"] = "channelsChanged"
        return resp

    @router.post("/veille/run", response_class=HTMLResponse)
    def veille_run(request: Request) -> HTMLResponse:
        summary = run_veille()
        resp = templates.TemplateResponse(
            request,
            "partials/flash.html",
            {
                "ok": True,
                "message": (
                    f"Veille lancée : {summary['checked']} chaîne(s) vérifiée(s), "
                    f"{summary['queued']} job(s) créé(s)."
                ),
            },
        )
        resp.headers["HX-Trigger"] = "channelsChanged"
        return resp

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")
