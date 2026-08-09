# TOUMAI — Documentation technique

Plateforme d'ingestion média : elle **télécharge l'audio + le transcript** de vidéos
provenant de n'importe quelle plateforme lisible par **yt-dlp** (YouTube, Vimeo,
Dailymotion, TikTok, Rumble, Odysee/LBRY, PeerTube, Reddit), les **indexe** dans un
catalogue Postgres et les **surveille** en continu (veille quotidienne de chaînes).

Ce document décrit **toutes les implémentations**, le **workflow**, la **stack
technique**, les **outils** et les **limites** connues.

---

## 1. Vue d'ensemble

TOUMAI est bâti en **Clean Architecture / ports & adapters**. Le cœur métier
(`domain` + `application`) ne connaît **aucun framework** : il ne dépend que
d'*interfaces* (les ports). Toutes les technologies concrètes (yt-dlp, Postgres,
Kafka, MinIO, FastAPI) sont des **adapters** interchangeables branchés au démarrage
(`bootstrap.py` / `cli.py`).

Conséquence directe : passer du disque local à MinIO, ou du CLI au worker Kafka, se
fait en **écrivant un nouvel adapter** — le use case ne change jamais.

Trois manières de déclencher une ingestion :

1. **CLI** (`toumai-ingest <url>`) — synchrone, mono-machine, pour tests/local.
2. **API + worker Kafka** — asynchrone, scalable, pour la production (batch de
   centaines de vidéos).
3. **Veille** (`toumai-veille` / Airflow) — surveillance automatique de chaînes
   YouTube, met en file les nouvelles vidéos.

---

## 2. Arborescence du code

```
src/media_ingestion/
├── domain/                     # cœur pur — zéro dépendance framework
│   ├── models.py               # entités (VideoMetadata, Transcript, Job, WatchedChannel…)
│   └── ports.py                # interfaces (Protocol) que les adapters implémentent
├── application/                # use cases (orchestration métier)
│   ├── ingest_video.py         # IngestVideoUseCase : ingérer 1 vidéo
│   └── watch_channels.py       # WatchChannelsUseCase : 1 passe de veille
├── adapters/                   # implémentations concrètes des ports
│   ├── ytdlp_downloader.py         # AudioDownloaderPort (yt-dlp + ffmpeg)
│   ├── rate_limited_downloader.py  # wrapper anti-blocage (délais/backoff/proxies)
│   ├── youtube_transcript.py       # TranscriptProviderPort (youtube-transcript-api)
│   ├── subtitle_transcript.py      # TranscriptProviderPort (sous-titres yt-dlp)
│   ├── composite_transcript.py     # routeur transcript par plateforme
│   ├── playlist_resolver.py        # PlaylistResolverPort (yt-dlp)
│   ├── channel_resolver.py         # ChannelResolverPort (yt-dlp, uploads récents)
│   ├── local_storage.py            # StoragePort (disque + JSON)
│   ├── minio_storage.py            # StoragePort (S3/MinIO)
│   ├── postgres_repo.py            # MetadataRepositoryPort (catalogue vidéos)
│   ├── postgres_jobs.py            # JobStorePort (états des jobs)
│   ├── postgres_channels.py        # ChannelWatchStorePort (registre de veille)
│   ├── postgres_veille_runs.py     # VeilleRunLogPort (historique des passes)
│   ├── kafka_producer.py           # EventPublisherPort
│   ├── serialization.py            # entités -> dict JSON
│   └── download_errors.py          # RateLimitedError (429 typé)
├── api/
│   ├── app.py                  # endpoints REST (FastAPI) + montage du dashboard
│   ├── ui.py                   # dashboard HTMX (routes HTML/partials)
│   ├── schemas.py              # DTO Pydantic (requêtes/réponses)
│   └── templates/              # Jinja2 + Tailwind (HTMX)
├── worker/
│   ├── consumer.py             # boucle KafkaConsumer (pool de threads)
│   └── handler.py              # traite 1 event job.requested
├── veille/cli.py               # toumai-veille (passe de veille en CLI)
├── provider.py                 # identification de la plateforme d'origine
├── channel.py                  # normalisation d'une réf. de chaîne YouTube
├── playlist.py                 # extraction d'un id de playlist
├── video_id.py                 # extraction d'un id vidéo (dédup)
├── transcript_quality.py       # métriques de qualité du transcript
├── env_file.py                 # lecture/écriture .env (page settings)
├── config.py                   # Settings (env TOUMAI_*)
├── bootstrap.py                # factories d'infra partagées API/worker
└── cli.py                      # toumai-ingest + câblage des adapters
```

