# Note technique — Nettoyage audio & transcript (couche Silver)

> **Projet TOUMAI — plateforme d'ingestion média**
> Sujet : choix techniques du pipeline de nettoyage audio/transcript (bronze → silver)
> Branche : `feature/audio-event-cleaning`
> Destinataire : encadrant — document de synthèse technique

---

## 1. Contexte et objectif

La plateforme ingère des vidéos (YouTube et autres sources) et les range selon une
architecture **medallion** :

| Couche | Contenu | Rôle |
|--------|---------|------|
| **Bronze** | audio brut extrait + transcript horodaté + métadonnées | données telles qu'ingérées |
| **Silver** | audio **nettoyé** + transcript **resynchronisé** + métadonnées enrichies | données prêtes à exploiter (STT, indexation, entraînement) |
| Gold | agrégats / features | usage analytique aval |

La **couche Silver** a un objectif précis : à partir de l'audio brut et de son
transcript, **retirer tout ce qui n'est pas de la parole utile** — silences, longues
plages sans voix, musique, applaudissements, rires, bruits — puis **réaligner les
timestamps du transcript** sur l'audio raccourci.

### Principe cardinal (contrat métier)

> **Ne jamais supprimer de parole par accident.**
> En cas de doute (faible confiance, évènement non identifié), on **conserve**.
> Une suppression n'a lieu que si une zone est **à la fois** (a) jugée non-parole
> par le VAD **et** (b) identifiée comme évènement supprimable par le classifieur,
> avec une confiance **au-dessus d'un seuil**.

Ce principe justifie une grande partie des choix ci-dessous (double garde
VAD → classifieur, seuils, marges de sécurité, replis conservateurs).

---

## 2. Vue d'ensemble de l'architecture

Le nettoyage est branché comme **moteur de la couche Silver** du dépôt. Deux moteurs
coexistent, sélectionnables par configuration (`TOUMAI_SILVER_ENGINE`) :

| Moteur | Techno | Ce qu'il sait faire | Statut |
|--------|--------|---------------------|--------|
| **`vad`** (défaut) | Silero VAD + YAMNet (package `audio_cleaning`) | comprend *quoi* est le non-parole (musique / applaudissements / bruit / silence) → suppression fine | moteur cible |
| **`ffmpeg`** | demucs + `silencedetect` (ffmpeg) | isole la voix, coupe les silences, coupe les passages annotés `[Applause]`/`[Musique]` dans le transcript | moteur historique / **repli automatique** |

Le moteur `vad` **retombe automatiquement sur `ffmpeg`** si ses dépendances lourdes
(TensorFlow, torch…) ne sont pas installées — par exemple sur un worker léger. Cette
tolérance aux pannes est un choix d'architecture assumé : **un lot ne doit jamais
échouer parce qu'une brique optionnelle manque**.

### Pipeline du moteur `vad` (package `audio_cleaning`)

```
audio bronze
   │
   ▼  ffmpeg : mono, 16 kHz, loudnorm (EBU R128)        ── audio_io.to_normalized_wav
normalisation
   │
   ▼  Silero VAD (repli : énergie RMS)                  ── vad.py
Voice Activity Detection ──────────► zones de PAROLE
   │
   ▼  YAMNet (repli : heuristique spectrale)            ── classifier.py
classification des évènements ─────► labels sur zones NON-parole
   │
   ▼  fusion + padding + seuils de confiance            ── segments.py / decision.py
décision keep / remove (par segment)
   │
   ├─► découpe audio (segments gardés)   ── audio_io.cut_and_write  ─► cleaned.wav
   ├─► resynchro transcript              ── transcript.remap        ─► transcript_cleaned.json
   ├─► métriques (VAD/nettoyage/perf)    ── metrics.py              ─► metrics.json
   └─► visualisation temporelle          ── visualize.py            ─► visualization.png
```

**Traitement par blocs** (`chunk_duration_s = 300 s`, recouvrement 1 s) : on ne charge
jamais des heures d'audio en RAM. Cela permet de traiter podcasts, conférences et
vidéos longues avec une **empreinte mémoire bornée**. Les modèles sont **chargés une
seule fois** et réutilisés sur tout un lot.

