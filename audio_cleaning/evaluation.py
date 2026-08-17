"""Évaluation manuelle du pipeline : dataset annoté -> matrice de confusion + métriques.

Format d'échange (CSV ou JSON), une ligne par segment audio annoté :

    video_id, start, end, true_label, predicted_label, confidence

Classes autorisées : speech, music, applause, laughter, noise, silence, other.

Deux usages :

- ``segments_to_eval_rows`` : pré-remplit ``predicted_label`` à partir des décisions
  du pipeline (``segments.json``) pour n'avoir plus qu'à saisir ``true_label`` à la main.
- ``evaluate`` : lit un fichier annoté et produit matrice de confusion + métriques
  qualité (precision/recall/F1, speech retention, taux de suppression par classe).

Aucune dépendance obligatoire : la matrice de confusion est calculée à la main
(scikit-learn utilisé seulement s'il est présent, pour recouper).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .labels import COARSE_LABELS
from .metrics import quality_metrics
from .segments import Segment

EVAL_FIELDS = ("video_id", "start", "end", "true_label", "predicted_label", "confidence")


def segments_to_eval_rows(video_id: str, segments: list[Segment]) -> list[dict[str, Any]]:
    """Transforme les décisions du pipeline en lignes d'évaluation à annoter.

    ``predicted_label`` = classe prédite ; ``true_label`` est laissé vide pour la
    saisie manuelle.
    """
    return [
        {
            "video_id": video_id,
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "true_label": "",
            "predicted_label": s.label,
            "confidence": round(float(s.confidence), 4),
        }
        for s in segments
    ]


def write_eval_template(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    """Écrit un gabarit d'annotation (CSV ou JSON selon l'extension)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(EVAL_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
    return path


def load_eval_rows(path: str | Path) -> list[dict[str, Any]]:
    """Charge un fichier d'évaluation annoté (CSV ou JSON)."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("rows", [])
    else:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    # Normalise les types numériques.
    for r in rows:
        for k in ("start", "end", "confidence"):
            if r.get(k) not in (None, ""):
                r[k] = float(r[k])
    return [r for r in rows if r.get("true_label")]  # ignore les lignes non annotées


def confusion_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Matrice de confusion (true vs predicted) pondérée par la durée des segments.

    Retourne ``{"labels": [...], "matrix": [[...], ...]}`` où ``matrix[i][j]`` est la
    durée totale (s) des segments de vraie classe ``labels[i]`` prédits ``labels[j]``.
    """
    idx = {lab: i for i, lab in enumerate(COARSE_LABELS)}
    n = len(COARSE_LABELS)
    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    for r in rows:
        t, p = r.get("true_label"), r.get("predicted_label")
        if t not in idx or p not in idx:
            continue
        dur = max(0.0, float(r.get("end", 0.0)) - float(r.get("start", 0.0)))
        matrix[idx[t]][idx[p]] += dur
    return {
        "labels": list(COARSE_LABELS),
        "matrix": [[round(v, 3) for v in row] for row in matrix],
    }


def evaluate(path: str | Path) -> dict[str, Any]:
    """Évaluation complète d'un fichier annoté : matrice + métriques qualité."""
    rows = load_eval_rows(path)
    result = {
        "num_annotated_segments": len(rows),
        "confusion_matrix": confusion_matrix(rows),
        "quality": quality_metrics(rows),
    }
    # Recoupement optionnel avec scikit-learn (accuracy multi-classes segment-level).
    try:
        from sklearn.metrics import accuracy_score

        y_true = [r["true_label"] for r in rows]
        y_pred = [r["predicted_label"] for r in rows]
        result["segment_accuracy"] = round(float(accuracy_score(y_true, y_pred)), 4)
    except Exception:  # noqa: BLE001, S110 - sklearn optionnel : recoupement facultatif
        pass
    return result


def save_evaluation(path: str | Path, result: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
