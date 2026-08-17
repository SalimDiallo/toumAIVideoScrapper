"""Calcul des métriques du pipeline : VAD, nettoyage, performance, qualité.

Chaque famille est isolée dans sa fonction et renvoie un dict sérialisable.
``build_metrics`` assemble le tout dans la structure écrite en ``metrics.json``.

Définitions des métriques (résumé) :

VAD
  speech_duration_s .............. temps total classé parole (après padding)
  non_speech_duration_s .......... reste de l'audio
  speech_ratio ................... speech / durée totale
  num_speech_segments ............ nombre de zones de parole distinctes
  avg_speech_segment_s ........... durée moyenne d'une zone de parole

Nettoyage
  original_duration_s ............ durée de l'audio d'entrée
  cleaned_duration_s ............. durée de l'audio produit
  removed_duration_s ............. original - cleaned
  cleaning_ratio ................. removed / original (fraction retirée)
  *_removed_duration_s ........... durée retirée par classe (music/applause/noise/silence)

Performance
  processing_time_s .............. temps mur du traitement
  audio_duration_s ............... durée d'audio traitée
  rtf ............................ processing_time / audio_duration (temps réel : <1 = +rapide que le direct)
  peak_ram_mb / cpu_percent ...... pic mémoire et charge CPU (si psutil dispo)

Qualité (si annotations disponibles — voir evaluation.py)
  precision/recall/f1 ............ par classe et macro, sur la tâche "à supprimer ?"
  speech_retention ............... part de la vraie parole conservée (doit tendre vers 1.0)
  false_speech_deletion_rate ..... part de la vraie parole supprimée à tort (doit tendre vers 0)
  music/applause/noise_removal_rate  part de chaque classe correctement supprimée
"""

from __future__ import annotations

from typing import Any

from .labels import APPLAUSE, MUSIC, NOISE, SILENCE, SPEECH
from .segments import Range, Segment, total_duration


def vad_metrics(duration: float, speech_ranges: list[Range]) -> dict[str, Any]:
    speech = total_duration(speech_ranges)
    n = len(speech_ranges)
    return {
        "speech_duration_s": round(speech, 3),
        "non_speech_duration_s": round(max(duration - speech, 0.0), 3),
        "speech_ratio": round(speech / duration, 4) if duration else 0.0,
        "num_speech_segments": n,
        "avg_speech_segment_s": round(speech / n, 3) if n else 0.0,
    }


def cleaning_metrics(
    original_duration: float,
    cleaned_duration: float,
    removed_by_label: dict[str, float],
) -> dict[str, Any]:
    removed = max(original_duration - cleaned_duration, 0.0)
    return {
        "original_duration_s": round(original_duration, 3),
        "cleaned_duration_s": round(cleaned_duration, 3),
        "removed_duration_s": round(removed, 3),
        "cleaning_ratio": round(removed / original_duration, 4) if original_duration else 0.0,
        "music_removed_duration_s": round(removed_by_label.get(MUSIC, 0.0), 3),
        "applause_removed_duration_s": round(removed_by_label.get(APPLAUSE, 0.0), 3),
        "noise_removed_duration_s": round(removed_by_label.get(NOISE, 0.0), 3),
        "silence_removed_duration_s": round(removed_by_label.get(SILENCE, 0.0), 3),
    }


def performance_metrics(
    processing_time_s: float,
    audio_duration_s: float,
    *,
    peak_ram_mb: float | None = None,
    cpu_percent: float | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "processing_time_s": round(processing_time_s, 3),
        "audio_duration_s": round(audio_duration_s, 3),
        "rtf": round(processing_time_s / audio_duration_s, 4) if audio_duration_s else 0.0,
    }
    if peak_ram_mb is not None:
        out["peak_ram_mb"] = round(peak_ram_mb, 1)
    if cpu_percent is not None:
        out["cpu_percent"] = round(cpu_percent, 1)
    return out


def quality_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Métriques qualité à partir de lignes annotées (true_label vs predicted_label).

    Chaque ligne : ``{start, end, true_label, predicted_label, ...}``. On raisonne
    en durée (pondération par la longueur du segment), plus honnête qu'un simple
    comptage de segments. La tâche binaire de référence est « faut-il supprimer ? » :
    tout sauf ``speech`` est « à supprimer ».
    """

    def dur(r: dict[str, Any]) -> float:
        return max(0.0, float(r.get("end", 0.0)) - float(r.get("start", 0.0)))

    # --- tâche binaire remove vs keep, en durée ---
    tp = fp = fn = 0.0
    speech_total = speech_kept = 0.0
    per_class_total: dict[str, float] = {}
    per_class_removed_ok: dict[str, float] = {}

    for r in rows:
        d = dur(r)
        true_remove = r.get("true_label") != SPEECH
        pred_remove = r.get("predicted_label") != SPEECH
        if true_remove and pred_remove:
            tp += d
        elif not true_remove and pred_remove:
            fp += d
        elif true_remove and not pred_remove:
            fn += d

        if r.get("true_label") == SPEECH:
            speech_total += d
            if not pred_remove:
                speech_kept += d
        else:
            c = r.get("true_label", "")
            per_class_total[c] = per_class_total.get(c, 0.0) + d
            if pred_remove:
                per_class_removed_ok[c] = per_class_removed_ok.get(c, 0.0) + d

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    def removal_rate(cls: str) -> float:
        tot = per_class_total.get(cls, 0.0)
        return round(per_class_removed_ok.get(cls, 0.0) / tot, 4) if tot else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1_score": round(f1, 4),
        "speech_retention": round(speech_kept / speech_total, 4) if speech_total else 0.0,
        "false_speech_deletion_rate": (
            round((speech_total - speech_kept) / speech_total, 4) if speech_total else 0.0
        ),
        "music_removal_rate": removal_rate(MUSIC),
        "applause_removal_rate": removal_rate(APPLAUSE),
        "noise_removal_rate": removal_rate(NOISE),
        "support_speech_s": round(speech_total, 3),
    }


def build_metrics(
    *,
    duration: float,
    speech_ranges: list[Range],
    segments: list[Segment],
    cleaned_duration: float,
    removed_by_label: dict[str, float],
    processing_time_s: float,
    peak_ram_mb: float | None = None,
    cpu_percent: float | None = None,
    quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble la structure complète de ``metrics.json``."""
    metrics = {
        "vad": vad_metrics(duration, speech_ranges),
        "cleaning": cleaning_metrics(duration, cleaned_duration, removed_by_label),
        "performance": performance_metrics(
            processing_time_s, duration, peak_ram_mb=peak_ram_mb, cpu_percent=cpu_percent
        ),
        "segments_total": len(segments),
        "segments_removed": sum(1 for s in segments if s.action == "remove"),
    }
    if quality is not None:
        metrics["quality"] = quality
    return metrics
