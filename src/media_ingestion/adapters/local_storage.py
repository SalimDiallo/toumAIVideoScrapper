"""StoragePort backed by the local filesystem (JSON).

Layout: <root>/<language>/<video_id>/{metadata.json, transcript.json, <audio>}
Everything for a video lives together, grouped by language.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..domain.models import IngestionResult
from .serialization import metadata_dict, transcript_dict


class LocalJsonStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, result: IngestionResult, language: str) -> str:
        out_dir = self._root / language / result.metadata.video_id
        out_dir.mkdir(parents=True, exist_ok=True)

        audio_path = self._relocate_audio(result.audio.path, out_dir)

        (out_dir / "metadata.json").write_text(
            json.dumps(metadata_dict(result, language, str(audio_path)), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if result.transcript is not None:
            (out_dir / "transcript.json").write_text(
                json.dumps(transcript_dict(result.transcript), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return str(out_dir)

    def load_transcript(self, storage_uri: str) -> dict | None:
        path = Path(storage_uri) / "transcript.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _relocate_audio(src: Path, out_dir: Path) -> Path:
        """Move the downloaded audio into the video folder. Returns final path."""
        dest = out_dir / src.name
        if src.exists() and src.resolve() != dest.resolve():
            shutil.move(str(src), str(dest))
        return dest
