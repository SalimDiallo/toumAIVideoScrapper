"""Couche SILVER de l'architecture medallion.

Transforme l'audio brut de la couche BRONZE (téléchargé en amont) en une version
nettoyée : suppression de la musique de fond (demucs) puis découpe des silences
(ffmpeg), avec resynchronisation du transcript sur la nouvelle timeline.

Les pièces suivent le même découpage ports/adapters que le reste du projet :

- ``remap``    : logique pure de remapping des timestamps (aucune I/O, testable).
- ``audio``    : wrappers ffmpeg/demucs (traitement du signal, sous-processus).
- ``storage``  : lecture/écriture bronze<->silver sur MinIO.
- ``pipeline`` : orchestration (idempotence, logs par vidéo, tolérance aux erreurs).
- ``cli``      : point d'entrée argparse (`toumai-silver`).
"""
