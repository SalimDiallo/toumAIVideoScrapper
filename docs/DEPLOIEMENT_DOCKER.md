# TOUMAI — Déploiement en conteneur

Guide pour conteneuriser et déployer TOUMAI (API + worker + veille). À la date de
rédaction, le dépôt **ne contient pas encore de Dockerfile** : `docker-compose.yml`
ne lance que l'**infra** (Postgres, MinIO, Kafka, Airflow) et l'app tourne sur l'hôte.
Ce document fournit tout le nécessaire (Dockerfile, `.dockerignore`, services compose,
variables) pour faire tourner l'app **elle-même** dans des conteneurs.

> ⚠️ Deux pièges réseau doivent être corrigés pour containeriser (Kafka & MinIO) —
> voir §4 et §7. Ne pas les traiter = worker qui ne se connecte pas / lecteur audio
> cassé.

---

## 1. Ce qui tourne dans quels conteneurs

| Conteneur | Commande | Rôle | Expose |
|---|---|---|---|
| `toumai-api` | `toumai-api` | API REST + dashboard | 8000 |
| `toumai-worker` | `toumai-worker` | consomme Kafka, télécharge, transcrit | — (scalable) |
| `postgres` | — | catalogue + jobs + veille | 5432 |
| `kafka` | — | bus d'events | 9092 |
| `minio` | — | stockage objet (si `STORAGE_BACKEND=minio`) | 9000/9001 |
| `airflow-*` | — | ordonnanceur veille quotidienne (optionnel) | 8080 |

L'image `toumai-api` et `toumai-worker` est **la même** (même code, entrypoint
différent). La veille (`toumai-veille`) se lance dans un conteneur one-shot ou via
Airflow (`POST /veille/run`).

Contraintes de l'image applicative :
- **Python ≥ 3.14** (défini dans `pyproject.toml`).
- **ffmpeg** obligatoire pour le ré-encodage audio (sinon audio natif conservé, cf. doc
  technique §4.2).

---

## 2. `Dockerfile` (à créer à la racine)

Build multi-étages : on installe le projet dans un venv, puis on copie ce venv dans une
image finale légère avec ffmpeg.

```dockerfile
# ---------- build ----------
FROM python:3.14-slim AS build
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# métadonnées + code (hatchling a besoin du package pour builder)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# ---------- runtime ----------
FROM python:3.14-slim AS runtime
# ffmpeg = ré-encodage audio ; tini = init PID 1 propre (signaux/zombies)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
# utilisateur non-root
RUN useradd -m -u 1000 toumai
USER toumai
WORKDIR /home/toumai/app
# volume pour le stockage local (si STORAGE_BACKEND=local)
ENV TOUMAI_DATA_DIR=/data
ENTRYPOINT ["tini", "--"]
# par défaut : l'API. Le worker surcharge la commande (voir compose).
CMD ["toumai-api"]
```

`toumai-api` écoute déjà sur `0.0.0.0:8000` (cf. `api/app.py:main`), donc joignable
depuis l'extérieur du conteneur sans réglage supplémentaire.

---

## 3. `.dockerignore` (à créer à la racine)

Évite d'envoyer le venv local, les données et le cache dans le contexte de build.

```gitignore
.venv/
data/
__pycache__/
*.pyc
.git/
.pytest_cache/
.ruff_cache/
.mypy_cache/
tests/
airflow/logs/
compose-output.txt
*.env
cookies.txt
```

---

## 4. Correction Kafka (indispensable pour containeriser)

Le `docker-compose.yml` actuel annonce **une seule** adresse :

```yaml
KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://localhost:9092
```

