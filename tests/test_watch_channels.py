"""WatchChannelsUseCase unit tests with in-memory fakes (no yt-dlp, no Kafka)."""

from __future__ import annotations

from media_ingestion.application.watch_channels import WatchChannelsUseCase
from media_ingestion.domain.models import JobStatus, WatchedChannel
from media_ingestion.domain.ports import PlaylistEntry


class FakeChannelStore:
    def __init__(self, channels):
        self.channels = list(channels)
        self.checked = []

    def add(self, channel):  # unused here
        self.channels.append(channel)

    def list_active(self):
        return [c for c in self.channels if c.active]

    def list_all(self):
        return list(self.channels)

    def remove(self, channel_key):
        self.channels = [c for c in self.channels if c.channel_key != channel_key]

    def mark_checked(self, channel_key, when):
        self.checked.append((channel_key, when))


class FakeResolver:
    def __init__(self, by_url):
        self.by_url = by_url

    def recent_uploads(self, channel_url, limit=15):
        return list(self.by_url.get(channel_url, []))[:limit]


class FakeJobStore:
    def __init__(self, existing=()):
        self.jobs = {}
        self._existing = set(existing)

    def create(self, job):
        self.jobs[job.job_id] = job

    def existing_video_ids(self, video_ids):
        wanted = {v for v in video_ids if v}
        known = set(self._existing)
        known |= {j.video_id for j in self.jobs.values() if j.status is not JobStatus.FAILED}
        return wanted & known


class FakePublisher:
    def __init__(self):
        self.batches = []

    def publish(self, topic, key, event):  # unused here
        self.batches.append((topic, [(key, event)]))

    def publish_batch(self, topic, items):
        self.batches.append((topic, list(items)))


def _entries(*vids):
    return [PlaylistEntry(video_id=v, url=f"https://youtu.be/{v}", title=v) for v in vids]


def _channel(key, url, active=True):
    return WatchedChannel(channel_key=key, url=url, name=key, active=active)


def _use_case(channel_store, resolver, job_store, publisher, **kw):
    return WatchChannelsUseCase(
        channel_store=channel_store,
        resolver=resolver,
        job_store=job_store,
        publisher=publisher,
        topic="job.requested",
        default_languages=["fr", "en"],
        recent_limit=kw.get("recent_limit", 15),
    )


def test_queues_only_unknown_videos():
    url = "https://www.youtube.com/@a/videos"
    cstore = FakeChannelStore([_channel("@a", url)])
    resolver = FakeResolver({url: _entries("v1", "v2", "v3")})
    jstore = FakeJobStore(existing={"v2"})  # v2 already ingested
    pub = FakePublisher()

    summary = _use_case(cstore, resolver, jstore, pub).run_once()

    assert summary == {
        "checked": 1,
        "queued": 2,
        "per_channel": [{"channel_key": "@a", "name": "@a", "url": url, "new": 2}],
    }
    queued_vids = {j.video_id for j in jstore.jobs.values()}
    assert queued_vids == {"v1", "v3"}
    # languages propagated, one batch published
    assert all(j.languages == ["fr", "en"] for j in jstore.jobs.values())
    assert len(pub.batches) == 1 and len(pub.batches[0][1]) == 2
    assert cstore.checked and cstore.checked[0][0] == "@a"


def test_dedups_within_and_across_channels():
    u1 = "https://www.youtube.com/@a/videos"
    u2 = "https://www.youtube.com/@b/videos"
    cstore = FakeChannelStore([_channel("@a", u1), _channel("@b", u2)])
    # v1 repeated within @a, and shared with @b
    resolver = FakeResolver({u1: _entries("v1", "v1", "v2"), u2: _entries("v1", "v3")})
    jstore = FakeJobStore()
    pub = FakePublisher()

    summary = _use_case(cstore, resolver, jstore, pub).run_once()

    assert summary["queued"] == 3  # v1, v2, v3 — v1 only once
    assert {j.video_id for j in jstore.jobs.values()} == {"v1", "v2", "v3"}
    assert len(cstore.checked) == 2


def test_skips_inactive_and_handles_empty_channel():
    u1 = "https://www.youtube.com/@a/videos"
    u2 = "https://www.youtube.com/@off/videos"
    cstore = FakeChannelStore([_channel("@a", u1), _channel("@off", u2, active=False)])
    resolver = FakeResolver({u1: [], u2: _entries("v9")})  # @a empty, @off inactive
    jstore = FakeJobStore()
    pub = FakePublisher()

    summary = _use_case(cstore, resolver, jstore, pub).run_once()

    assert summary["checked"] == 1 and summary["queued"] == 0
    assert not jstore.jobs
    # nothing to publish -> no batch call
    assert pub.batches == []
    # the empty active channel is still marked as checked
    assert cstore.checked == [c for c in cstore.checked if c[0] == "@a"]


def test_recent_limit_is_forwarded():
    url = "https://www.youtube.com/@a/videos"
    cstore = FakeChannelStore([_channel("@a", url)])

    seen = {}

    class LimitSpyResolver:
        def recent_uploads(self, channel_url, limit=15):
            seen["limit"] = limit
            return _entries("v1")

    _use_case(cstore, LimitSpyResolver(), FakeJobStore(), FakePublisher(), recent_limit=5).run_once()
    assert seen["limit"] == 5
