# TOUMAI — Documentation technique complète

> Inventaire exhaustif de **tout ce qui est implémenté** dans la plateforme d'ingestion média TOUMAI, et du **workflow complet** de bout en bout.
> Généré par inspection du code source (branche `feature/phase2`).

---

## 1. Vue d'ensemble

**Objectif** : ingérer des vidéos YouTube (audio + transcript) et les cataloguer, en s'appuyant sur une **Clean Architecture / ports-adapters** qui permet de brancher/débrancher l'infra (local ↔ MinIO/Kafka/Postgres) **sans réécrire le cœur métier**.

Deux phases :

| Phase | Contenu | État |
|-------|---------|------|
| **Phase 1 (MVP)** | Use case `IngestVideoUseCase`, adapters yt-dlp + youtube-transcript, stockage disque local, CLI | ✅ implémenté |
| **Phase 2** | API FastAPI + Kafka + worker, stockage MinIO (medallion Bronze), catalogue Postgres, ingestion CSV par lot | ✅ implémenté |

**Stack technique** (`pyproject.toml`) : Python **≥ 3.14**, yt-dlp, youtube-transcript-api, pydantic v2 + pydantic-settings, structlog, SQLAlchemy 2 + psycopg3, minio, FastAPI + uvicorn, kafka-python-ng.
Outillage dev : pytest, ruff, black, mypy (strict), httpx.

Trois points d'entrée (`[project.scripts]`) :
- `toumai-ingest` → CLI (`media_ingestion.cli:main`)
- `toumai-api` → API FastAPI (`media_ingestion.api.app:main`)
- `toumai-worker` → worker Kafka (`media_ingestion.worker.consumer:main`)

---

## 2. Architecture en couches (hexagonale)

```
src/media_ingestion/
├── domain/                 # entités pures + ports (interfaces). ZÉRO dépendance framework.
│   ├── models.py           # dataclasses immuables + enums
│   └── ports.py            # 7 Protocols (interfaces)
├── application/            # use case (orchestration métier)
│   └── ingest_video.py     # IngestVideoUseCase
├── adapters/               # implémentations concrètes des ports (remplaçables)
│   ├── ytdlp_downloader.py         # AudioDownloaderPort
│   ├── youtube_transcript.py       # TranscriptProviderPort
│   ├── local_storage.py            # StoragePort (disque)
│   ├── minio_storage.py            # StoragePort (S3/MinIO, Bronze)
│   ├── postgres_repo.py            # MetadataRepositoryPort (catalogue videos)
│   ├── postgres_jobs.py            # JobStorePort (suivi des jobs)
│   ├── kafka_producer.py           # EventPublisherPort
│   └── serialization.py            # shapes JSON partagées par les StoragePort
├── api/                    # FastAPI
│   ├── app.py             # routes + factory create_app()
│   └── schemas.py         # modèles Pydantic requête/réponse
├── worker/                 # consommateur Kafka
│   ├── consumer.py        # boucle KafkaConsumer
│   └── handler.py         # logique d'un événement job.requested (transport-agnostic)
├── bootstrap.py            # factories infra (publisher, job store, catalog)
├── cli.py                  # câblage adapters -> use case + entrée CLI
├── config.py              # Settings pydantic (env TOUMAI_*)
├── logging_setup.py       # structlog
└── _kafka_compat.py       # shim Python 3.12+ / kafka-python-ng
```

**Règle de dépendance** : le use case et les steps ne dépendent **que des ports** (`domain/ports.py`). Passer de disque local à MinIO, ou de « pas de catalogue » à Postgres = **écrire/activer un adapter**, le métier ne change pas.

---

## 3. Le domaine (`domain/models.py` + `domain/ports.py`)

### 3.1 Entités (dataclasses immuables, `frozen=True, slots=True`)

- **`VideoMetadata`** : `video_id`, `url`, `title`, `channel?`, `duration_s?`, `upload_date?`, `language?`.
- **`AudioAsset`** : `path`, `format`, `sample_rate?`.
- **`TranscriptSegment`** : `start_s`, `duration_s`, `text`.
- **`Transcript`** : `language`, `source`, `segments[]` + propriété `text` (concaténation nettoyée des segments).
- **`IngestionResult`** : agrège `metadata`, `audio`, `transcript?`, `transcript_status`, `created_at` (UTC), et après persistance `language?` + `storage_uri?`.
- **`Job`** : `job_id`, `url`, `languages[]`, `status`, `result_uri?`, `error?`, `created_at`, `updated_at`.

### 3.2 Enums

