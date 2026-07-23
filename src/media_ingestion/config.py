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
    # ignored so the video falls through to our own STT (often better than YT ASR).
    accept_youtube_asr: bool = True
    # As a last resort, machine-translate an existing track into a target language.
    enable_transcript_translation: bool = False

    # Phase 2 STT (faster-whisper). Off by default in the MVP.
    enable_stt: bool = False
    stt_model_size: str = "base"
    stt_device: str = "cpu"

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
    kafka_consumer_group: str = "toumai-workers"
    topic_job_requested: str = "job.requested"
    topic_job_completed: str = "job.completed"
    topic_job_dlq: str = "job.dlq"
