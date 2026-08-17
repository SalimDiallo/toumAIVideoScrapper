"""Remapping des timestamps bronze -> silver (logique pure, sans I/O).

C'est la partie la plus délicate du pipeline. Quand on retire les silences d'un
audio, on garde une suite de segments non-silencieux qu'on recolle bout à bout :
la durée totale change et *tous* les timestamps du transcript doivent être
recalculés pour rester alignés sur ce nouvel audio.

Vocabulaire :

- ``kept`` : les segments NON-silencieux gardés, exprimés dans la timeline
  ORIGINALE, sous forme de couples ``(start_s, end_s)`` triés et disjoints.
  Ce sont eux qu'on concatène pour produire l'audio silver.
- timeline NOUVELLE : les ``kept`` mis bout à bout, sans trou. La position d'un
  instant original ``t`` situé dans le i-ème segment gardé ``[s_i, e_i]`` vaut
  ``(somme des durées des segments gardés avant i) + (t - s_i)``.

Aucune dépendance ffmpeg/minio ici -> testable en isolation (voir tests/).
"""

from __future__ import annotations

import re
from collections.abc import Sequence

Range = tuple[float, float]

# Marqueurs de passages NON-parlés que les transcripts insèrent entre crochets ou
# parenthèses : applaudissements, musique, rires, chants... On coupe l'audio et on
# retire ces sous-titres. Volontairement conservateur (cue entre [] ou () + mot-clé,
# ou notes de musique seules) pour ne jamais supprimer de vraie parole.
_NONSPEECH_KEYWORDS = (
    "applause",
    "applaudiss",
    "music",
    "musique",
    "musik",
    "laughter",
    "laughs",
    "rire",  # rire / rires
    "cheer",
    "clapping",
    "chant",  # chant / chanting
    "singing",
    "instrumental",
    "inaudible",
)
_MUSIC_NOTES_RE = re.compile(r"^[\s♪♫♬♩]*[♪♫♬♩][\s♪♫♬♩]*$")


