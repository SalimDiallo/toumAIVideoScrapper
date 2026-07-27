"""Config-driven settings (env / .env). Prefix: TOUMAI_."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOUMAI_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    audio_format: str = "wav"
    languages: list[str] = Field(default_factory=lambda: ["fr", "en"])
    # Explicit ffmpeg/ffprobe folder (bin dir). None = rely on PATH.
    ffmpeg_location: Path | None = None

    # --- Transcript selection strategy (YouTube caption tracks) ---
    # Accept YouTube's auto-generated (ASR) captions. When False, ASR tracks are
    # ignored so the video is marked unavailable rather than using YT ASR.
    accept_youtube_asr: bool = True
    # As a last resort, machine-translate an existing track into a target language.
    enable_transcript_translation: bool = False

    # --- Download throttling / anti-blocage (batch CSV = centaines de vidéos) ---
    # Plafond de téléchargements simultanés par worker/serveur (yt-dlp est I/O bound).
    max_concurrent_downloads: int = 3
    # Délai aléatoire (secondes) inséré avant chaque téléchargement pour espacer les
    # requêtes. 0/0 = désactivé.
    download_delay_min_s: float = 1.0
    download_delay_max_s: float = 4.0
    # Reprise automatique sur HTTP 429 (Too Many Requests) : backoff exponentiel
    # borné + jitter. delay = min(backoff_max, base * 2**tentative) + random.
    download_max_retries: int = 5
    download_backoff_base_s: float = 2.0
    download_backoff_max_s: float = 300.0
    # Proxies / IP de sortie tournants (round-robin par tentative). Vide = connexion
    # directe. Ex JSON : ["http://ip1:port","http://ip2:port"].
    download_proxies: list[str] = Field(default_factory=list)

    # Phase 2 storage backend
    storage_backend: Literal["local", "minio"] = "local"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "toumai-media"
    minio_secure: bool = False

    # Phase 2 metadata catalog
    metadata_backend: Literal["none", "postgres"] = "none"
    postgres_dsn: str = "postgresql+psycopg://toumai:toumai@localhost:5432/toumai"

    # Phase 2 Kafka (API <-> workers decoupling)
    kafka_bootstrap_servers: str = "localhost:9092"
    # Version d'API fixe pour éviter le probing de kafka-python-ng qui plante sous
    # Windows/Python 3.14 (socket abandonnée -> selector.register(None)). "2.5.0".
    kafka_api_version: str = "2.5.0"
    kafka_consumer_group: str = "toumai-workers"
    # Délai max entre deux poll() avant que Kafka considère le worker mort et
    # rebalance. Doit couvrir le pire lot : N gros téléchargements + backoff 429
    # (jusqu'à backoff_max * retries). Trop bas => worker éjecté -> commit échoue
    # -> il s'arrête. 30 min par défaut.
    kafka_max_poll_interval_ms: int = 1_800_000
    topic_job_requested: str = "job.requested"
    topic_job_completed: str = "job.completed"
    topic_job_dlq: str = "job.dlq"
