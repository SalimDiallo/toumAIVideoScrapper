"""StoragePort backed by the local filesystem (JSON).

Layout: <root>/<language>/<video_id>/{metadata.json, transcript.json, <audio>}
Everything for a video lives together, grouped by language.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

from ..domain.models import IngestionResult
from ..domain.ports import AudioHandle
from .serialization import audio_media_type, metadata_dict, transcript_dict


class LocalJsonStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, result: IngestionResult, language: str) -> str:
        out_dir = self._root / language / result.metadata.video_id
        out_dir.mkdir(parents=True, exist_ok=True)

        audio_path = self._relocate_audio(result.audio.path, out_dir)

        (out_dir / "metadata.json").write_text(
            json.dumps(
                metadata_dict(result, language, str(audio_path)), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        if result.transcript is not None:
            (out_dir / "transcript.json").write_text(
                json.dumps(transcript_dict(result.transcript), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return str(out_dir)

    def load_transcript(self, storage_uri: str) -> dict | None:
        return self._load_json(storage_uri, "transcript.json")

    def load_metadata(self, storage_uri: str) -> dict | None:
        return self._load_json(storage_uri, "metadata.json")

    @staticmethod
    def _load_json(storage_uri: str, name: str) -> dict | None:
        path = Path(storage_uri) / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def open_audio(self, storage_uri: str) -> AudioHandle | None:
        """Locate the audio file in the video folder (the one non-JSON file)."""
        audio = self._audio_file(storage_uri)
        if audio is None:
            return None
        return AudioHandle(
            media_type=audio_media_type(audio.suffix),
            path=audio,
            size=audio.stat().st_size,
        )

    def read_audio(self, storage_uri: str) -> tuple[str, Iterator[bytes]] | None:
        audio = self._audio_file(storage_uri)
        if audio is None:
            return None

        def _chunks() -> Iterator[bytes]:
            with audio.open("rb") as f:
                while chunk := f.read(1 << 16):
                    yield chunk

        return audio.name, _chunks()

    def delete(self, storage_uri: str) -> None:
        folder = Path(storage_uri)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _audio_file(storage_uri: str) -> Path | None:
        folder = Path(storage_uri)
        if not folder.is_dir():
            return None
        return next(
            (p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() != ".json"),
            None,
        )

    @staticmethod
    def _relocate_audio(src: Path, out_dir: Path) -> Path:
        """Move the downloaded audio into the video folder. Returns final path."""
        dest = out_dir / src.name
        if src.exists() and src.resolve() != dest.resolve():
            shutil.move(str(src), str(dest))
        return dest
