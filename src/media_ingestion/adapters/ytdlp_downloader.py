"""AudioDownloaderPort implementation backed by yt-dlp.

If `ffmpeg` is on PATH, the audio is re-encoded to the configured format (e.g. wav).
If not, the native bestaudio stream (m4a/webm) is kept as-is
so the pipeline still runs — a warning is logged.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import structlog
import yt_dlp

from ..domain.models import AudioAsset, VideoMetadata
from ..provider import provider_from_extractor
from .download_errors import RateLimitedError

log = structlog.get_logger(__name__)


class YtDlpDownloader:
    def __init__(
        self,
        audio_format: str = "wav",
        ffmpeg_location: Path | None = None,
        *,
        cookies_from_browser: str | None = None,
        cookies_file: Path | None = None,
    ) -> None:
        self._audio_format = audio_format
        self._ffmpeg_location = Path(ffmpeg_location) if ffmpeg_location else None
        # Cookies pour passer le check anti-bot de YouTube. Le fichier prime sur le
        # navigateur si les deux sont fournis.
        self._cookies_file = Path(cookies_file) if cookies_file else None
        self._cookies_from_browser = cookies_from_browser or None

    def download(
        self, video_url: str, dest_dir: Path, *, proxy: str | None = None
    ) -> tuple[VideoMetadata, AudioAsset]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        has_ffmpeg = self._ffmpeg_location is not None or shutil.which("ffmpeg") is not None
        if not has_ffmpeg:
            log.warning(
                "ffmpeg.missing",
                detail="keeping native audio format (no re-encode). Install ffmpeg for wav.",
            )

        opts: dict = {
            "format": "bestaudio/best",
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        if proxy:
            opts["proxy"] = proxy
        # Cookies : le fichier a la priorité, sinon extraction depuis le navigateur.
        if self._cookies_file is not None:
            opts["cookiefile"] = str(self._cookies_file)
        elif self._cookies_from_browser is not None:
            # yt-dlp attend un tuple (browser, profile, keyring, container) ;
            # "chrome:Profile 1" -> ("chrome", "Profile 1").
            browser, _, profile = self._cookies_from_browser.partition(":")
            opts["cookiesfrombrowser"] = (browser.strip(), profile.strip() or None, None, None)
        if has_ffmpeg:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": self._audio_format}
            ]
            if self._ffmpeg_location is not None:
                opts["ffmpeg_location"] = str(self._ffmpeg_location)

        # `converted` suit ce qu'on a réellement obtenu : passe à False si la
        # conversion wav échoue et qu'on retombe sur l'audio natif (voir plus bas).
        converted = has_ffmpeg
        try:
            info = self._extract(opts, video_url)
        except yt_dlp.utils.DownloadError as exc:
            # Remonte le 429 en erreur typée pour que la couche throttling applique
            # son backoff ; toute autre erreur de download reste telle quelle.
            if _is_rate_limited(exc):
                raise RateLimitedError(str(exc)) from exc
            # Les cookies sont optionnels : si leur extraction échoue (navigateur
            # ouvert/verrouillé, chiffrement app-bound Chrome...), on réessaie sans
            # plutôt que de faire échouer le job.
            if _uses_cookies(opts) and _is_cookie_error(exc):
                log.warning("ytdlp.cookies.unavailable", url=video_url, error=str(exc))
                opts.pop("cookiefile", None)
                opts.pop("cookiesfrombrowser", None)
                try:
                    info = self._extract(opts, video_url)
                except yt_dlp.utils.DownloadError as exc2:
                    if has_ffmpeg and _is_conversion_error(exc2):
                        info, converted = self._retry_native(opts, video_url, exc2), False
                    else:
                        raise
            # La conversion wav de ffmpeg casse sur certains conteneurs/codecs
            # (flux Odysee/LBRY notamment) : on garde l'audio natif plutôt que de
            # perdre le job — l'audio est téléchargé, seul le ré-encodage a échoué.
            elif has_ffmpeg and _is_conversion_error(exc):
                info, converted = self._retry_native(opts, video_url, exc), False
            else:
                raise

        video_id = info["id"]
        if converted:
            audio_path = dest_dir / f"{video_id}.{self._audio_format}"
            audio_format = self._audio_format
        else:
            audio_path = self._downloaded_path(info, dest_dir, video_id)
            audio_format = audio_path.suffix.lstrip(".")

        provider = provider_from_extractor(info.get("extractor_key") or info.get("extractor"))
        metadata = VideoMetadata(
            video_id=video_id,
            url=info.get("webpage_url", video_url),
            title=info.get("title", ""),
            channel=info.get("uploader"),
            duration_s=info.get("duration"),
            upload_date=info.get("upload_date"),
            language=info.get("language"),
            provider=provider.value,
        )
        audio = AudioAsset(path=audio_path, format=audio_format)
        return metadata, audio

    @staticmethod
    def _extract(opts: dict, video_url: str) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(video_url, download=True)

    @staticmethod
    def _retry_native(opts: dict, video_url: str, exc: Exception) -> dict:
        """Ré-extrait en gardant l'audio source (drop le post-processeur wav).

        yt-dlp réutilise le fichier déjà téléchargé, seule l'étape de conversion
        est abandonnée — on récupère donc l'audio natif (m4a/webm...) sans re-DL.
        """
        log.warning("ytdlp.audio_conversion.failed", url=video_url, error=str(exc))
        opts.pop("postprocessors", None)
        opts.pop("ffmpeg_location", None)
        return YtDlpDownloader._extract(opts, video_url)

    @staticmethod
    def _downloaded_path(info: dict, dest_dir: Path, video_id: str) -> Path:
        downloads = info.get("requested_downloads") or []
        if downloads and downloads[0].get("filepath"):
            return Path(downloads[0]["filepath"])
        ext = info.get("ext", "m4a")
        return dest_dir / f"{video_id}.{ext}"


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


def _is_conversion_error(exc: Exception) -> bool:
    """Échec du ré-encodage audio par ffmpeg (post-processing), pas du download."""
    msg = str(exc).lower()
    return "conversion failed" in msg or "audio conversion failed" in msg


def _uses_cookies(opts: dict) -> bool:
    return "cookiefile" in opts or "cookiesfrombrowser" in opts


def _is_cookie_error(exc: Exception) -> bool:
    """Échec d'extraction/lecture des cookies (et non un vrai refus de la vidéo)."""
    msg = str(exc).lower()
    return "cookie" in msg and (
        "could not copy" in msg
        or "could not find" in msg
        or "unable to" in msg
        or "failed to" in msg
        or "decrypt" in msg
    )
