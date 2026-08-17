"""Visualisation temporelle des décisions du pipeline (matplotlib, backend Agg).

Génère ``visualization.png`` : deux pistes alignées sur le temps.

- Piste « classes »  : chaque segment coloré par sa classe (speech, music, ...).
- Piste « action »   : vert = gardé, rouge hachuré = supprimé.

Objectif : repérer d'un coup d'œil une erreur (p.ex. une zone verte de parole
marquée « removed », ou de la musique laissée en « kept »).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # rendu fichier, sans display (serveurs / batch)
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

from .labels import KEEP, LABEL_COLORS, OTHER
from .segments import Segment


def render_timeline(
    segments: list[Segment],
    duration: float,
    out_path: str | Path,
    *,
    title: str = "",
) -> Path:
    """Écrit la visualisation temporelle et retourne le chemin du PNG."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax_cls, ax_act) = plt.subplots(
        2, 1, figsize=(max(10, duration / 12), 3.2), sharex=True, height_ratios=[2, 1]
    )

    seen_labels: set[str] = set()
    for seg in segments:
        color = LABEL_COLORS.get(seg.label, LABEL_COLORS[OTHER])
        ax_cls.barh(
            0,
            seg.duration,
            left=seg.start,
            height=0.8,
            color=color,
            edgecolor="white",
            linewidth=0.3,
        )
        seen_labels.add(seg.label)
        # Piste action.
        if seg.action == KEEP:
            ax_act.barh(0, seg.duration, left=seg.start, height=0.8, color="#2e7d32")
        else:
            ax_act.barh(
                0,
                seg.duration,
                left=seg.start,
                height=0.8,
                color="#c62828",
                hatch="//",
                edgecolor="white",
                linewidth=0.3,
            )

    for ax, label in ((ax_cls, "classes"), (ax_act, "action")):
        ax.set_xlim(0, max(duration, 1e-3))
        ax.set_yticks([])
        ax.set_ylabel(label, rotation=0, ha="right", va="center", fontsize=9)
    ax_act.set_xlabel("temps (s)")

    class_handles = [
        mpatches.Patch(color=LABEL_COLORS[label], label=label)
        for label in LABEL_COLORS
        if label in seen_labels
    ]
    action_handles = [
        mpatches.Patch(color="#2e7d32", label="kept"),
        mpatches.Patch(facecolor="#c62828", hatch="//", label="removed"),
    ]
    ax_cls.legend(
        handles=class_handles,
        ncol=len(class_handles) or 1,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.6),
        fontsize=8,
        frameon=False,
    )
    ax_act.legend(
        handles=action_handles,
        ncol=2,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.7),
        fontsize=8,
        frameon=False,
    )

    if title:
        fig.suptitle(title, fontsize=10, y=1.02)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out_path
