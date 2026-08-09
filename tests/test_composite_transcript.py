"""CompositeTranscriptProvider routes per platform: YouTube vs everything else."""

from __future__ import annotations

from media_ingestion.adapters.composite_transcript import CompositeTranscriptProvider
from media_ingestion.domain.models import (
    Transcript,
    TranscriptSegment,
    TranscriptSource,
    VideoMetadata,
)


class Spy:
    def __init__(self, source: TranscriptSource):
        self.calls = 0
        self._t = Transcript(
            language="fr", source=source, segments=[TranscriptSegment(0.0, 1.0, "x")]
        )

    def fetch(self, metadata: VideoMetadata, languages: list[str]):
        self.calls += 1
        return self._t


def _meta(provider: str | None) -> VideoMetadata:
    return VideoMetadata(video_id="v", url="https://x/y", title="t", provider=provider)


def test_youtube_provider_routes_to_youtube_backend():
    yt, sub = Spy(TranscriptSource.YOUTUBE_MANUAL), Spy(TranscriptSource.PROVIDER_SUBTITLE)
    comp = CompositeTranscriptProvider(youtube=yt, subtitle=sub)

    t = comp.fetch(_meta("youtube"), ["fr"])

    assert t.source is TranscriptSource.YOUTUBE_MANUAL
    assert (yt.calls, sub.calls) == (1, 0)


def test_other_provider_routes_to_subtitle_backend():
    yt, sub = Spy(TranscriptSource.YOUTUBE_MANUAL), Spy(TranscriptSource.PROVIDER_SUBTITLE)
    comp = CompositeTranscriptProvider(youtube=yt, subtitle=sub)

    t = comp.fetch(_meta("vimeo"), ["fr"])

    assert t.source is TranscriptSource.PROVIDER_SUBTITLE
    assert (yt.calls, sub.calls) == (0, 1)


def test_unknown_provider_falls_back_to_subtitle_backend():
    yt, sub = Spy(TranscriptSource.YOUTUBE_MANUAL), Spy(TranscriptSource.PROVIDER_ASR)
    comp = CompositeTranscriptProvider(youtube=yt, subtitle=sub)

    comp.fetch(_meta(None), ["fr"])

    assert (yt.calls, sub.calls) == (0, 1)