- **`TranscriptSource`** — ordonné du plus fiable au moins fiable :
  `YOUTUBE_MANUAL` (sous-titres écrits par l'auteur, qualité humaine) > `YOUTUBE_ASR` (auto-générés par la reconnaissance vocale YouTube) > `YOUTUBE_TRANSLATED` (traduction automatique d'une autre piste).
  Propriétés utilitaires : `is_youtube`, `is_human` (vrai uniquement pour `YOUTUBE_MANUAL`).
- **`TranscriptStatus`** : `AVAILABLE` / `UNAVAILABLE` (aucun sous-titre YouTube disponible).
- **`JobStatus`** : `PENDING` (accepté par l'API, en file Kafka) → `RUNNING` (pris par un worker) → `COMPLETED` / `FAILED` (envoyé à la DLQ).

### 3.3 Ports (interfaces `Protocol`)

| Port | Méthodes | Implémentations |
|------|----------|-----------------|
| `AudioDownloaderPort` | `download(url, dest) -> (metadata, audio)` | `YtDlpDownloader` |
| `TranscriptProviderPort` | `fetch(video_id, langs) -> Transcript?` | `YouTubeTranscriptProvider` |
| `StoragePort` | `save(result, lang) -> uri` ; `load_transcript(uri) -> dict?` | `LocalJsonStorage`, `MinioStorage` |
| `MetadataRepositoryPort` | `upsert(result, lang, uri)` ; `list(...)` | `PostgresMetadataRepository` |
| `EventPublisherPort` | `publish(topic, key, event)` ; `publish_batch(topic, items)` | `KafkaEventPublisher` |
| `JobStorePort` | `create` ; `get` ; `list` ; `update_status` | `PostgresJobStore` |

---

## 4. Le use case central (`application/ingest_video.py`)

`IngestVideoUseCase.execute(video_url, work_dir, languages)` — **c'est le cœur, utilisé à l'identique par le CLI et le worker Kafka** :

1. **Download** : `downloader.download()` → `(VideoMetadata, AudioAsset)`. Log `audio.downloaded`.
2. **Transcript YouTube** : `transcript_provider.fetch(video_id, languages)`.
   - Si trouvé → `status = AVAILABLE`, log `transcript.fetched` (source/langue/nb segments).
   - Sinon → `status = UNAVAILABLE`, log `transcript.skipped`.
3. **Résolution de langue** (`_resolve_language`) : priorité `transcript.language` → `metadata.language` → `languages[0]` → `"unknown"`, puis normalisation `"fr-FR"` → `"fr"` (split `-`, lowercase).
4. **Persistance** : `storage.save(result, language)` → `storage_uri`. Log `ingestion.saved`. Le résultat est enrichi (`replace`) avec `language` + `storage_uri`.
5. **Indexation** (optionnelle) : si `metadata_repo` présent → `metadata_repo.upsert(result, language, storage_uri)`. Log `metadata.indexed`.

Retourne l'`IngestionResult` complet. Le catalogue est une dépendance **optionnelle** (`None` = étape sautée).

---

## 5. Les adapters (implémentations concrètes)

### 5.1 `YtDlpDownloader` (audio)
- Format cible configurable (`wav` par défaut).
- **Détection ffmpeg** : si ffmpeg sur PATH (ou `TOUMAI_FFMPEG_LOCATION` fourni) → ré-encodage via post-processeur `FFmpegExtractAudio`. Sinon → conserve le flux natif `bestaudio` (m4a/webm) tel quel + warning `ffmpeg.missing` (le pipeline ne casse pas).
- Options yt-dlp : `format=bestaudio/best`, `outtmpl=<dest>/<id>.<ext>`, `quiet`, `noplaylist`.
- Extrait les métadonnées de `info` (id, webpage_url, title, uploader, duration, upload_date, language).

### 5.2 `YouTubeTranscriptProvider` (sous-titres)
**Stratégie de sélection de la piste la plus fiable** (au lieu de prendre aveuglément la première) :
1. Piste **manuelle** dans une langue préférée →
2. Piste **manuelle** dans n'importe quelle langue →
3. Piste **ASR** dans une langue préférée *(sauté si `accept_asr=False`)* →
4. Piste **ASR** dans n'importe quelle langue *(sauté si `accept_asr=False`)* →
5. **Traduction** automatique d'une piste traduisible vers `languages[0]` *(seulement si `enable_translation=True`)*.

Retourne `None` si rien d'acceptable (transcripts désactivés, aucune piste, ASR-only alors qu'ASR refusé, ou erreur réseau — toute exception = « pas de transcript »). Compatible **API 1.x** (`list`/`fetch`) **et legacy 0.6.x** (`get_transcript`, où manuel/ASR est indiscernable → supposé ASR).

