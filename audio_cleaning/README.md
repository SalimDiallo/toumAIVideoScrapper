# audio_cleaning — nettoyage audio/transcript piloté par VAD + classification d'évènements

Étape automatique de nettoyage qui, à partir d'un audio de vidéo YouTube (déjà
extrait par le workflow existant) et de son transcript horodaté, supprime **tout
ce qui n'est pas de la parole utile** (silences, longues plages sans voix,
musique, applaudissements, bruits, autres évènements) et resynchronise le
transcript sur l'audio nettoyé.

> **Principe cardinal : ne jamais supprimer de parole par accident.**
> En cas de doute (faible confiance, évènement inconnu), on **conserve**.
> Une suppression n'a lieu que sur une zone que le VAD **n'a pas** jugée parole
> **et** qu'un classifieur identifie avec une confiance **au-dessus du seuil**.

---

## 1. Architecture

```
audio YouTube
   │
   ▼  ffmpeg (mono, 16 kHz, loudnorm)      ── audio_io.to_normalized_wav
normalisation
   │
   ▼  Silero VAD (ou repli énergie)        ── vad.py
Voice Activity Detection ─────────► zones de PAROLE
   │
   ▼  YAMNet (ou repli heuristique)        ── classifier.py
classification des évènements ────► labels sur les zones NON-parole
   │
   ▼  fusion + padding + seuils            ── segments.py / decision.py
décision keep / remove (par segment)
   │
   ├─► découpe audio (tranches gardées)    ── audio_io.cut_and_write  ─► cleaned.wav
   ├─► resynchro transcript                ── transcript.remap        ─► transcript_cleaned.json
   ├─► métriques (VAD/nettoyage/perf/qualité) ── metrics.py            ─► metrics.json
   └─► visualisation temporelle            ── visualize.py            ─► visualization.png
```

Chaque étape est un **module isolé et testable**. Le traitement est fait **par
blocs** (`processing.chunk_duration_s`) : on ne charge jamais des heures d'audio
en RAM, ce qui permet de traiter podcasts, conférences et vidéos longues.

## 2. Choix des modèles

| Rôle | Modèle par défaut | Pourquoi | Repli léger |
|------|-------------------|----------|-------------|
| VAD | **Silero VAD** (`vad.backend: silero`) | ~1 Mo, CPU, très rapide, robuste multi-langue (arabe/français/anglais…), option **ONNX** encore plus légère | `energy` (énergie RMS, sans dépendance ML) |
| Classification | **YAMNet** (`classification.backend: yamnet`) | MobileNet sur AudioSet (521 classes → speech/music/applause/laughter/noise), léger et rapide sur CPU | `heuristic` (descripteurs spectraux) |

**Alternative** à YAMNet, si TensorFlow n'est pas installable : **PANNs / CNN14**
(`classification.backend: panns`, `pip install panns-inference`, PyTorch pur).
Même interface, projeté sur les mêmes classes grossières.

Pourquoi cette combinaison **VAD → puis classifieur** plutôt qu'un seul modèle ?
Le VAD tranche d'abord la question la plus importante (« est-ce de la parole ? »)
avec un modèle spécialisé, fiable et multilingue. Le classifieur n'intervient
qu'**en aval, sur le non-parole**, pour décider *quoi* supprimer. On ne fait donc
**jamais** reposer une suppression sur un seul modèle, et la parole est protégée
en amont.

### Les 6 classes grossières
`speech`, `music`, `applause`, `laughter`, `noise`, `silence` (+ `other`, fourre-tout
conservé par défaut). YAMNet/PANNs sont projetés dessus via `classifier.YAMNET_TO_COARSE`.

## 3. Arborescence

