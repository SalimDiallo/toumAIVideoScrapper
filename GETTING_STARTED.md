# Démarrage — TOUMAI Media Ingestion

Guide pour repartir **d'un clone tout neuf** (aucun `.venv`, aucune infra lancée)
jusqu'à une ingestion YouTube de bout en bout (API → Kafka → worker → Postgres/MinIO)
et l'**interface web**.

> **Chemin recommandé : `make`** (voir §0). Les commandes brutes **Windows /
> PowerShell** sont données en repli sous chaque étape.

---

## 0. Démarrage rapide avec `make` ⭐

`make` est le moyen le plus court. Sur Windows il tourne **depuis Git Bash** une fois
installé : `winget install ezwinports.make` (puis rouvrir le terminal). `make` seul
affiche l'aide de toutes les cibles.

```bash
make install          # 1. venv + dépendances (+ dev)
cp .env.example .env  # 2. config (voir §3 pour l'éditer)
make up               # 3. infra Docker : postgres + minio + kafka (+ bucket)
make api              # 4. terminal 1 : API + interface web
make worker           # 5. terminal 2 : worker Kafka
```

Puis ouvre **http://localhost:8000/** (interface) et **/docs** (Swagger).

| Cible | Rôle |
|-------|------|
| `make` / `make help` | liste toutes les cibles |
| `make install` | crée `.venv` + `pip install -e ".[dev]"` |
| `make up` / `make down` / `make ps` / `make logs` | infra Docker (up/stop/état/logs) |
| `make clean` | arrête l'infra **et supprime les volumes** (⚠ données) |
| `make api` | lance l'API FastAPI + interface web |
| `make worker` | lance un worker Kafka |
| `make ingest URL="https://youtu.be/xxxx" LANGS=fr` | ingestion directe en CLI (sans API/Kafka) |
| `make test` | `pytest -q` |
| `make lint` / `make fmt` | style : vérifier / formater (ruff + black) |

> **Pas de `make` ?** Chaque étape ci-dessous donne l'équivalent PowerShell.

---

## 1. Prérequis

| Outil | Version | Rôle |
|-------|---------|------|
| **Python** | ≥ 3.14 | runtime (voir `pyproject.toml`) |
| **Docker Desktop** | récent | Postgres + MinIO + Kafka |
| **git** | — | cloner le repo |
| **make** | — | *(recommandé)* raccourcis — Windows : `winget install ezwinports.make`, à lancer depuis Git Bash |
| **ffmpeg** | récent | extraction audio par yt-dlp — sur le `PATH`* |

\* ffmpeg n'est requis que pour **ingérer une vraie vidéo** (worker / CLI). Il n'est
**pas** nécessaire pour lancer l'API, naviguer dans l'interface, ni pour les tests.
S'il n'est pas sur le `PATH`, renseigne `TOUMAI_FFMPEG_LOCATION` dans `.env`.

Vérifier :

```powershell
python --version      # 3.14.x
docker --version
ffmpeg -version        # optionnel
make --version         # optionnel (Git Bash)
```

---

## 2. Cloner et installer

