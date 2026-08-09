"""Heuristic transcription-quality metrics for an ingested video.

We keep no ground-truth reference transcript, so *true* accuracy (word error
rate) cannot be measured. Instead we derive interpretable proxies from the
caption timing and provenance:

- ``coverage`` — share of the audio duration actually spanned by caption
  segments. Low coverage means large untranscribed / silent stretches.
- ``start_offset_s`` — delay before the first caption (initial desync).
- ``max_gap_s`` — largest hole between consecutive captions (mid-stream loss).
- ``wpm`` — words per minute, a speaking-rate sanity check.
- source reliability — author-written captions are human quality, ASR is
  machine-guessed, translated is machine-translated on top.

These feed a coarse quality *label* / *score*. It is explicitly an estimate of
reliability, not a verified accuracy figure.
"""

from __future__ import annotations

# transcript_source values (kept as plain strings to avoid importing the enum).
_HUMAN_SOURCES = {"youtube_manual", "provider_subtitle"}
_ASR_SOURCES = {"youtube_asr", "provider_asr"}
_TRANSLATED_SOURCES = {"youtube_translated"}

# How much each provenance is trusted before timing is taken into account.
_SOURCE_WEIGHT = {"human": 1.0, "asr": 0.75, "translated": 0.6}


def source_kind(source: str | None) -> str:
    """Coarse provenance bucket: 'human' | 'asr' | 'translated' | 'none'."""
    if source in _HUMAN_SOURCES:
        return "human"
    if source in _ASR_SOURCES:
        return "asr"
    if source in _TRANSLATED_SOURCES:
        return "translated"
    return "none"


def _merged_covered_seconds(intervals: list[tuple[float, float]]) -> float:
    """Total length of the union of [start, end] intervals (overlaps counted once)."""
    if not intervals:
        return 0.0
    intervals = sorted(intervals)
    covered = 0.0
    cur_start, cur_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= cur_end:  # overlap / touch -> extend
            cur_end = max(cur_end, end)
        else:
            covered += cur_end - cur_start
            cur_start, cur_end = start, end
    covered += cur_end - cur_start
    return covered


def assess(
    segments: list[dict] | None,
    duration_s: int | float | None,
    source: str | None,
) -> dict:
    """Compute quality proxies for one transcript.

    ``segments`` are ``{start_s, duration_s, text}`` dicts (as stored). Returns a
    dict of metrics plus a human ``label`` and 0-100 ``score`` (both estimates).
    """
    segments = segments or []
    kind = source_kind(source)

    if not segments:
        return {
            "available": False,
            "source_kind": kind,
            "n_segments": 0,
            "words": 0,
            "coverage": None,
            "start_offset_s": None,
            "max_gap_s": None,
            "end_gap_s": None,
            "wpm": None,
            "score": 0,
            "label": "Indisponible",
        }

    intervals: list[tuple[float, float]] = []
    words = 0
    for s in segments:
        start = float(s.get("start_s", 0) or 0)
        dur = float(s.get("duration_s", 0) or 0)
        intervals.append((start, start + max(dur, 0.0)))
        words += len((s.get("text") or "").split())

    intervals.sort()
    first_start = intervals[0][0]
    last_end = max(end for _, end in intervals)
    covered_s = _merged_covered_seconds(intervals)

    # Largest hole between consecutive (already union-sorted) segments.
    max_gap = 0.0
    prev_end = intervals[0][1]
    for start, end in intervals[1:]:
        max_gap = max(max_gap, start - prev_end)
        prev_end = max(prev_end, end)

    dur = float(duration_s) if duration_s else None
    coverage = min(covered_s / dur, 1.0) if dur and dur > 0 else None
    end_gap = max(dur - last_end, 0.0) if dur else None
    wpm = round(words / (dur / 60.0), 1) if dur and dur > 0 else None

    weight = _SOURCE_WEIGHT.get(kind, 0.0)
    if coverage is None:
        score = round(100 * weight * 0.8)  # unknown duration -> provenance only
    else:
        score = round(100 * weight * (0.4 + 0.6 * coverage))
    score = max(0, min(100, score))

    if score >= 85:
        label = "Excellente"
    elif score >= 70:
        label = "Bonne"
    elif score >= 50:
        label = "Moyenne"
    else:
        label = "Faible"

    return {
        "available": True,
        "source_kind": kind,
        "n_segments": len(segments),
        "words": words,
        "coverage": coverage,  # 0..1 or None
        "start_offset_s": round(first_start, 1),
        "max_gap_s": round(max_gap, 1),
        "end_gap_s": round(end_gap, 1) if end_gap is not None else None,
        "wpm": wpm,
        "score": score,
        "label": label,
    }