def is_non_speech(text: str | None, keywords: Sequence[str] | None = None) -> bool:
    """True si le texte d'un sous-titre est une annotation non-parlée (à retirer).

    Reconnu :

    - tout sous-titre entièrement entre crochets ``[...]`` (annotation type
      ``[Applause]``, ``[Musique]``, ``[Foreign]``... — retiré quel qu'en soit le
      contenu) ;
    - un cue entre parenthèses ``(...)`` contenant un des ``keywords`` (garde-fou :
      les parenthèses servent aussi à de vraies incises parlées) ;
    - une ligne faite uniquement de notes de musique (``♪``).

    ``keywords`` : liste par défaut ``_NONSPEECH_KEYWORDS`` si ``None``.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _MUSIC_NOTES_RE.match(t):
        return True
    # Tout segment ENTIÈREMENT entre crochets est une annotation -> retiré.
    if t.startswith("[") and t.endswith("]"):
        return True
    # Entre parenthèses : seulement si un mot-clé est présent (évite de couper une
    # vraie incise parlée mise entre parenthèses dans le transcript).
    if t.startswith("(") and t.endswith(")"):
        low = t.lower()
        return any(
            kw and kw.lower() in low
            for kw in (keywords if keywords is not None else _NONSPEECH_KEYWORDS)
        )
    return False


def non_speech_ranges(segments: list[dict], keywords: Sequence[str] | None = None) -> list[Range]:
    """Plages temporelles ``(start, end)`` des sous-titres non-parlés (originale)."""
    out: list[Range] = []
    for seg in segments or []:
        if is_non_speech(seg.get("text"), keywords):
            start = float(seg.get("start_s", 0.0) or 0.0)
            end = start + max(float(seg.get("duration_s", 0.0) or 0.0), 0.0)
            if end > start:
                out.append((start, end))
    return out


def compute_kept_ranges(
    duration_s: float,
    silences: list[Range],
    *,
    min_silence_s: float,
    padding_s: float,
    extra_cuts: list[Range] | None = None,
) -> list[Range]:
    """Déduit les segments à GARDER à partir des silences détectés.

    ``silences`` : intervalles ``(start, end)`` détectés par ffmpeg silencedetect
    (timeline originale). ``extra_cuts`` : plages à retirer en plus, quelle que soit
    leur durée (p.ex. applaudissements / musique repérés dans le transcript). On
    procède ainsi :

    1. On ignore les silences plus courts que ``min_silence_s`` (garde-fou ; ffmpeg
       filtre déjà via ``d=``), et on ajoute les ``extra_cuts`` (non filtrés).
    2. ``kept`` = complément des coupes sur ``[0, duration_s]``.
    3. On ré-élargit chaque segment gardé de ``padding_s`` de chaque côté : on
       "rend" un peu de silence autour des coupes pour ne pas tronquer le début
       ou la fin des mots. Les segments qui se chevauchent après padding sont
       fusionnés (un silence plus court que 2*padding est entièrement récupéré).

    Retourne une liste de ``(start, end)`` triés, disjoints, dans ``[0, duration]``.
    """
    if duration_s <= 0:
        return []

    # 1. Silences assez longs + coupes supplémentaires (toute durée), bornés et triés.
    cuts: list[Range] = []
    for start, end in silences:
        start = max(0.0, min(start, duration_s))
        end = max(0.0, min(end, duration_s))
        if end - start >= min_silence_s:
            cuts.append((start, end))
    for start, end in extra_cuts or []:
        start = max(0.0, min(start, duration_s))
        end = max(0.0, min(end, duration_s))
        if end > start:
            cuts.append((start, end))
    cuts.sort()

    # 2. Complément des silences = segments non-silencieux (avant padding).
    kept: list[Range] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_s:
        kept.append((cursor, duration_s))

    # Aucun silence coupable -> on garde tout l'audio tel quel.
    if not kept:
        return [(0.0, duration_s)] if not cuts else []

    # 3. Padding + fusion des segments qui se recouvrent après élargissement.
    padded: list[Range] = []
    for start, end in kept:
        p_start = max(0.0, start - padding_s)
        p_end = min(duration_s, end + padding_s)
        if padded and p_start <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], p_end))
        else:
            padded.append((p_start, p_end))
    return [(s, e) for s, e in padded if e > s]


def _cumulative_offsets(kept: list[Range]) -> tuple[list[float], float]:
    """Décalage de départ de chaque segment gardé dans la NOUVELLE timeline.

    ``offsets[i]`` = somme des durées des segments gardés avant ``i`` = position,
    dans l'audio final, du début du i-ème segment. Retourne aussi la durée totale.
    """
    offsets: list[float] = []
    total = 0.0
    for start, end in kept:
        offsets.append(total)
        total += end - start
    return offsets, total


def _map_instant(t: float, kept: list[Range], offsets: list[float]) -> float | None:
    """Projette un instant original ``t`` sur la nouvelle timeline.

    Renvoie ``None`` si ``t`` tombe dans un passage supprimé (aucun segment gardé
    ne le contient). Les bornes sont inclusives pour que la fin d'un segment gardé
    se projette proprement sur le début du suivant.
    """
    for (start, end), offset in zip(kept, offsets):
        if start <= t <= end:
            return offset + (t - start)
    return None


def remap_transcript(segments: list[dict], kept: list[Range]) -> list[dict]:
    """Recalcule les timestamps d'un transcript sur l'audio silver.

    Pour chaque sous-titre ``{start_s, duration_s, text}`` (timeline originale) :

    - on intersecte ``[start, end]`` avec l'union des segments gardés ;
    - si l'intersection est vide, le sous-titre tombe entièrement dans un passage
      supprimé -> il est EXCLU du transcript final ;
    - sinon on projette le premier instant gardé (nouveau ``start_s``) et le
      dernier instant gardé (nouveau ``end``) sur la timeline finale. Comme les
      segments gardés sont contigus dans la nouvelle timeline, la durée obtenue
      correspond exactement au temps de parole réellement conservé (les silences
      internes au sous-titre, eux, se retrouvent bien retirés).

    Les segments sont supposés triés ; l'ordre d'origine est préservé.
    """
    offsets, _total = _cumulative_offsets(kept)
    out: list[dict] = []
    for seg in segments:
        start = float(seg.get("start_s", 0.0) or 0.0)
        duration = float(seg.get("duration_s", 0.0) or 0.0)
        end = start + max(duration, 0.0)

        # Portions du sous-titre qui survivent = intersection avec les gardés.
        overlaps: list[Range] = []
        for k_start, k_end in kept:
            lo = max(start, k_start)
            hi = min(end, k_end)
            if hi > lo:
                overlaps.append((lo, hi))
        if not overlaps:
            continue  # entièrement dans un silence supprimé

        new_start = _map_instant(overlaps[0][0], kept, offsets)
        new_end = _map_instant(overlaps[-1][1], kept, offsets)
        if new_start is None or new_end is None:  # garde-fou (ne devrait pas arriver)
            continue

        out.append(
            {
                **seg,
                "start_s": round(new_start, 3),
                "duration_s": round(max(new_end - new_start, 0.0), 3),
            }
        )
    return out
