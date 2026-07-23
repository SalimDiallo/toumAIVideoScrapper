"""Worker handler tests with fakes (no Kafka, no Postgres, no network)."""

from __future__ import annotations

from pathlib import Path

from media_ingestion.config import Settings
from media_ingestion.domain.models import (
    AudioAsset,
    IngestionResult,
    JobStatus,
    TranscriptStatus,
    VideoMetadata,
)
from media_ingestion.worker.handler import JobHandler


class FakeStore:
    def __init__(self):
        self.updates: list = []

    def create(self, job):
        pass

    def get(self, job_id):
        return None

    def update_status(self, job_id, status, *, result_uri=None, error=None):
        self.updates.append((job_id, status, result_uri, error))


class FakePublisher:
    def __init__(self):
        self.events: list = []

    def publish(self, topic, key, event):
        self.events.append((topic, key, event))


def _result() -> IngestionResult:
    return IngestionResult(
        metadata=VideoMetadata(video_id="vid1", url="http://yt/x", title="t"),
        audio=AudioAsset(path=Path("a.wav"), format="wav"),
        transcript=None,
        transcript_status=TranscriptStatus.UNAVAILABLE,
        storage_uri="s3://bucket/bronze/fr/vid1",
    )


class OkUseCase:
    def execute(self, url, work_dir, languages):
        return _result()


class BoomUseCase:
    def execute(self, url, work_dir, languages):
        raise RuntimeError("download failed")


def _handler(use_case):
    store, pub = FakeStore(), FakePublisher()
    return JobHandler(use_case, store, pub, Settings()), store, pub


def test_success_marks_completed_and_publishes():
    handler, store, pub = _handler(OkUseCase())
    handler.handle({"job_id": "j1", "url": "http://yt/x", "languages": ["fr"]})

    statuses = [u[1] for u in store.updates]
    assert JobStatus.RUNNING in statuses
    assert JobStatus.COMPLETED in statuses
    assert store.updates[-1][2] == "s3://bucket/bronze/fr/vid1"  # result_uri
    assert "job.completed" in [e[0] for e in pub.events]


def test_failure_marks_failed_and_dlq():
    handler, store, pub = _handler(BoomUseCase())
    handler.handle({"job_id": "j2", "url": "http://yt/x", "languages": ["fr"]})

    last = store.updates[-1]
    assert last[1] == JobStatus.FAILED
    assert last[3] == "download failed"  # error
    assert "job.dlq" in [e[0] for e in pub.events]