```
audio_cleaning/
├── README.md              ← ce fichier
├── config.yaml            ← TOUTE la configuration (rien de codé en dur)
├── requirements.txt
├── __init__.py
├── __main__.py            ← python -m audio_cleaning ...
├── labels.py              ← vocabulaire des classes + couleurs
├── config.py              ← chargement config.yaml -> dataclasses typées
├── audio_io.py            ← ffmpeg (normalisation), lecture par blocs, découpe
├── vad.py                 ← Silero VAD (torch/onnx) + repli énergie
├── classifier.py          ← YAMNet / PANNs + repli heuristique
├── segments.py            ← Segment + opérations sur intervalles (fusion, padding…)
├── decision.py            ← logique keep/remove (cœur : « ne jamais perdre de parole »)
├── transcript.py          ← resynchro des timestamps sur l'audio nettoyé
├── metrics.py             ← métriques VAD / nettoyage / performance / qualité
├── visualize.py           ← visualization.png (timeline colorée)
├── pipeline.py            ← orchestration bout-en-bout -> dossier de sorties
├── evaluation.py          ← dataset annoté -> matrice de confusion + métriques
├── cli.py                 ← run / evaluate / make-eval / benchmark
├── examples/
│   ├── make_synthetic.py  ← génère un audio+transcript de démo (sans modèle)
│   └── sample_outputs/    ← exemples réels (metrics.json, segments.json, viz…)
└── tests/
    └── test_logic.py      ← tests de la logique pure (13 tests)
```

### Sorties, par vidéo (`outputs/<video_id>/`)
```
original.wav            transcript_original.json    segments.json     visualization.png
cleaned.wav             transcript_cleaned.json     metrics.json
```
`segments.json` conserve **toutes** les décisions (gardées ET supprimées) pour audit.

## 4. Installation

```bash
# Recommandé : venv Python 3.11 (torch/tensorflow y sont plus stables qu'en 3.14)
python -m venv .venv && source .venv/Scripts/activate   # Windows : .venv\Scripts\activate
pip install -r audio_cleaning/requirements.txt
# ffmpeg/ffprobe doivent être dans le PATH (ou renseigner audio.ffmpeg_dir dans config.yaml)
```

Pour tourner **sans** les gros modèles (démo, CI, environnement contraint), seuls
`numpy soundfile matplotlib pyyaml pandas` suffisent (backends `energy` + `heuristic`).

## 5. Configuration (`config.yaml`)

Aucun paramètre n'est codé en dur — voir `audio_cleaning/config.yaml`. Extrait :

```yaml
vad:
  backend: silero          # silero | energy
  onnx: true
  threshold: 0.5
  min_speech_duration: 0.3
  speech_padding_before: 0.2   # marge de sécurité AVANT la parole
  speech_padding_after: 0.3    # marge de sécurité APRÈS la parole
classification:
  backend: yamnet          # yamnet | panns | heuristic
  music_threshold: 0.7
  applause_threshold: 0.7
  noise_threshold: 0.7
decision:
  remove_laughter: false   # les rires ponctuent la parole -> off par défaut
  keep_if_confidence_below: 0.5   # filet global : sous ce seuil, on CONSERVE
processing:
  sample_rate: 16000
  chunk_duration_s: 300.0  # traitement par blocs (RAM bornée)
  min_remove_duration: 0.5 # anti micro-coupure
```

Toute clé est **surchargeable en CLI** : `--set vad.threshold=0.6 --set decision.remove_laughter=true`.

## 6. Utilisation

```bash
# Une vidéo
python -m audio_cleaning.cli run chemin/audio.m4a --transcript chemin/transcript.json

# Un dossier entier (data/<lang>/<video_id>/{audio, transcript.json}) — grand volume
python -m audio_cleaning.cli run data/ar --output outputs

# Surcharge de paramètres à la volée
python -m audio_cleaning.cli run audio.wav --set vad.threshold=0.6 --set classification.music_threshold=0.6
```

Intégration au workflow existant : le pipeline prend un **fichier local** + un
**transcript JSON** (`{start_s, duration_s, text}`, exactement le format déjà
produit) et écrit un dossier de sorties. Aucun couplage réseau/MinIO : le
branchement amont/aval reste à votre main.

