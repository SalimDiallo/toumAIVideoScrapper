"""Tests du downloader yt-dlp : les cookies sont best-effort (non bloquants)."""

from __future__ import annotations

import shutil

import pytest
import yt_dlp

from media_ingestion.adapters.download_errors import RateLimitedError
from media_ingestion.adapters.ytdlp_downloader import YtDlpDownloader


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    """Force le chemin sans ré-encodage pour un résultat déterministe."""
    monkeypatch.setattr(shutil, "which", lambda _: None)


def _ok_info(video_url, dest_dir):
    return {
        "id": "vid",
        "title": "t",
        "ext": "m4a",
        "webpage_url": video_url,
        "requested_downloads": [{"filepath": str(dest_dir / "vid.m4a")}],
    }


def test_cookie_error_falls_back_without_cookies(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_extract(opts, video_url):
        calls.append(dict(opts))
        if "cookiesfrombrowser" in opts:  # 1er essai avec cookies -> échoue
            raise yt_dlp.utils.DownloadError("ERROR: Could not copy Chrome cookie database")
        return _ok_info(video_url, tmp_path)

    monkeypatch.setattr(YtDlpDownloader, "_extract", staticmethod(fake_extract))
    dl = YtDlpDownloader(cookies_from_browser="chrome")

    meta, _ = dl.download("http://yt/x", tmp_path)

    assert meta.video_id == "vid"
    assert len(calls) == 2  # échec cookies puis retry sans
    assert "cookiesfrombrowser" in calls[0]
    assert "cookiesfrombrowser" not in calls[1]


def test_provider_derived_from_extractor(tmp_path, monkeypatch):
    def fake_extract(opts, video_url):
        return {**_ok_info(video_url, tmp_path), "extractor_key": "Vimeo"}

    monkeypatch.setattr(YtDlpDownloader, "_extract", staticmethod(fake_extract))
    meta, _ = YtDlpDownloader().download("http://vimeo/x", tmp_path)
    assert meta.provider == "vimeo"


def test_audio_conversion_failure_falls_back_to_native(tmp_path, monkeypatch):
    # ffmpeg présent -> 1er essai tente le wav et échoue à la conversion ; on doit
    # retomber sur l'audio natif (m4a) au lieu de faire échouer le job.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    calls: list[dict] = []

    def fake_extract(opts, video_url):
        calls.append(dict(opts))
        if "postprocessors" in opts:  # 1er essai : conversion wav -> échec
            raise yt_dlp.utils.DownloadError(
                "ERROR: Postprocessing: audio conversion failed: Conversion failed!"
            )
        return _ok_info(video_url, tmp_path)

    monkeypatch.setattr(YtDlpDownloader, "_extract", staticmethod(fake_extract))

    meta, audio = YtDlpDownloader(audio_format="wav").download("http://odysee/x", tmp_path)

    assert meta.video_id == "vid"
    assert audio.format == "m4a"  # audio natif conservé, pas de wav
    assert len(calls) == 2
    assert "postprocessors" in calls[0] and "postprocessors" not in calls[1]


def test_non_cookie_error_is_not_swallowed(tmp_path, monkeypatch):
    def fake_extract(opts, video_url):
        raise yt_dlp.utils.DownloadError("ERROR: Video unavailable")

    monkeypatch.setattr(YtDlpDownloader, "_extract", staticmethod(fake_extract))
    dl = YtDlpDownloader(cookies_from_browser="chrome")

    with pytest.raises(yt_dlp.utils.DownloadError):
        dl.download("http://yt/x", tmp_path)


def test_rate_limit_still_raises_typed_error(tmp_path, monkeypatch):
    def fake_extract(opts, video_url):
        raise yt_dlp.utils.DownloadError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(YtDlpDownloader, "_extract", staticmethod(fake_extract))
    dl = YtDlpDownloader(cookies_from_browser="chrome")

    with pytest.raises(RateLimitedError):
        dl.download("http://yt/x", tmp_path)
