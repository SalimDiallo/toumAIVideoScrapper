"""Types et opérations sur les segments temporels (logique pure, sans I/O ni modèle).

Un ``Segment`` décrit une tranche ``[start, end]`` de l'audio avec la décision
prise dessus (classe, action keep/remove, confiance). C'est la structure
sérialisée dans ``segments.json`` : elle conserve *toutes* les décisions du
pipeline, pas seulement ce qui est gardé, pour pouvoir auditer les erreurs.

Les fonctions sur les ``Range`` (couples ``(start, end)``) servent partout :
fusion des micro-segments, complément d'un ensemble de coupes, padding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .labels import KEEP

Range = tuple[float, float]


@dataclass
class Segment:
    """Une décision sur une tranche audio (unité de ``segments.json``)."""

    start: float
    end: float
    label: str  # classe grossière (speech, music, ...)
    action: str = KEEP  # keep | remove
    confidence: float = 1.0
    source: str = ""  # "vad" | "classifier" | "silence" (traçabilité)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "label": self.label,
            "action": self.action,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
        }
        if self.extra:
            d["extra"] = self.extra
        return d


# --------------------------------------------------------------------- ranges
def clamp_ranges(ranges: list[Range], duration: float) -> list[Range]:
    """Borne les intervalles à ``[0, duration]``, retire les vides, trie."""
    out: list[Range] = []
    for start, end in ranges:
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if end > start:
            out.append((start, end))
    out.sort()
    return out


def merge_ranges(ranges: list[Range], *, max_gap: float = 0.0) -> list[Range]:
    """Fusionne les intervalles qui se chevauchent ou séparés d'au plus ``max_gap``.

    C'est l'anti micro-coupure : deux zones de même nature séparées par un trou
    minuscule sont recollées (``SPEECH 0-3.2`` + ``SPEECH 3.2-4.8`` -> ``0-4.8``).
    """
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[Range] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + max_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def complement(ranges: list[Range], duration: float) -> list[Range]:
    """Complément de ``ranges`` sur ``[0, duration]`` (les trous entre intervalles)."""
    out: list[Range] = []
    cursor = 0.0
    for start, end in merge_ranges(clamp_ranges(ranges, duration)):
        if start > cursor:
            out.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        out.append((cursor, duration))
    return out


def total_duration(ranges: list[Range]) -> float:
    """Somme des durées d'une liste d'intervalles (supposés disjoints)."""
    return sum(end - start for start, end in ranges)


def pad_ranges(ranges: list[Range], before: float, after: float, duration: float) -> list[Range]:
    """Élargit chaque intervalle de ``before`` à gauche / ``after`` à droite, puis fusionne.

    Sert à ajouter la marge de sécurité autour de la parole : on "rend" un peu de
    contexte avant/après pour ne jamais tronquer le début ou la fin d'un mot.
    """
    padded = [(start - before, end + after) for start, end in ranges]
    return merge_ranges(clamp_ranges(padded, duration))


def filter_min_duration(ranges: list[Range], min_duration: float) -> list[Range]:
    """Retire les intervalles plus courts que ``min_duration``."""
    return [(start, end) for start, end in ranges if end - start >= min_duration]


def subtract(base: list[Range], holes: list[Range], duration: float) -> list[Range]:
    """``base`` privé de ``holes`` : garde la partie de base non couverte par un trou."""
    if not holes:
        return merge_ranges(clamp_ranges(base, duration))
    holes_m = merge_ranges(clamp_ranges(holes, duration))
    out: list[Range] = []
    for b_start, b_end in merge_ranges(clamp_ranges(base, duration)):
        cursor = b_start
        for h_start, h_end in holes_m:
            if h_end <= cursor or h_start >= b_end:
                continue
            if h_start > cursor:
                out.append((cursor, min(h_start, b_end)))
            cursor = max(cursor, h_end)
            if cursor >= b_end:
                break
        if cursor < b_end:
            out.append((cursor, b_end))
    return [r for r in out if r[1] > r[0]]
