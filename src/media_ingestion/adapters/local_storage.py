"""StoragePort backed by the local filesystem (JSON).

Layout: <root>/<language>/<video_id>/{metadata.json, transcript.json, <audio>}
Everything for a video lives together, grouped by language.

Phase 2 swaps this for MinIO medallion (Bronze raw / Silver Parquet / Gold),
by writing a new adapter with the same `save()` signature.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..domain.models import IngestionResult, Transcript


class LocalJsonStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, result: IngestionResult, language: str) -> Path:
        out_dir = self._root / language / result.metadata.video_id
        out_dir.mkdir(parents=True, exist_ok=True)

        audio_path = self._relocate_audio(result.audio.path, out_dir)

        (out_dir / "metadata.json").write_text(
            json.dumps(self._metadata_dict(result, language, audio_path), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if result.transcript is not None:
            (out_dir / "transcript.json").write_text(
                json.dumps(self._transcript_dict(result.transcript), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return out_dir

    @staticmethod
    def _relocate_audio(src: Path, out_dir: Path) -> Path:
        """Move the downloaded audio into the video folder. Returns final path."""
        dest = out_dir / src.name
        if src.exists() and src.resolve() != dest.resolve():
            shutil.move(str(src), str(dest))
        return dest

    @staticmethod
    def _metadata_dict(result: IngestionResult, language: str, audio_path: Path) -> dict:
        m = result.metadata
        return {
            "video_id": m.video_id,
            "url": m.url,
            "title": m.title,
            "channel": m.channel,
            "duration_s": m.duration_s,
            "upload_date": m.upload_date,
            "language": language,
            "audio_path": str(audio_path),
            "audio_format": result.audio.format,
            "transcript_status": result.transcript_status.value,
            "created_at": result.created_at.isoformat(),
        }

    @staticmethod
    def _transcript_dict(transcript: Transcript) -> dict:
        return {
            "language": transcript.language,
            "source": transcript.source.value,
            "text": transcript.text,
            "segments": [
                {"start_s": s.start_s, "duration_s": s.duration_s, "text": s.text}
                for s in transcript.segments
            ],
        }
