"""(De)serialisation of domain objects to JSON-friendly dicts.

Airflow tasks hand state to each other via XCom, which must be JSON-serialisable —
so between steps we carry plain dicts and rebuild domain objects on the way in/out.
"""

from __future__ import annotations

from pathlib import Path

from ..domain.models import (
    AudioAsset,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)


def metadata_to_dict(m: VideoMetadata) -> dict:
    return {
        "video_id": m.video_id,
        "url": m.url,
        "title": m.title,
        "channel": m.channel,
        "duration_s": m.duration_s,
        "upload_date": m.upload_date,
        "language": m.language,
    }


def metadata_from_dict(d: dict) -> VideoMetadata:
    return VideoMetadata(**d)


def audio_to_dict(a: AudioAsset) -> dict:
    return {"path": str(a.path), "format": a.format, "sample_rate": a.sample_rate}


def audio_from_dict(d: dict) -> AudioAsset:
    return AudioAsset(path=Path(d["path"]), format=d["format"], sample_rate=d.get("sample_rate"))


def transcript_to_dict(t: Transcript) -> dict:
    return {
        "language": t.language,
        "source": t.source.value,
        "segments": [
            {"start_s": s.start_s, "duration_s": s.duration_s, "text": s.text} for s in t.segments
        ],
    }


def transcript_from_dict(d: dict | None) -> Transcript | None:
    if not d:
        return None
    return Transcript(
        language=d["language"],
        source=TranscriptSource(d["source"]),
        segments=[TranscriptSegment(**s) for s in d["segments"]],
    )
