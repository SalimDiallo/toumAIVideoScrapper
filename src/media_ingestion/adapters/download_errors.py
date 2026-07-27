"""Erreurs de téléchargement partagées entre le downloader et sa couche throttling."""

from __future__ import annotations


class RateLimitedError(Exception):
    """Le serveur a répondu HTTP 429 (Too Many Requests).

    Signalée par le downloader pour que la couche de reprise applique un backoff
    exponentiel et, si configuré, change de proxy avant de réessayer.
    """