---

## 3. Le domaine (entités & ports)

### 3.1 Entités clés (`domain/models.py`)

Toutes sont des `@dataclass(frozen=True, slots=True)` immuables.

| Entité | Rôle |
|---|---|
| `VideoMetadata` | id, url, titre, chaîne, durée, date, langue, **`provider`** (plateforme d'origine) |
| `AudioAsset` | chemin du fichier audio, format, sample rate |
| `TranscriptSegment` | `start_s`, `duration_s`, `text` |
| `Transcript` | langue, **`source`** (provenance), liste de segments ; `.text` concatène tout |
| `IngestionResult` | agrège métadonnées + audio + transcript + statut + uri de stockage |
| `Job` | 1 tâche d'ingestion (id, url, langues, statut, `video_id` pour dédup, erreur) |
| `WatchedChannel` | 1 chaîne sous veille (`channel_key`, url `/videos`, actif, dernier check) |
| `VeilleRun` | 1 passe de veille enregistrée (checked, queued, détail par chaîne) |

**Provenance du transcript** (`TranscriptSource`, du plus fiable au moins fiable) :

- `YOUTUBE_MANUAL` — sous-titres écrits par l'auteur (**humain**)
- `YOUTUBE_ASR` — reconnaissance vocale automatique de YouTube (machine)
- `YOUTUBE_TRANSLATED` — traduction automatique d'une autre piste
- `PROVIDER_SUBTITLE` — sous-titres auteur d'une autre plateforme (via yt-dlp)
- `PROVIDER_ASR` — sous-titres automatiques d'une autre plateforme

Distinction essentielle : une piste écrite par l'auteur ≠ une piste ASR (devinée par
la machine) ≠ une traduction machine. On ne les traite **jamais** de la même façon.

**Statuts de job** (`JobStatus`) : `PENDING` → `RUNNING` → `COMPLETED` / `FAILED`
(DLQ). **Statut transcript** : `AVAILABLE` / `UNAVAILABLE`.

### 3.2 Les ports (`domain/ports.py`)

Interfaces `Protocol` que les adapters implémentent :

| Port | Responsabilité | Adapter(s) |
|---|---|---|
| `AudioDownloaderPort` | télécharger audio + métadonnées | `YtDlpDownloader`, `RateLimitedDownloader` |
| `TranscriptProviderPort` | récupérer un transcript (ou `None`) | `YouTube…`, `YtDlpSubtitle…`, `Composite…` |
| `PlaylistResolverPort` | déplier une playlist en vidéos | `YtDlpPlaylistResolver` |
| `ChannelResolverPort` | lister les N uploads récents d'une chaîne | `YtDlpChannelResolver` |
| `StoragePort` | persister/lire/supprimer (audio + JSON) | `LocalJsonStorage`, `MinioStorage` |
| `MetadataRepositoryPort` | indexer/rechercher le catalogue | `PostgresMetadataRepository` |
| `JobStorePort` | états & historique des jobs | `PostgresJobStore` |
| `ChannelWatchStorePort` | registre des chaînes surveillées | `PostgresChannelWatchStore` |
| `VeilleRunLogPort` | historique append-only des passes | `PostgresVeilleRunLog` |
| `EventPublisherPort` | publier des events (Kafka) | `KafkaEventPublisher` |