## 7. Exemple complet (reproductible, sans modèle lourd)

```bash
# 1. Génère un audio synthétique + transcript (parole / musique / applaudissements / silences)
python -m audio_cleaning.examples.make_synthetic demo

# 2. Nettoie en mode léger (repli énergie + heuristique, sans torch/tensorflow)
python -m audio_cleaning.cli run demo/sample.wav --transcript demo/transcript.json \
  --output demo/outputs \
  --set audio.loudnorm=false --set vad.backend=energy --set classification.backend=heuristic
```

Résultat observé : **33.0s → 22.0s** (−33 %), musique et applaudissements
supprimés, **les 3 phrases de parole conservées** et re-timées :

```
transcript_original                    transcript_cleaned
start_s=14.5  "Nous allons…"    ─►     start_s=8.52  "Nous allons…"
start_s=28.0  "Merci…"          ─►     start_s=17.03 "Merci…"
```

Visualisation générée (`examples/sample_outputs/visualization.png`) — vert = gardé,
rouge hachuré = supprimé :

```
classes │ ███ SPEECH ███ │ MUSIC │ ███ SPEECH ███ │ NOISE │ ███ SPEECH ███ │
action  │      kept      │ remove│      kept       │ remove│      kept       │
0s                          14s                     28s                    33s
```

`segments.json` (une décision par segment) :

```json
{ "start": 8.31, "end": 14.29, "label": "music", "action": "remove",
  "confidence": 0.74, "source": "classifier" }
```

## 8. Exemple de `metrics.json`

(voir `examples/sample_outputs/metrics.json` — les champs `peak_ram_mb`/`cpu_percent`
apparaissent si `psutil` est installé)

```json
{
  "vad": {
    "speech_duration_s": 21.03, "non_speech_duration_s": 11.97,
    "speech_ratio": 0.6373, "num_speech_segments": 3, "avg_speech_segment_s": 7.01
  },
  "cleaning": {
    "original_duration_s": 33.0, "cleaned_duration_s": 22.03,
    "removed_duration_s": 10.97, "cleaning_ratio": 0.3324,
    "music_removed_duration_s": 5.98, "applause_removed_duration_s": 0.0,
    "noise_removed_duration_s": 4.99, "silence_removed_duration_s": 0.0
  },
  "performance": {
    "processing_time_s": 0.187, "audio_duration_s": 33.0, "rtf": 0.0057,
    "peak_ram_mb": 210.4, "cpu_percent": 98.0
  },
  "segments_total": 5, "segments_removed": 2
}
```

### Métriques qualité (avec données annotées)
| Métrique | Définition |
|----------|-----------|
| `precision` / `recall` / `f1_score` | sur la tâche binaire « faut-il supprimer ? » (tout sauf `speech`), pondérées par la durée |
| `speech_retention` | part de la **vraie** parole conservée → doit tendre vers **1.0** |
| `false_speech_deletion_rate` | part de la vraie parole supprimée à tort → doit tendre vers **0.0** (métrique la plus critique) |
| `music/applause/noise_removal_rate` | part de chaque classe correctement supprimée |

## Dataset d'évaluation manuelle

```bash
# 1. Génère un gabarit à annoter depuis les décisions du pipeline
python -m audio_cleaning.cli make-eval outputs/<video_id>/segments.json \
  --video-id <video_id> --output eval.csv
#    -> colonnes : video_id, start, end, true_label, predicted_label, confidence
#    Classes : speech | music | applause | laughter | noise | silence | other

# 2. Remplir true_label à la main, puis calculer matrice de confusion + métriques
python -m audio_cleaning.cli evaluate eval.csv --output evaluation.json
```

`evaluation.json` contient la **matrice de confusion** (pondérée par la durée) et
le bloc `quality` ci-dessus (voir `examples/sample_outputs/evaluation.json`).

