"""JSON shapes shared by every StoragePort adapter (local disk, MinIO, …)."""

from __future__ import annotations

from ..domain.models import IngestionResult, Transcript

# Map an audio file extension to the MIME type the browser needs to play it.
_AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


def audio_media_type(suffix: str) -> str:
    return _AUDIO_MEDIA_TYPES.get(suffix.lower(), "application/octet-stream")


def metadata_dict(result: IngestionResult, language: str, audio_location: str) -> dict:
    m = result.metadata
    return {
        "video_id": m.video_id,
        "url": m.url,
        "title": m.title,
        "channel": m.channel,
        "duration_s": m.duration_s,
        "upload_date": m.upload_date,
        "language": language,
        "provider": m.provider,
        "audio_path": audio_location,
        "audio_format": result.audio.format,
        "transcript_status": result.transcript_status.value,
        "created_at": result.created_at.isoformat(),
    }


def transcript_dict(transcript: Transcript) -> dict:
    return {
        "language": transcript.language,
        "source": transcript.source.value,
        "text": transcript.text,
        "segments": [
            {"start_s": s.start_s, "duration_s": s.duration_s, "text": s.text}
            for s in transcript.segments
        ],
    }
