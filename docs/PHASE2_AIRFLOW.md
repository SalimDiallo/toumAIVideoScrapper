# Phase 2 — Airflow (orchestration des étapes)

Airflow orchestre l'ingestion **étape par étape**, chaque étape étant une tâche
retriable avec SLA. C'est la source de vérité de l'ordre des étapes et de la reprise.

```
download >> transcript >> store >> index
```

- **download** : yt-dlp (audio + métadonnées). SLA 30 min.
- **transcript** : sous-titres YouTube (manual > ASR > traduction).
- **store** : écrit dans MinIO (Bronze) — ou local.
- **index** : upsert dans Postgres (table `videos`).

Retries : 3 par tâche, backoff exponentiel. Si `store` échoue, on **relance à partir
de `store`** sans refaire `download`/`transcript` (reprise fine dans l'UI Airflow).

## Où est la logique

| Fichier                                   | Rôle                                             |
| ----------------------------------------- | ------------------------------------------------ |
| `dags/ingest_video_dag.py`                | le DAG (TaskFlow API) — seul fichier qui importe Airflow |
| `src/media_ingestion/orchestration/steps.py` | la logique de chaque étape (testable sans Airflow) |
| `src/media_ingestion/orchestration/serde.py` | (dé)sérialisation JSON pour passer l'état via XCom |

Les étapes réutilisent **les mêmes ports/adapters** que le CLI et le worker Kafka —
zéro duplication de métier.

## Démarrer Airflow

Optionnel (profile dédié pour ne pas alourdir le `up` normal) :

```bash
docker compose up -d                       # postgres + minio + kafka
docker compose --profile airflow up -d     # + Airflow (init, scheduler, webserver)
```

UI : http://localhost:8080 — **admin / admin**.

> Les tâches Airflow atteignent `minio:9000` et `postgres:5432` par nom de service
> (variables `TOUMAI_*` injectées dans les conteneurs Airflow via le compose).
> `ffmpeg` n'est pas dans l'image Airflow → l'audio reste au format natif (webm/m4a),
> ce que le downloader gère déjà.

## Lancer une ingestion

Dans l'UI (DAG `ingest_video` → **Trigger DAG w/ config**) :

```json
{ "url": "https://www.youtube.com/watch?v=XXXX", "languages": ["fr"] }
```

ou en CLI dans le conteneur :

```bash
docker exec toumai-airflow-scheduler \
  airflow dags trigger ingest_video \
  --conf '{"url":"https://www.youtube.com/watch?v=XXXX","languages":["fr"]}'
```

Suis l'exécution dans la **Grid View** : chaque étape verte = tâche réussie ; une étape
rouge peut être relancée seule (Clear) une fois le problème corrigé.

## Deux chemins d'exécution (rappel)

| Chemin                       | Quand                                   |
| ---------------------------- | --------------------------------------- |
| **API → Kafka → worker**     | temps réel / à la demande (1 job = 1 message) |
| **Airflow DAG**              | orchestration visible, retry/SLA/reprise par étape, batch |

Les deux partagent le même cœur métier (ports/adapters).

## Arrêter

```bash
docker compose --profile airflow down      # stoppe Airflow
docker compose down -v                     # tout + volumes
```