## 9. Benchmark : Silero seul vs Silero + YAMNet

Le VAD seul ne peut que retirer les **silences** (il ignore *quoi* est le
non-parole). Ajouter le classifieur permet de retirer musique/applaudissements/bruit.
La commande compare les deux sur la même entrée :

```bash
python -m audio_cleaning.cli benchmark audio.wav --transcript transcript.json \
  --configs "silero_only:classification.backend=heuristic,decision.remove_music=false,decision.remove_noise=false,decision.remove_applause=false" \
            "silero_yamnet:"
```

Sortie (extrait réel du démo, backends de repli) :

```
config             orig(s)  clean(s)  retiré  speech%     RTF
vad_only              33.0      33.0      0%      70%   0.0165
vad_plus_cls          33.0     25.66     22%      70%   0.0164
```

On lit directement le gain de nettoyage (0 % → 22 %) pour un coût CPU quasi
identique (RTF stable). Pour un vrai comparatif Silero seul vs Silero+YAMNet,
remplacez `heuristic` par les vrais backends (`vad.backend=silero`,
`classification.backend=yamnet`) après `pip install -r requirements.txt`, et
appuyez-vous sur un **dataset annoté** (§ évaluation) pour comparer
`speech_retention` et `false_speech_deletion_rate` entre les deux.

## Intégration TOUMAI (couche silver)

Ce package est branché comme **moteur de nettoyage de la couche silver** du dépôt.
`SilverPipeline` (bronze → silver sur MinIO) sélectionne son moteur via
`TOUMAI_SILVER_ENGINE` :

- `vad` (défaut) → délègue à `audio_cleaning` (Silero VAD + YAMNet), pont dans
  `media_ingestion/silver/vad_cleaning.py` ;
- `ffmpeg` → moteur historique (demucs + silencedetect).

Le moteur `vad` **retombe automatiquement sur `ffmpeg`** si ses dépendances ne sont
pas installées (ex. worker léger sans TensorFlow) — même robustesse que le repli
demucs existant. Réglages : `TOUMAI_SILVER_VAD_BACKEND`,
`TOUMAI_SILVER_CLASSIFIER_BACKEND`, `TOUMAI_SILVER_VAD_THRESHOLD`,
`TOUMAI_SILVER_EVENT_THRESHOLD` (voir `.env.example`).

Installation via **uv** (extras) :

```bash
uv sync --extra cleaning                     # moteur vad : Silero (torch.hub) + repli heuristique
# YAMNet (TensorFlow) : uniquement Python < 3.14 -> image silver rebasée en 3.12
uv sync --extra cleaning --extra cleaning-yamnet   # (sous 3.12/3.11)
```

L'image `Dockerfile.silver` (base **python:3.12-slim**) installe
`.[silver,cleaning,cleaning-yamnet]` et lance `toumai-silver --engine vad`. En local
sur Python 3.14, YAMNet n'a pas de wheel → repli automatique sur le classifieur
heuristique (Silero VAD reste actif via torch).

Lancer un nettoyage silver avec le moteur VAD :

```bash
toumai-silver --engine vad --vad-backend silero --classifier-backend yamnet
```

## Tests

```bash
pytest audio_cleaning/tests -q      # 13 tests de la logique pure (padding, remap, décision, métriques)
```

## Robustesse & montée en charge
- **Multilingue** (ar/fr/en…) : Silero VAD est agnostique à la langue ; YAMNet classe l'acoustique, pas le texte.
- **Vidéos longues / podcasts / conférences** : traitement par blocs (`chunk_duration_s`) → RAM bornée.
- **Musique de fond / applaudissements / bruit / multi-locuteurs** : gérés par le classifieur d'évènements ; la parole superposée reste protégée par le VAD.
- **Grand volume** : les modèles sont chargés une seule fois et réutilisés sur tout le lot (`run` sur un dossier). Une vidéo en échec est journalisée et n'interrompt pas le lot.
```
