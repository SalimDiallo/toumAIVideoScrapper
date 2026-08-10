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

    def list(self, *, status=None, q=None, limit=50, offset=0):
        items = list(self.jobs.values())
        if status is not None:
            items = [j for j in items if j.status == status]
        if q:
            items = [j for j in items if q.lower() in (j.url or "").lower()]
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

    def list(self, *, language=None, transcript=None, provider=None, q=None, limit=50, offset=0):
        rows = self.rows
        if language is not None:
            rows = [r for r in rows if r.get("language") == language]
        if transcript is not None:
            rows = [r for r in rows if r.get("transcript_status") == transcript]
        if provider is not None:
            rows = [r for r in rows if r.get("provider") == provider]
        if q:
            ql = q.lower()
            rows = [
                r for r in rows
                if ql in (r.get("title") or "").lower()
                or ql in (r.get("channel") or "").lower()
                or ql in (r.get("url") or "").lower()
            ]
        return rows[offset : offset + limit]

    def get(self, video_id):
        return next((r for r in self.rows if r["video_id"] == video_id), None)

    def existing_video_ids(self, video_ids):
        wanted = {v for v in video_ids if v}
        return {r["video_id"] for r in self.rows if r["video_id"] in wanted}

    def delete(self, video_id):
        self.rows = [r for r in self.rows if r["video_id"] != video_id]

    def stats(self):
        rows = self.rows
        total = len(rows)
        total_dur = sum(int(r.get("duration_s") or 0) for r in rows)
        avail = sum(1 for r in rows if r.get("transcript_status") == "available")

        def _group(key):
            agg: dict = {}
            for r in rows:
                k = r.get(key) or "?"
                a = agg.setdefault(k, {"videos": 0, "duration_s": 0})
                a["videos"] += 1
                a["duration_s"] += int(r.get("duration_s") or 0)
            return [{key: k, **v} for k, v in agg.items()]

        by_source: dict = {}
        for r in rows:
            k = r.get("transcript_source") or "aucune"
            by_source[k] = by_source.get(k, 0) + 1

        return {
            "videos": total,
            "duration_s": total_dur,
            "with_transcript": avail,
            "without_transcript": total - avail,
            "by_language": _group("language"),
            "by_provider": _group("provider"),
            "by_source": [{"source": k, "videos": v} for k, v in by_source.items()],
        }


class FakePlaylistResolver:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.calls = []

    def resolve(self, playlist_url_or_id):
        self.calls.append(playlist_url_or_id)
        return list(self.entries)


class FakeChannelStore:
    def __init__(self, channels=None):
        self.channels = {c.channel_key: c for c in (channels or [])}
        self.checked = []

    def add(self, channel):
        self.channels.setdefault(channel.channel_key, channel)

    def list_active(self):
        return [c for c in self.channels.values() if c.active]

    def list_all(self):
        return list(self.channels.values())

    def remove(self, channel_key):
        self.channels.pop(channel_key, None)

    def set_active(self, channel_key, active):
        from dataclasses import replace

        c = self.channels.get(channel_key)
        if c is not None:
            self.channels[channel_key] = replace(c, active=active)

    def mark_checked(self, channel_key, when):
        self.checked.append((channel_key, when))


class FakeChannelResolver:
    def __init__(self, by_url=None):
        # {channel_url: [PlaylistEntry, ...]}
        self.by_url = by_url or {}
        self.calls = []

    def recent_uploads(self, channel_url, limit=15):
        self.calls.append((channel_url, limit))
        return list(self.by_url.get(channel_url, []))


class FakeRunLog:
    def __init__(self):
        self.runs = []

    def record(self, checked, queued, detail):
        from datetime import datetime, timezone

        from media_ingestion.domain.models import VeilleRun

        self.runs.insert(
            0,
            VeilleRun(
                run_id=len(self.runs) + 1,
                ran_at=datetime.now(timezone.utc),
                checked=checked,
                queued=queued,
                detail=detail,
            ),
        )

    def list_recent(self, limit=20):
        return self.runs[:limit]

    def stats(self):
        return {
            "runs": len(self.runs),
            "queued_total": sum(r.queued for r in self.runs),
            "last_ran_at": self.runs[0].ran_at if self.runs else None,
        }


