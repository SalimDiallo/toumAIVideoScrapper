"""StoragePort backed by MinIO (S3-compatible) — medallion Bronze layer.

Layout in the bucket: <layer>/<language>/<video_id>/{metadata.json, transcript.json, <audio>}
Returns an s3:// URI. Drop-in replacement for LocalJsonStorage (same port).
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import structlog
from minio import Minio

from ..domain.models import IngestionResult
from ..domain.ports import AudioHandle
from .serialization import audio_media_type, metadata_dict, transcript_dict

log = structlog.get_logger(__name__)


class MinioStorage:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        layer: str = "bronze",
    ) -> None:
        self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        self._bucket = bucket
        self._layer = layer
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def save(self, result: IngestionResult, language: str) -> str:
        prefix = f"{self._layer}/{language}/{result.metadata.video_id}"

        audio_path = result.audio.path
        audio_key = f"{prefix}/{audio_path.name}"
        if audio_path.exists():
            self._client.fput_object(self._bucket, audio_key, str(audio_path))
            audio_location = f"s3://{self._bucket}/{audio_key}"
        else:
            audio_location = ""

        self._put_json(f"{prefix}/metadata.json", metadata_dict(result, language, audio_location))
        if result.transcript is not None:
            self._put_json(f"{prefix}/transcript.json", transcript_dict(result.transcript))

        # audio is now in object storage — drop the local staging copy
        if audio_path.exists():
            audio_path.unlink()

        uri = f"s3://{self._bucket}/{prefix}"
        log.info("minio.saved", uri=uri)
        return uri

    def load_transcript(self, storage_uri: str) -> dict | None:
        return self._load_json(storage_uri, "transcript.json")

    def load_metadata(self, storage_uri: str) -> dict | None:
        return self._load_json(storage_uri, "metadata.json")

    def _load_json(self, storage_uri: str, name: str) -> dict | None:
        # storage_uri: s3://<bucket>/<prefix>
        prefix = storage_uri.split(f"s3://{self._bucket}/", 1)[-1]
        key = f"{prefix}/{name}"
        try:
            response = self._client.get_object(self._bucket, key)
            try:
                return json.loads(response.read().decode("utf-8"))
            finally:
                response.close()
                response.release_conn()
        except Exception:  # noqa: BLE001 - object missing / no such key
            return None

    def open_audio(self, storage_uri: str) -> AudioHandle | None:
        # storage_uri: s3://<bucket>/<prefix>; the audio is the one non-JSON object.
        # We hand back a presigned URL so the browser streams straight from MinIO
        # (S3 range requests => the player can seek), instead of proxying bytes.
        audio_key = self._audio_key(storage_uri)
        if audio_key is None:
            return None
        try:
            url = self._client.presigned_get_object(
                self._bucket, audio_key, expires=timedelta(hours=2)
            )
        except Exception:  # noqa: BLE001 - signing failed
            return None
        return AudioHandle(media_type=audio_media_type(Path(audio_key).suffix), url=url)

    def read_audio(self, storage_uri: str) -> tuple[str, Iterator[bytes]] | None:
        audio_key = self._audio_key(storage_uri)
        if audio_key is None:
            return None
        response = self._client.get_object(self._bucket, audio_key)

        def _chunks() -> Iterator[bytes]:
            try:
                yield from response.stream(1 << 16)
            finally:
                response.close()
                response.release_conn()

        return Path(audio_key).name, _chunks()

    def delete(self, storage_uri: str) -> None:
        prefix = storage_uri.split(f"s3://{self._bucket}/", 1)[-1]
        try:
            objs = self._client.list_objects(self._bucket, prefix=f"{prefix}/", recursive=True)
            for obj in objs:
                self._client.remove_object(self._bucket, obj.object_name)
        except Exception:  # noqa: BLE001 - prefix already gone / listing failed
            log.warning("minio.delete_failed", uri=storage_uri)

    def _audio_key(self, storage_uri: str) -> str | None:
        prefix = storage_uri.split(f"s3://{self._bucket}/", 1)[-1]
        try:
            objs = self._client.list_objects(self._bucket, prefix=f"{prefix}/", recursive=True)
            return next((o.object_name for o in objs if not o.object_name.endswith(".json")), None)
        except Exception:  # noqa: BLE001 - listing failed / prefix missing
            return None

    def _put_json(self, key: str, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type="application/json",
        )