### 5.3 Stockage (StoragePort)
Les deux backends partagent les shapes JSON de `serialization.py` :
- `metadata_dict` : video_id, url, title, channel, duration_s, upload_date, **language**, audio_path, audio_format, transcript_status, created_at (ISO).
- `transcript_dict` : language, source, **text** (plein texte), segments[].

**`LocalJsonStorage`** — layout `<root>/<language>/<video_id>/{metadata.json, transcript.json, <audio>}`. Déplace (`shutil.move`) l'audio téléchargé dans le dossier de la vidéo. `load_transcript` relit `transcript.json`. `save` retourne le **chemin** comme URI.

**`MinioStorage`** — couche **medallion Bronze**. Layout bucket `<layer>/<language>/<video_id>/...` (`layer="bronze"`). Crée le bucket au besoin (`_ensure_bucket`). Uploade l'audio (`fput_object`) puis **supprime la copie locale de staging** (`unlink`). Écrit metadata/transcript JSON via `put_object`. Retourne une **URI `s3://<bucket>/<prefix>`**. `load_transcript` relit l'objet `transcript.json`. → **Drop-in replacement** de LocalJsonStorage (même port).

### 5.4 `PostgresMetadataRepository` (catalogue `videos`)
SQLAlchemy Core. Table `videos` (PK `video_id`, colonnes url/title/channel/duration_s/upload_date/`language` indexée/transcript_status/transcript_source/storage_uri/created_at).
- `upsert` : **idempotent** via `INSERT ... ON CONFLICT (video_id) DO UPDATE` (dialecte PostgreSQL). Une ligne par vidéo.
- `list` : tri `created_at DESC`, filtre optionnel `language`, `limit`/`offset`.
- Les artefacts bruts vivent dans MinIO ; Postgres est l'**index requêtable**.

### 5.5 `PostgresJobStore` (suivi des jobs `jobs`)
Table `jobs` (PK `job_id`, url, `languages` ARRAY, status, result_uri, error, created_at, updated_at).
- `create` : idempotent (`ON CONFLICT (job_id) DO NOTHING` — re-POST du même id = no-op).
- `get` / `list` (filtre `status`, tri récent) / `update_status` (met à jour status + `updated_at`, et result_uri/error si fournis).
- **Statut partagé inter-process** : l'API écrit `PENDING`, le worker fait évoluer `RUNNING → COMPLETED/FAILED`, `GET /jobs/{id}` relit.

### 5.6 `KafkaEventPublisher` (événements)
kafka-python-ng, sérialisation JSON. `acks="all"`, `retries=3`, `api_version` **fixée** (évite le probing qui plante sous Windows).
- `publish` : envoi **bloquant** (`future.get(timeout=10)`) pour remonter les erreurs broker.
- `publish_batch` : envoie tout puis **un seul `flush()`** (efficace pour gros CSV, pas de blocage par message).

---

## 6. Configuration (`config.py`) — variables `TOUMAI_*`

Toutes ont des valeurs par défaut (via pydantic-settings, `.env` supporté). Principales :

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_DATA_DIR` | `data` | racine de sortie |
| `TOUMAI_AUDIO_FORMAT` | `wav` | format audio (ré-encodage ffmpeg) |
| `TOUMAI_LANGUAGES` | `["fr","en"]` | langues de sous-titres préférées |
| `TOUMAI_FFMPEG_LOCATION` | `None` | dossier bin ffmpeg si pas sur PATH |
| `TOUMAI_ACCEPT_YOUTUBE_ASR` | `true` | accepter l'ASR YouTube (sinon `unavailable`) |
| `TOUMAI_ENABLE_TRANSCRIPT_TRANSLATION` | `false` | traduction en dernier recours |
| `TOUMAI_STORAGE_BACKEND` | `local` | `local` \| `minio` |
| `TOUMAI_MINIO_*` | localhost:9000, minioadmin, bucket `toumai-media` | MinIO |
| `TOUMAI_METADATA_BACKEND` | `none` | `none` \| `postgres` |
| `TOUMAI_POSTGRES_DSN` | `postgresql+psycopg://toumai:toumai@localhost:5432/toumai` | Postgres |
| `TOUMAI_KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka |
| `TOUMAI_KAFKA_API_VERSION` | `2.5.0` | version fixe (anti-probing Windows) |
| `TOUMAI_KAFKA_CONSUMER_GROUP` | `toumai-workers` | groupe consumer |
| `TOUMAI_TOPIC_JOB_REQUESTED / _COMPLETED / _DLQ` | `job.requested` / `job.completed` / `job.dlq` | topics |

---

## 7. Câblage (`bootstrap.py` + `cli.py`)

- **`bootstrap.py`** applique d'abord le shim `_kafka_compat.apply()`, puis expose : `build_publisher` (Kafka), `build_job_store` (Postgres + `create_schema()`), `build_catalog` (Postgres videos + schema).
- **`cli.py`** : `_build_storage` (minio ou local selon config), `_build_metadata_repo` (postgres ou `None`), et surtout **`build_use_case(settings)`** qui assemble downloader + transcript provider + storage + catalogue(optionnel). Cette fabrique est **réutilisée par le worker** Kafka.

---

## 8. Le workflow complet de bout en bout

### 8.1 Chemin CLI (synchrone, sans infra) — MVP
```
toumai-ingest <url> --lang fr en
   └─> build_use_case(settings)
        └─> IngestVideoUseCase.execute(url, data_dir, langs)
             1. YtDlpDownloader.download        → audio + metadata
             2. YouTubeTranscriptProvider.fetch → transcript (ou None)
             3. Storage.save                    → data/<lang>/<video_id>/...
             4. [PostgresRepo.upsert si activé]
   Sortie: data/<video_id>/metadata.json (+ transcript.json si dispo)
