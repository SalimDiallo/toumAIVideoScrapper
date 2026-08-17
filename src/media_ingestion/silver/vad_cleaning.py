"""Pont entre la couche silver et le package ``audio_cleaning`` (moteur VAD).

Le nettoyage silver « moteur vad » délègue à ``audio_cleaning`` : Voice Activity
Detection (Silero) + classification d'évènements (YAMNet) au lieu de demucs +
silencedetect. Ce module isole cette dépendance pour qu'elle soit importée
*paresseusement* : le worker léger (sans ``audio_cleaning`` installé) n'échoue pas
à l'import, il retombe sur le moteur ffmpeg (voir ``SilverPipeline._resolve_engine``).

Tout ``audio_cleaning`` est importé ici, jamais au niveau du package silver.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class VadCleanResult:
    """Bilan d'un nettoyage VAD, prêt à être écrit en silver."""

    cleaned_audio: Path
    cleaned_segments: list[dict]  # transcript resynchronisé (liste de cues)
    original_duration_s: float
    cleaned_duration_s: float
    removed_by_label: dict[str, float]  # durée retirée par classe
    metrics: dict[str, Any]  # metrics.json complet du pipeline


def build_cleaning_pipeline(params: Any):
    """Construit le ``CleaningPipeline`` (charge les modèles) d'après ``SilverParams``.

    Levé si ``audio_cleaning`` ou ses dépendances manquent : l'appelant l'attrape
    pour retomber sur le moteur ffmpeg.
    """
    from audio_cleaning.config import (
        AudioConfig,
        ClassificationConfig,
        Config,
        DecisionConfig,
        OutputConfig,
        ProcessingConfig,
        VadConfig,
    )
    from audio_cleaning.pipeline import CleaningPipeline

    ffmpeg_dir = str(params.ffmpeg_dir) if params.ffmpeg_dir else None
    thr = params.event_threshold
    config = Config(
        audio=AudioConfig(loudnorm=True, ffmpeg_dir=ffmpeg_dir),
        processing=ProcessingConfig(sample_rate=16000, min_remove_duration=params.min_silence_s),
        vad=VadConfig(
            backend=params.vad_backend,
            onnx=False,  # torch déjà présent (via demucs) ; évite la dep onnxruntime
            threshold=params.vad_threshold,
            speech_padding_before=params.padding_s,
            speech_padding_after=params.padding_s,
        ),
        classification=ClassificationConfig(
            backend=params.classifier_backend,
            music_threshold=thr,
            applause_threshold=thr,
            noise_threshold=thr,
            laughter_threshold=thr,
        ),
        decision=DecisionConfig(
            remove_music=params.remove_music,
            # `remove_nonspeech` (applaudissements / musique annotés côté ffmpeg) se
            # traduit en suppression des évènements applaudissements + bruit.
            remove_applause=params.remove_nonspeech,
            remove_noise=params.remove_nonspeech,
            remove_silence=True,
            remove_laughter=False,
            remove_other=False,
        ),
        output=OutputConfig(save_original_wav=False, save_visualization=False),
    )
    return CleaningPipeline(config)


def clean(
    pipeline: Any,
    audio_path: Path,
    transcript_segments: list[dict],
    work_dir: Path,
    *,
    video_id: str,
) -> VadCleanResult:
    """Lance le nettoyage VAD sur un audio local + son transcript. Ne fait pas d'I/O réseau."""
    from audio_cleaning.transcript import save_transcript

    out_dir = work_dir / "vad_out"
    transcript_path = None
    if transcript_segments:
        transcript_path = work_dir / "transcript_in.json"
        save_transcript(transcript_path, transcript_segments)

    result = pipeline.process(
        audio_path, video_id=video_id, transcript_path=transcript_path, out_dir=out_dir
    )

    cleaned_audio = out_dir / "cleaned.wav"
    cleaned_segments: list[dict] = []
    tc = out_dir / "transcript_cleaned.json"
    if tc.exists():
        cleaned_segments = json.loads(tc.read_text(encoding="utf-8"))

    c = result.metrics["cleaning"]
    removed = {
        "music": c["music_removed_duration_s"],
        "applause": c["applause_removed_duration_s"],
        "noise": c["noise_removed_duration_s"],
        "silence": c["silence_removed_duration_s"],
    }
    return VadCleanResult(
        cleaned_audio=cleaned_audio,
        cleaned_segments=cleaned_segments,
        original_duration_s=c["original_duration_s"],
        cleaned_duration_s=c["cleaned_duration_s"],
        removed_by_label=removed,
        metrics=result.metrics,
    )
