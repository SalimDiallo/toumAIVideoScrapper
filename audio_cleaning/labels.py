"""Vocabulaire des classes d'évènements audio, commun à tout le pipeline.

On travaille avec un jeu de classes *grossier* (6 étiquettes) et non les 521
classes d'AudioSet/YAMNet : c'est ce niveau qui pilote les décisions
keep/remove et les métriques. Le classifieur fin (YAMNet) est projeté sur ces
classes via ``YAMNET_TO_COARSE`` (voir classifier.py).
"""

from __future__ import annotations

# Classes grossières manipulées par la logique de décision et les métriques.
SPEECH = "speech"
MUSIC = "music"
APPLAUSE = "applause"
LAUGHTER = "laughter"
NOISE = "noise"
SILENCE = "silence"
OTHER = "other"

# Ordre stable (matrices de confusion, colonnes CSV, couleurs de visualisation).
COARSE_LABELS: tuple[str, ...] = (
    SPEECH,
    MUSIC,
    APPLAUSE,
    LAUGHTER,
    NOISE,
    SILENCE,
    OTHER,
)

# Couleurs de la visualisation temporelle (une par classe).
LABEL_COLORS: dict[str, str] = {
    SPEECH: "#2e7d32",  # vert : la parole qu'on protège
    MUSIC: "#1565c0",  # bleu
    APPLAUSE: "#ef6c00",  # orange
    LAUGHTER: "#f9a825",  # jaune
    NOISE: "#6d4c41",  # brun
    SILENCE: "#9e9e9e",  # gris
    OTHER: "#7b1fa2",  # violet
}

# Actions possibles sur un segment.
KEEP = "keep"
REMOVE = "remove"
