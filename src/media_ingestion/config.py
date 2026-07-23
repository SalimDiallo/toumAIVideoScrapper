"""Config-driven settings (env / .env). Prefix: TOUMAI_."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOUMAI_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    audio_format: str = "wav"
    languages: list[str] = Field(default_factory=lambda: ["fr", "en"])
    # Explicit ffmpeg/ffprobe folder (bin dir). None = rely on PATH.
    ffmpeg_location: Path | None = None

    # Phase 2 STT (faster-whisper). Off by default in the MVP.
    enable_stt: bool = False
    stt_model_size: str = "base"
    stt_device: str = "cpu"
