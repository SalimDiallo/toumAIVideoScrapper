"""Heuristic transcription-quality metrics."""

from __future__ import annotations

from media_ingestion.transcript_quality import assess, source_kind


def _seg(start, dur, text="mot mot"):
    return {"start_s": start, "duration_s": dur, "text": text}


def test_source_kind_buckets():
    assert source_kind("youtube_manual") == "human"
    assert source_kind("provider_subtitle") == "human"
    assert source_kind("youtube_asr") == "asr"
    assert source_kind("provider_asr") == "asr"
    assert source_kind("youtube_translated") == "translated"
    assert source_kind(None) == "none"
    assert source_kind("weird") == "none"


def test_no_segments_is_unavailable():
    q = assess([], 100, "youtube_manual")
    assert q["available"] is False
    assert q["label"] == "Indisponible"
    assert q["score"] == 0


def test_full_coverage_human_is_excellent():
    # two contiguous 50s segments over a 100s audio -> 100% coverage, human source
    segs = [_seg(0, 50), _seg(50, 50)]
    q = assess(segs, 100, "youtube_manual")
    assert q["available"] is True
    assert q["coverage"] == 1.0
    assert q["start_offset_s"] == 0.0
    assert q["max_gap_s"] == 0.0
    assert q["score"] == 100
    assert q["label"] == "Excellente"


def test_partial_coverage_and_gap_lowers_score():
    # 20s covered out of 100s, big hole in the middle, ASR source
    segs = [_seg(0, 10), _seg(80, 10)]
    q = assess(segs, 100, "youtube_asr")
    assert q["coverage"] == 0.2
    assert q["start_offset_s"] == 0.0
    assert q["max_gap_s"] == 70.0  # 80 - 10
    assert q["end_gap_s"] == 10.0  # 100 - 90
    # asr weight 0.75 * (0.4 + 0.6*0.2) = 0.75*0.52 = 0.39 -> 39
    assert q["score"] == 39
    assert q["label"] == "Faible"


def test_overlapping_segments_counted_once():
    # overlapping intervals must not inflate coverage beyond the audio length
    segs = [_seg(0, 60), _seg(30, 60)]  # union = [0, 90]
    q = assess(segs, 90, "youtube_manual")
    assert q["coverage"] == 1.0


def test_start_offset_detected():
    segs = [_seg(12, 10)]
    q = assess(segs, 100, "youtube_asr")
    assert q["start_offset_s"] == 12.0


def test_unknown_duration_scores_on_source_only():
    segs = [_seg(0, 10)]
    q = assess(segs, None, "youtube_manual")
    assert q["coverage"] is None
    assert q["wpm"] is None
    assert q["score"] == 80  # 100 * 1.0 * 0.8
    assert q["label"] == "Bonne"


def test_wpm_computed():
    segs = [_seg(0, 60, text="one two three")]  # 3 words in 60s = 3 wpm
    q = assess(segs, 60, "youtube_asr")
    assert q["words"] == 3
    assert q["wpm"] == 3.0
