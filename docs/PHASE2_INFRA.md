# Phase 2 — Infra & stockage (MinIO + Postgres)

Cette brique remplace le stockage disque local par du stockage **industriel**, sans
toucher au use-case (grâce aux ports). Deux backends deviennent activables par config :

- **Stockage** : `local` (défaut) → `minio` (couche Bronze du medallion).
- **Catalogue metadata** : `none` (défaut) → `postgres` (index requêtable des vidéos).

## 1. Démarrer l'infra

Prérequis : **Docker Desktop lancé**.

```bash
docker compose up -d
```

Services :

| Service   | URL / port                    | Identifiants            |
| --------- | ----------------------------- | ----------------------- |
| Postgres  | `localhost:5432` (db `toumai`)| `toumai` / `toumai`     |
| MinIO S3  | `localhost:9000`              | `minioadmin` / `minioadmin` |
| MinIO UI  | http://localhost:9001         | `minioadmin` / `minioadmin` |

Le bucket `toumai-media` est créé automatiquement au démarrage.

## 2. Activer les backends

Dans `.env` (copié depuis `.env.example`) :

```env
TOUMAI_STORAGE_BACKEND=minio
TOUMAI_METADATA_BACKEND=postgres
```

(Les endpoints/identifiants par défaut correspondent déjà au docker-compose.)

## 3. Lancer une ingestion

```bash
.venv\Scripts\toumai-ingest "https://www.youtube.com/watch?v=XXXX" --lang fr
```

Résultat :
- **MinIO** : `toumai-media/bronze/<langue>/<video_id>/{metadata.json, transcript.json, <audio>}`
  (l'audio local de staging est supprimé après upload).
- **Postgres** : une ligne dans la table `videos` (upsert idempotent par `video_id`).

## 4. Vérifier

MinIO : ouvrir http://localhost:9001 → bucket `toumai-media`.

Postgres :
```bash
docker exec -it toumai-postgres psql -U toumai -d toumai -c "SELECT video_id, language, transcript_status, storage_uri FROM videos;"
```

## 5. Comment ça reste propre (Clean Archi)

Le use-case ne connaît que les **ports** :
- `StoragePort.save(result, language) -> str` → implémenté par `LocalJsonStorage` **ou** `MinioStorage`.
- `MetadataRepositoryPort.upsert(result, language, storage_uri)` → `PostgresMetadataRepository` (ou rien).

Changer de backend = changer une variable d'env. **Aucune ligne du use-case ne bouge.**

## 6. Arrêter / nettoyer

```bash
docker compose down          # stoppe les conteneurs
docker compose down -v       # + supprime les volumes (données Postgres/MinIO)
```

## Schéma de la table `videos`

| Colonne             | Type        | Note                                   |
| ------------------- | ----------- | -------------------------------------- |
| `video_id`          | PK          | ID YouTube                             |
| `url`               | text        |                                        |
| `title`             | text        |                                        |
| `channel`           | text        |                                        |
| `duration_s`        | int         |                                        |
| `upload_date`       | str         | `YYYYMMDD`                             |
| `language`          | str (index) | langue du dossier/objet                |
| `transcript_status` | str         | `available` / `unavailable`            |
| `transcript_source` | str         | `youtube_manual` / `youtube_asr` / …   |
| `storage_uri`       | text        | `s3://toumai-media/bronze/…`           |
| `created_at`        | timestamptz |                                        |
