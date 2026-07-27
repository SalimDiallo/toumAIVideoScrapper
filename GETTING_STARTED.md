# Démarrage — TOUMAI Media Ingestion

Guide pour repartir **d'un clone tout neuf** (aucun `.venv`, aucune infra lancée)
jusqu'à une ingestion YouTube de bout en bout (API → Kafka → worker → Postgres/MinIO).

> Commandes données pour **Windows / PowerShell** (l'environnement de dev par défaut).
> L'équivalent Git Bash / Linux est indiqué quand il diffère.

---

## 1. Prérequis

| Outil | Version | Rôle |
|-------|---------|------|
| **Python** | ≥ 3.14 | runtime (voir `pyproject.toml`) |
| **ffmpeg** | récent | extraction audio par yt-dlp — doit être sur le `PATH`* |
| **Docker Desktop** | récent | Postgres + MinIO + Kafka |
| **git** | — | cloner le repo |

\* Si ffmpeg n'est pas sur le `PATH`, renseigne `TOUMAI_FFMPEG_LOCATION` dans `.env`
(voir `.env.example`). ffmpeg n'est **pas** nécessaire pour les tests ni pour lancer
l'API/worker à vide — seulement pour ingérer une vraie vidéo.

Vérifier :

```powershell
python --version      # 3.14.x
ffmpeg -version        # optionnel
docker --version
```

---

## 2. Cloner et installer

Le projet s'installe en mode **editable** (`pip install -e`) : le paquet
`media-ingestion-platform` expose 3 commandes (`toumai-api`, `toumai-worker`,
`toumai-ingest`). Un **venv est requis** (pas d'install globale).

```powershell
git clone <URL_DU_REPO> ScrapperToumAI
cd ScrapperToumAI

python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Git Bash : source .venv/Scripts/activate
python -m pip install -U pip
pip install -e ".[dev]"
```

> **Sans activer le venv** (utile pour les scripts) : les exécutables sont dans
> `.\.venv\Scripts\` → `.\.venv\Scripts\toumai-api.exe`, etc.

Si un jour l'import échoue avec `ModuleNotFoundError: No module named 'media_ingestion'`,
c'est que l'editable install a été corrompu (dossier `~edia-ingestion-platform` dans
`site-packages`). Correctif : `pip install -e .` puis supprimer le dossier `~...`.

---

## 3. Configuration (`.env`)

Toutes les variables ont des valeurs par défaut. Pour le mode complet
(API + Kafka + MinIO + Postgres) :

```powershell
Copy-Item .env.example .env
```

Puis dans `.env`, active les backends Phase 2 :

```dotenv
TOUMAI_STORAGE_BACKEND=minio
TOUMAI_METADATA_BACKEND=postgres
# les valeurs Kafka / MinIO / Postgres par défaut collent au docker-compose
```

> Préfixe des variables : `TOUMAI_`. Détail complet dans `.env.example` et `config.py`.

---

## 4. Démarrer l'infra (Docker)

```powershell
docker compose up -d          # postgres + minio + kafka + création du bucket
docker compose ps             # tout doit être "healthy"/"running"
```

| Service | Adresse | Identifiants |
|---------|---------|--------------|
| Postgres | `localhost:5432` | `toumai` / `toumai` |
| MinIO (S3) | `localhost:9000` | `minioadmin` / `minioadmin` |
| MinIO console | http://localhost:9001 | `minioadmin` / `minioadmin` |
| Kafka | `localhost:9092` | — |

Bucket `toumai-media` créé automatiquement au démarrage.

Arrêt : `docker compose down` (ajouter `-v` pour **effacer** les volumes/données).

---

## 5. Lancer l'application (2 terminaux)

**Terminal 1 — API FastAPI**

```powershell
.\.venv\Scripts\toumai-api.exe        # ou: toumai-api  (venv activé)
```
→ http://localhost:8000/docs (Swagger)

**Terminal 2 — worker Kafka** (consomme `job.requested`)

```powershell
.\.venv\Scripts\toumai-worker.exe     # ou: toumai-worker
```

---

## 6. Tester une ingestion

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

Principaux endpoints (voir Swagger pour le reste) :

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

```powershell
toumai-ingest "https://www.youtube.com/watch?v=XXXX" --lang fr en
```
Sortie dans `data/<video_id>/` : `metadata.json` (+ `transcript.json` si dispo).

---

## 7. Tests

```powershell
pytest -q         # use case avec fakes : ni réseau ni ffmpeg requis
```

Qualité de code : `ruff check src tests` et `black --check src tests`.

---

## Notes Windows

- **`make` n'est pas natif.** Le `Makefile` n'est qu'un raccourci vers les commandes
  ci-dessus. Pour l'utiliser : `winget install ezwinports.make` (puis rouvrir le
  terminal, depuis Git Bash). Sinon, appelle directement `.\.venv\Scripts\toumai-*.exe`.
- **Python 3.14 + Kafka.** `kafka-python-ng` plantait au démarrage du worker
  (`ValueError: Invalid file descriptor: -1`) lors du rebalance de groupe. C'est
  corrigé par le shim `src/media_ingestion/_kafka_compat.py`, appliqué automatiquement
  via `bootstrap.py` — aucune action de ta part.

---

## Documentation

| Doc | Contenu |
|-----|---------|
| [`README.md`](README.md) | vue d'ensemble, stratégie transcript, architecture |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Clean Architecture, ports/adapters |
| [`docs/PHASE2_INFRA.md`](docs/PHASE2_INFRA.md) | MinIO / Postgres / stockage medallion |
| [`docs/PHASE2_API_KAFKA.md`](docs/PHASE2_API_KAFKA.md) | API FastAPI + événements Kafka + worker |
| [`docs/YTDLP_LIMITES.md`](docs/YTDLP_LIMITES.md) | limites/pièges yt-dlp |
