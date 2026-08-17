"""Orchestration bout-en-bout : une vidéo -> dossier de sorties complet.

Enchaîne les étapes (voir README) en gardant la RAM bornée (VAD et classification
tournent bloc par bloc via ``audio_io.iter_chunks``) :

    normalisation -> VAD -> classification -> décision -> découpe audio
    -> resynchro transcript -> métriques -> visualisation.

Produit, dans ``<output.root>/<video_id>/`` :
    original.wav, cleaned.wav, transcript_original.json, transcript_cleaned.json,
    segments.json, metrics.json, visualization.png.

Aucune de ces étapes n'attaque directement le réseau/MinIO : le pipeline prend un
fichier audio local (déjà extrait par le workflow YouTube existant) et un
transcript JSON. L'intégration avec le stockage se fait en amont/aval.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import audio_io
from .classifier import FrameScore, build_classifier
from .config import Config
from .decision import decide, summarize_removed
from .metrics import build_metrics
from .segments import Range, Segment, merge_ranges
from .transcript import load_transcript, remap_transcript, save_transcript
from .vad import build_vad


@dataclass
class PipelineResult:
    video_id: str
    output_dir: Path
    metrics: dict[str, Any]
    segments: list[Segment]
    kept: list[Range]


class CleaningPipeline:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # Les modèles sont construits une fois et réutilisés sur tout un lot de vidéos.
        self._vad = build_vad(cfg.vad)
        self._classifier = build_classifier(cfg.classification)
        # Silero (LSTM torch) et YAMNet portent un état interne réinitialisé à chaque
        # appel : deux ``process`` concurrents sur la MÊME instance (worker multi-thread)
        # corrompraient mutuellement cet état. On sérialise donc l'inférence. Les étapes
        # I/O (téléchargement, normalisation ffmpeg, découpe) restent hors du verrou et
        # continuent de se recouvrir entre threads.
        self._infer_lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def process(
        self,
        audio_path: str | Path,
        *,
        video_id: str,
        transcript_path: str | Path | None = None,
        out_dir: str | Path | None = None,
    ) -> PipelineResult:
        """Nettoie une vidéo et écrit toutes les sorties. Retourne le bilan.

        ``out_dir`` : dossier de sortie explicite (sinon ``<output.root>/<video_id>``).
        Le passer permet d'appeler ``process`` en parallèle sur une instance partagée
        (modèles chargés une fois) sans muter la config commune — utile côté worker.
        """
        cfg = self._cfg
        out_dir = Path(out_dir) if out_dir is not None else Path(cfg.output.root) / video_id
        out_dir.mkdir(parents=True, exist_ok=True)
        work = out_dir / "_work"
        work.mkdir(exist_ok=True)

        proc = _ResourceProbe()
        t0 = time.perf_counter()
        try:
            # 1. Normalisation -> WAV mono 16 kHz (référence de toute la suite).
            norm_wav = audio_io.to_normalized_wav(
                audio_path,
                work / "normalized.wav",
                sample_rate=cfg.processing.sample_rate,
                loudnorm=cfg.audio.loudnorm,
                ffmpeg_dir=cfg.audio.ffmpeg_dir,
            )
            duration = audio_io.probe_duration(norm_wav, ffmpeg_dir=cfg.audio.ffmpeg_dir)

            # 2+3. VAD + classification par blocs (RAM bornée).
            speech_ranges, frame_scores = self._analyze(norm_wav, duration)

            # 4. Décisions keep/remove.
            segments, kept = decide(duration, speech_ranges, frame_scores, cfg)
            if not kept:
                # Garde-fou : jamais produire un audio vide (préférence conservation).
                # On garde tout ET on réconcilie les décisions pour que segments.json
                # ne contredise pas l'audio produit.
                kept = [(0.0, duration)]
                for seg in segments:
                    seg.action = "keep"
                    seg.extra["kept_by_guard"] = True

            # 5. Découpe audio -> cleaned.wav.
            cleaned_wav = out_dir / f"cleaned.{cfg.output.audio_format}"
            cleaned_duration = audio_io.cut_and_write(
                norm_wav, kept, cleaned_wav, sample_rate=cfg.processing.sample_rate
            )
            if cfg.output.save_original_wav:
                shutil.copyfile(norm_wav, out_dir / f"original.{cfg.output.audio_format}")

            # 6. Transcript resynchronisé.
            self._write_transcripts(transcript_path, kept, out_dir)

            # 7. Métriques + segments + visualisation.
            elapsed = time.perf_counter() - t0
            ram, cpu = proc.sample()
            metrics = build_metrics(
                duration=duration,
                speech_ranges=merge_ranges(speech_ranges),
                segments=segments,
                cleaned_duration=cleaned_duration,
                removed_by_label=summarize_removed(segments),
                processing_time_s=elapsed,
                peak_ram_mb=ram,
                cpu_percent=cpu,
            )
            self._write_json(out_dir / "segments.json", [s.to_dict() for s in segments])
            self._write_json(out_dir / "metrics.json", metrics)
            if cfg.output.save_visualization:
                from .visualize import render_timeline  # import paresseux (matplotlib)

                render_timeline(segments, duration, out_dir / "visualization.png", title=video_id)

            return PipelineResult(video_id, out_dir, metrics, segments, kept)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ----------------------------------------------------------------- interne
    def _analyze(self, wav: Path, duration: float) -> tuple[list[Range], list[FrameScore]]:
        """VAD + classification bloc par bloc ; concatène les résultats en absolu."""
        cfg = self._cfg
        speech: list[Range] = []
        frames: list[FrameScore] = []
        # Verrou tenu pendant toute l'analyse : Silero/YAMNet ne sont pas ré-entrants
        # (état interne partagé). Deux vidéos analysées en parallèle sur cette instance
        # attendent donc leur tour ici, mais leurs autres étapes (I/O) restent parallèles.
        with self._infer_lock:
            for offset_s, block in audio_io.iter_chunks(
                wav,
                sample_rate=cfg.processing.sample_rate,
                chunk_duration_s=cfg.processing.chunk_duration_s,
                overlap_s=cfg.processing.chunk_overlap_s,
            ):
                sr = cfg.processing.sample_rate
                for start, end in self._vad.speech_ranges(block, sr):
                    speech.append((offset_s + start, offset_s + end))
                # On ne classe que les zones non-parole du bloc (économie de calcul) :
                # la classification sert uniquement à décider quoi supprimer hors parole.
                frames.extend(self._classify_nonspeech(block, sr, offset_s))
        # Fusion des recouvrements de blocs.
        return merge_ranges(speech, max_gap=cfg.vad.min_silence_duration), frames

    def _classify_nonspeech(self, block: np.ndarray, sr: int, offset_s: float) -> list[FrameScore]:
        """Classe le bloc entier ; les trames tombant dans la parole seront ignorées
        au moment de la décision (elles ne recouvrent pas de zone non-parole)."""
        return self._classifier.classify(block, sr, offset_s)

    def _write_transcripts(
        self, transcript_path: str | Path | None, kept: list[Range], out_dir: Path
    ) -> None:
        if transcript_path is None or not Path(transcript_path).exists():
            return
        original = load_transcript(transcript_path)
        save_transcript(out_dir / "transcript_original.json", original)
        cleaned = remap_transcript(original, kept)
        save_transcript(out_dir / "transcript_cleaned.json", cleaned)

    @staticmethod
    def _write_json(path: Path, obj: Any) -> None:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


class _ResourceProbe:
    """Mesure pic RAM et charge CPU du process (via psutil si présent)."""

    def __init__(self) -> None:
        try:
            import psutil

            self._p = psutil.Process()
            self._p.cpu_percent(None)  # amorce la mesure
        except Exception:  # noqa: BLE001 - psutil optionnel
            self._p = None

    def sample(self) -> tuple[float | None, float | None]:
        if self._p is None:
            return None, None
        try:
            ram = self._p.memory_info().rss / (1024 * 1024)
            cpu = self._p.cpu_percent(None)
            return ram, cpu
        except Exception:  # noqa: BLE001
            return None, None
