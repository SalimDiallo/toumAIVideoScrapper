"""audio_cleaning — nettoyage audio/transcript piloté par VAD + classification d'évènements.

Pipeline (voir README) :

    audio brut -> normalisation -> VAD (Silero) -> classification (YAMNet)
    -> fusion des segments -> décision keep/remove -> découpe audio
    -> resynchro transcript -> métriques + visualisation.

Principe directeur : ne JAMAIS supprimer de parole. En cas de doute (faible
confiance, évènement inconnu), on conserve le segment.

Le package est modulaire : chaque étape est un module isolé et testable. Les
modèles lourds (torch/tensorflow) sont importés paresseusement ; des repères
légers (énergie / heuristique spectrale) permettent d'exécuter le pipeline de
bout en bout même sans eux, ce qui rend l'exemple synthétique reproductible.
"""

from __future__ import annotations

from .config import Config, load_config
from .segments import Segment

__all__ = ["Config", "Segment", "load_config"]
__version__ = "0.1.0"
