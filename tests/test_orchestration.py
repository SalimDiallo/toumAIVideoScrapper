"""Tests for the orchestration steps + serde (no Airflow, no network)."""

from __future__ import annotations

from pathlib import Path

from media_ingestion.config import Settings
from media_ingestion.domain.models import (
    AudioAsset,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)
from media_ingestion.orchestration import serde, steps


def test_serde_round_trips():
    m = VideoMetadata(video_id="v1", url="http://y/x", title="t", channel="c", duration_s=42, language="fr")
    a = AudioAsset(path=Path("data/v1.wav"), format="wav", sample_rate=16000)
    t = Transcript(
        language="fr",
        source=TranscriptSource.YOUTUBE_MANUAL,
        segments=[TranscriptSegment(0.0, 1.0, "bonjour")],
    )
    assert serde.metadata_from_dict(serde.metadata_to_dict(m)) == m
    assert serde.audio_from_dict(serde.audio_to_dict(a)) == a
    assert serde.transcript_from_dict(serde.transcript_to_dict(t)) == t
    assert serde.transcript_from_dict(None) is None


def _ctx_after_transcript() -> dict:
    m = VideoMetadata(video_id="v1", url="http://y/x", title="t", language="ar")
    a = AudioAsset(path=Path("data/v1.webm"), format="webm")
    t = Transcript(
        language="ar",
        source=TranscriptSource.YOUTUBE_MANUAL,
        segments=[TranscriptSegment(0.0, 1.0, "مرحبا")],
    )
    return {
        "url": "http://y/x",
        "languages": ["fr"],
        "metadata": serde.metadata_to_dict(m),
        "audio": serde.audio_to_dict(a),
        "transcript": serde.transcript_to_dict(t),
    }


def test_store_step_saves_and_resolves_language(monkeypatch):
    saved = {}

    class FakeStorage:
        def save(self, result, language):
            saved["video_id"] = result.metadata.video_id
            saved["language"] = language
            return f"s3://bucket/bronze/{language}/{result.metadata.video_id}"

    monkeypatch.setattr(steps, "_build_storage", lambda settings: FakeStorage())

    out = steps.store_step(_ctx_after_transcript(), Settings())

    assert out["language"] == "ar"  # from transcript, wins over --lang fr
    assert out["storage_uri"] == "s3://bucket/bronze/ar/v1"
    assert saved == {"video_id": "v1", "language": "ar"}


def test_index_step_upserts_when_repo_present(monkeypatch):
    calls = []

    class FakeRepo:
        def upsert(self, result, language, storage_uri):
            calls.append((result.metadata.video_id, language, storage_uri))

    monkeypatch.setattr(steps, "_build_metadata_repo", lambda settings: FakeRepo())

    ctx = {**_ctx_after_transcript(), "language": "ar", "storage_uri": "s3://b/bronze/ar/v1"}
    steps.index_step(ctx, Settings())

    assert calls == [("v1", "ar", "s3://b/bronze/ar/v1")]


def test_index_step_noop_when_repo_absent(monkeypatch):
    monkeypatch.setattr(steps, "_build_metadata_repo", lambda settings: None)
    ctx = {**_ctx_after_transcript(), "language": "ar", "storage_uri": "s3://b/bronze/ar/v1"}
    assert steps.index_step(ctx, Settings()) is ctx  # returns ctx unchanged
