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
from .download_errors import RateLimitedError

log = structlog.get_logger(__name__)


class YtDlpDownloader:
    def __init__(self, audio_format: str = "wav", ffmpeg_location: Path | None = None) -> None:
        self._audio_format = audio_format
        self._ffmpeg_location = Path(ffmpeg_location) if ffmpeg_location else None

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
        if has_ffmpeg:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": self._audio_format}
            ]
            if self._ffmpeg_location is not None:
                opts["ffmpeg_location"] = str(self._ffmpeg_location)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            # Remonte le 429 en erreur typée pour que la couche throttling applique
            # son backoff ; toute autre erreur de download reste telle quelle.
            if _is_rate_limited(exc):
                raise RateLimitedError(str(exc)) from exc
            raise

        video_id = info["id"]
        if has_ffmpeg:
            audio_path = dest_dir / f"{video_id}.{self._audio_format}"
            audio_format = self._audio_format
        else:
            audio_path = self._downloaded_path(info, dest_dir, video_id)
            audio_format = audio_path.suffix.lstrip(".")

        metadata = VideoMetadata(
            video_id=video_id,
            url=info.get("webpage_url", video_url),
            title=info.get("title", ""),
            channel=info.get("uploader"),
            duration_s=info.get("duration"),
            upload_date=info.get("upload_date"),
            language=info.get("language"),
        )
        audio = AudioAsset(path=audio_path, format=audio_format)
        return metadata, audio

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
