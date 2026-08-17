"""Intégration bout en bout : bronze -> silver réaligne bien le transcript.

Requiert ffmpeg/ffprobe (sinon le test est ignoré). demucs n'est PAS requis :
on teste le repli ffmpeg seul (remove_music=False), le chemin le plus courant.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from media_ingestion.silver.pipeline import SilverParams, SilverPipeline
from media_ingestion.silver.storage import BronzeEntry

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe requis pour le test d'intégration silver",
)


def _make_bronze_audio(path: Path) -> None:
    """2 s de son / 1 s de silence / 2 s de son -> silence détectable en (2, 3)."""
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=2",
            "-filter_complex",
            "[0]aformat=channel_layouts=mono[a0];[1]aformat=channel_layouts=mono[a1];"
            "[2]aformat=channel_layouts=mono[a2];[a0][a1][a2]concat=n=3:v=0:a=1[a]",
            "-map",
            "[a]",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class _FakeStore:
    """Store minimal : sert un audio bronze + un transcript, capture le silver écrit."""

    def __init__(self, bronze_audio: Path, transcript: dict) -> None:
        self._bronze_audio = bronze_audio
        self._transcript = transcript
        self.uploaded: dict = {}

    def download_bronze(self, entry: BronzeEntry, dest_dir: Path):
        dest = dest_dir / f"{entry.video_id}.wav"
        shutil.copy(self._bronze_audio, dest)
        return dest, self._transcript, {"video_id": entry.video_id}

    def silver_uri(self, entry: BronzeEntry) -> str:
        return f"s3://bucket/{entry.silver_prefix}"

    def upload_silver(self, entry, *, audio_path, transcript, metadata):
        # On sonde la durée pendant que le fichier existe encore (le pipeline
        # nettoie son dossier temporaire juste après l'upload).
        from media_ingestion.silver import audio as audio_ops

        self.uploaded = {
            "audio_exists": audio_path.exists(),
            "audio_duration": audio_ops.probe_duration(audio_path),
            "transcript": transcript,
            "metadata": metadata,
        }
        return self.silver_uri(entry)


def test_pipeline_realigns_transcript_and_writes_silver(tmp_path: Path) -> None:
    bronze = tmp_path / "bronze.wav"
    _make_bronze_audio(bronze)

    # Transcript bronze (timeline d'origine) :
    #  A [0.5, 1.5]  -> gardé, avant le silence
    #  B [2.2, 2.7]  -> ENTIÈREMENT dans le silence (2,3) -> doit disparaître
    #  [Applause] [3.2, 3.4] -> passage non-parlé -> coupé + retiré du transcript
    #  C [3.5, 4.5]  -> gardé, après le silence -> doit être décalé
    transcript = {
        "language": "fr",
        "source": "youtube_manual",
        "text": "A B C",
        "segments": [
            {"start_s": 0.5, "duration_s": 1.0, "text": "A"},
            {"start_s": 2.2, "duration_s": 0.5, "text": "B"},
            {"start_s": 3.2, "duration_s": 0.2, "text": "[Applause]"},
            {"start_s": 3.5, "duration_s": 1.0, "text": "C"},
        ],
    }

    store = _FakeStore(bronze, transcript)
    pipeline = SilverPipeline(
        store,  # type: ignore[arg-type]
        SilverParams(
            silence_threshold_db=-35.0, min_silence_s=0.5, padding_s=0.1, remove_music=False
        ),
    )
    pipeline._process_entry(BronzeEntry(language="fr", video_id="VID"))

    out = store.uploaded
    segs = out["transcript"]["segments"]
    texts = [s["text"] for s in segs]

    # B (silence) et [Applause] (non-parlé) exclus ; A et C conservés, dans l'ordre.
    assert texts == ["A", "C"]
    # A inchangé (avant toute coupe).
    assert segs[0]["start_s"] == pytest.approx(0.5, abs=0.15)
    # C décalé : silence (~1 s) retiré + padding -> ~2.7 s (au lieu de 3.5).
    assert segs[1]["start_s"] == pytest.approx(2.7, abs=0.2)
    # Le texte global est régénéré à partir des segments réalignés.
    assert out["transcript"]["text"] == "A C"
    # Métadonnées silver.
    assert out["metadata"]["layer"] == "silver"
    # L'audio nettoyé a bien été produit : ~4.2 s (5 s - ~1 s de silence + padding).
    assert out["audio_exists"] is True
    assert out["audio_duration"] == pytest.approx(4.2, abs=0.3)
