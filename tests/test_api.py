"""API tests with fake job store + publisher (no Kafka, no Postgres)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from media_ingestion.api.app import create_app
from media_ingestion.config import Settings
from media_ingestion.domain.models import JobStatus


class FakeStore:
    def __init__(self):
        self.jobs: dict = {}

    def create(self, job):
        self.jobs[job.job_id] = job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def update_status(self, job_id, status, *, result_uri=None, error=None):
        pass


class FakePublisher:
    def __init__(self):
        self.events: list = []

    def publish(self, topic, key, event):
        self.events.append((topic, key, event))


def _client():
    store, pub = FakeStore(), FakePublisher()
    client = TestClient(create_app(Settings(), store=store, publisher=pub))
    return client, store, pub


def test_process_accepts_and_publishes():
    client, store, pub = _client()
    r = client.post("/process", json={"url": "http://yt/x", "languages": ["fr"]})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == JobStatus.PENDING.value
    assert body["job_id"] in store.jobs
    assert pub.events and pub.events[0][0] == "job.requested"
    assert pub.events[0][2]["url"] == "http://yt/x"


def test_get_job_returns_status():
    client, _, _ = _client()
    job_id = client.post("/process", json={"url": "http://yt/x"}).json()["job_id"]
    r = client.get(f"/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["url"] == "http://yt/x"


def test_get_unknown_job_404():
    client, _, _ = _client()
    assert client.get("/jobs/does-not-exist").status_code == 404
