"""Compatibilité kafka-python-ng sous Python >= 3.12.

`kafka-python-ng` (2.2.3) nettoie ses sockets fermés dans `KafkaClient._poll`
via `selector.unregister(key.fileobj)`. Quand la socket est déjà fermée
(fileno() == -1), typiquement lors d'un rebalance de groupe, les `selectors`
de Python 3.12+ lèvent `ValueError: Invalid file descriptor: -1` au lieu de
retrouver la clé, ce qui fait planter la boucle du worker.

On rend `unregister` tolérant : si le fileobj est mort, on retrouve sa clé par
identité et on la retire via son fd d'origine (toujours positif). Correctif
additif — le comportement ne change que dans le cas qui échoue aujourd'hui.
"""

from __future__ import annotations

import selectors

_PATCH_FLAG = "_toumai_dead_fd_tolerant"


def apply() -> None:
    cls = selectors.DefaultSelector
    original = cls.unregister
    if getattr(original, _PATCH_FLAG, False):
        return

    def unregister(self, fileobj, _original=original):  # type: ignore[no-untyped-def]
        try:
            return _original(self, fileobj)
        except ValueError, OSError:
            # Socket déjà fermée (fileno() == -1) : retrouve la clé par identité
            # et retire-la via son fd d'origine.
            for fd, key in list(self._fd_to_key.items()):
                if key.fileobj is fileobj:
                    return _original(self, fd)
            # Déjà retirée du selector : désenregistrer un fileobj absent est un
            # no-op voulu, pas une erreur. On avale au lieu de faire planter la
            # boucle du worker.
            return None

    setattr(unregister, _PATCH_FLAG, True)
    cls.unregister = unregister