---

## 4. Workflow d'ingestion (le cœur)

Use case unique : `IngestVideoUseCase.execute(url, work_dir, languages)`
(`application/ingest_video.py`).

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. download(url)                         [AudioDownloaderPort]   │
│    -> VideoMetadata (dont provider) + AudioAsset                 │
│    yt-dlp télécharge le meilleur audio, ffmpeg ré-encode (wav)   │
├─────────────────────────────────────────────────────────────────┤
│ 2. fetch(metadata, languages)            [TranscriptProviderPort]│
│    routé selon metadata.provider :                               │
│      - youtube  -> youtube-transcript-api                        │
│      - autres   -> sous-titres yt-dlp                            │
│    -> Transcript OU None                                         │
├─────────────────────────────────────────────────────────────────┤
│ 3. statut = AVAILABLE si transcript, sinon UNAVAILABLE           │
│    (pas de transcript n'annule PAS l'ingestion : l'audio reste)  │
├─────────────────────────────────────────────────────────────────┤
│ 4. save(result, language)                [StoragePort]           │
│    -> storage_uri (chemin local ou s3://)                        │
├─────────────────────────────────────────────────────────────────┤
│ 5. upsert(result, language, uri)         [MetadataRepositoryPort]│
│    indexe la vidéo dans le catalogue Postgres (idempotent)       │
└─────────────────────────────────────────────────────────────────┘
```

Résolution de la **langue** de rangement (`_resolve_language`) : langue du transcript
→ sinon langue de la métadonnée → sinon 1re langue demandée → sinon `unknown`, puis
normalisée (`fr-FR` → `fr`).

### 4.1 Stratégie transcript (détaillée)

Le `CompositeTranscriptProvider` **route** selon la plateforme :

- **YouTube** → `YouTubeTranscriptProvider` (youtube-transcript-api). Il **liste**
  les pistes et choisit la plus fiable dans cet ordre :
  1. manuelle dans une langue préférée
  2. manuelle dans n'importe quelle langue
  3. ASR dans une langue préférée *(si `accept_youtube_asr=True`)*
  4. ASR dans n'importe quelle langue
  5. traduction machine *(si `enable_transcript_translation=True`)*
- **Autres plateformes** → `YtDlpSubtitleProvider`. yt-dlp expose deux buckets :
  `subtitles` (auteur) et `automatic_captions` (ASR). Même ordre de préférence, puis
  parsing du **WebVTT/SRT** (regex de timing, dé-dup des lignes ASR qui se répètent).
  Seuls `vtt` et `srt` sont parsés.
- **Provider inconnu** → filet de sécurité : on tente quand même la voie sous-titres
  yt-dlp (qui renvoie `None` s'il n'y a rien).

Aucun transcript trouvé → `unavailable`, **l'audio est quand même ingéré**.

### 4.2 Téléchargement robuste (`YtDlpDownloader`)

- Format `bestaudio/best`, ré-encodage **ffmpeg** vers le format configuré (`wav` par
  défaut). **Sans ffmpeg** : on garde l'audio natif (m4a/webm) — le pipeline continue.
- La plateforme d'origine (`provider`) est déduite de l'`extractor_key` yt-dlp *après*
  download (`provider.py`), donc **fiable**.
- **Cookies** (anti-bot, ex. TikTok/YouTube) : fichier `cookies.txt` (prioritaire) ou
  extraction navigateur. Best-effort : si illisibles → retry **sans** cookies.
- Erreurs typées :
  - **429 (Too Many Requests)** → `RateLimitedError` (laisse la couche throttling gérer).
  - **Échec conversion ffmpeg** (certains flux Odysee/LBRY) → retry en audio natif
    sans re-télécharger (yt-dlp réutilise le fichier).

### 4.3 Couche anti-blocage (`RateLimitedDownloader`)

Wrapper au-dessus du downloader, piloté par `TOUMAI_DOWNLOAD_*` :

- **Délai aléatoire** avant chaque download (espace les requêtes).
- **Backoff exponentiel borné + jitter** sur 429 : `min(cap, base·2^(n-1)) + jitter`.
- **Rotation de proxies** round-robin (1 IP de sortie par tentative, thread-safe).

Toute cette politique vit **hors** de yt-dlp : le downloader reste « bête », la
politique est configurable.

---

## 5. Chaîne asynchrone API → Kafka → Worker

### 5.1 API REST (`api/app.py`)

| Endpoint | Rôle |
|---|---|
| `GET /health` | healthcheck |
| `POST /process` | 1 URL → job `PENDING` + event `job.requested` (202) |
| `POST /process/csv` | batch CSV (colonne `url`, `lang` optionnelle) → publish_batch |
| `POST /process/playlist` | déplie une playlist YouTube → 1 job/vidéo |
| `GET /jobs` / `GET /jobs/{id}` | liste / détail des jobs |
| `GET /jobs/{id}/transcript` | lit le transcript.json du résultat |
| `POST /jobs/{id}/retry` | re-file un job (repasse en PENDING) |
| `POST /veille/run` | déclenche 1 passe de veille (endpoint appelé par Airflow) |
| `POST /veille/channels/csv` | import CSV de chaînes à surveiller |
| `GET /veille/runs` | historique des passes |
| `GET /videos` | catalogue (filtres : langue, transcript, provider) |

Un job est créé avec un `video_id` extrait de l'URL (`video_id.py`) pour la
**dé-duplication**. L'API **ne télécharge rien** : elle publie sur Kafka et rend la
main immédiatement (202).

### 5.2 Bus d'events Kafka

3 topics (`config.py`) :

- `job.requested` — jobs à traiter (produit par l'API, consommé par les workers)
- `job.completed` — jobs terminés (produit par le worker)
- `job.dlq` — Dead Letter Queue : jobs en échec

Topics auto-créés avec **3 partitions** → jusqu'à 3 workers en parallèle par groupe.

### 5.3 Worker (`worker/consumer.py` + `handler.py`)

- `KafkaConsumer` en `enable_auto_commit=False`, `auto_offset_reset=earliest`.
- Lit au plus `max_concurrent_downloads` messages par `poll()`, les traite dans un
  **ThreadPoolExecutor** (yt-dlp est I/O-bound), puis **commit le lot** →
  sémantique **at-least-once** (les étapes sont idempotentes).
- `max_poll_interval_ms` large (30 min par défaut) pour couvrir un lot lent (gros
  téléchargements + backoff 429) sans être éjecté du groupe.
- **Hot-reload** de la config : si `.env` change (mtime), le worker reconstruit le use
  case sans redémarrer (proxies, cookies, délais, langues, ASR).
- `CommitFailedError` (éjection/rebalance) est **loggé sans planter** : le lot sera
  re-consommé (idempotence).
- `handler.handle(event)` : `RUNNING` → `execute()` → `COMPLETED` (+ event completed)
  ou, sur toute exception, `FAILED` + publication sur la **DLQ** (sans bloquer les
  autres jobs).

---

## 6. La veille (surveillance de chaînes)

Use case `WatchChannelsUseCase.run_once()` (`application/watch_channels.py`) :

1. Pour chaque **chaîne active** du registre, lister les `veille_recent_limit`
   uploads récents (`ChannelResolverPort`, `extract_flat` yt-dlp — pas de download).
2. **Dé-dupliquer** contre les vidéos déjà ingérées/en file (`existing_video_ids`) et
   au sein du lot.
3. Créer un `Job` par nouveauté + accumuler les events.
4. `mark_checked` sur la chaîne, puis `publish_batch` sur `job.requested`.
5. Enregistrer la passe dans l'historique (`VeilleRunLogPort`).

La veille **ne fait qu'enfiler** : le worker Kafka existant fait le vrai travail.

**Déclenchement** :
- **Airflow** (`airflow/dags/veille_youtube.py`) : DAG quotidien à 06:00 qui fait un
  simple `POST /veille/run` (LocalExecutor, aucune dépendance yt-dlp dans l'image).
- **CLI** `toumai-veille` : passe manuelle/locale (fallback au DAG).

Normalisation des chaînes (`channel.py`) : gère `@handle`, `UC…`, `channel/UC…`,
`c/Nom`, `user/Nom` → URL canonique `/videos` (énumérable par yt-dlp) + une clé stable.

---

## 7. Stockage & catalogue

### 7.1 Storage (audio + JSON)

- **Local** (`LocalJsonStorage`) : `data/<langue>/<video_id>/{metadata.json,
  transcript.json, <audio>}`. Sert l'audio avec support des **HTTP range requests**
  (seek dans le player).
- **MinIO/S3** (`MinioStorage`) : renvoie une **URL présignée** que le navigateur
  récupère directement (range S3 = seek sans proxifier les octets par l'API).

L'abstraction `AudioHandle` (`path` XOR `url`) rend les deux interchangeables.

### 7.2 Catalogue Postgres (`postgres_repo.py`)

`MetadataRepositoryPort` : `upsert` idempotent par `video_id`, `list` avec filtres
(langue, statut transcript, provider, recherche texte), `get`, `delete`, et `stats`
(totaux + répartitions par langue / provider / source) pour la page analytics.

Autres tables Postgres : jobs (`postgres_jobs.py`, avec `timeseries` pour le graphe
d'évolution), chaînes surveillées (`postgres_channels.py`), historique de veille
(`postgres_veille_runs.py`).

### 7.3 Qualité du transcript (`transcript_quality.py`)

Pas de transcript de référence → pas de WER réel. On calcule des **proxys
interprétables** : `coverage` (part de l'audio couverte par des sous-titres),
`start_offset_s`, `max_gap_s`, `wpm`, + un poids de provenance (humain 1.0 / ASR 0.75
/ traduit 0.6). D'où un **score 0-100** et un label (`Excellente`/`Bonne`/`Moyenne`/
`Faible`). C'est une **estimation de fiabilité, pas une précision vérifiée**.

---

## 8. Dashboard web (HTMX + Jinja + Tailwind)

`api/ui.py` monte un dashboard server-rendered **sans React** (HTMX pour les
interactions, Jinja2 pour le templating, Tailwind pour le style). Pages : dashboard
(KPIs, graphe), jobs, vidéos (+ lecteur audio, transcript, zip, suppression en masse),
veille (ajout/import/toggle/run de chaînes), stats, et une page **settings** qui édite
le `.env` en direct (`env_file.py`) — les changements sont hot-reloadés par le worker.

---

## 9. Stack technique

| Domaine | Techno |
|---|---|
| Langage | **Python ≥ 3.14** |
| Download média | **yt-dlp** (`[default]`) + **ffmpeg** (ré-encodage audio) |
| Transcript YouTube | **youtube-transcript-api** (≥ 1.0, API instance + legacy 0.6) |
| Transcript autres | sous-titres yt-dlp (WebVTT/SRT parsés maison) |
| API web | **FastAPI** + **uvicorn** + python-multipart |
| Dashboard | **HTMX + Jinja2 + Tailwind** (server-rendered) |
| Config | **pydantic** / **pydantic-settings** (préfixe `TOUMAI_`) |
| Logs | **structlog** (logs structurés) |
| Base de données | **PostgreSQL 16** via **SQLAlchemy 2** + **psycopg 3** |
| Object storage | **MinIO** (S3) via `minio` — sinon disque local |
| Bus d'events | **Apache Kafka 3.8** via **kafka-python-ng** |
| Ordonnanceur | **Airflow 2.10** (LocalExecutor) |
| Tests / qualité | **pytest**, **ruff**, **black**, **mypy** (strict), **httpx** |
| Infra locale | **docker-compose** (postgres, minio, kafka, airflow) |

Points d'entrée (scripts) : `toumai-ingest`, `toumai-api`, `toumai-worker`,
`toumai-veille`.

Configuration : tout est piloté par variables d'env `TOUMAI_*` (ou `.env`). Clés
notables : `AUDIO_FORMAT`, `LANGUAGES`, `FFMPEG_LOCATION`, `YTDLP_COOKIES_*`,
`ACCEPT_YOUTUBE_ASR`, `ENABLE_TRANSCRIPT_TRANSLATION`, `WEBSHARE_PROXY_*`,
`MAX_CONCURRENT_DOWNLOADS`, `DOWNLOAD_DELAY_*`, `DOWNLOAD_MAX_RETRIES`,
`DOWNLOAD_BACKOFF_*`, `DOWNLOAD_PROXIES`, `STORAGE_BACKEND`, `METADATA_BACKEND`,
`POSTGRES_DSN`, `VEILLE_RECENT_LIMIT`, `KAFKA_*`.

---

## 10. Plateformes supportées

Toutes passent par le **même pipeline yt-dlp** (audio + métadonnées + sous-titres) ;
seul le provider transcript diffère.

| Plateforme | Transcript |
|---|---|
| YouTube | youtube-transcript-api (manuel / ASR / traduction) |
| Vimeo, Dailymotion, PeerTube | sous-titres yt-dlp |
| TikTok, Rumble, Odysee, Reddit | sous-titres yt-dlp **si présents**, sinon `unavailable` |

Le **download** fonctionne pour toute URL supportée par yt-dlp ; l'absence de
sous-titres n'empêche pas l'ingestion de l'audio.

---

## 11. Limites connues

**Transcript / qualité**
- Pas de transcription propre (Whisper/ASR interne) : on **récupère** des sous-titres
  existants, on n'en **génère pas**. Vidéo sans sous-titres = pas de texte.
- Seuls **WebVTT et SRT** sont parsés côté yt-dlp ; autres formats ignorés.
- Le score de qualité est une **estimation** (coverage + provenance), pas un WER.
- youtube-transcript-api se fait **bannir par IP** (surtout depuis une IP cloud) →
  nécessite un proxy résidentiel (Webshare) pour tourner à l'échelle.

**Plateformes anti-bot**
- **TikTok/Instagram** : extracteurs fragiles, exigent une **session (cookies)** ;
  sans cookies → erreur d'extraction. Garder yt-dlp à jour (nightly).
- Sous Windows, l'extraction de cookies Chrome/Edge échoue souvent (chiffrement
  app-bound/DPAPI) → préférer un `cookies.txt` exporté ou Firefox.
- **PeerTube** est fédéré (domaines arbitraires) → provider connu seulement *après*
  download (pas de détection par URL).

**Robustesse / exploitation**
- Sémantique Kafka **at-least-once** : un job peut être rejoué (les étapes sont
  idempotentes, mais un re-download peut se reproduire).
- Taille du pool de threads et config Kafka **fixées au démarrage** (seul le reste de
  la config est hot-reloadé).
- `kafka-python-ng` nécessite une `api_version` **figée** (`2.5.0`) pour éviter un
  probing qui plante sous Windows/Python récent.
- Le worker est **I/O-bound** (threads), pas de parallélisme CPU réel pour d'éventuels
  post-traitements lourds.

**Périmètre**
- Pas d'authentification/autorisation sur l'API ni le dashboard (usage interne).
- Dé-duplication par `video_id` **fiable pour YouTube** ; pour les autres plateformes,
  l'id n'est connu qu'après download (dédup plus faible en amont).
- Le DAG Airflow appelle l'API via `host.docker.internal` : suppose l'API lancée sur
  l'hôte.
