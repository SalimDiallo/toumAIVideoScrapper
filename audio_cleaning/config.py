"""Chargement de ``config.yaml`` vers des dataclasses typées.

Un seul point d'entrée : ``load_config(path)``. Toutes les valeurs ont un défaut
(le YAML n'a pas besoin d'être exhaustif) et peuvent être surchargées par clé
pointée (``vad.threshold=0.6``) depuis la CLI — voir ``apply_overrides``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AudioConfig:
    loudnorm: bool = True
    ffmpeg_dir: str | None = None


@dataclass
class ProcessingConfig:
    sample_rate: int = 16000
    chunk_duration_s: float = 300.0
    chunk_overlap_s: float = 1.0
    min_remove_duration: float = 0.5


@dataclass
class VadConfig:
    backend: str = "silero"  # silero | energy
    onnx: bool = True
    threshold: float = 0.5
    min_speech_duration: float = 0.3
    min_silence_duration: float = 0.3
    speech_padding_before: float = 0.2
    speech_padding_after: float = 0.3


@dataclass
class ClassificationConfig:
    backend: str = "yamnet"  # yamnet | heuristic | panns
    frame_duration_s: float = 0.96
    frame_hop_s: float = 0.48
    music_threshold: float = 0.7
    applause_threshold: float = 0.7
    laughter_threshold: float = 0.7
    noise_threshold: float = 0.7

    def threshold_for(self, coarse_label: str) -> float:
        """Seuil de confiance associé à une classe grossière (0.5 par défaut)."""
        return {
            "music": self.music_threshold,
            "applause": self.applause_threshold,
            "laughter": self.laughter_threshold,
            "noise": self.noise_threshold,
        }.get(coarse_label, 0.5)


@dataclass
class DecisionConfig:
    remove_music: bool = True
    remove_applause: bool = True
    remove_noise: bool = True
    remove_silence: bool = True
    remove_laughter: bool = False
    remove_other: bool = False
    keep_if_confidence_below: float = 0.5

    def removes(self, coarse_label: str) -> bool:
        """La classe est-elle candidate à la suppression (avant seuil de confiance) ?"""
        return {
            "music": self.remove_music,
            "applause": self.remove_applause,
            "noise": self.remove_noise,
            "silence": self.remove_silence,
            "laughter": self.remove_laughter,
            "other": self.remove_other,
        }.get(coarse_label, False)


@dataclass
class OutputConfig:
    root: str = "outputs"
    save_original_wav: bool = True
    save_visualization: bool = True
    audio_format: str = "wav"


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    classification: ClassificationConfig = field(default_factory=ClassificationConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_SECTION_TYPES = {f.name: f.type for f in fields(Config)}


def _build_section(section_cls: type, data: dict[str, Any]) -> Any:
    """Instancie une dataclass de section en ignorant les clés inconnues."""
    known = {f.name for f in fields(section_cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"clés inconnues dans {section_cls.__name__}: {sorted(unknown)}")
    return section_cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    """Charge la configuration. ``path=None`` -> défauts + ``config.yaml`` voisin s'il existe."""
    cfg = Config()
    if path is None:
        default = Path(__file__).with_name("config.yaml")
        path = default if default.exists() else None
    if path is None:
        return cfg

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    section_by_name = {f.name: f.default_factory() for f in fields(Config)}  # type: ignore[misc]
    for name, section_default in section_by_name.items():
        data = raw.get(name) or {}
        if not isinstance(data, dict):
            raise TypeError(f"section '{name}' doit être un mapping, pas {type(data).__name__}")
        section_by_name[name] = _build_section(type(section_default), data)
    return Config(**section_by_name)


def apply_overrides(cfg: Config, overrides: list[str]) -> Config:
    """Applique des surcharges ``section.clé=valeur`` (typées d'après le défaut)."""
    for item in overrides:
        if "=" not in item:
            raise ValueError(f"surcharge invalide (attendu section.clé=valeur) : {item!r}")
        dotted, raw_value = item.split("=", 1)
        parts = dotted.split(".")
        if len(parts) != 2:
            raise ValueError(f"clé de surcharge invalide : {dotted!r} (attendu section.clé)")
        section_name, key = parts
        section = getattr(cfg, section_name, None)
        if section is None or not is_dataclass(section):
            raise ValueError(f"section inconnue : {section_name!r}")
        if not hasattr(section, key):
            raise ValueError(f"clé inconnue : {section_name}.{key}")
        current = getattr(section, key)
        setattr(section, key, _coerce(raw_value, current))
    return cfg


def _coerce(raw: str, like: Any) -> Any:
    """Convertit une valeur texte vers le type de la valeur courante."""
    if isinstance(like, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(like, int) and not isinstance(like, bool):
        return int(raw)
    if isinstance(like, float):
        return float(raw)
    if like is None:
        return None if raw.strip().lower() in {"", "null", "none"} else raw
    return raw
