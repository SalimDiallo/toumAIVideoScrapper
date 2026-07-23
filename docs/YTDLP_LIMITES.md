# yt-dlp — Limites & garde-fous (à traiter en Phase 2)

Limites de **yt-dlp** (l'outil, pas notre code) qui comptent pour un pipeline à l'échelle.
Statut : **connu / accepté pour le MVP**. Les garde-fous seront ajoutés plus tard (Phase 2,
couche workers + orchestration Kafka/Airflow).

> Priorité des vrais murs à l'échelle : **#2 anti-bot**, **#3 rate-limit IP**, **#1 fragilité/màj**.

---

## 1. Fragilité / maintenance permanente
- Pas d'API officielle : yt-dlp *scrape* et rétro-ingénie les mécanismes internes de YouTube.
- YouTube change souvent → yt-dlp casse et doit être **mis à jour fréquemment**.
- **Épingler une version** (ce qu'on fait dans `pyproject.toml`) finit par ne plus marcher.

**Garde-fou prévu :** màj automatique/régulière de yt-dlp (job planifié), alerte si un
téléchargement échoue massivement (signal de « yt-dlp cassé »).

## 2. Détection anti-bot (le plus gros à l'échelle)
- YouTube renvoie de plus en plus **« Sign in to confirm you're not a bot »**.
- Nécessite parfois **cookies** (`--cookies`) et **PO Tokens**.
- Utiliser les cookies de **son compte** = risque de **suspension du compte**.

**Garde-fou prévu :** pool de cookies/identités dédiées (jamais un compte perso critique),
rotation, détection du message anti-bot → repli/backoff.

## 3. Rate limiting / throttling IP
- Trop de requêtes depuis une même IP → **HTTP 429** et ralentissements.
- Débit parfois bridé tant que le *nsig/signature* n'est pas résolu.

**Garde-fou prévu :** **proxies tournants** / pool d'IP, limitation du débit (rate limiter),
**retry avec backoff exponentiel** côté worker.

## 4. Contenus verrouillés (échouent sans authentification)
- Vidéos **privées**, parfois **non répertoriées**.
- **Age-restricted** (18+).
- **Membres/adhérents seulement**.
- **Géo-bloquées** (région) → besoin d'un proxy dans le bon pays.
- **Premium/achats**.

**Garde-fou prévu :** classer ces échecs comme « non récupérables » (→ DLQ) vs
« à réessayer », cookies si accès légitime, proxy géo si besoin.

## 5. Dépendance à ffmpeg
- Sans ffmpeg : pas de conversion `.wav`, pas de fusion audio+vidéo, pas de découpe.
- (Déjà contourné dans le MVP : on garde l'audio natif si ffmpeg absent.)

**Garde-fou prévu :** ffmpeg garanti dans l'image Docker des workers (Phase 2).

## 6. Performance
- **1 vidéo à la fois par process**, largement **I/O-bound** (réseau).
- Négociation des formats + résolution *nsig* (JS) = latence par vidéo.

**Garde-fou prévu :** parallélisme au niveau orchestration (plusieurs workers Kafka),
pas dans yt-dlp lui-même.

## 7. Juridique / conformité
- Télécharger du contenu YouTube **viole les CGU** de YouTube (hors API officielle / cas autorisés).
- **Droit d'auteur** selon les vidéos.

**Garde-fou prévu :** cadrage usage (recherche/interne vs diffusion), traçabilité des sources.

## 8. Divers pièges
- **Livestreams / premières** : gestion particulière (flux en cours).
- Formats qui **changent/disparaissent** entre le listing et le téléchargement.
- Métadonnées parfois **incomplètes** (`language` souvent absent → d'où le repli sur la
  langue du transcript dans notre code).

**Garde-fou prévu :** validation des métadonnées, filtrage des types non supportés en amont.

---

## Récap des garde-fous à implémenter (Phase 2)

| Problème            | Parade                                                        |
| ------------------- | ------------------------------------------------------------ |
| Anti-bot            | pool de cookies dédiés + détection message + backoff         |
| Rate-limit / 429    | proxies tournants + rate limiter + retry backoff exponentiel |
| Fragilité / màj     | màj auto de yt-dlp + alerte sur échecs massifs               |
| Contenus verrouillés| tri récupérable vs non-récupérable → DLQ                      |
| ffmpeg manquant     | ffmpeg garanti dans l'image Docker worker                    |
| Débit / volume      | parallélisme via workers (Kafka), pas via yt-dlp             |
| Conformité          | cadrage usage + traçabilité                                  |

Ces parades vivent naturellement dans la couche **workers + Kafka/Airflow** de la Phase 2.
