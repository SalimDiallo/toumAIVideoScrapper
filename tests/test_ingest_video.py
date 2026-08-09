"""Use-case tests with in-memory fakes — no network, no ffmpeg."""

from __future__ import annotations

from pathlib import Path

from media_ingestion.application.ingest_video import IngestVideoUseCase
from media_ingestion.domain.models import (
    AudioAsset,
    IngestionResult,
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    TranscriptStatus,
    VideoMetadata,
)


class FakeDownloader:
    def download(self, video_url: str, dest_dir: Path):
        meta = VideoMetadata(video_id="vid123", url=video_url, title="Test", language="fr")
        return meta, AudioAsset(path=dest_dir / "vid123.wav", format="wav")


class FakeTranscriptProvider:
    def __init__(self, transcript: Transcript | None):
        self._transcript = transcript

    def fetch(self, metadata: VideoMetadata, languages: list[str]):
        return self._transcript


class FakeStorage:
    def __init__(self):
        self.saved: IngestionResult | None = None
        self.language: str | None = None

    def save(self, result: IngestionResult, language: str) -> str:
        self.saved = result
        self.language = language
        return str(Path("/tmp") / language / result.metadata.video_id)


def _yt_transcript() -> Transcript:
    return Transcript(
        language="fr",
        source=TranscriptSource.YOUTUBE_MANUAL,
        segments=[TranscriptSegment(0.0, 2.0, "bonjour"), TranscriptSegment(2.0, 2.0, "monde")],
    )


def test_uses_youtube_transcript_when_available(tmp_path):
    storage = FakeStorage()
    uc = IngestVideoUseCase(FakeDownloader(), FakeTranscriptProvider(_yt_transcript()), storage)

    result = uc.execute("http://yt/x", tmp_path, ["fr"])

    assert result.transcript_status is TranscriptStatus.AVAILABLE
    assert result.transcript is not None
    assert result.transcript.source is TranscriptSource.YOUTUBE_MANUAL
    assert result.transcript.text == "bonjour monde"
    assert storage.language == "fr"  # grouped under data/fr/...


def test_skips_transcript_when_none(tmp_path):
    storage = FakeStorage()
    uc = IngestVideoUseCase(FakeDownloader(), FakeTranscriptProvider(None), storage)

    result = uc.execute("http://yt/x", tmp_path, ["fr"])

    assert result.transcript is None
    assert result.transcript_status is TranscriptStatus.UNAVAILABLE


def test_metadata_repo_is_called_with_storage_uri(tmp_path):
    class FakeRepo:
        def __init__(self):
            self.calls: list[tuple] = []

        def upsert(self, result, language, storage_uri):
            self.calls.append((result.metadata.video_id, language, storage_uri))

    storage = FakeStorage()
    repo = FakeRepo()
    uc = IngestVideoUseCase(
        FakeDownloader(), FakeTranscriptProvider(_yt_transcript()), storage, metadata_repo=repo
    )

    uc.execute("http://yt/x", tmp_path, ["fr"])

    assert repo.calls == [("vid123", "fr", str(Path("/tmp") / "fr" / "vid123"))]