Un client **dans un conteneur** (API/worker) se connecte à `kafka:9092`, mais Kafka lui
renvoie l'adresse annoncée `localhost:9092` → le conteneur tape sur lui-même → **échec**.
Il faut **deux listeners** : un interne (pour les conteneurs) et un externe (pour l'hôte).

Remplace le bloc `environment` du service `kafka` par :

```yaml
    environment:
      KAFKA_NODE_ID: 1
      KAFKA_PROCESS_ROLES: broker,controller
      # deux listeners applicatifs : INTERNAL (réseau compose) + EXTERNAL (hôte)
      KAFKA_LISTENERS: INTERNAL://:29092,EXTERNAL://:9092,CONTROLLER://:9093
      KAFKA_ADVERTISED_LISTENERS: INTERNAL://kafka:29092,EXTERNAL://localhost:9092
      KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT
      KAFKA_INTER_BROKER_LISTENER_NAME: INTERNAL
      KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
      KAFKA_CONTROLLER_QUORUM_VOTERS: 1@localhost:9093
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 1
      KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 1
      KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
      KAFKA_NUM_PARTITIONS: "3"
```

Résultat :
- Depuis un **conteneur** : `TOUMAI_KAFKA_BOOTSTRAP_SERVERS=kafka:29092`.
- Depuis l'**hôte** (CLI de dev) : `localhost:9092` (inchangé).

---

## 5. Services applicatifs pour `docker-compose.yml`

Ajoute ces deux services (build de l'image locale). Ils démarrent après Postgres/Kafka.

```yaml
  toumai-api:
    build: .
    image: toumai-app
    container_name: toumai-api
    depends_on:
      postgres:
        condition: service_healthy
      kafka:
        condition: service_started
    env_file: .env
    environment:
      # surcharge les hôtes "localhost" du .env par les noms de services compose
      TOUMAI_KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      TOUMAI_POSTGRES_DSN: postgresql+psycopg://toumai:toumai@postgres:5432/toumai
      TOUMAI_METADATA_BACKEND: postgres
      TOUMAI_MINIO_ENDPOINT: minio:9000
    ports:
      - "8000:8000"
    volumes:
      - toumai-data:/data          # utile seulement si STORAGE_BACKEND=local
    restart: unless-stopped

  toumai-worker:
    build: .
    image: toumai-app
    command: ["toumai-worker"]
    depends_on:
      postgres:
        condition: service_healthy
      kafka:
        condition: service_started
    env_file: .env
    environment:
      TOUMAI_KAFKA_BOOTSTRAP_SERVERS: kafka:29092
      TOUMAI_POSTGRES_DSN: postgresql+psycopg://toumai:toumai@postgres:5432/toumai
      TOUMAI_METADATA_BACKEND: postgres
      TOUMAI_MINIO_ENDPOINT: minio:9000
    volumes:
      - toumai-data:/data
    restart: unless-stopped
    deploy:
      replicas: 1                   # voir §8 pour scaler

volumes:
  # ... pgdata, miniodata déjà présents ...
  toumai-data:
```

> `env_file: .env` charge tes réglages ; le bloc `environment` **écrase** juste les
> adresses `localhost` par les noms de services internes. Ne mets pas de valeurs
> `localhost` réseau dans le `.env` si tu comptes surtout tourner en conteneur.

---

## 6. Variables d'environnement (rappel des hôtes)

En conteneur, les `localhost` du `.env` deviennent des **noms de services** :

| Variable | Sur l'hôte | En conteneur (réseau compose) |
|---|---|---|
| `TOUMAI_KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | `kafka:29092` |
| `TOUMAI_POSTGRES_DSN` | `…@localhost:5432/…` | `…@postgres:5432/…` |
| `TOUMAI_MINIO_ENDPOINT` | `localhost:9000` | `minio:9000` (voir §7) |
| `TOUMAI_METADATA_BACKEND` | `none`/`postgres` | **`postgres`** (requis pour l'API) |
| `TOUMAI_KAFKA_API_VERSION` | `2.5.0` | `2.5.0` (garder figé) |

Le reste (`LANGUAGES`, throttling `DOWNLOAD_*`, cookies, Webshare, ASR…) est identique à
la doc technique §9. **Cookies/ffmpeg** : ffmpeg est dans l'image ; pour les cookies,
monte un `cookies.txt` en volume et pointe `TOUMAI_YTDLP_COOKIES_FILE=/…/cookies.txt`
(l'extraction depuis un navigateur n'a **aucun sens** dans un conteneur headless).

---

## 7. Piège MinIO (URLs présignées)

`MinioStorage` renvoie au navigateur une **URL présignée** construite avec
`TOUMAI_MINIO_ENDPOINT`. Si l'endpoint est `minio:9000`, le navigateur **sur l'hôte** ne
sait pas résoudre `minio` → le lecteur audio et les téléchargements cassent.

Options :
- **Rester en stockage local** (`STORAGE_BACKEND=local`) + volume partagé `toumai-data`
  entre API et worker (le plus simple pour un déploiement mono-machine).
- **Utiliser MinIO** : exposer un endpoint **résoluble par le navigateur** (ex.
  `TOUMAI_MINIO_ENDPOINT=localhost:9000` en dev, ou un vrai domaine derrière un reverse
  proxy en prod), et faire pointer l'app dessus. Le worker et l'API doivent alors
  atteindre ce même endpoint.

---

## 8. Commandes

```bash
# build de l'image applicative
docker compose build

# démarrer l'infra + l'app
docker compose up -d

# API dispo sur http://localhost:8000/docs et le dashboard sur http://localhost:8000/

# suivre les logs du worker
docker compose logs -f toumai-worker

# scaler les workers (Kafka a 3 partitions -> jusqu'à 3 consumers actifs utiles)
docker compose up -d --scale toumai-worker=3

# lancer une passe de veille one-shot
docker compose run --rm toumai-worker toumai-veille

# ingestion CLI directe dans un conteneur jetable
docker compose run --rm toumai-worker toumai-ingest "https://youtu.be/XXXX" --lang fr
```

> **Scaling worker** : le broker est configuré avec `KAFKA_NUM_PARTITIONS=3`. Au-delà de
> 3 workers dans le même `KAFKA_CONSUMER_GROUP`, les workers supplémentaires restent
> **inactifs** (1 partition = 1 consumer). Augmente `KAFKA_NUM_PARTITIONS` pour scaler
> davantage. Chaque worker télécharge en plus `MAX_CONCURRENT_DOWNLOADS` vidéos en
> parallèle (threads).

---

## 9. Healthchecks (optionnel mais recommandé)

L'API expose `GET /health`. Ajoute au service `toumai-api` :

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"]
      interval: 10s
      timeout: 3s
      retries: 5
```

(On utilise Python plutôt que `curl`, absent de l'image slim.)

---

## 10. Airflow (veille quotidienne)

Le DAG (`airflow/dags/veille_youtube.py`) fait un `POST /veille/run` sur l'API via la
connexion `AIRFLOW_CONN_TOUMAI_API`. Aujourd'hui elle pointe sur
`http://host.docker.internal:8000` (API sur l'hôte). Si l'API tourne **aussi en
conteneur** dans le même compose, change-la pour le nom de service :

```yaml
      AIRFLOW_CONN_TOUMAI_API: http://toumai-api:8000
```

---

## 11. Limites & points d'attention en conteneur

- **Kafka advertised listeners** : le défaut `localhost:9092` empêche tout client
  conteneurisé (§4). C'est le blocage n°1.
- **MinIO présigné** : l'endpoint doit être joignable par le **navigateur**, pas
  seulement par l'app (§7).
- **Stockage local partagé** : si API et worker sont dans des conteneurs séparés en
  local storage, ils **doivent** monter le même volume `toumai-data`, sinon l'API ne
  retrouve pas l'audio produit par le worker.
- **Cookies navigateur** inutilisables en conteneur headless → uniquement `cookies.txt`
  monté en volume.
- **Pas d'auth** sur l'API/dashboard : ne pas exposer le port 8000 publiquement sans
  reverse proxy + authentification devant.
- **Ressources** : le worker est I/O-bound (réseau) ; dimensionne surtout la bande
  passante et le disque `/data`. Prévoir la place pour l'audio `wav` (volumineux — un
  format compressé via `TOUMAI_AUDIO_FORMAT` réduit l'empreinte).
- **Image non publiée** : pas de registre configuré. Pour un déploiement multi-machine,
  `docker build -t <registry>/toumai-app:tag .` puis `docker push`, et référence l'image
  au lieu de `build: .`.

---

## 12. Checklist de déploiement

1. Créer `Dockerfile` (§2) et `.dockerignore` (§3) à la racine.
2. Corriger les listeners Kafka dans `docker-compose.yml` (§4).
3. Ajouter les services `toumai-api` / `toumai-worker` + volume `toumai-data` (§5).
4. Préparer un `.env` (copie de `.env.example`) — hôtes internes surchargés via compose.
5. Choisir le stockage : local (volume partagé) **ou** MinIO (endpoint joignable navigateur).
6. `docker compose build && docker compose up -d`.
7. Vérifier `http://localhost:8000/health` puis le dashboard.
8. (Optionnel) Airflow : `AIRFLOW_CONN_TOUMAI_API=http://toumai-api:8000`.
```
