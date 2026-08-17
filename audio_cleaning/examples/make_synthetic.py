"""Génère un audio synthétique + transcript pour démontrer le pipeline sans modèle.

Construit ~30 s d'audio enchaînant des zones typées :

    [0-8]   parole (bruit modulé façon voix)   -> à CONSERVER
    [8-13]  musique (accords tenus)            -> à SUPPRIMER
    [13-21] parole                             -> à CONSERVER
    [21-25] applaudissements (bruit transitoire)-> à SUPPRIMER
    [25-30] parole                             -> à CONSERVER

Écrit ``sample.wav`` et ``transcript.json`` (mêmes clés que le workflow réel :
start_s / duration_s / text). Suffisant pour vérifier keep/remove et le remap.

Usage :
    python -m audio_cleaning.examples.make_synthetic  [dossier_sortie]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16000


def _speech_like(duration: float) -> np.ndarray:
    """Bruit passe-bande modulé en amplitude : imite l'enveloppe de la parole."""
    n = int(duration * SR)
    rng = np.random.default_rng(0)
    noise = rng.standard_normal(n)
    # Modulation d'amplitude ~4 Hz (rythme syllabique).
    t = np.arange(n) / SR
    env = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 4 * t))
    return (noise * env * 0.3).astype(np.float32)


def _music(duration: float) -> np.ndarray:
    """Accord tenu (contenu tonal, faible flatness) -> classé music.

    Volontairement plus faible que la parole : sans modèle, le VAD de repli
    (énergie) sépare la parole du reste par le niveau. Le vrai Silero, lui,
    distingue parole/musique quel que soit le volume.
    """
    n = int(duration * SR)
    t = np.arange(n) / SR
    chord = sum(np.sin(2 * np.pi * f * t) for f in (261.63, 329.63, 392.0))
    return (chord / 3 * 0.06).astype(np.float32)


def _applause(duration: float) -> np.ndarray:
    """Bruit blanc haché (transitoires denses) -> classé applause."""
    n = int(duration * SR)
    rng = np.random.default_rng(1)
    noise = rng.standard_normal(n)
    claps = (rng.random(n) > 0.6).astype(np.float32)
    return (noise * claps * 0.08).astype(np.float32)


def _silence(duration: float) -> np.ndarray:
    """Silence quasi-parfait (léger bruit de fond) -> classé silence."""
    n = int(duration * SR)
    rng = np.random.default_rng(2)
    return (rng.standard_normal(n) * 1e-3).astype(np.float32)


def main(out_dir: str | Path = ".") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plan = [
        ("speech", 8.0, _speech_like),
        ("silence", 1.5, _silence),
        ("music", 5.0, _music),
        ("speech", 8.0, _speech_like),
        ("applause", 4.0, _applause),
        ("silence", 1.5, _silence),
        ("speech", 5.0, _speech_like),
    ]
    audio = np.concatenate([fn(d) for _, d, fn in plan])
    sf.write(str(out_dir / "sample.wav"), audio, SR, subtype="PCM_16")

    # Transcript : une phrase par zone de parole (positions absolues).
    segments = []
    cursor = 0.0
    phrases = iter(
        [
            "Bonjour et bienvenue dans cette conférence.",
            "Nous allons parler du nettoyage audio automatique.",
            "Merci de votre attention et à bientôt.",
        ]
    )
    for label, dur, _ in plan:
        if label == "speech":
            segments.append(
                {"start_s": round(cursor, 3), "duration_s": round(dur, 3), "text": next(phrases)}
            )
        cursor += dur
    (out_dir / "transcript.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"écrit : {out_dir / 'sample.wav'} et {out_dir / 'transcript.json'}")
    return out_dir / "sample.wav"


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
