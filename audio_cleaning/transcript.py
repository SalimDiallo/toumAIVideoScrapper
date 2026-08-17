"""Alignement du transcript sur l'audio nettoyé (logique pure, testable).

Quand on retire des tranches d'audio, la timeline se contracte : tous les
timestamps doivent être recalculés. Règles, alignées sur le principe « ne jamais
perdre de parole » :

- une phrase qui recouvre AU MOINS un instant gardé est CONSERVÉE (on ne jette
  pas une phrase entière parce qu'un bout de son intervalle tombe dans une zone
  supprimée) ;
- ses nouveaux ``start_s`` / ``duration_s`` sont projetés sur la timeline
  contractée, bornés à la portion réellement gardée ;
- une phrase entièrement dans une zone supprimée est retirée.

Format d'entrée/sortie identique au transcript existant :
``{"start_s": float, "duration_s": float, "text": str}``.

Ce module partage sa logique avec ``media_ingestion.silver.remap`` du dépôt ; il
est réimplémenté ici pour garder le package ``audio_cleaning`` autonome.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .segments import Range


def load_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Charge un transcript JSON. Accepte une liste ou ``{"segments": [...]}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("segments") or [])
    return list(data)


def save_transcript(path: str | Path, segments: list[dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")


def _cumulative_offsets(kept: list[Range]) -> list[float]:
    """Position, dans l'audio final, du début de chaque tranche gardée."""
    offsets: list[float] = []
    total = 0.0
    for start, end in kept:
        offsets.append(total)
        total += end - start
    return offsets


def _map_instant(t: float, kept: list[Range], offsets: list[float]) -> float | None:
    """Projette un instant original ``t`` sur la timeline contractée (ou None si supprimé)."""
    for (start, end), offset in zip(kept, offsets):
        if start <= t <= end:
            return offset + (t - start)
    return None


def remap_transcript(segments: list[dict[str, Any]], kept: list[Range]) -> list[dict[str, Any]]:
    """Recalcule les timestamps du transcript sur l'audio nettoyé.

    ``kept`` : tranches gardées (timeline originale), triées et disjointes.
    """
    if not kept:
        return []
    offsets = _cumulative_offsets(kept)
    out: list[dict[str, Any]] = []
    for seg in segments:
        start = float(seg.get("start_s", 0.0) or 0.0)
        duration = float(seg.get("duration_s", 0.0) or 0.0)
        end = start + max(duration, 0.0)

        # Portions de la phrase qui survivent = intersection avec les tranches gardées.
        overlaps: list[Range] = []
        for k_start, k_end in kept:
            lo = max(start, k_start)
            hi = min(end, k_end)
            if hi > lo:
                overlaps.append((lo, hi))
        if not overlaps:
            continue  # phrase entièrement supprimée

        new_start = _map_instant(overlaps[0][0], kept, offsets)
        new_end = _map_instant(overlaps[-1][1], kept, offsets)
        if new_start is None or new_end is None:
            continue
        out.append(
            {
                **seg,
                "start_s": round(new_start, 3),
                "duration_s": round(max(new_end - new_start, 0.0), 3),
            }
        )
    return out