```

### 8.2 Chemin API + Kafka + worker (asynchrone) — Phase 2

```
   CLIENT                    API (FastAPI)              KAFKA                 WORKER
     │                           │                        │                     │
     │  POST /process            │                        │                     │
     │  {url, languages}         │                        │                     │
     ├──────────────────────────>│                        │                     │
     │                           │ 1. job_id = uuid4      │                     │
     │                           │ 2. JobStore.create     │                     │
     │                           │    (PENDING, Postgres) │                     │
     │                           │ 3. publish             │                     │
     │                           │    job.requested ──────>│  topic              │
     │  202 {job_id, pending}    │                        │  job.requested      │
     │<──────────────────────────┤                        │                     │
     │                           │                        │ 4. consume ────────>│
     │                           │                        │                     │ 5. JobStore
     │                           │                        │                     │    RUNNING
     │                           │                        │                     │ 6. use_case.execute
     │                           │                        │                     │    (download/transcript/
     │                           │                        │                     │     store MinIO/index PG)
     │                           │                        │  job.completed <────┤ 7a. COMPLETED
     │                           │                        │  (ou job.dlq) <─────┤ 7b. FAILED -> DLQ
     │  GET /jobs/{id}           │                        │                     │ 8. consumer.commit()
     ├──────────────────────────>│ JobStore.get (Postgres)│                     │
     │  {status, result_uri}     │                        │                     │
     │<──────────────────────────┤                        │                     │
     │  GET /jobs/{id}/transcript │                        │                     │
     ├──────────────────────────>│ Storage.load_transcript│                     │
     │<──────────────────────────┤ (MinIO/local)          │                     │
