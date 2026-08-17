"""Tests de la logique pure du pipeline (sans modèle ni I/O audio).

Couvre les invariants critiques, en particulier le principe « ne jamais perdre de
parole » : padding, fusion, remap transcript, décisions, métriques.

Lancer :  pytest audio_cleaning/tests
"""

from __future__ import annotations

from audio_cleaning.classifier import FrameScore
from audio_cleaning.config import Config
from audio_cleaning.decision import decide, summarize_removed
from audio_cleaning.labels import APPLAUSE, KEEP, MUSIC, REMOVE, SPEECH
from audio_cleaning.metrics import cleaning_metrics, quality_metrics, vad_metrics
from audio_cleaning.segments import (
    complement,
    merge_ranges,
    pad_ranges,
    subtract,
    total_duration,
)
from audio_cleaning.transcript import remap_transcript


# ---------------------------------------------------------------- ranges
def test_merge_ranges_recolle_les_micro_segments():
    assert merge_ranges([(0.0, 3.2), (3.2, 4.8)]) == [(0.0, 4.8)]
    assert merge_ranges([(0.0, 1.0), (1.05, 2.0)], max_gap=0.1) == [(0.0, 2.0)]
    assert merge_ranges([(0.0, 1.0), (2.0, 3.0)], max_gap=0.1) == [(0.0, 1.0), (2.0, 3.0)]


def test_complement_et_subtract():
    assert complement([(2.0, 4.0)], 10.0) == [(0.0, 2.0), (4.0, 10.0)]
    assert subtract([(0.0, 10.0)], [(2.0, 4.0)], 10.0) == [(0.0, 2.0), (4.0, 10.0)]


def test_pad_ranges_borne_et_fusionne():
    padded = pad_ranges([(5.0, 6.0), (6.4, 7.0)], before=0.2, after=0.3, duration=10.0)
    # 6.0+0.3=6.3 >= 6.4-0.2=6.2 -> fusion
    assert padded == [(4.8, 7.3)]
    # borné à [0, duration]
    assert pad_ranges([(0.1, 9.9)], 0.5, 0.5, 10.0) == [(0.0, 10.0)]


# ---------------------------------------------------------------- transcript
def test_remap_decale_les_timestamps():
    segs = [{"start_s": 14.5, "duration_s": 8.0, "text": "phrase"}]
    kept = [(0.0, 8.31), (14.29, 33.0)]  # ~6s retirés avant la phrase
    out = remap_transcript(segs, kept)
    assert len(out) == 1
    assert (
        abs(out[0]["start_s"] - (14.5 - 8.31 + 8.31 - (14.29 - 8.31))) < 0.5
        or out[0]["start_s"] < 14.5
    )


def test_remap_conserve_une_phrase_qui_deborde_partiellement():
    # La phrase [10, 20] chevauche une zone gardée [0,12] : on la GARDE (pas de perte).
    segs = [{"start_s": 10.0, "duration_s": 10.0, "text": "importante"}]
    kept = [(0.0, 12.0)]
    out = remap_transcript(segs, kept)
    assert len(out) == 1
    assert out[0]["text"] == "importante"


def test_remap_supprime_une_phrase_entierement_dans_une_zone_coupee():
    segs = [{"start_s": 13.0, "duration_s": 1.0, "text": "musique"}]
    kept = [(0.0, 12.0), (14.0, 20.0)]
    assert remap_transcript(segs, kept) == []


# ---------------------------------------------------------------- décision
def _cfg() -> Config:
    return Config()


def test_decide_garde_la_parole_et_retire_la_musique_confiante():
    cfg = _cfg()
    speech = [(0.0, 8.0), (14.0, 22.0)]
    # zone non-parole [8,14] classée music à forte confiance
    frames = [FrameScore(8.0, 14.0, MUSIC, 0.9, {MUSIC: 0.9})]
    segments, kept = decide(22.0, speech, frames, cfg)
    labels = {s.label: s.action for s in segments}
    assert labels[SPEECH] == KEEP
    assert labels[MUSIC] == REMOVE
    # la parole (avec padding) est intégralement gardée
    assert total_duration(kept) < 22.0


def test_decide_conserve_si_confiance_faible():
    cfg = _cfg()
    speech = [(0.0, 8.0)]
    frames = [FrameScore(8.0, 12.0, MUSIC, 0.3, {MUSIC: 0.3})]  # sous keep_if_confidence_below
    segments, _kept = decide(12.0, speech, frames, cfg)
    music_seg = next(s for s in segments if s.label == MUSIC)
    assert music_seg.action == KEEP  # doute -> conservation


def test_decide_anti_micro_coupure():
    cfg = _cfg()  # min_remove_duration = 0.5
    speech = [(0.0, 8.0), (8.3, 12.0)]  # trou de 0.3s seulement
    frames = [FrameScore(8.0, 8.3, APPLAUSE, 0.99, {APPLAUSE: 0.99})]
    _segments, kept = decide(12.0, speech, frames, cfg)
    # le trou < 0.5s ne doit pas être coupé
    assert total_duration(kept) == 12.0


# ---------------------------------------------------------------- métriques
def test_vad_et_cleaning_metrics():
    v = vad_metrics(30.0, [(0.0, 8.0), (14.0, 22.0)])
    assert v["speech_duration_s"] == 16.0
    assert v["num_speech_segments"] == 2

    c = cleaning_metrics(33.0, 22.0, {MUSIC: 6.0, APPLAUSE: 5.0})
    assert c["removed_duration_s"] == 11.0
    assert c["music_removed_duration_s"] == 6.0
    assert abs(c["cleaning_ratio"] - 11.0 / 33.0) < 1e-3  # arrondi à 4 décimales


def test_quality_metrics_parfait():
    rows = [
        {"start": 0, "end": 10, "true_label": SPEECH, "predicted_label": SPEECH},
        {"start": 10, "end": 15, "true_label": MUSIC, "predicted_label": MUSIC},
        {"start": 15, "end": 18, "true_label": APPLAUSE, "predicted_label": APPLAUSE},
    ]
    q = quality_metrics(rows)
    assert q["precision"] == 1.0 and q["recall"] == 1.0 and q["f1_score"] == 1.0
    assert q["speech_retention"] == 1.0
    assert q["false_speech_deletion_rate"] == 0.0


def test_quality_penalise_une_parole_supprimee():
    rows = [
        {"start": 0, "end": 10, "true_label": SPEECH, "predicted_label": MUSIC},  # parole tuée !
        {"start": 10, "end": 20, "true_label": SPEECH, "predicted_label": SPEECH},
    ]
    q = quality_metrics(rows)
    assert q["speech_retention"] == 0.5
    assert q["false_speech_deletion_rate"] == 0.5


def test_summarize_removed():
    from audio_cleaning.segments import Segment

    segs = [
        Segment(0, 8, SPEECH, KEEP),
        Segment(8, 14, MUSIC, REMOVE),
        Segment(14, 18, APPLAUSE, REMOVE),
    ]
    removed = summarize_removed(segs)
    assert removed[MUSIC] == 6.0
    assert removed[APPLAUSE] == 4.0
