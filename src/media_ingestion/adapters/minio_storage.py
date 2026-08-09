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
        public_endpoint: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        self._client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure, region=region
        )
        # Presigned URLs are handed to the *browser*, which can't resolve an internal
        # Docker hostname like "minio:9000". When the server talks to MinIO over an
        # internal endpoint, `public_endpoint` (e.g. "localhost:9000" or a real domain)
        # is the host the browser will actually reach. The host is part of the S3
        # signature, so we presign with a client bound to that public host instead of
        # rewriting the URL after the fact.
        #
        # `region` is pinned so presigning stays offline: otherwise minio-py issues a
        # GetBucketLocation call against the (public) endpoint to discover the region,
        # which fails when that endpoint isn't reachable from the server side.
        self._presign_client = (
            Minio(
                public_endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
                region=region,
            )
            if public_endpoint
            else self._client
        )
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
            # Set the real audio MIME type; without it minio-py stores the object as
            # application/octet-stream, which browsers refuse to play in <audio>.
            self._client.fput_object(
                self._bucket,
                audio_key,
                str(audio_path),
                content_type=audio_media_type(audio_path.suffix),
            )
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
        media_type = audio_media_type(Path(audio_key).suffix)
        try:
            # Force the response Content-Type via the presigned URL so the browser
            # gets e.g. audio/wav even for objects stored (before this fix) as
            # application/octet-stream — no re-upload needed.
            url = self._presign_client.presigned_get_object(
                self._bucket,
                audio_key,
                expires=timedelta(hours=2),
                response_headers={"response-content-type": media_type},
            )
        except Exception:  # noqa: BLE001 - signing failed
            return None
        return AudioHandle(media_type=media_type, url=url)

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
