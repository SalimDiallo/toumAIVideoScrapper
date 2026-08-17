"""Orchestration bronze -> silver (idempotence, logs par vidéo, tolérance aux erreurs).

Pour chaque entrée bronze non encore présente en silver (sauf ``force``) :

1. télécharge l'audio brut + le transcript + les métadonnées depuis MinIO ;
2. (option) isole la voix avec demucs pour retirer la musique de fond ;
3. détecte les silences (ffmpeg) et en déduit les segments à garder ;
4. recolle les segments gardés en un seul .wav ;
5. resynchronise les timestamps du transcript sur ce nouvel audio ;
6. écrit l'audio nettoyé + le transcript resynchronisé dans la couche silver.

Une erreur sur une vidéo est journalisée puis ignorée : le traitement continue
sur les suivantes (le lot ne s'arrête pas pour une seule vidéo en échec).
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from . import audio as audio_ops
from .remap import compute_kept_ranges, is_non_speech, non_speech_ranges, remap_transcript
from .storage import BronzeEntry, SilverMinioStore

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SilverParams:
    """Paramètres de nettoyage (défauts alignés sur la config / le prompt)."""

    silence_threshold_db: float = -35.0
    min_silence_s: float = 0.5
    padding_s: float = 0.1
    remove_music: bool = True
    # Couper les passages non-parlés annotés dans le transcript (applaudissements,
    # musique seule, rires...) et les retirer du transcript.
    remove_nonspeech: bool = True
    # Mots-clés des cues non-parlés ([...]/(...)). None = liste par défaut du module.
    nonspeech_keywords: tuple[str, ...] | None = None
    demucs_model: str = "htdemucs"
    ffmpeg_dir: Path | None = None
    # --- Moteur de nettoyage ---
    # "vad"    : pipeline audio_cleaning (Silero VAD + classification d'évènements).
    # "ffmpeg" : moteur historique (demucs + silencedetect). Repli auto si "vad"
    #            est demandé mais ses dépendances manquent.
    engine: str = "ffmpeg"
    vad_backend: str = "silero"
    classifier_backend: str = "yamnet"
    vad_threshold: float = 0.5
    event_threshold: float = 0.7


@dataclass
class SilverSummary:
    """Bilan d'un passage silver (pour les logs et l'affichage API)."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "details": self.details,
        }