---

## 3. Modèles utilisés — justification détaillée

### 3.1 Silero VAD — détection de la parole

| Critère | Détail |
|---------|--------|
| **Rôle** | trancher la question la plus critique : « est-ce de la parole ? » |
| **Modèle** | Silero VAD (réseau léger, ~1 Mo) |
| **Chargement** | via le paquet `silero-vad`, ou repli `torch.hub` (`snakers4/silero-vad`) si le paquet/torchaudio n'a pas de wheel |
| **Format d'entrée** | mono, 16 kHz, float32 |
| **Backend optionnel** | ONNX Runtime (`vad.onnx: true`) — encore plus léger/rapide sur CPU |
| **Repli sans ML** | `EnergyVAD` — détection par énergie RMS court-terme (fenêtres 30 ms, seuil adaptatif) |

**Pourquoi Silero ?**
- **Léger et CPU-friendly** : pas besoin de GPU, tourne sur un worker standard.
- **Multilingue par conception** : agnostique à la langue (arabe / français / anglais…),
  ce qui est essentiel pour un corpus hétérogène. Il détecte la *présence de voix*,
  pas le contenu linguistique.
- **Robuste et éprouvé** : standard de facto pour la VAD légère.

Le repli `EnergyVAD` est **volontairement permissif** : en cas de doute il classe en
parole, conformément au principe cardinal. Son seuil est placé *entre* le plancher de
bruit et le pic (et non un multiple du plancher) pour rester stable après loudnorm.

**Paramètres clés** (`config.yaml` → section `vad`) :
- `threshold: 0.5` — probabilité de parole au-delà de laquelle on retient « speech ».
- `min_speech_duration: 0.3` / `min_silence_duration: 0.3` — durées minimales pour
  fusionner/ignorer les bribes.
- `speech_padding_before: 0.2` / `speech_padding_after: 0.3` — **marges de sécurité**
  ajoutées autour de chaque zone de parole, pour ne jamais tronquer un début/fin de mot.

### 3.2 YAMNet — classification des évènements audio

| Critère | Détail |
|---------|--------|
| **Rôle** | sur une zone **non-parole**, dire *quoi* c'est, avec une confiance |
| **Modèle** | YAMNet (MobileNet-v1 entraîné sur **AudioSet**, **521 classes**) |
| **Chargement** | TensorFlow Hub (`https://tfhub.dev/google/yamnet/1`) |
| **Fenêtrage** | trames natives de 0,96 s, hop 0,48 s ; un score par trame et par classe |
| **Alternative** | **PANNs / CNN14** (`panns-inference`, PyTorch pur) — même interface |
| **Repli sans ML** | `HeuristicClassifier` — descripteurs spectraux (centroïde, flatness, ZCR, énergie) |

**Pourquoi YAMNet ?**
- **Rapide sur CPU** (architecture MobileNet), adapté au grand volume.
- **Couverture riche** : les 521 classes fines d'AudioSet couvrent musique, instruments,
  applaudissements, foule, rires, bruits de fond, etc.
- **Classe l'acoustique, pas le texte** → complémentaire du VAD et **indépendant de la
  langue**.

**Projection 521 → 6 classes grossières.** Les 521 classes AudioSet sont projetées sur
un vocabulaire métier restreint via `YAMNET_TO_COARSE` (correspondance par sous-chaîne
du nom de classe, motifs spécifiques avant génériques) :

`speech` · `music` · `applause` · `laughter` · `noise` · `silence` (+ `other`, fourre-tout **conservé** par défaut).

L'alternative **PANNs/CNN14** existe pour les environnements où TensorFlow n'est pas
installable (notamment Python ≥ 3.14, voir §6) ; le repli **heuristique** garantit que
le pipeline reste exécutable *partout*, même sans aucune dépendance ML lourde.

### 3.3 Pourquoi VAD *puis* classifieur, et non un seul modèle ?

C'est le choix d'architecture central :

