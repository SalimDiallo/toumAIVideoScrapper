"""Tests de la couche throttling : délais, backoff 429, rotation de proxies."""

from __future__ import annotations

from pathlib import Path

import pytest

from media_ingestion.adapters.download_errors import RateLimitedError
from media_ingestion.adapters.rate_limited_downloader import RateLimitedDownloader
from media_ingestion.domain.models import AudioAsset, VideoMetadata


class FakeInner:
    """Renvoie un résultat, mais lève 429 les `fail_times` premières fois."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls: list[str | None] = []  # proxy utilisé à chaque appel

    def download(self, video_url: str, dest_dir: Path, *, proxy: str | None = None):
        self.calls.append(proxy)
        if len(self.calls) <= self.fail_times:
            raise RateLimitedError("HTTP Error 429: Too Many Requests")
        meta = VideoMetadata(video_id="vid", url=video_url, title="t")
        return meta, AudioAsset(path=dest_dir / "vid.wav", format="wav")


def _make(inner, **kw):
    slept: list[float] = []
    dl = RateLimitedDownloader(
        inner,
        sleep=slept.append,
        rand=lambda a, b: b,  # déterministe : borne haute
        **kw,
    )
    return dl, slept


def test_paces_before_download(tmp_path):
    inner = FakeInner()
    dl, slept = _make(inner, delay_min_s=1.0, delay_max_s=4.0, max_retries=0)
    dl.download("http://yt/x", tmp_path)
    assert slept == [4.0]  # un seul délai (pacing), pas de retry


def test_retries_on_429_then_succeeds(tmp_path):
    inner = FakeInner(fail_times=2)
    dl, slept = _make(
        inner, delay_min_s=0, delay_max_s=0, max_retries=5, backoff_base_s=2.0, backoff_max_s=300.0
    )
    meta, audio = dl.download("http://yt/x", tmp_path)
    assert meta.video_id == "vid"
    assert len(inner.calls) == 3  # 2 échecs + 1 succès
    # backoff exponentiel + jitter (rand->borne haute = base) : 2*1+2=4, 2*2+2=6
    assert slept == [4.0, 6.0]


def test_gives_up_after_max_retries(tmp_path):
    inner = FakeInner(fail_times=99)
    dl, _ = _make(inner, delay_min_s=0, delay_max_s=0, max_retries=3)
    with pytest.raises(RateLimitedError):
        dl.download("http://yt/x", tmp_path)
    assert len(inner.calls) == 4  # 1 tentative initiale + 3 retries


def test_rotates_proxies_across_attempts(tmp_path):
    inner = FakeInner(fail_times=2)
    dl, _ = _make(
        inner,
        delay_min_s=0,
        delay_max_s=0,
        max_retries=5,
        proxies=["http://p1", "http://p2"],
    )
    dl.download("http://yt/x", tmp_path)
    # tentative 0 -> p1, tentative 1 -> p2, tentative 2 -> p1 (round-robin)
    assert inner.calls == ["http://p1", "http://p2", "http://p1"]


def test_rotates_proxies_across_downloads(tmp_path):
    """Le curseur persiste entre vidéos : chaque download sort par une IP différente."""
    inner = FakeInner()  # aucun 429 -> une seule tentative par download
    dl, _ = _make(
        inner,
        delay_min_s=0,
        delay_max_s=0,
        max_retries=0,
        proxies=["http://p1", "http://p2"],
    )
    for _ in range(3):
        dl.download("http://yt/x", tmp_path)
    # video 1 -> p1, video 2 -> p2, video 3 -> p1 (et non p1 à chaque fois)
    assert inner.calls == ["http://p1", "http://p2", "http://p1"]


def test_backoff_is_capped(tmp_path):
    inner = FakeInner(fail_times=1)
    dl, slept = _make(
        inner, delay_min_s=0, delay_max_s=0, max_retries=5, backoff_base_s=100.0, backoff_max_s=10.0
    )
    dl.download("http://yt/x", tmp_path)
    assert slept == [10.0]  # borné à backoff_max malgré base élevée