class SilverPipeline:
    def __init__(self, store: SilverMinioStore, params: SilverParams) -> None:
        self._store = store
        self._params = params
        # Recalculé au début de run() selon la dispo de demucs ; valeur de repli ici.
        self._music_removal = params.remove_music
        # Moteur effectif ("vad" ou "ffmpeg"), résolu au 1er passage. Le pipeline VAD
        # (modèles chargés une fois) est mis en cache ici pour tout le lot.
        self._engine = "ffmpeg"
        self._cleaning_pipeline: object | None = None

    def _resolve_engine(self, *, warn: bool) -> str:
        """Moteur de nettoyage effectif : "vad" si demandé ET disponible, sinon "ffmpeg".

        Construit (une fois) le pipeline audio_cleaning ; s'il échoue à s'importer/charger
        (worker léger sans les dépendances, pas de TensorFlow...), on retombe proprement
        sur le moteur ffmpeg au lieu de faire échouer le lot.
        """
        if self._params.engine != "vad":
            return "ffmpeg"
        if self._cleaning_pipeline is None:
            try:
                from .vad_cleaning import build_cleaning_pipeline

                self._cleaning_pipeline = build_cleaning_pipeline(self._params)
            except Exception as exc:  # noqa: BLE001 - repli ffmpeg annoncé
                if warn:
                    log.warning(
                        "silver.vad_engine_unavailable",
                        reason=str(exc),
                        hint="pip install -e .[cleaning]  (moteur ffmpeg utilisé en attendant)",
                    )
                return "ffmpeg"
        return "vad"

    def _resolve_music_removal(self, *, warn: bool) -> bool:
        """Suppression de musique effective : demandée ET demucs disponible.

        Demandée mais demucs absent -> on retombe sur ffmpeg seul (découpe des
        silences) au lieu d'échouer. ``warn`` journalise l'info (une fois par lot).
        """
        if self._params.remove_music and not audio_ops.demucs_available():
            if warn:
                log.warning(
                    "silver.music_removal_disabled",
                    reason="module demucs non installé",
                    hint="pip install demucs  (ou  pip install -e .[silver])",
                )
            return False
        return self._params.remove_music

    def process_video(self, language: str, video_id: str, *, force: bool = False) -> bool:
        """Nettoie une seule entrée bronze (pour l'enchaînement auto après un job).

        Retourne True si elle a été traitée, False si déjà en silver (et non forcée).
        Lève en cas d'échec réel — l'appelant décide quoi en faire (best-effort côté
        worker). Idempotent comme le passage complet.
        """
        entry = BronzeEntry(language=language, video_id=video_id)
        if not force and self._store.silver_exists(entry):
            log.info("silver.skip", video_id=video_id, language=language)
            return False
        self._engine = self._resolve_engine(warn=False)
        self._music_removal = self._resolve_music_removal(warn=False)
        uri = self._process_entry(entry)
        log.info("silver.ok", video_id=video_id, uri=uri)
        return True

    def run(self, *, force: bool = False) -> SilverSummary:
        """Traite toutes les entrées bronze. ``force`` re-traite même si déjà en silver."""
        summary = SilverSummary()
        entries = self._store.iter_bronze_entries()

        # Suppression de la musique demandée mais demucs absent : on ne fait pas
        # échouer le lot, on retombe sur ffmpeg seul (averti une fois par passage).
        self._engine = self._resolve_engine(warn=True)
        self._music_removal = self._resolve_music_removal(warn=True)

        log.info(
            "silver.run.start",
            entries=len(entries),
            force=force,
            engine=self._engine,
            music_removal=self._music_removal,
        )

        for entry in entries:
            if not force and self._store.silver_exists(entry):
                summary.skipped += 1
                log.info("silver.skip", video_id=entry.video_id, language=entry.language)
                continue
            try:
                uri = self._process_entry(entry)
                summary.processed += 1
                summary.details.append(
                    {"video_id": entry.video_id, "language": entry.language, "ok": True, "uri": uri}
                )
                log.info("silver.ok", video_id=entry.video_id, uri=uri)
            except Exception as exc:  # noqa: BLE001 - on continue sur les autres vidéos
                summary.failed += 1
                summary.details.append(
                    {
                        "video_id": entry.video_id,
                        "language": entry.language,
                        "ok": False,
                        "error": str(exc),
                    }
                )
                log.error("silver.failed", video_id=entry.video_id, error=str(exc))

        log.info(
            "silver.run.done",
            processed=summary.processed,
            skipped=summary.skipped,
            failed=summary.failed,
        )
        return summary

    def _process_entry(self, entry: BronzeEntry) -> str:
        """Traite une entrée bronze de bout en bout. Retourne l'URI silver écrite."""
        if self._engine == "vad":
            return self._process_entry_vad(entry)
        return self._process_entry_ffmpeg(entry)

    def _process_entry_vad(self, entry: BronzeEntry) -> str:
        """Nettoyage via le moteur VAD (audio_cleaning). Retourne l'URI silver écrite."""
        from .vad_cleaning import clean as vad_clean

        params = self._params
        work_dir = Path(tempfile.mkdtemp(prefix=f"silver_vad_{entry.video_id}_"))
        try:
            audio_path, transcript, metadata = self._store.download_bronze(entry, work_dir)
            if audio_path is None:
                raise RuntimeError("aucun audio bronze trouvé")

            src_segments = (transcript or {}).get("segments") or []
            result = vad_clean(
                self._cleaning_pipeline,
                audio_path,
                src_segments,
                work_dir,
                video_id=entry.video_id,
            )

            # Transcript resynchronisé (les cues supprimés ont déjà été retirés par
            # le remap ; on reconstruit le champ `text` concaténé comme le moteur ffmpeg).
            new_transcript = transcript
            if transcript is not None:
                text = " ".join(
                    (s.get("text") or "").strip()
                    for s in result.cleaned_segments
                    if (s.get("text") or "").strip()
                )
                new_transcript = {
                    **transcript,
                    "segments": result.cleaned_segments,
                    "text": text,
                }

            seg_in = len(src_segments)
            seg_out = len(result.cleaned_segments)
            log.info(
                "silver.diagnostic",
                video_id=entry.video_id,
                engine="vad",
                vad_backend=params.vad_backend,
                classifier_backend=params.classifier_backend,
                duration_before_s=round(result.original_duration_s, 1),
                duration_after_s=round(result.cleaned_duration_s, 1),
                removed_s=round(result.original_duration_s - result.cleaned_duration_s, 1),
                music_removed_s=round(result.removed_by_label.get("music", 0.0), 1),
                applause_removed_s=round(result.removed_by_label.get("applause", 0.0), 1),
                noise_removed_s=round(result.removed_by_label.get("noise", 0.0), 1),
                silence_removed_s=round(result.removed_by_label.get("silence", 0.0), 1),
                transcript_segments_before=seg_in,
                transcript_segments_after=seg_out,
                transcript_dropped=seg_in - seg_out,
            )

            new_metadata = self._refine_metadata_vad(metadata, result)
            return self._store.upload_silver(
                entry,
                audio_path=result.cleaned_audio,
                transcript=new_transcript,
                metadata=new_metadata,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _process_entry_ffmpeg(self, entry: BronzeEntry) -> str:
        """Traite une entrée bronze de bout en bout. Retourne l'URI silver écrite."""
        params = self._params
        work_dir = Path(tempfile.mkdtemp(prefix=f"silver_{entry.video_id}_"))
        try:
            audio_path, transcript, metadata = self._store.download_bronze(entry, work_dir)
            if audio_path is None:
                raise RuntimeError("aucun audio bronze trouvé")

            # 1. Suppression de la musique (option) : on travaille ensuite sur la voix.
            # Si demucs échoue sur cette vidéo précise, on ne la perd pas : on retombe
            # sur l'audio d'origine (ffmpeg seul) au lieu de faire échouer l'entrée.
            source_audio = audio_path
            music_removed = False
            if self._music_removal:
                try:
                    source_audio = audio_ops.isolate_vocals(
                        audio_path,
                        work_dir / "demucs",
                        model=params.demucs_model,
                        ffmpeg_dir=params.ffmpeg_dir,
                    )
                    music_removed = True
                except Exception as exc:  # noqa: BLE001 - fallback ffmpeg seul
                    log.warning(
                        "silver.demucs_fallback",
                        video_id=entry.video_id,
                        error=str(exc),
                    )

            # 2. Détection des silences + passages non-parlés (applaudissements /
            # musique seule) repérés dans le transcript -> segments à garder.
            # Avec demucs, la musique de fond disparaît du stem voix : les passages
            # purement musicaux/applaudissements y deviennent silencieux et sont donc
            # aussi coupés par silencedetect. Les marqueurs du transcript ([Applause],
            # [Musique], ♪) assurent la coupe même sans demucs (repli ffmpeg).
            src_segments = (transcript or {}).get("segments") or []
            kw = params.nonspeech_keywords
            nonspeech_cuts = non_speech_ranges(src_segments, kw) if params.remove_nonspeech else []
            duration = audio_ops.probe_duration(source_audio, ffmpeg_dir=params.ffmpeg_dir)
            silences = audio_ops.detect_silences(
                source_audio,
                threshold_db=params.silence_threshold_db,
                min_silence_s=params.min_silence_s,
                ffmpeg_dir=params.ffmpeg_dir,
            )
            kept = compute_kept_ranges(
                duration,
                silences,
                min_silence_s=params.min_silence_s,
                padding_s=params.padding_s,
                extra_cuts=nonspeech_cuts,
            )
            if not kept:
                raise RuntimeError("aucun segment non-silencieux à conserver")

            # 3. Recollage des segments gardés -> audio silver (.wav).
            # Sous-dossier dédié : en repli ffmpeg seul, source_audio EST l'audio
            # bronze téléchargé dans work_dir ; écrire à côté avec le même nom ferait
            # une écriture "in-place" que ffmpeg refuse. On garde le nom d'origine.
            out_audio = work_dir / "silver" / f"{audio_path.stem}.wav"
            audio_ops.cut_and_concat(source_audio, kept, out_audio, ffmpeg_dir=params.ffmpeg_dir)

            # 4. Resynchronisation du transcript sur la nouvelle timeline.
            # On retire d'abord explicitement les sous-titres non-parlés (le padding
            # autour des coupes pourrait sinon en laisser un fragment), puis on
            # remappe les timestamps des sous-titres de parole restants.
            new_transcript = transcript
            if transcript is not None and src_segments:
                speech_segments = (
                    [s for s in src_segments if not is_non_speech(s.get("text"), kw)]
                    if params.remove_nonspeech
                    else src_segments
                )
                new_segments = remap_transcript(speech_segments, kept)
                text = " ".join(
                    (s.get("text") or "").strip()
                    for s in new_segments
                    if (s.get("text") or "").strip()
                )
                new_transcript = {**transcript, "segments": new_segments, "text": text}

            # 5. Métadonnées enrichies (traçabilité du nettoyage).
            new_duration = audio_ops.probe_duration(out_audio, ffmpeg_dir=params.ffmpeg_dir)

            # Diagnostic : ce qui a été détecté/coupé, pour vérifier le nettoyage.
            # `music_removed` True => les silences ont été détectés sur le stem VOIX
            # (demucs), donc musique seule + applaudissements + silences y sont coupés.
            seg_in = len(src_segments)
            seg_out = len((new_transcript or {}).get("segments") or [])
            log.info(
                "silver.diagnostic",
                video_id=entry.video_id,
                music_removed=music_removed,
                silences=len(silences),
                nonspeech_cuts=len(nonspeech_cuts),
                kept_segments=len(kept),
                duration_before_s=round(duration, 1),
                duration_after_s=round(new_duration, 1),
                removed_s=round(duration - new_duration, 1),
                transcript_segments_before=seg_in,
                transcript_segments_after=seg_out,
                transcript_dropped=seg_in - seg_out,
            )
            new_metadata = self._refine_metadata(
                metadata,
                duration,
                new_duration,
                kept,
                music_removed=music_removed,
                nonspeech_cuts=len(nonspeech_cuts),
            )

            return self._store.upload_silver(
                entry,
                audio_path=out_audio,
                transcript=new_transcript,
                metadata=new_metadata,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _refine_metadata_vad(self, metadata: dict | None, result) -> dict:
        """Complète metadata.json pour le moteur VAD (traçabilité + métriques).

        On embarque le bloc ``metrics`` complet du pipeline audio_cleaning (VAD,
        nettoyage, performance) : utile pour auditer/benchmarker un grand volume.
        """
        params = self._params
        base = dict(metadata or {})
        base.update(
            {
                "layer": "silver",
                "duration_s": round(result.cleaned_duration_s, 3),
                "original_duration_s": round(result.original_duration_s, 3),
                "silver_refined_at": datetime.now(timezone.utc).isoformat(),
                "silver_engine": "vad",
                "silver_vad_backend": params.vad_backend,
                "silver_classifier_backend": params.classifier_backend,
                "silver_music_removed_s": round(result.removed_by_label.get("music", 0.0), 3),
                "silver_applause_removed_s": round(result.removed_by_label.get("applause", 0.0), 3),
                "silver_noise_removed_s": round(result.removed_by_label.get("noise", 0.0), 3),
                "silver_silence_removed_s": round(result.removed_by_label.get("silence", 0.0), 3),
                "silver_metrics": result.metrics,
            }
        )
        return base

    def _refine_metadata(
        self,
        metadata: dict | None,
        original_duration: float,
        new_duration: float,
        kept: list,
        *,
        music_removed: bool,
        nonspeech_cuts: int,
    ) -> dict:
        """Complète metadata.json avec la traçabilité du passage silver."""
        params = self._params
        base = dict(metadata or {})
        base.update(
            {
                "layer": "silver",
                "duration_s": round(new_duration, 3),
                "original_duration_s": round(original_duration, 3),
                "silver_refined_at": datetime.now(timezone.utc).isoformat(),
                "silver_music_removed": music_removed,
                "silver_nonspeech_removed": params.remove_nonspeech,
                "silver_nonspeech_cuts": nonspeech_cuts,
                "silver_silence_threshold_db": params.silence_threshold_db,
                "silver_min_silence_s": params.min_silence_s,
                "silver_padding_s": params.padding_s,
                "silver_kept_segments": len(kept),
            }
        )
        return base