1. Le **VAD spécialisé** répond d'abord à la question la plus importante et la plus
   fiable (« parole ou non ? »), en protégeant la parole en amont.
2. Le **classifieur n'intervient qu'en aval**, uniquement sur le non-parole, pour
   décider *quoi* supprimer.

Conséquence : **aucune suppression ne repose sur un seul modèle**, et la parole est
protégée par deux garde-fous successifs. On classe d'ailleurs uniquement les zones
non-parole (économie de calcul).

### 3.4 demucs — isolation de la voix (moteur `ffmpeg`)

| Critère | Détail |
|---------|--------|
| **Rôle** | retirer la musique de fond en isolant le stem « voix » |
| **Modèle** | `htdemucs` (Hybrid Transformer Demucs, défaut — bon compromis qualité/coût) |
| **Invocation** | `python -m demucs --two-stems vocals -n htdemucs …` |
| **Sortie** | `<model>/<nom>/vocals.wav` |

Dans le moteur `ffmpeg`, demucs isole la voix, puis `silencedetect` coupe les silences :
comme la musique de fond disparaît du stem voix, les passages purement musicaux et les
applaudissements y deviennent silencieux et sont donc **coupés au passage**. Si demucs
échoue sur une vidéo (ou est absent), on retombe sur ffmpeg seul sans perdre l'entrée.

### 3.5 ffmpeg / ffprobe — traitement du signal

Utilisés dans les deux moteurs comme briques bas niveau :
- **normalisation** : mono, 16 kHz, `loudnorm` (norme de loudness EBU R128) pour
  homogénéiser un corpus hétérogène ;
- **`silencedetect`** : détection des plages de silence (seuil en dB + durée mini) ;
- **`atrim` + `concat`** : recollage des segments gardés en une seule passe, **sans
  ré-encodage intermédiaire** (ffmpeg décode une fois, écrit directement).

---

## 4. Logique de décision keep / remove

Cœur du système (`decision.py`), appliqué après VAD + classification :

1. **Zones de parole** (VAD) → gardées, **élargies du padding** de sécurité
   avant/après. Ces marges rognent d'autant les zones non-parole adjacentes (« on rend
   du contexte à la parole »).
2. **Zones non-parole** = complément des zones de parole *paddées*. Chacune est
   étiquetée par la **classe dominante** du classifieur (moyenne pondérée par le
   recouvrement des trames).
3. **Décision par zone non-parole** :
   - classe candidate à la suppression **ET** confiance ≥ seuil → `remove` ;
   - sinon → `keep` (le doute profite à la conservation).
4. **Anti micro-coupure** : une zone plus courte que `min_remove_duration` (0,5 s) n'est
   jamais supprimée (évite un audio haché).

**Filets de sécurité empilés** (tous conservateurs) :
- `keep_if_confidence_below: 0.5` — sous ce seuil global, on **conserve** quoi qu'il
  arrive (sauf silence franc).
- Confiance = **score brut moyen** de la classe gagnante, **non normalisé** par la somme
  des classes. Rationale : une région où le modèle n'a vu qu'une classe faible
  (ex. `music = 0.3`) ne doit **pas** ressortir artificiellement à 1.0 et déclencher une
  suppression à tort — ici `0.3` reste `0.3` → conservé.
- **Garde-fou anti-vide** : si tout serait supprimé, on garde tout l'audio (jamais de
  sortie vide) et on réconcilie les décisions pour l'audit.
- `remove_laughter: false` — les rires ponctuent souvent la parole, désactivé par
  défaut. `remove_other: false` — classe fourre-tout conservée par prudence.

### Nettoyage guidé par le transcript

En complément du signal, les **annotations non-parlées du transcript** sont exploitées
(`remap.py`) : tout sous-titre entièrement entre crochets `[...]` (ex. `[Applause]`,
`[Musique]`), les cues entre parenthèses contenant un mot-clé, ou les lignes de notes
de musique (`♪`) sont coupés de l'audio **et** retirés du transcript. La liste de
mots-clés est **configurable** (`TOUMAI_SILVER_NONSPEECH_KEYWORDS`). Approche
volontairement conservatrice pour ne jamais couper une vraie incise parlée.

