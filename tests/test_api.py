"""API tests with fake job store + publisher (no Kafka, no Postgres)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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

    def counts_by_status(self):
        counts: dict = {}
        for j in self.jobs.values():
            counts[j.status.value] = counts.get(j.status.value, 0) + 1
        return counts

    def existing_video_ids(self, video_ids):
        wanted = {v for v in video_ids if v}
        return {
            j.video_id
            for j in self.jobs.values()
            if j.video_id in wanted and j.status is not JobStatus.FAILED
        }

    def timeseries(self, *, days=14):
        return []

    def delete(self, job_id):
        self.jobs.pop(job_id, None)

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
    def __init__(self, transcript=None, audio=None, audio_bytes=None, metadata=None):
        self.transcript = transcript
        self.audio = audio
        self.audio_bytes = audio_bytes
        self.metadata = metadata
        self.deleted = []

    def load_transcript(self, storage_uri):
        return self.transcript

    def load_metadata(self, storage_uri):
        return self.metadata

    def open_audio(self, storage_uri):
        return self.audio

    def read_audio(self, storage_uri):
        if self.audio_bytes is None:
            return None
        return "audio.wav", iter([self.audio_bytes])

    def delete(self, storage_uri):
        self.deleted.append(storage_uri)


class FakeCatalog:
    def __init__(self, rows=None):
        self.rows = rows or []

    def list(self, *, language=None, transcript=None, limit=50, offset=0):
        rows = self.rows
        if language is not None:
            rows = [r for r in rows if r.get("language") == language]
        if transcript is not None:
            rows = [r for r in rows if r.get("transcript_status") == transcript]
        return rows[offset : offset + limit]

    def get(self, video_id):
        return next((r for r in self.rows if r["video_id"] == video_id), None)

    def delete(self, video_id):
        self.rows = [r for r in self.rows if r["video_id"] != video_id]


class FakePlaylistResolver:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.calls = []

    def resolve(self, playlist_url_or_id):
        self.calls.append(playlist_url_or_id)
        return list(self.entries)


def _client(storage=None, catalog=None, playlist_resolver=None):
    store, pub = FakeStore(), FakePublisher()
    client = TestClient(
        create_app(
            Settings(),
            store=store,
            publisher=pub,
            storage=storage,
            catalog=catalog,
            playlist_resolver=playlist_resolver,
        )
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


def test_ui_dashboard_renders():
    client, _, _ = _client()
    client.post("/process", json={"url": "http://yt/x"})
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Tableau de bord" in r.text


def test_ui_jobs_rows_partial_and_retry():
    client, store, pub = _client()
    job_id = client.post("/process", json={"url": "http://yt/x"}).json()["job_id"]
    store.update_status(job_id, JobStatus.FAILED, error="boom")
    pub.events.clear()

    rows = client.get("/ui/partials/jobs-rows")
    assert rows.status_code == 200 and job_id[:8] in rows.text

    r = client.post(f"/ui/jobs/{job_id}/retry", data={"status": ""})
    assert r.status_code == 200
    assert store.jobs[job_id].status is JobStatus.PENDING
    assert pub.events and pub.events[0][1] == job_id


def test_ui_root_redirects_to_dashboard():
    client, _, _ = _client()
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/ui/"


def test_ui_video_detail_shows_synced_segments():
    transcript = {
        "language": "fr",
        "source": "youtube_manual",
        "text": "bonjour le monde",
        "segments": [
            {"start_s": 0.0, "duration_s": 2.0, "text": "bonjour"},
            {"start_s": 2.0, "duration_s": 3.0, "text": "le monde"},
        ],
    }
    rows = [
        {
            "video_id": "v1",
            "url": "http://y/1",
            "title": "Une vidéo",
            "language": "fr",
            "storage_uri": "s3://b/1",
        }
    ]
    from media_ingestion.domain.ports import AudioHandle

    storage = FakeStorage(transcript=transcript, audio=AudioHandle(media_type="audio/wav"))
    client, _, _ = _client(storage=storage, catalog=FakeCatalog(rows=rows))

    r = client.get("/ui/videos/v1")
    assert r.status_code == 200
    # segments rendered with their audio offset + timestamp, and the player is present
    assert 'data-start="2.0"' in r.text
    assert "le monde" in r.text
    assert "/ui/videos/v1/audio" in r.text


def test_ui_video_audio_streams_and_404():
    import tempfile

    rows = [{"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"}]

    # no audio handle -> 404
    client, _, _ = _client(storage=FakeStorage(audio=None), catalog=FakeCatalog(rows=rows))
    assert client.get("/ui/videos/v1/audio").status_code == 404

    # local file handle -> served
    from media_ingestion.domain.ports import AudioHandle

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"RIFFxxxx")
        path = f.name
    handle = AudioHandle(media_type="audio/wav", path=Path(path), size=8)
    client, _, _ = _client(storage=FakeStorage(audio=handle), catalog=FakeCatalog(rows=rows))
    r = client.get("/ui/videos/v1/audio")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"


def test_ui_video_audio_minio_presigned_url():
    """MinIO audio: the page points the player straight at the presigned URL,
    and the /audio route redirects there (browser seeks against S3)."""
    from media_ingestion.domain.ports import AudioHandle

    presigned = "http://localhost:9000/toumai-media/bronze/fr/v1/a.m4a?X-Amz-Signature=abc"
    rows = [{"video_id": "v1", "url": "http://y/1", "title": "T", "storage_uri": "s3://b/1"}]
    storage = FakeStorage(
        transcript={"segments": [{"start_s": 0.0, "duration_s": 1.0, "text": "hi"}]},
        audio=AudioHandle(media_type="audio/mp4", url=presigned),
    )
    client, _, _ = _client(storage=storage, catalog=FakeCatalog(rows=rows))

    page = client.get("/ui/videos/v1")
    assert page.status_code == 200 and presigned in page.text

    r = client.get("/ui/videos/v1/audio", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == presigned


def test_ui_videos_zip_selection():
    import io
    import zipfile

    rows = [
        {"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"},
        {"video_id": "v2", "url": "http://y/2", "storage_uri": "s3://b/2"},
    ]
    storage = FakeStorage(
        transcript={"text": "hi", "segments": []},
        audio_bytes=b"RIFFwave",
        metadata={"video_id": "v1", "title": "T", "duration_s": 12},
    )
    client, _, _ = _client(storage=storage, catalog=FakeCatalog(rows=rows))

    # no selection -> 400
    assert client.post("/ui/videos/zip", data={}).status_code == 400

    # selection of one video -> zip with its audio + transcript + metadata
    r = client.post("/ui/videos/zip", data={"video_ids": ["v1"]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "v1/audio.wav" in names and "v1/transcript.json" in names
    assert "v1/metadata.json" in names
    assert not any(n.startswith("v2/") for n in names)

    # scope=all -> every video
    r = client.post("/ui/videos/zip", data={"scope": "all"})
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "v1/audio.wav" in names and "v2/audio.wav" in names


def test_ui_delete_video_removes_row_and_audio():
    rows = [{"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"}]
    storage = FakeStorage(audio_bytes=b"x")
    catalog = FakeCatalog(rows=rows)
    client, _, _ = _client(storage=storage, catalog=catalog)

    r = client.post("/ui/videos/v1/delete")
    assert r.status_code == 200 and r.text == ""
    assert storage.deleted == ["s3://b/1"]  # audio artifacts dropped
    assert catalog.get("v1") is None


def test_ui_delete_job_removes_it():
    client, store, _ = _client()
    job_id = client.post("/process", json={"url": "http://yt/x"}).json()["job_id"]
    r = client.post(f"/ui/jobs/{job_id}/delete", data={"status": ""})
    assert r.status_code == 200
    assert job_id not in store.jobs


def test_ui_bulk_delete_jobs():
    client, store, _ = _client()
    a = client.post("/process", json={"url": "http://yt/a"}).json()["job_id"]
    b = client.post("/process", json={"url": "http://yt/b"}).json()["job_id"]
    c = client.post("/process", json={"url": "http://yt/c"}).json()["job_id"]

    # the selectable rows partial exposes checkboxes
    rows = client.get("/ui/partials/jobs-rows?selectable=1")
    assert 'name="job_ids"' in rows.text

    r = client.post("/ui/jobs/bulk-delete", data={"job_ids": [a, b]})
    assert r.status_code == 200
    assert a not in store.jobs and b not in store.jobs
    assert c in store.jobs  # untouched


def test_ui_bulk_delete_videos():
    rows = [
        {"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"},
        {"video_id": "v2", "url": "http://y/2", "storage_uri": "s3://b/2"},
        {"video_id": "v3", "url": "http://y/3", "storage_uri": "s3://b/3"},
    ]
    storage = FakeStorage(audio_bytes=b"x")
    catalog = FakeCatalog(rows=rows)
    client, _, _ = _client(storage=storage, catalog=catalog)

    r = client.post("/ui/videos/bulk-delete", data={"video_ids": ["v1", "v2"]})
    assert r.status_code == 200
    assert storage.deleted == ["s3://b/1", "s3://b/2"]  # audio dropped for both
    assert catalog.get("v1") is None and catalog.get("v2") is None
    assert catalog.get("v3") is not None  # untouched
    assert "v3" in r.text  # refreshed rows still list the survivor


def test_extract_video_id_forms():
    from media_ingestion.video_id import extract_video_id

    vid = "dQw4w9WgXcQ"
    assert extract_video_id(f"https://www.youtube.com/watch?v={vid}") == vid
    assert extract_video_id(f"https://youtu.be/{vid}?t=30") == vid
    assert extract_video_id(f"https://www.youtube.com/watch?v={vid}&list=abc") == vid
    assert extract_video_id(f"https://www.youtube.com/shorts/{vid}") == vid
    assert extract_video_id("https://example.com/whatever") is None
    assert extract_video_id("") is None


def test_ui_submit_url_dedup():
    client, store, pub = _client()
    vid = "dQw4w9WgXcQ"
    url = f"https://youtu.be/{vid}"

    r1 = client.post("/ui/process", data={"url": url})
    assert r1.status_code == 200 and "Job créé" in r1.text
    assert len(store.jobs) == 1

    # same video, different URL shape -> refused as duplicate, no new job
    r2 = client.post("/ui/process", data={"url": f"https://www.youtube.com/watch?v={vid}&t=5"})
    assert "déjà présente" in r2.text
    assert len(store.jobs) == 1


def test_ui_submit_csv_dedup_within_batch_and_existing():
    client, store, _ = _client()
    vid = "dQw4w9WgXcQ"
    # pre-existing job for this video
    client.post("/ui/process", data={"url": f"https://youtu.be/{vid}"})
    assert len(store.jobs) == 1

    csv_content = (
        "url\n"
        f"https://www.youtube.com/watch?v={vid}\n"  # dup of existing
        "https://youtu.be/ABCDEFGHIJK\n"  # new
        "https://youtu.be/ABCDEFGHIJK\n"  # dup within batch
    )
    r = client.post(
        "/ui/process/csv",
        files={"file": ("jobs.csv", csv_content.encode(), "text/csv")},
    )
    assert r.status_code == 200
    assert "1 job(s) créé(s)" in r.text  # only the one new video
    assert "2 doublon(s)" in r.text
    assert len(store.jobs) == 2  # original + one new


def _pl_entries(*video_ids):
    from media_ingestion.domain.ports import PlaylistEntry

    return [
        PlaylistEntry(video_id=v, url=f"https://www.youtube.com/watch?v={v}", title=v)
        for v in video_ids
    ]


PLAYLIST = "PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb"


def test_process_playlist_creates_one_job_per_entry():
    resolver = FakePlaylistResolver(_pl_entries("aaaaaaaaaaa", "bbbbbbbbbbb"))
    client, store, pub = _client(playlist_resolver=resolver)
    r = client.post("/process/playlist", json={"playlist": PLAYLIST, "languages": ["fr"]})
    assert r.status_code == 202
    body = r.json()
    assert body["accepted"] == 2
    assert len(store.jobs) == 2
    assert len(pub.events) == 2
    assert resolver.calls == [PLAYLIST]


def test_process_playlist_rejects_non_playlist():
    client, _, _ = _client(playlist_resolver=FakePlaylistResolver())
    r = client.post("/process/playlist", json={"playlist": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 400


def test_process_playlist_empty_is_404():
    client, _, _ = _client(playlist_resolver=FakePlaylistResolver(entries=[]))
    r = client.post("/process/playlist", json={"playlist": PLAYLIST})
    assert r.status_code == 404


def test_ui_submit_playlist_dedup():
    resolver = FakePlaylistResolver(
        _pl_entries("aaaaaaaaaaa", "bbbbbbbbbbb", "aaaaaaaaaaa")  # dup within playlist
    )
    client, store, _ = _client(playlist_resolver=resolver)
    r = client.post("/ui/process/playlist", data={"playlist": PLAYLIST})
    assert r.status_code == 200
    assert "2 job(s) créé(s)" in r.text
    assert "1 doublon(s)" in r.text
    assert len(store.jobs) == 2


def test_ui_submit_playlist_invalid_input():
    client, _, _ = _client(playlist_resolver=FakePlaylistResolver())
    r = client.post("/ui/process/playlist", data={"playlist": "pas une playlist"})
    assert r.status_code == 200
    assert "Aucune playlist détectée" in r.text


def test_list_videos():
    rows = [
        {
            "video_id": "v1",
            "url": "http://y/1",
            "title": "un",
            "language": "fr",
            "storage_uri": "s3://b/1",
        },
        {
            "video_id": "v2",
            "url": "http://y/2",
            "title": "deux",
            "language": "ar",
            "storage_uri": "s3://b/2",
        },
    ]
    client, _, _ = _client(catalog=FakeCatalog(rows=rows))
    assert len(client.get("/videos").json()) == 2
    fr = client.get("/videos?language=fr").json()
    assert len(fr) == 1 and fr[0]["video_id"] == "v1"


def _catalog_with_transcripts():
    rows = [
        {
            "video_id": "v1",
            "url": "http://y/1",
            "title": "QWERTYAVEC",
            "language": "fr",
            "transcript_status": "available",
            "storage_uri": "s3://b/1",
        },
        {
            "video_id": "v2",
            "url": "http://y/2",
            "title": "QWERTYMANQUANT",
            "language": "fr",
            "transcript_status": "unavailable",
            "storage_uri": "s3://b/2",
        },
    ]
    return FakeCatalog(rows=rows)


def test_list_videos_filter_by_transcript():
    client, _, _ = _client(catalog=_catalog_with_transcripts())
    avail = client.get("/videos?transcript=available").json()
    assert len(avail) == 1 and avail[0]["video_id"] == "v1"
    unavail = client.get("/videos?transcript=unavailable").json()
    assert len(unavail) == 1 and unavail[0]["video_id"] == "v2"


def test_ui_videos_page_filter_by_transcript():
    client, _, _ = _client(catalog=_catalog_with_transcripts())
    # the filter chips are rendered
    page = client.get("/ui/videos")
    assert "Disponible" in page.text and "Indisponible" in page.text

    # available -> only v1
    avail = client.get("/ui/videos?transcript=available")
    assert "QWERTYAVEC" in avail.text and "QWERTYMANQUANT" not in avail.text
    # unavailable -> only v2
    unavail = client.get("/ui/videos?transcript=unavailable")
    assert "QWERTYMANQUANT" in unavail.text and "QWERTYAVEC" not in unavail.text
