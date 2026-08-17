"""Logique de décision : de (parole VAD + évènements classés) aux segments keep/remove.

C'est ici qu'on applique le principe cardinal : **ne jamais supprimer de parole**.
On ne considère à la suppression QUE les zones que le VAD n'a pas jugées parole,
et toute suppression exige une confiance suffisante ; sinon on conserve.

Étapes de ``decide`` :

1. Zones de parole (VAD) -> segments ``speech`` gardés, élargis du padding de
   sécurité (avant/après). Ces marges rognent d'autant les zones non-parole
   adjacentes : on rend du contexte à la parole.
2. Zones non-parole = complément des zones de parole *paddées*. Chacune est
   étiquetée par la classe dominante du classifieur sur son étendue.
3. Décision par zone non-parole :
   - classe candidate à la suppression ET confiance >= seuil -> ``remove`` ;
   - sinon -> ``keep`` (le doute profite à la conservation).
4. On ne supprime pas une zone plus courte que ``min_remove_duration`` (anti
   micro-coupure) : elle repasse en ``keep``.

Sortie : la liste ordonnée de ``Segment`` (toutes les décisions, pour l'audit) et
les intervalles gardés ``kept`` (pour la découpe audio et le remap transcript).
"""

from __future__ import annotations

from .classifier import FrameScore
from .config import Config
from .labels import KEEP, REMOVE, SILENCE, SPEECH
from .segments import (
    Range,
    Segment,
    complement,
    merge_ranges,
    pad_ranges,
)


def decide(
    duration: float,
    speech_ranges: list[Range],
    frame_scores: list[FrameScore],
    cfg: Config,
) -> tuple[list[Segment], list[Range]]:
    """Combine VAD + classification en décisions. Retourne ``(segments, kept_ranges)``."""
    speech = merge_ranges(speech_ranges)
    padded_speech = pad_ranges(
        speech, cfg.vad.speech_padding_before, cfg.vad.speech_padding_after, duration
    )
    non_speech = complement(padded_speech, duration)

    segments: list[Segment] = [
        Segment(start, end, SPEECH, KEEP, 1.0, source="vad") for start, end in padded_speech
    ]

    removed: list[Range] = []
    for start, end in non_speech:
        label, confidence = _label_region(start, end, frame_scores)
        action, conf = _decide_region(label, confidence, end - start, cfg)
        segments.append(Segment(start, end, label, action, conf, source="classifier"))
        if action == REMOVE:
            removed.append((start, end))

    segments.sort(key=lambda s: s.start)
    kept = complement(merge_ranges(removed), duration)
    return segments, kept


def _label_region(start: float, end: float, frame_scores: list[FrameScore]) -> tuple[str, float]:
    """Classe dominante d'une zone : moyenne pondérée (par recouvrement) des trames.

    Une zone sans aucune trame de classifieur (audio quasi-nul) est jugée ``silence``.
    """
    agg: dict[str, float] = {}
    weight = 0.0
    for fs in frame_scores:
        overlap = min(end, fs.end) - max(start, fs.start)
        if overlap <= 0:
            continue
        weight += overlap
        # On répartit la confiance de la trame sur ses classes connues.
        contrib = fs.scores or {fs.label: fs.confidence}
        for lab, sc in contrib.items():
            agg[lab] = agg.get(lab, 0.0) + sc * overlap
    if not agg or weight == 0.0:
        return SILENCE, 0.8
    label = max(agg, key=agg.get)
    # Confiance = score BRUT moyen de la classe gagnante sur la région (pondéré par
    # la durée). On NE normalise PAS par la somme des classes : sinon une région où
    # le modèle n'a vu qu'une classe faible (p.ex. music=0.3) ressortirait à 1.0 et
    # déclencherait une suppression à tort. Ici music=0.3 reste 0.3 -> conservé.
    return label, float(agg[label] / weight)


def _decide_region(
    label: str, confidence: float, duration: float, cfg: Config
) -> tuple[str, float]:
    """Décision finale sur une zone non-parole (keep/remove) + confiance retenue."""
    dec = cfg.decision

    # Filet global : sous ce seuil, on conserve quoi qu'il arrive (sauf silence franc).
    if label != SILENCE and confidence < dec.keep_if_confidence_below:
        return KEEP, confidence

    if not dec.removes(label):
        return KEEP, confidence

    # Silence : seuil dédié absent -> on se fie à la détection de zone.
    threshold = 0.0 if label == SILENCE else cfg.classification.threshold_for(label)
    if confidence < threshold:
        return KEEP, confidence

    # Anti micro-coupure : une suppression trop courte est annulée.
    if duration < cfg.processing.min_remove_duration:
        return KEEP, confidence

    return REMOVE, confidence


def summarize_removed(segments: list[Segment]) -> dict[str, float]:
    """Durée supprimée par classe (pour les métriques de nettoyage)."""
    by_label: dict[str, float] = {}
    for seg in segments:
        if seg.action == REMOVE:
            by_label[seg.label] = by_label.get(seg.label, 0.0) + seg.duration
    return by_label
