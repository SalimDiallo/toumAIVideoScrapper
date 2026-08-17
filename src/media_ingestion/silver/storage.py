"""Lecture/écriture bronze <-> silver sur MinIO (S3-compatible, minio-py).

Réutilise le layout medallion déjà en place :

    <bucket>/<layer>/<language>/<video_id>/{metadata.json, transcript.json, <audio>}

Le lecteur bronze énumère les entrées existantes, télécharge leurs artefacts, et
le writer silver ré-écrit l'audio nettoyé + le transcript resynchronisé sous le
préfixe ``silver/`` (même bucket par défaut, ou un bucket dédié).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import structlog
from minio import Minio

from ..adapters.serialization import audio_media_type

log = structlog.get_logger(__name__)

_BRONZE = "bronze"
_SILVER = "silver"


@dataclass(frozen=True)
class BronzeEntry:
    """Une entrée bronze à traiter (identité stable partagée avec le silver)."""

    language: str
    video_id: str

    @property
    def bronze_prefix(self) -> str:
        return f"{_BRONZE}/{self.language}/{self.video_id}"

    @property
    def silver_prefix(self) -> str:
        return f"{_SILVER}/{self.language}/{self.video_id}"


class SilverMinioStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        bronze_bucket: str,
        silver_bucket: str,
        secure: bool = False,
        region: str = "us-east-1",
    ) -> None:
        self._client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure, region=region
        )
        self._bronze_bucket = bronze_bucket
        self._silver_bucket = silver_bucket
        self._ensure_bucket(silver_bucket)

    def _ensure_bucket(self, bucket: str) -> None:
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)

    # ------------------------------------------------------------- énumération
    def iter_bronze_entries(self) -> list[BronzeEntry]:
        """Liste les entrées bronze distinctes ``(language, video_id)``.

        Parcourt les objets sous ``bronze/`` et en déduit les couples langue/vidéo
        (on ignore les clés mal formées). Trié pour un traitement déterministe.
        """
        seen: set[tuple[str, str]] = set()
        objects = self._client.list_objects(
            self._bronze_bucket, prefix=f"{_BRONZE}/", recursive=True
        )
        for obj in objects:
            # clé attendue : bronze/<language>/<video_id>/<fichier>
            parts = obj.object_name.split("/")
            if len(parts) >= 4 and parts[0] == _BRONZE:
                seen.add((parts[1], parts[2]))
        return [BronzeEntry(language=lang, video_id=vid) for lang, vid in sorted(seen)]

    def silver_exists(self, entry: BronzeEntry) -> bool:
        """True si un audio silver existe déjà pour cette entrée (idempotence)."""
        objects = self._client.list_objects(
            self._silver_bucket, prefix=f"{entry.silver_prefix}/", recursive=True
        )
        return any(not o.object_name.endswith(".json") for o in objects)

    def silver_uri(self, entry: BronzeEntry) -> str:
        """URI s3:// des artefacts silver de cette entrée (pour la lecture UI)."""
        return f"s3://{self._silver_bucket}/{entry.silver_prefix}"

    # --------------------------------------------------------------- download
    def download_bronze(
        self, entry: BronzeEntry, dest_dir: Path
    ) -> tuple[Path | None, dict | None, dict | None]:
        """Télécharge les artefacts bronze dans ``dest_dir``.

        Retourne ``(audio_path, transcript, metadata)``. ``audio_path`` est le
        premier objet non-JSON rencontré (l'audio) ; ``None`` si absent.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        audio_path: Path | None = None
        transcript: dict | None = None
        metadata: dict | None = None

        objects = self._client.list_objects(
            self._bronze_bucket, prefix=f"{entry.bronze_prefix}/", recursive=True
        )
        for obj in objects:
            name = obj.object_name.split("/")[-1]
            if name == "transcript.json":
                transcript = self._get_json(self._bronze_bucket, obj.object_name)
            elif name == "metadata.json":
                metadata = self._get_json(self._bronze_bucket, obj.object_name)
            elif not name.endswith(".json"):
                local = dest_dir / name
                self._client.fget_object(self._bronze_bucket, obj.object_name, str(local))
                audio_path = local
        return audio_path, transcript, metadata

    # ----------------------------------------------------------------- upload
    def upload_silver(
        self,
        entry: BronzeEntry,
        *,
        audio_path: Path,
        transcript: dict | None,
        metadata: dict | None,
    ) -> str:
        """Écrit l'audio nettoyé + les JSON sous ``silver/…``. Retourne l'URI s3://."""
        audio_key = f"{entry.silver_prefix}/{audio_path.name}"
        self._client.fput_object(
            self._silver_bucket,
            audio_key,
            str(audio_path),
            content_type=audio_media_type(audio_path.suffix),
        )
        if transcript is not None:
            self._put_json(f"{entry.silver_prefix}/transcript.json", transcript)
        if metadata is not None:
            self._put_json(f"{entry.silver_prefix}/metadata.json", metadata)

        uri = f"s3://{self._silver_bucket}/{entry.silver_prefix}"
        log.info("silver.uploaded", uri=uri)
        return uri

    # ---------------------------------------------------------------- helpers
    def silver_pairs(self) -> set[tuple[str, str]]:
        """Ensemble des ``(language, video_id)`` déjà présents en silver.

        Une seule requête de listing : sert à badger la liste des vidéos sans un
        appel par ligne.
        """
        seen: set[tuple[str, str]] = set()
        for obj in self._client.list_objects(
            self._silver_bucket, prefix=f"{_SILVER}/", recursive=True
        ):
            parts = obj.object_name.split("/")
            if len(parts) >= 4 and parts[0] == _SILVER and not obj.object_name.endswith(".json"):
                seen.add((parts[1], parts[2]))
        return seen

    def counts(self) -> tuple[int, int]:
        """(#entrées bronze, #entrées silver) pour l'affichage API."""
        return len(self.iter_bronze_entries()), len(self.silver_pairs())

    def _get_json(self, bucket: str, key: str) -> dict | None:
        try:
            response = self._client.get_object(bucket, key)
            try:
                return json.loads(response.read().decode("utf-8"))
            finally:
                response.close()
                response.release_conn()
        except Exception:  # noqa: BLE001 - objet absent / illisible
            return None

    def _put_json(self, key: str, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(
            self._silver_bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type="application/json",
        )