Le projet s'installe en mode **editable** : le paquet `media-ingestion-platform`
expose 3 commandes (`toumai-api`, `toumai-worker`, `toumai-ingest`) et l'interface web
(via `toumai-api`). Un **venv est requis** (pas d'install globale). `jinja2` (interface)
fait déjà partie des dépendances.

```bash
git clone <URL_DU_REPO> ScrapperToumAI
cd ScrapperToumAI
make install
```

<details><summary>Équivalent PowerShell (sans make)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Git Bash : source .venv/Scripts/activate
python -m pip install -U pip
pip install -e ".[dev]"
```
</details>

> **Sans activer le venv** : les exécutables sont dans `.\.venv\Scripts\`
> → `.\.venv\Scripts\toumai-api.exe`, etc.
>
> Si un jour l'import échoue avec `ModuleNotFoundError: No module named 'media_ingestion'`,
> l'editable install a été corrompu (dossier `~edia-ingestion-platform` dans
> `site-packages`). Correctif : `pip install -e .` puis supprimer le dossier `~...`.

---

## 3. Configuration (`.env`) — de A à Z

Toutes les variables sont **optionnelles** (préfixe `TOUMAI_`, valeurs par défaut
sensées). Pour partir vite :

```bash
cp .env.example .env      # PowerShell : Copy-Item .env.example .env
```

### 3.1 Le minimum pour l'API + l'interface web

L'API et l'interface s'appuient **toujours sur Postgres** (stockage des jobs + catalogue
vidéos) et sur **Kafka** (file des jobs). Deux réglages activent le mode complet :

```dotenv
TOUMAI_STORAGE_BACKEND=minio      # audio + json dans MinIO (défaut: local disque)
TOUMAI_METADATA_BACKEND=postgres  # catalogue vidéos indexé dans Postgres
```

Les valeurs Kafka / MinIO / Postgres par défaut **collent au `docker-compose`** :
si tu ne changes rien d'autre, tout se connecte tout seul après `make up`.

### 3.2 Référence complète des variables

**Général**

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_DATA_DIR` | `data` | dossier de sortie en stockage local / CLI |
| `TOUMAI_AUDIO_FORMAT` | `wav` | format audio extrait par yt-dlp |
| `TOUMAI_FFMPEG_LOCATION` | *(vide → PATH)* | dossier `bin` de ffmpeg si absent du PATH |
| `TOUMAI_LANGUAGES` | `["fr","en"]` | langues de sous-titres préférées (liste JSON ou `fr,en`) |

**Sélection des sous-titres YouTube** (confiance : manuel > ASR > traduit)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_ACCEPT_YOUTUBE_ASR` | `true` | accepter les sous-titres auto-générés (ASR). `false` = vidéo marquée indisponible si pas de piste manuelle |
| `TOUMAI_ENABLE_TRANSCRIPT_TRANSLATION` | `false` | en dernier recours, traduire une piste vers la langue voulue |

**Throttling / anti-blocage des téléchargements** (lots CSV de centaines de vidéos)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_MAX_CONCURRENT_DOWNLOADS` | `3` | téléchargements simultanés max par worker (2–4 conseillé) |
| `TOUMAI_DOWNLOAD_DELAY_MIN_S` / `_MAX_S` | `1.0` / `4.0` | délai aléatoire (s) avant chaque téléchargement |
| `TOUMAI_DOWNLOAD_MAX_RETRIES` | `5` | reprises auto sur HTTP 429 |
| `TOUMAI_DOWNLOAD_BACKOFF_BASE_S` / `_MAX_S` | `2.0` / `300.0` | backoff exponentiel borné + jitter |
| `TOUMAI_DOWNLOAD_PROXIES` | `[]` | proxies / IP de sortie tournants (round-robin). Liste JSON |

**Stockage des artefacts** (audio + `transcript.json` + `metadata.json`)

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_STORAGE_BACKEND` | `local` | `local` (disque) ou `minio` (S3) |
| `TOUMAI_MINIO_ENDPOINT` | `localhost:9000` | endpoint MinIO **joignable depuis le navigateur** (lecture/ZIP audio via URL présignée) |
| `TOUMAI_MINIO_ACCESS_KEY` / `_SECRET_KEY` | `minioadmin` / `minioadmin` | identifiants MinIO |
| `TOUMAI_MINIO_BUCKET` | `toumai-media` | bucket cible |
| `TOUMAI_MINIO_SECURE` | `false` | `true` si MinIO en HTTPS |

**Catalogue metadata (vidéos)**

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_METADATA_BACKEND` | `none` | `postgres` pour indexer le catalogue vidéos |
| `TOUMAI_POSTGRES_DSN` | `postgresql+psycopg://toumai:toumai@localhost:5432/toumai` | DSN Postgres (jobs + catalogue) |

**Kafka (API ↔ workers)**

| Variable | Défaut | Rôle |
|----------|--------|------|
| `TOUMAI_KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | brokers Kafka |
| `TOUMAI_KAFKA_CONSUMER_GROUP` | `toumai-workers` | groupe de consommateurs |
| `TOUMAI_TOPIC_JOB_REQUESTED` | `job.requested` | topic des jobs à traiter |
| `TOUMAI_TOPIC_JOB_COMPLETED` | `job.completed` | topic des jobs terminés |
| `TOUMAI_TOPIC_JOB_DLQ` | `job.dlq` | dead-letter queue (échecs) |

> Détail exhaustif dans `.env.example` et `src/media_ingestion/config.py`.

---

## 4. Démarrer l'infra (Docker)

```bash
make up      # postgres + minio + kafka + création du bucket
make ps      # tout doit être "healthy"/"running"
```

<details><summary>Équivalent PowerShell</summary>

```powershell
docker compose up -d
docker compose ps
```
</details>

| Service | Adresse | Identifiants |
|---------|---------|--------------|
| Postgres | `localhost:5432` | `toumai` / `toumai` |
| MinIO (S3) | `localhost:9000` | `minioadmin` / `minioadmin` |
| MinIO console | http://localhost:9001 | `minioadmin` / `minioadmin` |
| Kafka | `localhost:9092` | — |

Bucket `toumai-media` créé automatiquement. Arrêt : `make down`
(ou `make clean` pour **effacer** les volumes/données).

---

## 5. Lancer l'application (2 terminaux)

```bash
make api      # terminal 1 : API FastAPI + interface web
make worker   # terminal 2 : worker Kafka (consomme job.requested)
```

<details><summary>Équivalent PowerShell</summary>

```powershell
.\.venv\Scripts\toumai-api.exe        # terminal 1
.\.venv\Scripts\toumai-worker.exe     # terminal 2
```
</details>

→ http://localhost:8000/ — **interface web (dashboard)**
→ http://localhost:8000/docs — Swagger (API JSON)

### Interface web (dashboard)

Ouvre **http://localhost:8000/** (redirige vers `/ui/`). Interface HTMX + Tailwind
servie directement par l'API — aucun build ni serveur front séparé.

| Vue | Contenu |
|-----|---------|
| **Tableau de bord** | KPIs par statut + courbe d'évolution des téléchargements (live) |
| **Jobs** | liste filtrable (auto-refresh 5 s), retry, suppression unitaire **et par sélection** |
| **Upload** | soumettre une URL ou un CSV de lot ; **anti-doublon par `video_id`** |
| **Vidéos** | catalogue ; lecteur audio avec **transcript synchronisé** (clic sur un segment = seek) ; **téléchargement ZIP** (audio + `transcript.json` + `metadata.json`) par sélection ou tout ; suppression unitaire **et par sélection** (efface aussi l'audio MinIO) |

---

## 6. Tester une ingestion

Le plus simple : depuis l'**interface** (§5) → page **Upload** (une URL ou un CSV).
En ligne de commande :

```powershell
# 1) soumettre un job (202 + job_id)
curl.exe -X POST http://localhost:8000/process `
  -H "Content-Type: application/json" `
  -d '{"url":"https://www.youtube.com/watch?v=XXXX","languages":["fr","en"]}'

# 2) suivre son statut
curl.exe http://localhost:8000/jobs
curl.exe http://localhost:8000/jobs/<job_id>

# 3) récupérer le transcript quand le job est terminé
curl.exe http://localhost:8000/jobs/<job_id>/transcript
```

Principaux endpoints API (voir Swagger pour le reste) :

| Méthode | Route | Rôle |
|---------|-------|------|
| `POST` | `/process` | soumet une URL → 202 + `job_id` |
| `POST` | `/process/csv` | ingestion par lot (CSV avec colonne `url`) |
| `GET` | `/jobs` | liste des jobs (filtre `?status=`) |
| `GET` | `/jobs/{id}` | statut d'un job |
| `GET` | `/jobs/{id}/transcript` | transcript du job |
| `POST` | `/jobs/{id}/retry` | relance un job |
| `GET` | `/videos` | catalogue Postgres |
| `GET` | `/health` | healthcheck |

### Alternative : ingestion directe en CLI (sans API/Kafka)

```bash
make ingest URL="https://www.youtube.com/watch?v=XXXX" LANGS=fr
```

<details><summary>Équivalent PowerShell</summary>

```powershell
.\.venv\Scripts\toumai-ingest.exe "https://www.youtube.com/watch?v=XXXX" --lang fr en
```
</details>

Sortie dans `data/<video_id>/` : `metadata.json` (+ `transcript.json` si dispo).

---

## 7. Tests & qualité

```bash
make test     # pytest -q — use case avec fakes : ni réseau ni ffmpeg requis
make lint     # ruff check + black --check
make fmt      # black + ruff --fix
```

<details><summary>Équivalent PowerShell</summary>

```powershell
pytest -q
ruff check src tests
black --check src tests
```
</details>

---

## Notes Windows

- **`make` n'est pas natif.** Le `Makefile` cible Git Bash (`SHELL := bash`) et le venv
  Windows (`.venv/Scripts`). Installe-le via `winget install ezwinports.make`, puis
  utilise-le **depuis Git Bash**. Sans make, suis les blocs PowerShell de chaque étape.
- **Python 3.14 + Kafka.** `kafka-python-ng` plantait au démarrage du worker
  (`ValueError: Invalid file descriptor: -1`) lors du rebalance de groupe. Corrigé par
  le shim `src/media_ingestion/_kafka_compat.py`, appliqué automatiquement via
  `bootstrap.py` — aucune action de ta part.

---

## Documentation

| Doc | Contenu |
|-----|---------|
| [`README.md`](README.md) | vue d'ensemble, stratégie transcript, architecture |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Clean Architecture, ports/adapters |
| [`docs/PHASE2_INFRA.md`](docs/PHASE2_INFRA.md) | MinIO / Postgres / stockage medallion |
| [`docs/PHASE2_API_KAFKA.md`](docs/PHASE2_API_KAFKA.md) | API FastAPI + événements Kafka + worker |
| [`docs/YTDLP_LIMITES.md`](docs/YTDLP_LIMITES.md) | limites/pièges yt-dlp |
















