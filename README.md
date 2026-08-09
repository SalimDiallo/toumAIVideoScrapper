# TOUMAI — Media Ingestion Platform

Scraper multi-plateformes (audio + transcript) construit en **Clean Architecture / ports-adapters**.
Tout ce que **yt-dlp** sait lire est ingérable : YouTube, Vimeo, Dailymotion, TikTok,
Rumble, Odysee, PeerTube, Reddit (voir la liste plus bas).
Phase 1 = MVP fonctionnel. Phase 2 = branchement Kafka / MinIO / Postgres sans réécrire le cœur.

## Stratégie transcript

1. On télécharge l'audio + les métadonnées via **yt-dlp** ; la **plateforme d'origine**
   (`provider`) est déduite de l'extracteur yt-dlp.
2. On récupère les sous-titres, **routés selon la plateforme** :
   - **YouTube** → `youtube-transcript-api` (manual > ASR > traduction).
   - **autres plateformes** → sous-titres exposés par **yt-dlp** (`subtitles` = humains,
     `automatic_captions` = ASR), parsés depuis le WebVTT/SRT.
3. S'il n'y en a pas → on ne transcrit pas, statut `unavailable`.

## Architecture

```
src/media_ingestion/
├── domain/          # entités pures + ports (interfaces). Zéro dépendance framework.
│   ├── models.py
│   └── ports.py
├── application/     # use case (orchestration métier)
│   └── ingest_video.py
├── adapters/        # implémentations des ports (remplaçables)
│   ├── ytdlp_downloader.py       # AudioDownloaderPort
│   ├── youtube_transcript.py     # TranscriptProviderPort
│   └── local_storage.py          # StoragePort (→ MinIO en Phase 2)
├── config.py        # settings env-driven (TOUMAI_*)
└── cli.py           # point d'entrée MVP (→ worker Kafka en Phase 2)
```

Le use case ne dépend **que des ports**. Passer à MinIO/Kafka = écrire un nouvel adapter, le use case ne change pas.

## Prérequis

- Python 3.13
- **ffmpeg** sur le PATH (extraction audio par yt-dlp)

## Raccourcis (Makefile)

Depuis **Git Bash** (`make` installé) :

```bash
make            # liste toutes les commandes
make install    # venv + dépendances
make up         # infra : postgres + minio + kafka
make api        # API FastAPI (terminal 1) -> http://localhost:8000/docs
make worker     # worker Kafka (terminal 2)
make ingest URL="https://youtu.be/xxxx" LANGS=fr
make test       # tests
```

> `make` n'est pas natif sous Windows : `winget install ezwinports.make`, puis rouvrir le terminal.

## Installation (manuel)

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

## Usage

```bash
toumai-ingest "https://www.youtube.com/watch?v=XXXX" --lang fr en
# ou
python -m media_ingestion.cli "https://www.youtube.com/watch?v=XXXX"
```

Sortie dans `data/<video_id>/` : `metadata.json` (+ `transcript.json` si dispo).

## Tests

```bash
pytest            # tests du use case avec fakes (sans réseau ni ffmpeg)
```




### Plateformes cibles

Toutes passent par le même pipeline yt-dlp (audio + métadonnées + sous-titres) ;
seul le provider transcript diffère (voir *Stratégie transcript*).

| Plateforme | Statut | Transcript |
|---|---|---|
| YouTube | ✅ | youtube-transcript-api (manual/ASR/traduction) |
| Vimeo | ✅ | sous-titres yt-dlp |
| Dailymotion | ✅ | sous-titres yt-dlp |
| TikTok (contenus publics) | ✅ | sous-titres yt-dlp si présents |
| Rumble | ✅ | sous-titres yt-dlp si présents |
| Odysee | ✅ | sous-titres yt-dlp si présents |
| PeerTube | ✅ | sous-titres yt-dlp |
| Reddit (vidéos publiques) | ✅ | sous-titres yt-dlp si présents |

> Le téléchargement fonctionne pour toute URL supportée par yt-dlp. Beaucoup de
> plateformes (TikTok, Rumble, Reddit, Odysee) n'exposent pas toujours de
> sous-titres : dans ce cas le transcript est marqué `unavailable`, l'audio reste ingéré.

#### TikTok & plateformes à forte protection anti-bot

Les extracteurs des grosses plateformes cassent souvent : garde **yt-dlp à jour**
(idéalement la *nightly*, qui corrige TikTok/Instagram avant la release stable) :

```bash
pip install -U --pre "yt-dlp[default]"     # nightly / pré-release
```

TikTok refuse en plus l'extraction sans **session** — l'erreur
`Unable to extract universal data for rehydration` signifie « pas de cookies ».
Fournis des cookies via un des deux réglages (`.env`) :

```dotenv
# 1) Fichier cookies.txt (recommandé sous Windows : contourne le chiffrement DPAPI/
#    app-bound de Chrome/Edge). Exporte-le avec une extension "Get cookies.txt".
TOUMAI_YTDLP_COOKIES_FILE=./cookies.txt
# 2) OU extraction directe depuis le navigateur (échoue si Chrome est ouvert ou
#    chiffré app-bound ; Firefox est le plus fiable). "chrome:Profile 1" possible.
TOUMAI_YTDLP_COOKIES_FROM_BROWSER=firefox
```

Les cookies restent **best-effort** : s'ils sont illisibles, le download réessaie
sans plutôt que d'échouer. Un job dont l'extracteur casse part en `failed` (DLQ)
**sans bloquer les autres**.