"""Silver : calcul des segments gardés + remapping des timestamps (logique pure)."""

from __future__ import annotations

from media_ingestion.silver.remap import (
    compute_kept_ranges,
    is_non_speech,
    non_speech_ranges,
    remap_transcript,
)


def _seg(start, dur, text="mot"):
    return {"start_s": start, "duration_s": dur, "text": text}


# ---------------------------------------------------------------- kept ranges
def test_kept_ranges_complement_of_silence():
    kept = compute_kept_ranges(10.0, [(4.0, 6.0)], min_silence_s=0.5, padding_s=0.0)
    assert kept == [(0.0, 4.0), (6.0, 10.0)]


def test_kept_ranges_apply_padding_around_cuts():
    kept = compute_kept_ranges(10.0, [(4.0, 6.0)], min_silence_s=0.5, padding_s=0.1)
    assert kept == [(0.0, 4.1), (5.9, 10.0)]


def test_kept_ranges_ignore_short_silences():
    # silence de 0.2s < min 0.5s -> non coupé, tout l'audio est gardé
    kept = compute_kept_ranges(10.0, [(4.0, 4.2)], min_silence_s=0.5, padding_s=0.0)
    assert kept == [(0.0, 10.0)]


def test_kept_ranges_no_silence_keeps_everything():
    assert compute_kept_ranges(10.0, [], min_silence_s=0.5, padding_s=0.0) == [(0.0, 10.0)]


def test_kept_ranges_padding_merges_close_segments():
    # silence de 0.1s entre deux segments, padding 0.1 -> entièrement récupéré
    kept = compute_kept_ranges(10.0, [(5.0, 5.1)], min_silence_s=0.05, padding_s=0.1)
    assert kept == [(0.0, 10.0)]


# ------------------------------------------------------------------- remapping
def test_remap_shifts_segment_after_a_cut():
    # kept = [(0,4),(6,10)] : la 2e plage démarre à 4.0 dans le nouvel audio.
    kept = [(0.0, 4.0), (6.0, 10.0)]
    out = remap_transcript([_seg(7.0, 1.0)], kept)
    assert out == [{"start_s": 5.0, "duration_s": 1.0, "text": "mot"}]


def test_remap_keeps_segment_before_cut_unchanged():
    kept = [(0.0, 4.0), (6.0, 10.0)]
    out = remap_transcript([_seg(1.0, 1.0)], kept)
    assert out == [{"start_s": 1.0, "duration_s": 1.0, "text": "mot"}]


def test_remap_drops_segment_inside_removed_silence():
    kept = [(0.0, 4.0), (6.0, 10.0)]
    out = remap_transcript([_seg(4.5, 1.0)], kept)  # [4.5, 5.5] entièrement supprimé
    assert out == []


def test_remap_collapses_segment_spanning_a_cut():
    # [3,7] chevauche la coupe [4,6] : parties gardées (3,4) et (6,7).
    kept = [(0.0, 4.0), (6.0, 10.0)]
    out = remap_transcript([_seg(3.0, 4.0)], kept)
    # new_start = map(3) = 3 ; new_end = map(7) = 4 + (7-6) = 5 -> durée 2
    assert out == [{"start_s": 3.0, "duration_s": 2.0, "text": "mot"}]


def test_remap_preserves_order_and_extra_keys():
    kept = [(0.0, 100.0)]
    segs = [{"start_s": 1.0, "duration_s": 1.0, "text": "a", "extra": 1}]
    assert remap_transcript(segs, kept)[0]["extra"] == 1


# ------------------------------------------------------------- non-speech cues
def test_is_non_speech_detects_annotations():
    assert is_non_speech("[Applause]")
    assert is_non_speech("(applaudissements)")
    assert is_non_speech("[MUSIC PLAYING]")
    assert is_non_speech("[Musique]")
    assert is_non_speech("[Rires]")
    assert is_non_speech("♪♪♪")
    assert is_non_speech("♪")


def test_is_non_speech_removes_any_bracketed_segment():
    # Tout segment entièrement entre crochets est retiré, même sans mot-clé connu.
    assert is_non_speech("[Foreign]")
    assert is_non_speech("[bruit de fond]")
    assert is_non_speech("[__]")
    # mais pas une phrase qui contient seulement des crochets internes
    assert not is_non_speech("dis bonjour [à ta sœur] maintenant")


def test_is_non_speech_keeps_real_speech():
    assert not is_non_speech("Bonjour à tous")
    assert not is_non_speech("")
    assert not is_non_speech(None)
    # une vraie phrase mentionnant la musique n'est pas un cue (pas entre crochets)
    assert not is_non_speech("j'adore la musique classique")


def test_is_non_speech_honours_custom_keywords_for_parentheses():
    # Le garde-fou mots-clés s'applique aux PARENTHÈSES (les crochets sont toujours
    # retirés). Liste custom : "(toux)" reconnu, "(applause)" hors liste ignoré.
    assert is_non_speech("(toux)", keywords=["toux"])
    assert not is_non_speech("(applause)", keywords=["toux"])
    # crochets : toujours retirés, quelle que soit la liste.
    assert is_non_speech("[Applause]", keywords=["toux"])
    # les notes de musique restent détectées quelle que soit la liste.
    assert is_non_speech("♪", keywords=["toux"])


def test_non_speech_ranges_extracts_time_windows():
    segs = [
        {"start_s": 0.0, "duration_s": 2.0, "text": "Bonjour"},
        {"start_s": 5.0, "duration_s": 3.0, "text": "[Applause]"},
        {"start_s": 10.0, "duration_s": 4.0, "text": "♪♪"},
    ]
    assert non_speech_ranges(segs) == [(5.0, 8.0), (10.0, 14.0)]


def test_extra_cuts_are_removed_regardless_of_length():
    # une plage non-parlée courte (0.3s) est coupée même sous min_silence_s.
    kept = compute_kept_ranges(10.0, [], min_silence_s=0.5, padding_s=0.0, extra_cuts=[(4.0, 4.3)])
    assert kept == [(0.0, 4.0), (4.3, 10.0)]
