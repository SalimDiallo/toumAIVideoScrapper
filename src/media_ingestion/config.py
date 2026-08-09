"""Config-driven settings (env / .env). Prefix: TOUMAI_."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "reload_into"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOUMAI_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    audio_format: str = "wav"
    languages: list[str] = Field(default_factory=lambda: ["fr", "en"])
    # Explicit ffmpeg/ffprobe folder (bin dir). None = rely on PATH.
    ffmpeg_location: Path | None = None

    # --- yt-dlp cookies (contourne "Sign in to confirm you're not a bot") ---
    # Navigateur d'où extraire les cookies (chrome/firefox/edge/brave...),
    # éventuellement suffixé du profil : "chrome:Profile 1". Vide = désactivé.
    ytdlp_cookies_from_browser: str | None = None
    # OU chemin d'un fichier cookies.txt exporté (format Netscape). N'en renseigner
    # qu'un seul des deux ; si les deux sont fournis, le fichier a la priorité.
    ytdlp_cookies_file: Path | None = None

    # --- Transcript selection strategy (YouTube caption tracks) ---
    # Accept YouTube's auto-generated (ASR) captions. When False, ASR tracks are
    # ignored so the video is marked unavailable rather than using YT ASR.
    accept_youtube_asr: bool = True
    # As a last resort, machine-translate an existing track into a target language.
    enable_transcript_translation: bool = False

    # --- Webshare (proxy résidentiel) pour youtube-transcript-api ---
    # L'API de transcripts se fait bannir par IP (surtout depuis une IP cloud).
    # Webshare fait tourner des IP résidentielles ; renseigner user+pass du
    # dashboard. Vide = transcripts en connexion directe (pas de proxy).
    webshare_proxy_username: str | None = None
    webshare_proxy_password: str | None = None

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

    # storage backend
    storage_backend: Literal["local", "minio"] = "local"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "toumai-media"
    minio_secure: bool = False
    # Endpoint public utilisé UNIQUEMENT pour signer les URLs présignées remises au
    # navigateur. À renseigner quand le serveur atteint MinIO via un hôte interne non
    # résoluble côté navigateur (ex. conteneur : interne "minio:9000", public
    # "localhost:9000" en dev ou un vrai domaine en prod). Vide = on réutilise l'endpoint.
    minio_public_endpoint: str | None = None
    # Région S3 épinglée. Sans elle, minio-py fait un appel réseau GetBucketLocation
    # au moment de signer une URL — ce qui échoue quand le client de presign vise un
    # endpoint public injoignable depuis le serveur (ex. "localhost:9000" vu depuis un
    # conteneur). La valeur par défaut de MinIO est "us-east-1".
    minio_region: str = "us-east-1"

    # metadata catalog
    metadata_backend: Literal["none", "postgres"] = "none"
    postgres_dsn: str = "postgresql+psycopg://toumai:toumai@localhost:5432/toumai"

    # --- Veille (surveillance quotidienne des chaînes) ---
    # Nombre de vidéos récentes scannées par chaîne à chaque passage. On dédup
    # ce lot contre les vidéos déjà ingérées ; toute nouveauté est mise en file.
    veille_recent_limit: int = 15

    # Kafka (API <-> workers decoupling)
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


def reload_into(target: Settings, env_file: str | Path | None = None) -> Settings:
    """Re-read config from `.env` (+ OS env) and copy it onto an existing Settings.

    Mutating the shared instance in place — rather than returning a new object —
    means every closure/adapter that already holds a reference to `target`
    immediately sees the new values, so the app applies config changes without a
    process restart. Returns `target` for convenience.
    """
    fresh = Settings(_env_file=str(env_file)) if env_file else Settings()
    for name in Settings.model_fields:
        setattr(target, name, getattr(fresh, name))
    return target