```

**Détails worker** (`worker/consumer.py` + `handler.py`) :
- `KafkaConsumer` sur `job.requested`, groupe `toumai-workers`, `enable_auto_commit=False`, `auto_offset_reset="earliest"`, `api_version` fixée.
- Pour chaque message : `JobHandler.handle(event)` dans un `try/except` — une exception (event malformé) est **loggée mais ne bloque pas** (pas de poison-pill), puis `consumer.commit()` **dans le `finally`** (commit manuel après traitement).
- `JobHandler.handle` (transport-agnostic, testable) : passe le job en `RUNNING`, exécute le use case ; en cas de succès → `COMPLETED` + result_uri + publie `job.completed` ; en cas d'échec → `FAILED` + error + publie sur la **DLQ** `job.dlq`.
- **Parallélisme** : les topics sont auto-créés avec `KAFKA_NUM_PARTITIONS=3` → jusqu'à 3 workers actifs en parallèle (1 partition = 1 consumer actif dans le groupe).

---

## 9. L'API FastAPI (`api/app.py`) — endpoints

`create_app()` est une **factory injectable** (store/publisher/storage/catalog surchargeables → testable). `build_asgi()` configure le logging et est utilisée par uvicorn (`--factory`), port 8000.

| Méthode | Route | Rôle |
|---------|-------|------|
| `GET` | `/health` | `{"status":"ok"}` |
| `POST` | `/process` | Body `{url, languages?}` → crée job PENDING, publie `job.requested`, **202** `{job_id, pending}` |
| `POST` | `/process/csv` | Upload CSV (colonne **`url`** requise, `lang`/`languages` optionnelle) → 1 job/ligne, **publish_batch**, 202 `{accepted, jobs[], errors[]}` |
| `GET` | `/jobs` | Liste jobs, filtre `?status=`, `limit`(1–500)/`offset` |
| `GET` | `/jobs/{id}` | Statut d'un job (404 si absent) |
| `GET` | `/jobs/{id}/transcript` | Relit le transcript via `Storage.load_transcript` (409 si job non fini, 404 si pas de transcript) |
| `POST` | `/jobs/{id}/retry` | Remet PENDING + republie `job.requested` → 202 |
| `GET` | `/videos` | Catalogue Postgres, filtre `?language=`, `limit`/`offset` |

Parsing CSV : décodage `utf-8-sig` (BOM), normalisation des en-têtes (`strip().lower()`), langues parsées depuis `lang`/`languages` (séparateurs `,`/`;`/espaces), lignes sans URL → collectées dans `errors`.

Schémas Pydantic (`api/schemas.py`) : `ProcessRequest`, `ProcessAccepted`, `BatchItem`, `BatchAccepted`, `JobResponse`, `VideoItem`.

---

## 10. Infrastructure Docker (`docker-compose.yml`)

Services **de base** (`docker compose up -d`) :
- **postgres** (postgres:16, `toumai/toumai/toumai`, port 5432, healthcheck `pg_isready`).
- **minio** (console 9001, API S3 9000, `minioadmin/minioadmin`, healthcheck `mc ready`).
- **kafka** (apache/kafka:3.8.0, mode **KRaft** broker+controller, port 9092, `AUTO_CREATE_TOPICS_ENABLE=true`, **`NUM_PARTITIONS=3`**).
- **createbuckets** (minio/mc) : crée le bucket `toumai-media` au démarrage puis se termine.

Volumes persistants : `pgdata`, `miniodata`.

---

## 11. Détails de robustesse / compatibilité

- **`_kafka_compat.py`** : sous Python ≥ 3.12, kafka-python-ng plante lors d'un rebalance (`selector.unregister` sur socket fermée `fileno()==-1` → `ValueError: Invalid file descriptor: -1`). Le shim rend `selectors.DefaultSelector.unregister` **tolérant** (retrouve la clé par identité, retrait via le fd d'origine). Correctif **additif**, appliqué automatiquement via `bootstrap.py`.
- **`api_version` Kafka fixée** (`2.5.0`) partout → évite le `check_version()` probing qui échoue sous Windows/Python 3.14.
- **Logging** : structlog en rendu console (`configure_logging`), events structurés (`audio.downloaded`, `transcript.selected`, `job.started/completed/failed`, etc.).
- **Idempotence** : jobs (`ON CONFLICT DO NOTHING`) et catalogue videos (`ON CONFLICT DO UPDATE`) — re-traiter une vidéo/un job est sûr.

---

## 12. Tests (`tests/`, ~1150 lignes)

Exécutables **sans réseau ni ffmpeg** (fakes/monkeypatch). `pytest -q`.

| Fichier | Couverture |
|---------|-----------|
| `test_ingest_video.py` | use case : transcript YouTube utilisé, skip si rien, appel du metadata_repo |
| `test_youtube_transcript.py` | stratégie de sélection : manuel > ASR, fallback ASR, rejet ASR, ordre des langues, manuel autre-langue > ASR préféré, traduction dernier recours, None si aucune piste |
| `test_api.py` | `/process` publie, `/jobs/{id}`, 404, `/process/csv` (1 job/ligne + rejet colonne url manquante), filtre status, retry, transcript (+404), `/videos` |
| `test_worker.py` | succès → COMPLETED + publish ; échec → FAILED + DLQ |

---

## 13. Documentation existante du repo

| Doc | Contenu |
|-----|---------|
| `README.md` | vue d'ensemble, stratégie transcript, architecture, roadmap |
| `GETTING_STARTED.md` | de zéro à une ingestion complète (Windows/PowerShell) |
| `docs/ARCHITECTURE.md` | Clean Architecture, ports/adapters |
| `docs/PHASE2_INFRA.md` | MinIO / Postgres / stockage medallion |
| `docs/PHASE2_API_KAFKA.md` | API FastAPI + événements Kafka + worker |
| `docs/YTDLP_LIMITES.md` | limites/pièges yt-dlp |

---

## 14. Ce qui n'est PAS (encore) implémenté

D'après la roadmap `README.md` et le code : les couches **Silver/Gold** du medallion (seul **Bronze** existe dans MinIO), l'export **Parquet**, **Elasticsearch** et **Qdrant** (recherche/embeddings) sont mentionnés en roadmap mais **pas implémentés**. La transcription se limite aux sous-titres YouTube (pas de STT).
