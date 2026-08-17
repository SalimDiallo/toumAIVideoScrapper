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
        language="fr",
        storage_uri="s3://bucket/bronze/fr/vid1",
    )


class OkUseCase:
    def execute(self, url, work_dir, languages):
        return _result()


class FakeSilver:
    def __init__(self):
        self.calls: list = []

    def process_video(self, language, video_id, *, force=False):
        self.calls.append((language, video_id))
        return True


class FailingSilver:
    def process_video(self, language, video_id, *, force=False):
        raise RuntimeError("silver boom")


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


def test_completed_job_triggers_auto_silver():
    handler, store, _ = _handler(OkUseCase())
    silver = FakeSilver()
    handler.silver_pipeline = silver
    handler.handle({"job_id": "j3", "url": "http://yt/x", "languages": ["fr"]})

    # Le nettoyage silver est enchaîné pour la vidéo tout juste ingérée.
    assert silver.calls == [("fr", "vid1")]
    assert store.updates[-1][1] == JobStatus.COMPLETED


def test_auto_silver_failure_does_not_fail_the_job():
    handler, store, _ = _handler(OkUseCase())
    handler.silver_pipeline = FailingSilver()
    handler.handle({"job_id": "j4", "url": "http://yt/x", "languages": ["fr"]})

    # Un échec du nettoyage (best-effort) laisse le job terminé, pas en échec.
    assert store.updates[-1][1] == JobStatus.COMPLETED
