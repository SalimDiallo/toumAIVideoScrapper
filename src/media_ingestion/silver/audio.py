"""Traitement du signal audio pour la couche silver (ffmpeg + demucs).

Wrappers minces autour de sous-processus système :

- ``isolate_vocals``   : retire la musique de fond via demucs (--two-stems vocals).
- ``detect_silences``  : repère les plages de silence via ffmpeg silencedetect.
- ``probe_duration``   : durée d'un fichier audio via ffprobe.
- ``cut_and_concat``   : recolle les segments gardés en un seul .wav (atrim+concat).

ffmpeg / ffprobe doivent être dans le PATH (ou pointés par ``ffmpeg_dir``). demucs
est une dépendance Python optionnelle (extra ``silver``), appelée en module.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import structlog

from .remap import Range

log = structlog.get_logger(__name__)


def demucs_available() -> bool:
    """True si demucs est réellement utilisable (sinon on retombe sur ffmpeg seul).

    On vérifie demucs ET ses dépendances lourdes (torch, numpy) : demucs peut être
    "installé" mais incapable de tourner si numpy/torch manquent (cas vu sous Python
    3.14). Sans ce contrôle, on tenterait demucs à chaque vidéo pour échouer à chaque
    fois, au lieu d'un repli propre annoncé une seule fois.
    """
    return all(importlib.util.find_spec(m) is not None for m in ("demucs", "torch", "numpy"))


# ffmpeg logue les évènements de silencedetect sur stderr, p.ex. :
#   [silencedetect @ 0x..] silence_start: 12.345
#   [silencedetect @ 0x..] silence_end: 13.201 | silence_duration: 0.856
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def _bin(name: str, ffmpeg_dir: Path | None) -> str:
    """Chemin d'un binaire ffmpeg/ffprobe : dossier explicite sinon PATH."""
    return str(ffmpeg_dir / name) if ffmpeg_dir else name


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Exécute un sous-processus en capturant stdout/stderr (texte)."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def probe_duration(audio: Path, *, ffmpeg_dir: Path | None = None) -> float:
    """Durée (secondes) d'un fichier audio via ffprobe. 0.0 si indéterminée."""
    proc = _run(
        [
            _bin("ffprobe", ffmpeg_dir),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio),
        ]
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def detect_silences(
    audio: Path,
    *,
    threshold_db: float,
    min_silence_s: float,
    ffmpeg_dir: Path | None = None,
) -> list[Range]:
    """Détecte les plages de silence ``(start, end)`` avec ffmpeg silencedetect.

    ``threshold_db`` : niveau (dB) sous lequel l'audio est jugé silencieux
    (p.ex. -35). ``min_silence_s`` : durée minimale d'un silence pour être
    signalé. Retourne des intervalles triés ; un ``silence_start`` final sans
    ``silence_end`` est refermé sur la fin du fichier.
    """
    proc = _run(
        [
            _bin("ffmpeg", ffmpeg_dir),
            "-i",
            str(audio),
            "-af",
            f"silencedetect=noise={threshold_db}dB:d={min_silence_s}",
            "-f",
            "null",
            "-",
        ]
    )
    # silencedetect écrit sur stderr, même quand tout se passe bien.
    starts = [float(m) for m in _SILENCE_START_RE.findall(proc.stderr)]
    ends = [float(m) for m in _SILENCE_END_RE.findall(proc.stderr)]

    silences: list[Range] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else probe_duration(audio, ffmpeg_dir=ffmpeg_dir)
        if end > start:
            silences.append((start, end))
    silences.sort()
    return silences


def isolate_vocals(
    audio: Path,
    out_dir: Path,
    *,
    model: str = "htdemucs",
    ffmpeg_dir: Path | None = None,
) -> Path:
    """Isole la voix (retire la musique) avec demucs ``--two-stems vocals``.

    demucs écrit ``<out_dir>/<model>/<nom_sans_ext>/vocals.wav``. On renvoie ce
    chemin. Lève ``RuntimeError`` si demucs échoue (absent, ou traitement KO).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            sys.executable,
            "-m",
            "demucs",
            "--two-stems",
            "vocals",
            "-n",
            model,
            "-o",
            str(out_dir),
            str(audio),
        ]
    )
    vocals = out_dir / model / audio.stem / "vocals.wav"
    if proc.returncode != 0 or not vocals.exists():
        raise RuntimeError(
            f"demucs a échoué (code {proc.returncode}) : {proc.stderr.strip()[-500:]}"
        )
    return vocals


def cut_and_concat(
    audio: Path,
    kept: list[Range],
    out_path: Path,
    *,
    ffmpeg_dir: Path | None = None,
) -> None:
    """Recolle les segments gardés en un seul .wav, en une passe ffmpeg.

    On construit un ``filter_complex`` : un ``atrim`` par segment gardé (avec
    ``asetpts`` pour repartir de zéro), puis un ``concat`` audio de tous les
    morceaux. Pas de fichier intermédiaire ni de ré-encodage entre segments :
    ffmpeg décode une fois et écrit directement le résultat.
    """
    if not kept:
        raise ValueError("aucun segment à garder : audio entièrement silencieux ?")

    parts = []
    labels = []
    for i, (start, end) in enumerate(kept):
        parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]")
        labels.append(f"[a{i}]")
    filter_complex = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(kept)}:v=0:a=1[out]"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            _bin("ffmpeg", ffmpeg_dir),
            "-y",
            "-i",
            str(audio),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            str(out_path),
        ]
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"ffmpeg concat a échoué (code {proc.returncode}) : {proc.stderr.strip()[-500:]}"
        )