### Resynchronisation du transcript (`remap_transcript`)

Partie la plus délicate : après suppression, les segments gardés sont recollés bout à
bout, donc **tous les timestamps changent**. Pour chaque sous-titre on intersecte son
intervalle avec l'union des segments gardés ; s'il tombe entièrement dans un passage
supprimé il est **exclu**, sinon ses `start_s`/`duration_s` sont **reprojetés** sur la
nouvelle timeline (logique pure, testée en isolation).

---

## 5. Métriques produites (`metrics.json`)

Chaque vidéo produit un bilan complet, embarqué dans les métadonnées Silver pour
audit/benchmark à grande échelle :

| Famille | Métriques |
|---------|-----------|
| **VAD** | `speech_duration_s`, `non_speech_duration_s`, `speech_ratio`, `num_speech_segments`, `avg_speech_segment_s` |
| **Nettoyage** | `original_duration_s`, `cleaned_duration_s`, `removed_duration_s`, `cleaning_ratio`, `*_removed_duration_s` (par classe) |
| **Performance** | `processing_time_s`, `rtf` (real-time factor : < 1 = plus rapide que le direct), `peak_ram_mb`, `cpu_percent` (si `psutil`) |
| **Qualité** (si données annotées) | `precision` / `recall` / `f1_score`, `speech_retention`, `false_speech_deletion_rate`, `*_removal_rate` |

Les **deux métriques qualité les plus importantes** (mesurables sur dataset annoté via
la commande `evaluate`) :
- **`speech_retention`** — part de la vraie parole conservée → doit tendre vers **1.0** ;
- **`false_speech_deletion_rate`** — part de vraie parole supprimée à tort → doit tendre
  vers **0.0** (métrique la plus critique, cohérente avec le principe cardinal).

