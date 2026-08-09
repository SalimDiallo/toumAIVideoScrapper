"""Selection-strategy tests for YouTubeTranscriptProvider.

We fake the youtube-transcript-api objects so no network is touched. The point
is to prove the *priority* logic: manual > ASR > translated, language order, and
the accept_asr / enable_translation switches.
"""

from __future__ import annotations

import media_ingestion.adapters.youtube_transcript as yt_mod
from media_ingestion.adapters.youtube_transcript import YouTubeTranscriptProvider
from media_ingestion.domain.models import TranscriptSource, VideoMetadata

# The provider only reads `video_id` off the metadata; the rest is filler.
_META = VideoMetadata(video_id="vid", url="https://youtu.be/vid", title="t", provider="youtube")


class FakeSnippet:
    def __init__(self, text: str, start: float, duration: float):
        self.text = text
        self.start = start
        self.duration = duration


class FakeTrack:
    def __init__(self, language_code: str, is_generated: bool, is_translatable: bool = False):
        self.language_code = language_code
        self.is_generated = is_generated
        self.is_translatable = is_translatable
        self.translated_to: str | None = None

    def fetch(self):
        return [FakeSnippet(f"{self.language_code} text", 0.0, 1.0)]

    def translate(self, target: str) -> "FakeTrack":
        t = FakeTrack(target, is_generated=True)
        t.translated_to = target
        return t


class FakeApi:
    """Stand-in for YouTubeTranscriptApi with an instance `list`."""

    tracks: list[FakeTrack] = []

    def list(self, video_id: str):
        return list(self.tracks)


def _install(monkeypatch, tracks: list[FakeTrack]):
    FakeApi.tracks = tracks
    monkeypatch.setattr(yt_mod, "YouTubeTranscriptApi", FakeApi)


def test_prefers_manual_over_asr(monkeypatch):
    _install(monkeypatch, [FakeTrack("fr", is_generated=True), FakeTrack("fr", is_generated=False)])
    t = YouTubeTranscriptProvider().fetch(_META, ["fr"])
    assert t is not None
    assert t.source is TranscriptSource.YOUTUBE_MANUAL


def test_falls_back_to_asr_when_no_manual(monkeypatch):
    _install(monkeypatch, [FakeTrack("fr", is_generated=True)])
    t = YouTubeTranscriptProvider().fetch(_META, ["fr"])
    assert t is not None
    assert t.source is TranscriptSource.YOUTUBE_ASR


def test_rejects_asr_when_disabled(monkeypatch):
    _install(monkeypatch, [FakeTrack("fr", is_generated=True)])
    t = YouTubeTranscriptProvider(accept_asr=False).fetch(_META, ["fr"])
    assert t is None  # -> caller marks the video unavailable


def test_language_preference_order(monkeypatch):
    _install(
        monkeypatch,
        [FakeTrack("en", is_generated=False), FakeTrack("fr", is_generated=False)],
    )
    t = YouTubeTranscriptProvider().fetch(_META, ["fr", "en"])
    assert t is not None
    assert t.language == "fr"


def test_manual_in_other_language_beats_asr_in_preferred(monkeypatch):
    # A human track in the "wrong" language is still better than machine ASR.
    _install(
        monkeypatch,
        [FakeTrack("en", is_generated=False), FakeTrack("fr", is_generated=True)],
    )
    t = YouTubeTranscriptProvider().fetch(_META, ["fr"])
    assert t is not None
    assert t.source is TranscriptSource.YOUTUBE_MANUAL
    assert t.language == "en"


def test_translation_last_resort(monkeypatch):
    _install(monkeypatch, [FakeTrack("es", is_generated=True, is_translatable=True)])
    t = YouTubeTranscriptProvider(accept_asr=False, enable_translation=True).fetch(_META, ["fr"])
    assert t is not None
    assert t.source is TranscriptSource.YOUTUBE_TRANSLATED
    assert t.language == "fr"


def test_none_when_no_tracks(monkeypatch):
    _install(monkeypatch, [])
    assert YouTubeTranscriptProvider().fetch(_META, ["fr"]) is None
