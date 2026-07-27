"""API tests with fake job store + publisher (no Kafka, no Postgres)."""

from __future__ import annotations

from dataclasses import replace

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

    def list(self, *, status=None, limit=50, offset=0):
        items = list(self.jobs.values())
        if status is not None:
            items = [j for j in items if j.status == status]
        return items[offset : offset + limit]

    def update_status(self, job_id, status, *, result_uri=None, error=None):
        job = self.jobs.get(job_id)
        if job is not None:
            self.jobs[job_id] = replace(job, status=status, result_uri=result_uri, error=error)


class FakePublisher:
    def __init__(self):
        self.events: list = []

    def publish(self, topic, key, event):
        self.events.append((topic, key, event))

    def publish_batch(self, topic, items):
        for key, event in items:
            self.events.append((topic, key, event))


class FakeStorage:
    def __init__(self, transcript=None):
        self.transcript = transcript

    def load_transcript(self, storage_uri):
        return self.transcript


class FakeCatalog:
    def __init__(self, rows=None):
        self.rows = rows or []

    def list(self, *, language=None, limit=50, offset=0):
        rows = self.rows if language is None else [r for r in self.rows if r["language"] == language]
        return rows[offset : offset + limit]


def _client(storage=None, catalog=None):
    store, pub = FakeStore(), FakePublisher()
    client = TestClient(
        create_app(Settings(), store=store, publisher=pub, storage=storage, catalog=catalog)
    )
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


def test_process_csv_creates_one_job_per_row():
    client, store, pub = _client()
    csv_content = (
        "url,lang\n"
        "http://yt/a,fr\n"
        'http://yt/b,"fr,en"\n'
        ",fr\n"  # empty url -> error, skipped
    )
    r = client.post(
        "/process/csv",
        files={"file": ("jobs.csv", csv_content.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] == 2
    assert len(body["errors"]) == 1
    assert len(store.jobs) == 2
    assert len(pub.events) == 2
    # multi-lang cell is parsed
    langs = {tuple(item["languages"]) for item in body["jobs"]}
    assert ("fr",) in langs and ("fr", "en") in langs


def test_process_csv_rejects_missing_url_column():
    client, _, _ = _client()
    r = client.post(
        "/process/csv",
        files={"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")},
    )
    assert r.status_code == 400


def test_list_jobs_and_filter_by_status():
    client, store, _ = _client()
    client.post("/process", json={"url": "http://yt/a"})
    client.post("/process", json={"url": "http://yt/b"})
    # everything is pending
    assert len(client.get("/jobs").json()) == 2
    assert len(client.get("/jobs?status=pending").json()) == 2
    assert len(client.get("/jobs?status=completed").json()) == 0
    assert client.get("/jobs?status=bogus").status_code == 400


def test_retry_republishes_and_resets_pending():
    client, store, pub = _client()
    job_id = client.post("/process", json={"url": "http://yt/x"}).json()["job_id"]
    # simulate a failure
    store.update_status(job_id, JobStatus.FAILED, error="boom")
    pub.events.clear()

    r = client.post(f"/jobs/{job_id}/retry")
    assert r.status_code == 202
    assert store.jobs[job_id].status is JobStatus.PENDING
    assert pub.events and pub.events[0][1] == job_id  # republished with same key
    assert client.post("/jobs/unknown/retry").status_code == 404


def test_get_transcript():
    transcript = {"language": "fr", "source": "youtube_manual", "text": "bonjour", "segments": []}
    client, store, _ = _client(storage=FakeStorage(transcript=transcript))
    job_id = client.post("/process", json={"url": "http://yt/x"}).json()["job_id"]

    # not finished yet -> 409
    assert client.get(f"/jobs/{job_id}/transcript").status_code == 409

    store.update_status(job_id, JobStatus.COMPLETED, result_uri="s3://b/bronze/fr/v1")
    r = client.get(f"/jobs/{job_id}/transcript")
    assert r.status_code == 200
    assert r.json()["text"] == "bonjour"


def test_get_transcript_404_when_absent():
    client, store, _ = _client(storage=FakeStorage(transcript=None))
    job_id = client.post("/process", json={"url": "http://yt/x"}).json()["job_id"]
    store.update_status(job_id, JobStatus.COMPLETED, result_uri="s3://b/bronze/fr/v1")
    assert client.get(f"/jobs/{job_id}/transcript").status_code == 404


def test_list_videos():
    rows = [
        {"video_id": "v1", "url": "http://y/1", "title": "un", "language": "fr", "storage_uri": "s3://b/1"},
        {"video_id": "v2", "url": "http://y/2", "title": "deux", "language": "ar", "storage_uri": "s3://b/2"},
    ]
    client, _, _ = _client(catalog=FakeCatalog(rows=rows))
    assert len(client.get("/videos").json()) == 2
    fr = client.get("/videos?language=fr").json()
    assert len(fr) == 1 and fr[0]["video_id"] == "v1"
