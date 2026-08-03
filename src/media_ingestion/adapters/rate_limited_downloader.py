"""Couche de politesse/anti-blocage au-dessus d'un AudioDownloaderPort.

Enveloppe n'importe quel downloader (typiquement `YtDlpDownloader`) et ajoute,
tout en respectant le même port :

- un **délai aléatoire** avant chaque téléchargement (espace les requêtes) ;
- une **reprise automatique sur HTTP 429** en backoff exponentiel borné + jitter ;
- une **rotation de proxies** (une IP de sortie par tentative) si configurée.

Aucune de ces politiques ne vit dans yt-dlp lui-même : le downloader reste bête,
la politique est ici et pilotée par la config (`TOUMAI_DOWNLOAD_*`).
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

import structlog

from ..domain.models import AudioAsset, VideoMetadata
from .download_errors import RateLimitedError

log = structlog.get_logger(__name__)


class _Inner(Protocol):
    def download(
        self, video_url: str, dest_dir: Path, *, proxy: str | None = None
    ) -> tuple[VideoMetadata, AudioAsset]: ...


class RateLimitedDownloader:
    def __init__(
        self,
        inner: _Inner,
        *,
        delay_min_s: float = 1.0,
        delay_max_s: float = 4.0,
        max_retries: int = 5,
        backoff_base_s: float = 2.0,
        backoff_max_s: float = 300.0,
        proxies: list[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._inner = inner
        self._delay_min = max(0.0, delay_min_s)
        self._delay_max = max(self._delay_min, delay_max_s)
        self._max_retries = max(0, max_retries)
        self._backoff_base = max(0.0, backoff_base_s)
        self._backoff_max = max(0.0, backoff_max_s)
        self._proxies = list(proxies or [])
        self._sleep = sleep
        self._rand = rand
        # Curseur round-robin global (avance à chaque tentative, tout download
        # confondu) pour répartir les vidéos sur le pool d'IP et pas seulement les
        # retries. Verrou : le worker partage une instance entre N threads.
        self._proxy_cursor = 0
        self._proxy_lock = threading.Lock()

    def download(self, video_url: str, dest_dir: Path) -> tuple[VideoMetadata, AudioAsset]:
        self._pace()
        attempt = 0
        while True:
            proxy = self._next_proxy()
            try:
                return self._inner.download(video_url, dest_dir, proxy=proxy)
            except RateLimitedError:
                attempt += 1
                if attempt > self._max_retries:
                    log.error("download.rate_limited.giveup", url=video_url, attempts=attempt)
                    raise
                wait = self._backoff(attempt)
                log.warning(
                    "download.rate_limited.retry",
                    url=video_url,
                    attempt=attempt,
                    max_retries=self._max_retries,
                    wait_s=round(wait, 1),
                    proxy=proxy,
                )
                self._sleep(wait)

    def _pace(self) -> None:
        """Délai aléatoire avant la requête pour ne pas taper en rafale."""
        if self._delay_max > 0:
            self._sleep(self._rand(self._delay_min, self._delay_max))

    def _backoff(self, attempt: int) -> float:
        """Backoff exponentiel borné + jitter (delay = min(cap, base*2^(n-1)) + jitter)."""
        capped = min(self._backoff_max, self._backoff_base * (2 ** (attempt - 1)))
        jitter = self._rand(0.0, self._backoff_base) if self._backoff_base > 0 else 0.0
        return min(self._backoff_max, capped + jitter)

    def _next_proxy(self) -> str | None:
        """Prochain proxy en round-robin (avance à chaque appel) ; None = direct."""
        if not self._proxies:
            return None
        with self._proxy_lock:
            proxy = self._proxies[self._proxy_cursor % len(self._proxies)]
            self._proxy_cursor += 1
        return proxy