def _client(
    storage=None,
    catalog=None,
    playlist_resolver=None,
    channel_store=None,
    channel_resolver=None,
    veille_run_log=None,
):
    store, pub = FakeStore(), FakePublisher()
    client = TestClient(
        create_app(
            Settings(),
            store=store,
            publisher=pub,
            storage=storage,
            catalog=catalog or FakeCatalog(),
            playlist_resolver=playlist_resolver,
            channel_store=channel_store,
            channel_resolver=channel_resolver,
            veille_run_log=veille_run_log or FakeRunLog(),
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


def test_ui_stats_page_shows_audio_volume():
    rows = [
        {
            "video_id": "v1", "url": "http://y/1", "title": "un", "language": "fr",
            "provider": "youtube", "duration_s": 3600,
            "transcript_status": "available", "transcript_source": "youtube_manual",
            "storage_uri": "s3://b/1",
        },
        {
            "video_id": "v2", "url": "http://y/2", "title": "deux", "language": "ar",
            "provider": "youtube", "duration_s": 1800,
            "transcript_status": "unavailable", "transcript_source": None,
            "storage_uri": "s3://b/2",
        },
    ]
    client, _, _ = _client(catalog=FakeCatalog(rows=rows))
    r = client.get("/ui/stats")
    assert r.status_code == 200
    assert "Audio collecté" in r.text
    assert "1 h 30" in r.text        # 5400s total -> 1 h 30
    assert "Audio par langue" in r.text
    # transcript availability: 1 of 2 -> 50 %
    assert "50 %" in r.text


def test_ui_videos_search_and_pagination():
    rows = [
        {"video_id": f"v{i}", "url": f"http://y/{i}", "title": f"Vidéo {i}",
         "channel": "Acme" if i % 2 else "Beta", "language": "fr",
         "provider": "youtube", "storage_uri": f"s3://b/{i}"}
        for i in range(60)
    ]
    client, _, _ = _client(catalog=FakeCatalog(rows=rows))

    # page 1 shows the search box + a "Suivant" that is enabled (60 > 50)
    page1 = client.get("/ui/videos")
    assert page1.status_code == 200
    assert "Rechercher un titre" in page1.text
    assert "Suivant" in page1.text and "page=2" in page1.text

    # page 2 exists and links back
    page2 = client.get("/ui/videos?page=2")
    assert page2.status_code == 200
    assert "Précédent" in page2.text and "page=1" in page2.text

    # search narrows results (by channel)
    hit = client.get("/ui/videos?q=Beta")
    assert "Vidéo 0" in hit.text  # channel Beta -> even indices


def test_ui_jobs_search():
    client, store, _ = _client()
    client.post("/process", json={"url": "https://youtu.be/alpha"})
    client.post("/process", json={"url": "https://youtu.be/beta"})

    rows = client.get("/ui/partials/jobs-rows?q=alpha")
    assert rows.status_code == 200
    assert "alpha" in rows.text and "beta" not in rows.text
    # the search box is on the jobs page
    page = client.get("/ui/jobs?q=alpha")
    assert "Rechercher une URL" in page.text


def test_ui_dashboard_shows_audio_strip():
    rows = [{"video_id": "v1", "url": "http://y/1", "duration_s": 7200,
             "language": "fr", "provider": "youtube", "storage_uri": "s3://b/1"}]
    client, _, _ = _client(catalog=FakeCatalog(rows=rows))
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "Audio collecté" in r.text and "Voir les statistiques" in r.text


def test_ui_video_detail_shows_quality_panel():
    transcript = {
        "language": "fr",
        "source": "youtube_manual",
        "text": "bonjour le monde",
        "segments": [
            {"start_s": 0.0, "duration_s": 50.0, "text": "bonjour"},
            {"start_s": 50.0, "duration_s": 50.0, "text": "le monde"},
        ],
    }
    rows = [{"video_id": "v1", "url": "http://y/1", "title": "T",
             "language": "fr", "storage_uri": "s3://b/1"}]
    from media_ingestion.domain.ports import AudioHandle

    storage = FakeStorage(
        transcript=transcript,
        audio=AudioHandle(media_type="audio/wav"),
        metadata={"video_id": "v1", "duration_s": 100},
    )
    client, _, _ = _client(storage=storage, catalog=FakeCatalog(rows=rows))
    r = client.get("/ui/videos/v1")
    assert r.status_code == 200
    assert "Qualité de transcription" in r.text
    assert "Excellente" in r.text            # full coverage + human source
    assert "Couverture" in r.text and "100 %" in r.text
    assert "Sous-titres auteur" in r.text


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


def _completed_job(store, job_id="j1", video_id="v1", url="http://y/1"):
    """Seed a COMPLETED job tied to a video (bypasses the async worker)."""
    from media_ingestion.domain.models import Job

    store.jobs[job_id] = Job(
        job_id=job_id,
        url=url,
        languages=["fr"],
        status=JobStatus.COMPLETED,
        result_uri="s3://b/1",
        video_id=video_id,
    )
    return job_id


def test_ui_delete_job_with_video_also_deletes_video():
    storage = FakeStorage(audio_bytes=b"x")
    catalog = FakeCatalog(rows=[{"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"}])
    client, store, _ = _client(storage=storage, catalog=catalog)
    job_id = _completed_job(store)

    r = client.post(f"/ui/jobs/{job_id}/delete", data={"delete_video": "true"})
    assert r.status_code == 200
    assert job_id not in store.jobs
    assert catalog.get("v1") is None  # video removed too
    assert storage.deleted == ["s3://b/1"]  # and its audio


def test_ui_delete_job_keeps_video_by_default():
    storage = FakeStorage(audio_bytes=b"x")
    catalog = FakeCatalog(rows=[{"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"}])
    client, store, _ = _client(storage=storage, catalog=catalog)
    job_id = _completed_job(store)

    r = client.post(f"/ui/jobs/{job_id}/delete")  # no delete_video flag
    assert r.status_code == 200
    assert job_id not in store.jobs
    assert catalog.get("v1") is not None  # video untouched
    assert storage.deleted == []


def test_ui_delete_dialog_offers_video_option():
    catalog = FakeCatalog(rows=[{"video_id": "v1", "url": "http://y/1", "title": "Ma vidéo"}])
    client, store, _ = _client(catalog=catalog)
    job_id = _completed_job(store)

    r = client.get(f"/ui/jobs/{job_id}/delete-dialog")
    assert r.status_code == 200
    assert "Supprimer le job et la vidéo" in r.text
    assert "Supprimer le job seulement" in r.text
    assert "Ma vidéo" in r.text


def test_ui_job_shows_replay_when_video_deleted():
    # Completed job whose video is no longer in the catalog -> offer a replay.
    client, store, _ = _client(catalog=FakeCatalog(rows=[]))
    _completed_job(store)

    rows = client.get("/ui/partials/jobs-rows")
    assert "Rejouer" in rows.text
    assert "/delete-dialog" not in rows.text  # no video to prompt about


def test_ui_job_with_surviving_video_prompts_no_replay():
    catalog = FakeCatalog(rows=[{"video_id": "v1", "url": "http://y/1"}])
    client, store, _ = _client(catalog=catalog)
    _completed_job(store)

    rows = client.get("/ui/partials/jobs-rows")
    assert "Rejouer" not in rows.text  # video still there, nothing to redo
    assert "/delete-dialog" in rows.text  # delete asks about the video


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


def test_ui_bulk_delete_jobs_with_videos():
    storage = FakeStorage(audio_bytes=b"x")
    catalog = FakeCatalog(
        rows=[
            {"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"},
            {"video_id": "v2", "url": "http://y/2", "storage_uri": "s3://b/2"},
        ]
    )
    client, store, _ = _client(storage=storage, catalog=catalog)
    _completed_job(store, job_id="j1", video_id="v1", url="http://y/1")
    _completed_job(store, job_id="j2", video_id="v2", url="http://y/2")

    r = client.post("/ui/jobs/bulk-delete", data={"job_ids": ["j1", "j2"], "delete_video": "true"})
    assert r.status_code == 200
    assert "j1" not in store.jobs and "j2" not in store.jobs
    assert catalog.get("v1") is None and catalog.get("v2") is None
    assert sorted(storage.deleted) == ["s3://b/1", "s3://b/2"]


def test_ui_bulk_delete_jobs_keeps_videos_by_default():
    storage = FakeStorage(audio_bytes=b"x")
    catalog = FakeCatalog(rows=[{"video_id": "v1", "url": "http://y/1", "storage_uri": "s3://b/1"}])
    client, store, _ = _client(storage=storage, catalog=catalog)
    _completed_job(store, job_id="j1", video_id="v1", url="http://y/1")

    r = client.post("/ui/jobs/bulk-delete", data={"job_ids": ["j1"]})  # no flag
    assert r.status_code == 200
    assert "j1" not in store.jobs
    assert catalog.get("v1") is not None  # video kept
    assert storage.deleted == []


def test_ui_bulk_delete_dialog_counts_videos():
    catalog = FakeCatalog(rows=[{"video_id": "v1", "url": "http://y/1"}])  # only v1 survives
    client, store, _ = _client(catalog=catalog)
    _completed_job(store, job_id="j1", video_id="v1", url="http://y/1")
    _completed_job(store, job_id="j2", video_id="v2", url="http://y/2")  # video already gone

    r = client.get("/ui/jobs/bulk-delete-dialog", params={"job_ids": ["j1", "j2"]})
    assert r.status_code == 200
    assert "(1)" in r.text  # exactly one surviving video offered
    assert "Supprimer les jobs seulement" in r.text


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


# --------------------------------------------------------------------- veille
def _watched(channel_key, url, name=None, active=True):
    from media_ingestion.domain.models import WatchedChannel

    return WatchedChannel(channel_key=channel_key, url=url, name=name, active=active)


def test_veille_run_queues_new_uploads_and_dedups():
    url = "https://www.youtube.com/@acme/videos"
    resolver = FakeChannelResolver({url: _pl_entries("aaaaaaaaaaa", "bbbbbbbbbbb")})
    cstore = FakeChannelStore([_watched("@acme", url, name="Acme")])
    client, store, pub = _client(channel_store=cstore, channel_resolver=resolver)

    r = client.post("/veille/run")
    assert r.status_code == 202
    body = r.json()
    assert body["checked"] == 1 and body["queued"] == 2
    assert len(store.jobs) == 2 and len(pub.events) == 2
    assert cstore.checked and cstore.checked[0][0] == "@acme"

    # second pass: everything already known -> nothing new
    r2 = client.post("/veille/run")
    assert r2.json()["queued"] == 0
    assert len(store.jobs) == 2


def test_veille_run_skips_inactive_channels():
    active_url = "https://www.youtube.com/@on/videos"
    off_url = "https://www.youtube.com/@off/videos"
    resolver = FakeChannelResolver(
        {active_url: _pl_entries("aaaaaaaaaaa"), off_url: _pl_entries("bbbbbbbbbbb")}
    )
    cstore = FakeChannelStore(
        [_watched("@on", active_url), _watched("@off", off_url, active=False)]
    )
    client, store, _ = _client(channel_store=cstore, channel_resolver=resolver)

    body = client.post("/veille/run").json()
    assert body["checked"] == 1 and body["queued"] == 1
    assert [c[0] for c in resolver.calls] == [active_url]


def test_ui_veille_add_and_list():
    cstore = FakeChannelStore()
    client, _, _ = _client(channel_store=cstore)

    r = client.post("/ui/veille/add", data={"channel": "@acme", "name": "Acme"})
    assert r.status_code == 200 and "Chaîne ajoutée" in r.text
    assert r.headers.get("HX-Trigger") == "channelsChanged"
    assert "@acme" in cstore.channels
    assert cstore.channels["@acme"].url == "https://www.youtube.com/@acme/videos"

    page = client.get("/ui/veille")
    assert page.status_code == 200 and "Acme" in page.text


def test_ui_veille_add_rejects_invalid():
    cstore = FakeChannelStore()
    client, _, _ = _client(channel_store=cstore)
    r = client.post("/ui/veille/add", data={"channel": "not a channel!!"})
    assert r.status_code == 200 and "non reconnue" in r.text
    assert not cstore.channels


def test_ui_veille_import_csv():
    cstore = FakeChannelStore([_watched("@existing", "https://www.youtube.com/@existing/videos")])
    client, _, _ = _client(channel_store=cstore)

    csv_content = (
        "channel,name\n"
        "@acme,Acme\n"
        "https://www.youtube.com/@news,News\n"
        "@acme,dup within batch\n"          # duplicate within file
        "@existing,already there\n"          # duplicate of existing
        "not a channel!!,bad\n"              # invalid
        ",\n"                                 # empty -> skipped
    )
    r = client.post(
        "/ui/veille/import-csv",
        files={"file": ("channels.csv", csv_content.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200
    assert "2 chaîne(s) ajoutée(s)" in r.text
    assert "2 doublon(s)" in r.text
    assert "1 ligne(s) invalide(s)" in r.text
    assert r.headers.get("HX-Trigger") == "channelsChanged"
    assert "@acme" in cstore.channels and "@news" in cstore.channels


def test_ui_veille_import_csv_rejects_missing_column():
    client, _, _ = _client(channel_store=FakeChannelStore())
    r = client.post(
        "/ui/veille/import-csv",
        files={"file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")},
    )
    assert r.status_code == 200
    assert "Le CSV doit contenir une colonne" in r.text


def test_api_veille_channels_csv():
    cstore = FakeChannelStore()
    client, _, _ = _client(channel_store=cstore)
    csv_content = "channel\n@acme\nhttps://www.youtube.com/@news\nbogus!!\n"
    r = client.post(
        "/veille/channels/csv",
        files={"file": ("channels.csv", csv_content.encode(), "text/csv")},
    )
    assert r.status_code == 202
    body = r.json()
    assert body == {"added": 2, "duplicates": 0, "invalid": 1}
    assert len(cstore.channels) == 2


def test_ui_settings_page_renders():
    client, _, _ = _client()
    r = client.get("/ui/settings")
    assert r.status_code == 200
    assert "Proxies de téléchargement" in r.text
    assert "appliqué immédiatement" in r.text


def test_ui_settings_save_writes_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setenv("TOUMAI_ENV_FILE", str(env))
    client, _, _ = _client()

    r = client.post(
        "/ui/settings",
        data={
            "languages": "fr, en",
            "proxies": "http://ip1:8080\nhttp://ip2:8080",
            "webshare_username": "user1",
            "veille_recent_limit": "20",
            "max_concurrent_downloads": "5",
            "download_delay_min_s": "1.5",
            "download_delay_max_s": "5",
            "accept_youtube_asr": "true",
        },
    )
    assert r.status_code == 200 and "appliquée" in r.text
    content = env.read_text(encoding="utf-8")
    assert 'TOUMAI_DOWNLOAD_PROXIES=["http://ip1:8080", "http://ip2:8080"]' in content
    assert 'TOUMAI_LANGUAGES=["fr", "en"]' in content
    assert "TOUMAI_WEBSHARE_PROXY_USERNAME=user1" in content
    assert "TOUMAI_VEILLE_RECENT_LIMIT=20" in content
    assert "TOUMAI_ACCEPT_YOUTUBE_ASR=true" in content
    # unchecked box -> false
    assert "TOUMAI_ENABLE_TRANSCRIPT_TRANSLATION=false" in content

    # applied live (no restart): the settings page now reflects the new values
    page = client.get("/ui/settings")
    assert 'value="20"' in page.text            # veille_recent_limit
    assert "http://ip1:8080" in page.text        # proxies textarea
    assert "user1" in page.text                   # webshare username


def test_ui_veille_toggle_pause_resume():
    url = "https://www.youtube.com/@acme/videos"
    cstore = FakeChannelStore([_watched("@acme", url, name="Acme")])
    client, _, _ = _client(channel_store=cstore)

    r = client.post("/ui/veille/toggle", data={"channel_key": "@acme", "active": "false"})
    assert r.status_code == 200
    assert cstore.channels["@acme"].active is False
    assert r.headers.get("HX-Trigger") == "channelsChanged"
    assert "Réactiver" in r.text

    r = client.post("/ui/veille/toggle", data={"channel_key": "@acme", "active": "true"})
    assert cstore.channels["@acme"].active is True
    assert "Mettre en pause" in r.text


def test_ui_veille_delete():
    url = "https://www.youtube.com/@acme/videos"
    cstore = FakeChannelStore([_watched("@acme", url, name="Acme")])
    client, _, _ = _client(channel_store=cstore)

    r = client.post("/ui/veille/delete", data={"channel_key": "@acme"})
    assert r.status_code == 200
    assert "@acme" not in cstore.channels
    assert "Aucune chaîne" in r.text  # empty rows partial


def test_ui_veille_run_now():
    url = "https://www.youtube.com/@acme/videos"
    resolver = FakeChannelResolver({url: _pl_entries("aaaaaaaaaaa")})
    cstore = FakeChannelStore([_watched("@acme", url)])
    client, store, _ = _client(channel_store=cstore, channel_resolver=resolver)

    r = client.post("/ui/veille/run")
    assert r.status_code == 200 and "Veille lancée" in r.text
    assert r.headers.get("HX-Trigger") == "channelsChanged"
    assert len(store.jobs) == 1


def test_veille_run_is_logged_and_visible():
    url = "https://www.youtube.com/@acme/videos"
    resolver = FakeChannelResolver({url: _pl_entries("aaaaaaaaaaa", "bbbbbbbbbbb")})
    cstore = FakeChannelStore([_watched("@acme", url, name="Acme")])
    runlog = FakeRunLog()
    client, _, _ = _client(
        channel_store=cstore, channel_resolver=resolver, veille_run_log=runlog
    )

    client.post("/veille/run")
    assert len(runlog.runs) == 1 and runlog.runs[0].queued == 2

    # JSON monitoring log
    runs = client.get("/veille/runs").json()
    assert runs and runs[0]["checked"] == 1 and runs[0]["queued"] == 2

    # rendered on the veille page + refreshable partials
    page = client.get("/ui/veille")
    assert "Journal de suivi" in page.text
    assert "Acme" in page.text and "+2" in page.text
    rows = client.get("/ui/veille/partials/runs")
    assert "Acme" in rows.text and "+2" in rows.text


def test_ui_veille_kpis():
    url = "https://www.youtube.com/@acme/videos"
    resolver = FakeChannelResolver({url: _pl_entries("aaaaaaaaaaa", "bbbbbbbbbbb")})
    cstore = FakeChannelStore(
        [_watched("@acme", url, name="Acme"), _watched("@off", url, active=False)]
    )
    runlog = FakeRunLog()
    client, _, _ = _client(
        channel_store=cstore, channel_resolver=resolver, veille_run_log=runlog
    )

    # before any run: KPIs render with zeros / one active channel
    kpis = client.get("/ui/veille/partials/kpis")
    assert kpis.status_code == 200
    assert "Chaînes surveillées" in kpis.text
    assert "Vidéos filées" in kpis.text

    client.post("/veille/run")
    kpis = client.get("/ui/veille/partials/kpis").text
    # cumulative queued total (2) shown, and only 1 active channel scanned
    assert "+2" in kpis or ">2<" in kpis
    assert "actives" in kpis
