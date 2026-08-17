"""Entrées/sorties audio : normalisation ffmpeg, lecture par blocs, découpe.

Deux principes :

- **RAM bornée** : on ne charge jamais des heures d'audio d'un coup. ``iter_chunks``
  lit le WAV normalisé par blocs via soundfile (seek + read), et ``cut_and_write``
  ne relit que les tranches gardées.
- **ffmpeg pour la normalisation seulement** : conversion mono + resample (+ loudnorm
  optionnel) une fois en tête de pipeline ; ensuite tout le monde travaille sur un
  WAV PCM 16 kHz mono, format attendu par Silero et YAMNet.

ffmpeg/ffprobe doivent être dans le PATH (ou pointés par ``ffmpeg_dir``).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import soundfile as sf


def _bin(name: str, ffmpeg_dir: str | Path | None) -> str:
    return str(Path(ffmpeg_dir) / name) if ffmpeg_dir else name


def probe_duration(path: str | Path, *, ffmpeg_dir: str | Path | None = None) -> float:
    """Durée (s) d'un fichier audio via ffprobe. 0.0 si indéterminée."""
    proc = subprocess.run(
        [
            _bin("ffprobe", ffmpeg_dir),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def to_normalized_wav(
    src: str | Path,
    dst: str | Path,
    *,
    sample_rate: int,
    loudnorm: bool = True,
    ffmpeg_dir: str | Path | None = None,
) -> Path:
    """Convertit ``src`` en WAV PCM 16 bits, mono, ``sample_rate`` Hz.

    ``loudnorm`` applique une normalisation de loudness EBU R128 (homogénéise un
    grand volume de vidéos aux niveaux disparates). Lève ``RuntimeError`` si ffmpeg
    échoue.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_bin("ffmpeg", ffmpeg_dir), "-y", "-i", str(src), "-ac", "1", "-ar", str(sample_rate)]
    if loudnorm:
        cmd += ["-af", "loudnorm=I=-23:LRA=7:TP=-2"]
    cmd += ["-c:a", "pcm_s16le", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(
            f"ffmpeg normalisation a échoué ({proc.returncode}): {proc.stderr[-500:]}"
        )
    return dst


def iter_chunks(
    path: str | Path,
    *,
    sample_rate: int,
    chunk_duration_s: float,
    overlap_s: float = 0.0,
) -> Iterator[tuple[float, np.ndarray]]:
    """Itère l'audio par blocs ``(offset_s, samples_float32)`` sans tout charger.

    Chaque bloc dure ``chunk_duration_s`` et recouvre le précédent de ``overlap_s``
    (évite de couper un mot pile à la frontière d'un bloc). ``offset_s`` est la
    position absolue du premier échantillon du bloc dans le fichier.
    """
    info = sf.info(str(path))
    file_sr = info.samplerate
    chunk_frames = int(chunk_duration_s * file_sr)
    overlap_frames = int(overlap_s * file_sr)
    step = max(1, chunk_frames - overlap_frames)

    start = 0
    while start < info.frames:
        stop = min(start + chunk_frames, info.frames)
        block, _ = sf.read(str(path), start=start, stop=stop, dtype="float32", always_2d=False)
        if block.ndim > 1:
            block = block.mean(axis=1)
        offset_s = start / file_sr
        if file_sr != sample_rate:
            block = _resample(block, file_sr, sample_rate)
        yield offset_s, block.astype(np.float32, copy=False)
        if stop >= info.frames:
            break
        start += step


def cut_and_write(
    src_wav: str | Path,
    kept: list[tuple[float, float]],
    dst: str | Path,
    *,
    sample_rate: int,
) -> float:
    """Recolle les tranches ``kept`` (timeline originale) en un seul WAV.

    Ne relit que les échantillons gardés (seek/read par intervalle) : mémoire
    proportionnelle à la plus longue tranche, pas au fichier entier. Retourne la
    durée écrite (s).
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = sf.info(str(src_wav))
    file_sr = info.samplerate

    written = 0
    with sf.SoundFile(
        str(dst), mode="w", samplerate=sample_rate, channels=1, subtype="PCM_16"
    ) as out:
        for start_s, end_s in kept:
            a = max(0, int(start_s * file_sr))
            b = min(info.frames, int(end_s * file_sr))
            if b <= a:
                continue
            block, _ = sf.read(str(src_wav), start=a, stop=b, dtype="float32", always_2d=False)
            if block.ndim > 1:
                block = block.mean(axis=1)
            if file_sr != sample_rate:
                block = _resample(block, file_sr, sample_rate)
            out.write(block.astype(np.float32, copy=False))
            written += len(block)
    return written / sample_rate


def _resample(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Rééchantillonne (librosa si dispo, sinon interpolation linéaire de repli)."""
    if src_sr == dst_sr:
        return data
    try:
        import librosa  # import paresseux : lourd

        return librosa.resample(data, orig_sr=src_sr, target_sr=dst_sr)
    except Exception:  # noqa: BLE001 - repli sans dépendance
        duration = len(data) / src_sr
        n_out = round(duration * dst_sr)
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        x_old = np.linspace(0.0, duration, num=len(data), endpoint=False)
        x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, data).astype(np.float32)
