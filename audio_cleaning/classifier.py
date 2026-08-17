"""Classification d'évènements audio sur les zones NON-parole.

Rôle : sur une tranche que le VAD n'a pas jugée « parole », dire *quoi* c'est
(musique / applaudissements / rires / bruit / silence / autre) avec une confiance,
pour décider si on la supprime.

Backends (``config.classification.backend``) :

- ``yamnet``    : YAMNet (MobileNet sur AudioSet, 521 classes) via tensorflow-hub.
  Rapide sur CPU. Les 521 classes fines sont projetées sur nos 6 classes grossières
  par ``YAMNET_TO_COARSE`` (correspondance par sous-chaîne du nom de classe).
- ``panns``     : alternative PyTorch (CNN14, ``panns-inference``) — même interface,
  utile si TensorFlow n'est pas installable. Voir README.
- ``heuristic`` : repli sans ML, à base de descripteurs spectraux (centroïde, flatness,
  ZCR, énergie). Grossier mais rend le pipeline exécutable partout et sert de
  démonstration reproductible.

Interface commune :
    classify(samples, sr, offset_s) -> list[FrameScore]
où FrameScore = (start_s, end_s, coarse_label, confidence, raw_scores).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from .config import ClassificationConfig
from .labels import APPLAUSE, LAUGHTER, MUSIC, NOISE, OTHER, SILENCE, SPEECH


@dataclass
class FrameScore:
    start: float
    end: float
    label: str  # classe grossière
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)  # scores par classe grossière


class Classifier(Protocol):
    def classify(self, samples: np.ndarray, sr: int, offset_s: float) -> list[FrameScore]: ...


def build_classifier(cfg: ClassificationConfig) -> Classifier:
    """Fabrique le classifieur demandé, avec repli heuristique si indisponible."""
    if cfg.backend == "heuristic":
        return HeuristicClassifier(cfg)
    if cfg.backend == "yamnet":
        try:
            return YamnetClassifier(cfg)
        except Exception as exc:  # noqa: BLE001 - repli annoncé
            print(f"[audio_cleaning] YAMNet indisponible ({exc}); repli classifieur heuristique.")
            return HeuristicClassifier(cfg)
    if cfg.backend == "panns":
        return PannsClassifier(cfg)
    raise ValueError(f"backend de classification inconnu : {cfg.backend!r}")


# Projection AudioSet(YAMNet) -> classes grossières. On teste par sous-chaîne du nom
# de classe (display_name) en minuscules ; premier motif trouvé l'emporte. Ordre
# important : les motifs spécifiques (applause) avant les génériques (music).
YAMNET_TO_COARSE: tuple[tuple[str, str], ...] = (
    ("applause", APPLAUSE),
    ("clapping", APPLAUSE),
    ("cheering", APPLAUSE),
    ("crowd", APPLAUSE),
    ("laughter", LAUGHTER),
    ("laugh", LAUGHTER),
    ("giggle", LAUGHTER),
    ("chuckle", LAUGHTER),
    ("speech", SPEECH),
    ("conversation", SPEECH),
    ("narration", SPEECH),
    ("singing", MUSIC),
    ("music", MUSIC),
    ("musical", MUSIC),
    ("instrument", MUSIC),
    ("guitar", MUSIC),
    ("piano", MUSIC),
    ("drum", MUSIC),
    ("silence", SILENCE),
    ("noise", NOISE),
    ("static", NOISE),
    ("hum", NOISE),
    ("vehicle", NOISE),
    ("engine", NOISE),
    ("wind", NOISE),
)


def coarse_of_yamnet(display_name: str) -> str:
    """Projette un nom de classe YAMNet sur une classe grossière (défaut: other)."""
    low = display_name.lower()
    for needle, coarse in YAMNET_TO_COARSE:
        if needle in low:
            return coarse
    return OTHER


def _build_projection(coarse_by_index: list[str]) -> tuple[list[str], np.ndarray]:
    """Matrice one-hot ``(n_classes_yamnet, n_classes_grossières)`` d'agrégation.

    Permet de projeter les 521 scores YAMNet sur nos classes grossières en un seul
    produit matriciel ``scores @ projection`` (pour toutes les trames d'un coup), au
    lieu d'une boucle Python de 521 itérations par trame. Les colonnes suivent l'ordre
    de première apparition des classes dans ``coarse_by_index`` : même départage des
    égalités que l'ancienne agrégation par dictionnaire.
    """
    labels: list[str] = []
    col_of: dict[str, int] = {}
    for coarse in coarse_by_index:
        if coarse not in col_of:
            col_of[coarse] = len(labels)
            labels.append(coarse)
    proj = np.zeros((len(coarse_by_index), len(labels)), dtype=np.float32)
    for idx, coarse in enumerate(coarse_by_index):
        proj[idx, col_of[coarse]] = 1.0
    return labels, proj


class YamnetClassifier:
    """YAMNet via tensorflow-hub, scores agrégés sur nos classes grossières."""

    _HUB_URL = "https://tfhub.dev/google/yamnet/1"

    def __init__(self, cfg: ClassificationConfig) -> None:
        self._cfg = cfg
        import csv

        import tensorflow_hub as hub  # imports paresseux (lourds)

        self._model = hub.load(self._HUB_URL)
        # La liste des 521 classes est fournie par un asset CSV du modèle.
        class_map_path = self._model.class_map_path().numpy().decode("utf-8")
        with open(class_map_path, newline="") as f:
            coarse_by_index = [coarse_of_yamnet(row["display_name"]) for row in csv.DictReader(f)]
        # Précalcul de la matrice d'agrégation 521 -> classes grossières (une fois).
        self._coarse_labels, self._projection = _build_projection(coarse_by_index)

    def classify(self, samples: np.ndarray, sr: int, offset_s: float) -> list[FrameScore]:
        # YAMNet attend du 16 kHz mono float32. Il segmente en trames de 0.96 s
        # (hop 0.48 s) et renvoie un score par trame et par classe.
        scores, _embeddings, _spectro = self._model(samples.astype(np.float32))
        scores = np.asarray(scores, dtype=np.float32)  # (n_frames, 521)
        # Agrégation vectorisée : un seul produit matriciel pour toutes les trames,
        # puis normalisation par ligne (somme des scores grossiers = somme des 521).
        coarse = scores @ self._projection  # (n_frames, n_classes_grossières)
        totals = coarse.sum(axis=1, keepdims=True)
        coarse = coarse / np.where(totals == 0.0, 1.0, totals)

        labels = self._coarse_labels
        best = coarse.argmax(axis=1)
        hop = 0.48
        out: list[FrameScore] = []
        for i in range(coarse.shape[0]):
            row = coarse[i]
            start = offset_s + i * hop
            out.append(
                FrameScore(
                    start=start,
                    end=start + 0.96,
                    label=labels[best[i]],
                    confidence=float(row[best[i]]),
                    scores={lab: float(row[j]) for j, lab in enumerate(labels)},
                )
            )
        return out


class PannsClassifier:
    """Alternative PyTorch (CNN14, panns-inference). Interface identique à YAMNet."""

    def __init__(self, cfg: ClassificationConfig) -> None:
        self._cfg = cfg
        from panns_inference import AudioTagging  # import paresseux

        self._model = AudioTagging(checkpoint_path=None, device="cpu")

    def classify(self, samples: np.ndarray, sr: int, offset_s: float) -> list[FrameScore]:
        # CNN14 classe l'extrait entier ; on l'attribue à toute la tranche.
        clipwise, _ = self._model.inference(samples[None, :])
        labels = self._model.labels
        agg: dict[str, float] = {}
        for idx, score in enumerate(clipwise[0]):
            c = coarse_of_yamnet(labels[idx])
            agg[c] = agg.get(c, 0.0) + float(score)
        total = sum(agg.values()) or 1.0
        agg = {k: v / total for k, v in agg.items()}
        label = max(agg, key=agg.get)
        dur = len(samples) / sr
        return [FrameScore(offset_s, offset_s + dur, label, agg[label], agg)]


class HeuristicClassifier:
    """Repli sans ML : classes déduites de descripteurs spectraux simples.

    Règles (par trame de ``frame_duration_s``) :

    - énergie très faible                       -> silence
    - flatness élevée + énergie modérée         -> noise (spectre plat, façon bruit)
    - centroïde stable + harmonique + soutenu   -> music
    - transitoires denses (ZCR/energie hachée)  -> applause
    - sinon                                     -> other

    Volontairement conservateur : la parole étant déjà traitée par le VAD, ce
    classifieur n'agit que sur du non-parole. En cas d'ambiguïté il renvoie
    ``other`` (conservé par défaut), jamais une suppression forcée.
    """

    def __init__(self, cfg: ClassificationConfig) -> None:
        self._cfg = cfg

    def classify(self, samples: np.ndarray, sr: int, offset_s: float) -> list[FrameScore]:
        frame = max(1, int(self._cfg.frame_duration_s * sr))
        hop = max(1, int(self._cfg.frame_hop_s * sr))
        out: list[FrameScore] = []
        for start in range(0, max(1, len(samples) - frame + 1), hop):
            seg = samples[start : start + frame]
            if len(seg) < frame // 2:
                break
            label, conf, scores = self._label_frame(seg, sr)
            t0 = offset_s + start / sr
            out.append(FrameScore(t0, t0 + self._cfg.frame_duration_s, label, conf, scores))
        return out

    def _label_frame(self, seg: np.ndarray, sr: int) -> tuple[str, float, dict[str, float]]:
        energy = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2) + 1e-12))
        if energy < 5e-3:
            return SILENCE, 0.9, {SILENCE: 0.9}

        # Spectre.
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        spec += 1e-12
        freqs = np.fft.rfftfreq(len(seg), 1.0 / sr)
        centroid = float(np.sum(freqs * spec) / np.sum(spec))
        geo_mean = float(np.exp(np.mean(np.log(spec))))
        arith_mean = float(np.mean(spec))
        flatness = geo_mean / arith_mean  # ~1 = bruit blanc, ~0 = tonal
        zcr = float(np.mean(np.abs(np.diff(np.sign(seg)))) / 2.0)

        scores: dict[str, float] = {}
        if flatness > 0.5:
            scores[NOISE] = min(1.0, flatness + 0.2)
        # Applaudissements : transitoires denses -> ZCR élevé + flatness moyenne.
        if zcr > 0.15 and 0.2 < flatness < 0.6:
            scores[APPLAUSE] = min(1.0, zcr * 3.0)
        # Musique : contenu tonal (flatness basse), centroïde en bande médium.
        if flatness < 0.3 and 200 < centroid < 5000:
            scores[MUSIC] = min(1.0, (0.3 - flatness) / 0.3 * 0.9 + 0.1)
        if not scores:
            return OTHER, 0.4, {OTHER: 0.4}
        label = max(scores, key=scores.get)
        return label, scores[label], scores
