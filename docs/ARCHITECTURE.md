# TOUMAI — Comment ça marche (Phase 1 / MVP)

Ce document explique en détail ce qu'on a construit : l'architecture, le trajet d'une
commande, le téléchargement audio, la récupération du transcript, le rangement par langue
et le stockage.

---

## 0. Ce que fait le projet en une phrase

À partir d'une URL YouTube : on télécharge **l'audio** + les **métadonnées**, on récupère le
**transcript fourni par YouTube** (s'il existe), et on range le tout dans un dossier **par langue**.

Exemple de sortie réelle :

```
data/
├── ar/
│   └── syJv_uzgofM/
│       ├── metadata.json
│       ├── transcript.json      (transcript arabe trouvé)
│       └── syJv_uzgofM.webm
└── fr/
    ├── x5ppD9fMjag/
    │   ├── metadata.json
    │   ├── transcript.json
    │   └── x5ppD9fMjag.webm
    └── LM7rtFJcnG8/
        ├── metadata.json        (transcript_status = unavailable)
        └── LM7rtFJcnG8.webm      (pas de transcript.json : aucun sous-titre)
```

---

## 1. L'idée d'architecture (le « pourquoi »)

Le code est séparé en **couches**, du plus abstrait au plus concret :

```
domain/        ← vocabulaire métier (entités) + CONTRATS (ports). Zéro techno.
application/   ← logique d'orchestration (le use-case). Ne connaît QUE les contrats.
adapters/      ← implémentations concrètes (yt-dlp, youtube-api, disque…).
config.py      ← réglages (env / .env)
cli.py         ← point d'entrée qui « branche » les adapters dans le use-case
```

**Règle d'or :** les dépendances pointent vers l'intérieur. Le use-case ignore que l'audio
vient de yt-dlp ou que le stockage est du disque. Il ne connaît que des **interfaces** (*ports*) :

| Port                    | Contrat                                              |
| ----------------------- | ---------------------------------------------------- |
| `AudioDownloaderPort`   | `download(url) -> (metadata, audio)`                 |
| `TranscriptProviderPort`| `fetch(video_id, langues) -> Transcript \| None`     |
| `SpeechToTextPort`      | `transcribe(audio) -> Transcript` (débranché, Phase 2)|
| `StoragePort`           | `save(result, langue) -> chemin`                     |

**Bénéfice concret pour le scale (Phase 2) :** remplacer le disque par MinIO = écrire un
nouvel adapter `MinioStorage` avec la même méthode `save()`. Le use-case ne change pas une
ligne. Idem pour brancher faster-whisper sur le port STT. C'est ça qui rend l'évolution peu coûteuse.

---

## 2. Le trajet d'une commande (bout en bout)

Commande : `toumai-ingest "…url…" --lang fr`

```
cli.py
  └─ lit la config (Settings, préfixe TOUMAI_)
  └─ build_use_case() : instancie les 3 adapters concrets et les injecte dans le use-case
  └─ use_case.execute(url, langues)
        1. downloader.download(url)      → yt-dlp télécharge l'audio + métadonnées
        2. transcript_provider.fetch()   → sous-titres YouTube ? -> Transcript ou None
        3. décision :
             transcript trouvé        → status = available
             sinon + STT branché      → transcribe()  (Phase 2, désactivé)
             sinon                     → status = unavailable (on ne transcrit pas)
        4. résout la langue du dossier
        5. storage.save(result, langue) → écrit metadata.json (+ transcript.json)
                                          et DÉPLACE l'audio dans le dossier
```

---

## 3. Le téléchargement en détail (yt-dlp)

Fichier : `adapters/ytdlp_downloader.py`

### a) Configuration de yt-dlp
```python
opts = {
    "format": "bestaudio/best",              # meilleure piste AUDIO seule
    "outtmpl": ".../data/%(id)s.%(ext)s",    # gabarit de nom de fichier
    "quiet": True, "no_warnings": True,
    "noplaylist": True,                       # une URL de playlist ne prend qu'1 vidéo
}
```

- `bestaudio/best` : yt-dlp lit la page YouTube, découvre **tous les flux disponibles**, et
  choisit la meilleure piste **audio sans vidéo** (économise la bande passante). Le `/best`
  est un repli si aucune piste audio isolée n'existe.
- YouTube sert cet audio dans un **conteneur** `.webm` (codec Opus) ou `.m4a` (codec AAC) →
  d'où les fichiers `.webm`.
- `outtmpl` : `%(id)s` = ID YouTube (ex. `syJv_uzgofM`), `%(ext)s` = extension réelle.

### b) Extraction audio optionnelle (ffmpeg)
```python
if has_ffmpeg:
    opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}]
```
- Un **post-processeur** tourne *après* le téléchargement. `FFmpegExtractAudio` ré-encode le
  `.webm`/`.m4a` en `.wav`. Ça **exige ffmpeg** (l'outil qui décode/réencode l'audio).
- **Pourquoi wav ?** Pour la Phase 2 STT : faster-whisper préfère du PCM propre (wav 16 kHz mono).

### c) Le fallback qu'on a codé (panne ffmpeg)
```python
has_ffmpeg = self._ffmpeg_location is not None or shutil.which("ffmpeg") is not None
```
- `shutil.which("ffmpeg")` cherche ffmpeg dans le PATH. Absent **et** pas de
  `TOUMAI_FFMPEG_LOCATION` → `has_ffmpeg = False`.
- Dans ce cas on **n'ajoute pas** le post-processeur → yt-dlp garde l'audio **natif** (`.webm`),
  **pas de plantage**. C'est ce qui s'est passé pour la vidéo `ar`.
- Sinon, on récupère le vrai chemin via `info["requested_downloads"][0]["filepath"]`.

### d) Déclenchement + métadonnées
```python
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(video_url, download=True)
```
- `extract_info(..., download=True)` fait **deux choses** : scrape la page (titre, chaîne,
  durée, date, langue…) **et** télécharge. Le `info` renvoyé est un gros dictionnaire.
- On en extrait un objet propre `VideoMetadata` → c'est le contenu de `metadata.json`.

> **Résumé :** yt-dlp = le navigateur qui trouve et aspire l'audio + les infos ;
> ffmpeg = le convertisseur audio optionnel. ffmpeg est non-bloquant pour que le MVP
> tourne dans tous les cas.

---

## 4. Le transcript en détail (youtube-transcript-api)

Fichier : `adapters/youtube_transcript.py`

**On ne fabrique aucun transcript** : on récupère celui que **YouTube possède déjà**
(sous-titres manuels de l'auteur *ou* auto-générés par YouTube).

```python
api = YouTubeTranscriptApi()
fetched = api.fetch(video_id, languages=["fr", ...])   # essaie fr, puis les suivantes
raw = [{"text", "start", "duration"} pour chaque segment]
```

- On passe une liste de langues **par ordre de préférence** ; l'API renvoie la première dispo.
- Chaque sous-titre est un **segment** (`start` = seconde de début, `duration`). On mappe en
  `TranscriptSegment`, et `.text` recolle tout le texte.
- **Règle clé :** si aucun sous-titre (désactivés, inexistants, erreur réseau), l'appel lève
  une exception → on renvoie **`None`**. Le use-case met `status = unavailable` et **n'écrit
  pas** de `transcript.json`.

---

## 5. La langue du dossier

Dans le use-case, `_resolve_language()` choisit le nom du dossier par priorité :

```
langue du transcript  →  langue détectée par yt-dlp  →  1re langue de --lang  →  "unknown"
```

puis normalise `fr-FR` → `fr`. C'est pourquoi la vidéo arabe est allée dans `data/ar/`
**même avec `--lang fr`** : son transcript réel est en `ar`, qui l'emporte.

---

## 6. Le stockage (regroupement par langue)

Fichier : `adapters/local_storage.py` — `save(result, language)` :

1. crée `data/<langue>/<video_id>/`,
2. **déplace** l'audio (téléchargé à la racine de `data/`) dans ce dossier (`shutil.move`),
3. écrit `metadata.json` (chemin final de l'audio + `transcript_status`),
4. écrit `transcript.json` **seulement si** un transcript existe.

---

## 7. La configuration

`config.py` (pydantic-settings) : tout est réglable par variable d'env préfixée `TOUMAI_`
ou via `.env`, avec des défauts sains :

| Variable                 | Rôle                                   | Défaut       |
| ------------------------ | -------------------------------------- | ------------ |
| `TOUMAI_DATA_DIR`        | dossier racine de sortie               | `data`       |
| `TOUMAI_AUDIO_FORMAT`    | format cible si ffmpeg présent         | `wav`        |
| `TOUMAI_LANGUAGES`       | langues de sous-titres préférées       | `["fr","en"]`|
| `TOUMAI_FFMPEG_LOCATION` | dossier bin de ffmpeg (si pas au PATH) | `None`       |
| `TOUMAI_ENABLE_STT`      | activer faster-whisper (Phase 2)       | `false`      |

Aucune valeur en dur = « config-driven », indispensable pour l'industrialisation Phase 2.

---

## 8. Limites actuelles (assumées, MVP)

- **Séquentiel**, une vidéo par commande (la file d'attente = Kafka, Phase 2).
- Audio en `.webm` tant que ffmpeg n'est pas dans le PATH (l'installer pour du `.wav`).
- Stockage disque local (→ MinIO), pas de base de données (→ Postgres), pas d'idempotence/
  reprise (→ Airflow).
- STT débranché : le port existe, l'adapter faster-whisper est un placeholder prêt à coder.

---

## 9. Roadmap Phase 2

- **Kafka** : mise en file des vidéos (`job.requested`, `step.completed`, DLQ).
- **Airflow** : orchestration, retry/SLA/reprise, ordre des étapes.
- **MinIO** : stockage medallion (Bronze brut / Silver Parquet / Gold), + **Postgres**,
  **Elasticsearch**, **Qdrant**.
- **FastAPI** : `POST /process` → `202 + job_id`, suivi via `GET /jobs/{id}`.
