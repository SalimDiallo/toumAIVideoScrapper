"""Voice Activity Detection : où se trouve la parole ?

Deux backends, choisis par ``config.vad.backend`` :

- ``silero``  : Silero VAD (léger, CPU, ~1 Mo). Chargé via le paquet ``silero-vad``
  ou ``torch.hub``. Option ONNX Runtime (``vad.onnx: true``) encore plus légère.
- ``energy``  : repli sans dépendance ML — détection par énergie court-terme.
  Grossier mais suffisant pour faire tourner le pipeline (exemple synthétique,
  environnements sans torch). Volontairement *permissif* : dans le doute il classe
  en parole, conformément au principe « ne jamais supprimer de parole ».

Interface commune : ``VAD.speech_ranges(samples, sr) -> list[(start_s, end_s)]``,
timestamps en secondes relatifs au début de ``samples``.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from .config import VadConfig
from .segments import Range, filter_min_duration, merge_ranges


class VAD(Protocol):
    def speech_ranges(self, samples: np.ndarray, sr: int) -> list[Range]: ...


def build_vad(cfg: VadConfig) -> VAD:
    """Fabrique le VAD demandé, avec repli ``energy`` si Silero est indisponible."""
    if cfg.backend == "energy":
        return EnergyVAD(cfg)
    if cfg.backend == "silero":
        try:
            return SileroVAD(cfg)
        except Exception as exc:  # noqa: BLE001 - repli annoncé
            print(f"[audio_cleaning] Silero indisponible ({exc}); repli VAD énergie.")
            return EnergyVAD(cfg)
    raise ValueError(f"backend VAD inconnu : {cfg.backend!r}")


class SileroVAD:
    """Enveloppe de Silero VAD (torch ou ONNX) via les helpers officiels."""

    def __init__(self, cfg: VadConfig) -> None:
        self._cfg = cfg
        self._model, self._get_speech_timestamps = self._load(cfg)

    @staticmethod
    def _load(cfg: VadConfig):
        """Charge Silero via le paquet ``silero-vad`` sinon via ``torch.hub``.

        Le repli torch.hub évite d'imposer ``silero-vad``/``torchaudio`` (pas
        toujours disponible en wheel selon la version de Python) : torch suffit.
        """
        try:
            from silero_vad import get_speech_timestamps, load_silero_vad

            return load_silero_vad(onnx=cfg.onnx), get_speech_timestamps
        except Exception:  # noqa: BLE001 - repli torch.hub
            import torch

            model, utils = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                onnx=cfg.onnx,
                trust_repo=True,
            )
            get_speech_timestamps = utils[0]
            return model, get_speech_timestamps

    def speech_ranges(self, samples: np.ndarray, sr: int) -> list[Range]:
        import torch

        # Silero attend 16 kHz (ou 8 kHz). On suppose l'audio déjà rééchantillonné.
        audio = torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32))
        ts = self._get_speech_timestamps(
            audio,
            self._model,
            sampling_rate=sr,
            threshold=self._cfg.threshold,
            min_speech_duration_ms=int(self._cfg.min_speech_duration * 1000),
            min_silence_duration_ms=int(self._cfg.min_silence_duration * 1000),
            return_seconds=True,
        )
        ranges = [(float(t["start"]), float(t["end"])) for t in ts]
        return merge_ranges(ranges, max_gap=self._cfg.min_silence_duration)


class EnergyVAD:
    """VAD de repli par énergie RMS court-terme (sans dépendance ML).

    On calcule l'énergie par fenêtres de 30 ms ; une fenêtre est « parole » si son
    énergie dépasse un seuil adaptatif (bruit de fond estimé par un percentile bas).
    Approche prudente : le seuil est volontairement bas pour préférer un faux
    « speech » à une suppression de parole.
    """

    def __init__(self, cfg: VadConfig, frame_ms: float = 30.0) -> None:
        self._cfg = cfg
        self._frame_ms = frame_ms

    def speech_ranges(self, samples: np.ndarray, sr: int) -> list[Range]:
        if len(samples) == 0:
            return []
        frame = max(1, int(sr * self._frame_ms / 1000.0))
        n = len(samples) // frame
        if n == 0:
            return []
        trimmed = samples[: n * frame].reshape(n, frame)
        rms = np.sqrt(np.mean(trimmed.astype(np.float64) ** 2, axis=1) + 1e-12)

        # Seuil adaptatif placé ENTRE le plancher de bruit et le pic, plutôt qu'un
        # multiple du plancher : ce dernier s'effondre quand le signal est
        # uniformément fort (après loudnorm) et finit par dépasser le pic -> plus
        # aucune parole détectée. Ici on reste borné dans [floor, peak].
        noise_floor = float(np.percentile(rms, 15))
        peak = float(rms.max())
        threshold = max(noise_floor + 0.15 * (peak - noise_floor), 1e-4)

        speech_frames = rms > threshold
        ranges: list[Range] = []
        start_idx: int | None = None
        for i, is_speech in enumerate(speech_frames):
            if is_speech and start_idx is None:
                start_idx = i
            elif not is_speech and start_idx is not None:
                ranges.append((start_idx * frame / sr, i * frame / sr))
                start_idx = None
        if start_idx is not None:
            ranges.append((start_idx * frame / sr, n * frame / sr))

        merged = merge_ranges(ranges, max_gap=self._cfg.min_silence_duration)
        return filter_min_duration(merged, self._cfg.min_speech_duration)