Le tout est pondéré par la **durée** (plus honnête qu'un simple comptage de segments).
Un mode `benchmark` compare « VAD seul » vs « VAD + classifieur » sur la même entrée.

---

## 6. Contraintes d'environnement & packaging

- **Versions Python** : l'application tourne en **3.14**, mais **TensorFlow n'a pas de
  wheel pour 3.14**. Conséquence :
  - l'**image Docker Silver** (`Dockerfile.silver`) est basée sur **python:3.12-slim**
    et installe `.[silver,cleaning,cleaning-yamnet]` → YAMNet actif ;
  - en **local sous 3.14**, YAMNet n'est pas installable → **repli automatique sur le
    classifieur heuristique**, tout en gardant Silero VAD actif (torch a un wheel 3.14).
- **Extras `pyproject.toml`** modulaires :
  - `silver` = demucs + numpy (moteur ffmpeg) ;
  - `cleaning` = torch + Silero via torch.hub (moteur vad, sans TensorFlow) ;
  - `cleaning-yamnet` = TensorFlow + tensorflow-hub, **marqués `python_version < "3.14"`**
    pour garder le lock résoluble partout.
- **ffmpeg / ffprobe** doivent être dans le PATH (ou pointés par `ffmpeg_dir`).
- **Tout est configurable** : aucun paramètre important n'est codé en dur. Réglages via
  `config.yaml`, surcharges CLI (`--set vad.threshold=0.6`) ou variables d'environnement
  `TOUMAI_SILVER_*` (`ENGINE`, `VAD_BACKEND`, `CLASSIFIER_BACKEND`, `VAD_THRESHOLD`,
  `EVENT_THRESHOLD`, seuils de silence, etc.).

---

## 7. Robustesse & montée en charge

- **Multilingue** (ar / fr / en…) : Silero est agnostique à la langue ; YAMNet classe
  l'acoustique, pas le texte.
- **Vidéos longues** (podcasts, conférences) : traitement par blocs → RAM bornée.
- **Grand volume** : modèles chargés une fois, réutilisés sur tout le lot ; une vidéo en
  échec est **journalisée et ignorée**, le lot continue.
- **Idempotence** : une entrée déjà présente en Silver est sautée (sauf `--force`).
- **Dégradation gracieuse** en cascade : `vad` → `ffmpeg` ; Silero → énergie ;
  YAMNet → heuristique ; demucs → ffmpeg seul. **Aucune brique optionnelle n'est
  bloquante.**
- **Traçabilité** : `segments.json` conserve *toutes* les décisions (gardées ET
  supprimées) ; les métriques complètes sont embarquées dans les métadonnées Silver.

---

## 8. Limites connues & pistes d'amélioration

### Limites actuelles

1. **YAMNet non disponible en Python 3.14** → en local hors Docker, le classifieur
   retombe sur l'heuristique (moins précise). La précision « pleine » n'est garantie
   que dans l'image Silver 3.12.
2. **YAMNet entraîné sur AudioSet** (contenu majoritairement anglophone/occidental) :
   la classification d'évènements peut être moins fiable sur des contenus/acoustiques
   sous-représentés. À valider sur le corpus réel (notamment arabophone).
3. **Projection 521 → 6 classes par sous-chaîne de nom** : simple et lisible, mais
   grossière ; une classe AudioSet mal nommée peut tomber dans `other`.
4. **Pas encore de mesure qualité sur corpus réel** : `precision`/`recall`/
   `speech_retention` nécessitent un **dataset annoté manuellement** (outillage
   `make-eval` / `evaluate` fourni, mais annotation à réaliser).
5. **Seuils par défaut non calibrés sur nos données** (`vad.threshold=0.5`,
   `event_threshold=0.7`) : hérités des valeurs usuelles, à affiner par validation.
6. **VAD mono-canal 16 kHz** : pas de diarisation (« qui parle »), pas de séparation de
   locuteurs superposés (la parole superposée est protégée mais pas séparée).
7. **demucs coûteux** (CPU/temps) : réservé au moteur ffmpeg, désactivable.
8. **Pas d'accélération GPU** activée par défaut : dimensionnement CPU.

### Pistes

- Constituer un **dataset annoté** (ar/fr/en) et **calibrer les seuils** sur les
  métriques `speech_retention` / `false_speech_deletion_rate`.
- **Benchmark chiffré** « Silero seul vs Silero + YAMNet » et « YAMNet vs PANNs » sur ce
  dataset.
- Affiner la table de projection `YAMNET_TO_COARSE` d'après les erreurs observées.
- Évaluer **PANNs/CNN14** comme classifieur par défaut (PyTorch pur, homogène avec
  torch/demucs, pas de contrainte de version Python).
- Option **GPU** pour demucs/YAMNet sur les gros volumes.

---

## 9. Récapitulatif des choix

| Décision | Choix retenu | Raison principale |
|----------|--------------|-------------------|
| Détection parole | **Silero VAD** | léger, CPU, multilingue, robuste |
| Classification évènements | **YAMNet** (AudioSet) | rapide CPU, couverture riche, acoustique ≠ texte |
| Architecture modèle | **VAD puis classifieur** | double garde, parole protégée en amont |
| Isolation voix (ffmpeg) | **demucs htdemucs** | bon compromis qualité/coût |
| Signal bas niveau | **ffmpeg** (loudnorm, silencedetect, atrim/concat) | standard, sans ré-encodage inutile |
| Politique de décision | **conservatrice** (seuils + filets + anti micro-coupure) | « ne jamais perdre de parole » |
| Mémoire | **traitement par blocs** | RAM bornée, vidéos longues |
| Robustesse | **replis en cascade** | aucune dépendance optionnelle bloquante |
| Packaging | **extras modulaires + image Silver 3.12** | contourner l'absence de wheel TF 3.14 |
| Configuration | **tout externalisé** (YAML / CLI / env) | reproductibilité, tuning sans recompilation |

---

*Références code : `audio_cleaning/` (pipeline autonome) et
`src/media_ingestion/silver/` (intégration medallion). Voir aussi
`audio_cleaning/README.md` pour l'usage CLI et les exemples reproductibles.*
