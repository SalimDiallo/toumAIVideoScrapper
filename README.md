# TOUMAI — Media Ingestion Platform

Scraper YouTube (audio + transcript) construit en **Clean Architecture / ports-adapters**.
Phase 1 = MVP fonctionnel. Phase 2 = branchement Kafka / MinIO / Postgres sans réécrire le cœur.

## Stratégie transcript

1. On télécharge l'audio + les métadonnées via **yt-dlp**.
2. On récupère les **sous-titres fournis par YouTube** (youtube-transcript-api).
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

## Roadmap Phase 2

- Événements Kafka (`job.requested`, `job.completed`, DLQ)
- MinIO medallion (Bronze/Silver/Gold, Parquet), Postgres, Elasticsearch, Qdrant
- API FastAPI (`POST /process` → 202 + `job_id`)
```
