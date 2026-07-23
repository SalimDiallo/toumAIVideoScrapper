# Phase 2 — API FastAPI + Kafka + Worker

Découple le déclenchement (HTTP) de l'exécution (workers) via Kafka. Les jobs sont
asynchrones : l'API accepte et renvoie un `job_id`, un worker fait le travail, et le
statut est suivi en base.

## Flux

```
POST /process {url}
   └─ crée un job (PENDING) en Postgres
   └─ publie "job.requested" sur Kafka         ──► 202 {job_id}
                                   │
                          toumai-worker (consumer)
                                   │  status=RUNNING
                                   │  IngestVideoUseCase.execute()
                        ┌──────────┴───────────┐
                     succès                   échec
              status=COMPLETED           status=FAILED
              + "job.completed"          + "job.dlq"
GET /jobs/{job_id} ──► lit le statut (Postgres)
```

## Composants

| Élément                     | Rôle                                                        |
| --------------------------- | ----------------------------------------------------------- |
| `api/app.py`                | FastAPI : `POST /process`, `GET /jobs/{id}`, `GET /health`  |
| `adapters/kafka_producer.py`| `EventPublisherPort` (kafka-python-ng)                      |
| `worker/consumer.py`        | boucle Kafka `job.requested` → `JobHandler`                 |
| `worker/handler.py`         | logique d'un job (transport-agnostique, testable)          |
| `adapters/postgres_jobs.py` | `JobStorePort` : table `jobs` (statut cross-process)       |

Topics : `job.requested`, `job.completed`, `job.dlq` (auto-créés par le broker).

## Prérequis

L'API et le worker ont besoin de **Postgres** (statut des jobs) et **Kafka**.
Pour une vraie ingestion, active aussi **MinIO** :

```env
TOUMAI_STORAGE_BACKEND=minio
TOUMAI_METADATA_BACKEND=postgres
```

## Démarrer

```bash
docker compose up -d                      # postgres + minio + kafka
.venv\Scripts\toumai-api                  # API sur http://localhost:8000
.venv\Scripts\toumai-worker               # worker (dans un autre terminal)
```

Lancer plusieurs `toumai-worker` = **scaling horizontal** : Kafka répartit les jobs
entre les consommateurs du même groupe (`toumai-workers`).

## Utiliser

```bash
# soumettre un job
curl -X POST http://localhost:8000/process ^
  -H "Content-Type: application/json" ^
  -d "{\"url\": \"https://www.youtube.com/watch?v=XXXX\", \"languages\": [\"fr\"]}"
# -> {"job_id":"...","status":"pending"}

# suivre
curl http://localhost:8000/jobs/<job_id>
# -> status pending -> running -> completed (+ result_uri s3://...)
```

Doc interactive : http://localhost:8000/docs (Swagger UI).

## Table `jobs`

| Colonne      | Type        | Note                              |
| ------------ | ----------- | --------------------------------- |
| `job_id`     | PK          | uuid hex                          |
| `url`        | text        |                                   |
| `languages`  | text[]      |                                   |
| `status`     | str         | pending/running/completed/failed  |
| `result_uri` | text        | `s3://…` une fois terminé         |
| `error`      | text        | message si failed                 |
| `created_at` | timestamptz |                                   |
| `updated_at` | timestamptz |                                   |

## Pourquoi ça reste propre

Le use-case d'ingestion **n'a pas changé**. L'API et le worker sont juste de nouveaux
*points d'entrée* branchés sur les mêmes ports. Kafka et Postgres sont des adapters
derrière `EventPublisherPort` / `JobStorePort` — remplaçables sans toucher au métier.
