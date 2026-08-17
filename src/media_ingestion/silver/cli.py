"""Point d'entrée CLI de la couche silver : `toumai-silver [options]`.

Lit les entrées bronze sur MinIO, les nettoie (suppression musique + silences),
resynchronise le transcript et écrit le résultat en silver. Idempotent : une
entrée déjà présente en silver est ignorée, sauf avec ``--force``.

Exemples ::

    toumai-silver                                   # défauts de la config
    toumai-silver --silence-threshold -30 --min-silence 0.8
    toumai-silver --no-music-removal --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..bootstrap import build_silver_store
from ..config import Settings
from ..logging_setup import configure_logging
from .pipeline import SilverParams, SilverPipeline


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(prog="toumai-silver", description=__doc__)
    parser.add_argument(
        "--silence-threshold",
        type=float,
        default=settings.silver_silence_threshold_db,
        help="Seuil de silence en dB (défaut : %(default)s).",
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=settings.silver_min_silence_s,
        help="Durée minimale d'un silence à couper, en secondes (défaut : %(default)s).",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=settings.silver_padding_s,
        help="Marge conservée autour des coupes, en secondes (défaut : %(default)s).",
    )
    parser.add_argument(
        "--engine",
        choices=["vad", "ffmpeg"],
        default=settings.silver_engine,
        help="Moteur de nettoyage : vad (Silero+YAMNet) ou ffmpeg (demucs). Défaut : %(default)s.",
    )
    parser.add_argument(
        "--vad-backend",
        default=settings.silver_vad_backend,
        help="Backend VAD du moteur vad : silero | energy (défaut : %(default)s).",
    )
    parser.add_argument(
        "--classifier-backend",
        default=settings.silver_classifier_backend,
        help="Classifieur du moteur vad : yamnet | panns | heuristic (défaut : %(default)s).",
    )
    parser.add_argument(
        "--demucs-model",
        default=settings.silver_demucs_model,
        help="Modèle demucs pour l'isolation de la voix (moteur ffmpeg). Défaut : %(default)s.",
    )
    parser.add_argument(
        "--no-music-removal",
        action="store_true",
        help="Ne pas lancer demucs (garde l'audio d'origine avant la découpe).",
    )
    parser.add_argument(
        "--no-nonspeech-removal",
        action="store_true",
        help="Ne pas couper les passages non-parlés annotés (applaudissements, musique, ♪).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-traiter une entrée même si elle est déjà présente en silver.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    params = SilverParams(
        silence_threshold_db=args.silence_threshold,
        min_silence_s=args.min_silence,
        padding_s=args.padding,
        remove_music=not args.no_music_removal,
        remove_nonspeech=not args.no_nonspeech_removal,
        nonspeech_keywords=tuple(settings.silver_nonspeech_keywords) or None,
        demucs_model=args.demucs_model,
        ffmpeg_dir=Path(settings.ffmpeg_location) if settings.ffmpeg_location else None,
        engine=args.engine,
        vad_backend=args.vad_backend,
        classifier_backend=args.classifier_backend,
        vad_threshold=settings.silver_vad_threshold,
        event_threshold=settings.silver_event_threshold,
    )
    pipeline = SilverPipeline(build_silver_store(settings), params)
    summary = pipeline.run(force=args.force)

    print(
        f"silver terminé : {summary.processed} traitée(s), "
        f"{summary.skipped} ignorée(s), {summary.failed} échec(s)"
    )
    return 1 if summary.failed and not summary.processed else 0


if __name__ == "__main__":
    sys.exit(main())
