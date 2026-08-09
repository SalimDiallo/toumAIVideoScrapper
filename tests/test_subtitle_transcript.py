"""YtDlpSubtitleProvider: VTT/SRT parsing, track selection, end-to-end with a fake yt-dlp.

No network: we stub yt_dlp.YoutubeDL to return canned info + subtitle bytes.
"""

from __future__ import annotations

import media_ingestion.adapters.subtitle_transcript as sub_mod
from media_ingestion.adapters.subtitle_transcript import YtDlpSubtitleProvider, _parse
from media_ingestion.domain.models import TranscriptSource, VideoMetadata

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.000
Bonjour <c>le</c> monde

00:00:03.000 --> 00:00:05.500
Deuxième ligne
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Bonjour le monde

2
00:00:03,000 --> 00:00:05,500
Deuxième ligne
"""


def _meta(provider: str = "vimeo") -> VideoMetadata:
    return VideoMetadata(video_id="v", url="https://vimeo.com/1", title="t", provider=provider)


# -- parser -----------------------------------------------------------------


def test_parse_vtt_strips_tags_and_computes_duration():
    segs = _parse(VTT, "vtt")
    assert [s.text for s in segs] == ["Bonjour le monde", "Deuxième ligne"]
    assert segs[0].start_s == 1.0
    assert segs[0].duration_s == 2.0
    assert segs[1].duration_s == 2.5


def test_parse_srt():
    segs = _parse(SRT, "srt")
    assert [s.text for s in segs] == ["Bonjour le monde", "Deuxième ligne"]
    assert segs[0].start_s == 1.0


def test_parse_dedups_repeated_rolling_captions():
    rolling = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nsame\n\n00:00:01.000 --> 00:00:02.000\nsame\n"
    )
    assert len(_parse(rolling, "vtt")) == 1


def test_parse_unknown_format_is_empty():
    assert _parse("whatever", "ttml") == []


# -- fake yt-dlp ------------------------------------------------------------


class FakeResp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeYDL:
    """Context-manager stand-in for yt_dlp.YoutubeDL."""

    info: dict = {}
    contents: dict[str, bytes] = {}

    def __init__(self, opts=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(self.info)

    def urlopen(self, url):
        return FakeResp(self.contents[url])


def _install(monkeypatch, info, contents):
    FakeYDL.info = info
    FakeYDL.contents = contents
    monkeypatch.setattr(sub_mod.yt_dlp, "YoutubeDL", FakeYDL)


def test_prefers_manual_subtitles_over_automatic(monkeypatch):
    info = {
        "subtitles": {"fr": [{"ext": "vtt", "url": "man"}]},
        "automatic_captions": {"fr": [{"ext": "vtt", "url": "asr"}]},
    }
    _install(monkeypatch, info, {"man": VTT.encode(), "asr": VTT.encode()})
    t = YtDlpSubtitleProvider().fetch(_meta(), ["fr"])
    assert t is not None
    assert t.source is TranscriptSource.PROVIDER_SUBTITLE
    assert t.language == "fr"
    assert t.text.startswith("Bonjour")


def test_falls_back_to_automatic_when_no_manual(monkeypatch):
    info = {"subtitles": {}, "automatic_captions": {"en": [{"ext": "vtt", "url": "asr"}]}}
    _install(monkeypatch, info, {"asr": VTT.encode()})
    t = YtDlpSubtitleProvider().fetch(_meta(), ["fr"])
    assert t is not None
    assert t.source is TranscriptSource.PROVIDER_ASR
    assert t.language == "en"  # any language beats nothing


def test_rejects_automatic_when_disabled(monkeypatch):
    info = {"subtitles": {}, "automatic_captions": {"fr": [{"ext": "vtt", "url": "asr"}]}}
    _install(monkeypatch, info, {"asr": VTT.encode()})
    assert YtDlpSubtitleProvider(accept_asr=False).fetch(_meta(), ["fr"]) is None


def test_language_preference_order(monkeypatch):
    info = {
        "subtitles": {
            "en": [{"ext": "vtt", "url": "en"}],
            "fr": [{"ext": "vtt", "url": "fr"}],
        },
        "automatic_captions": {},
    }
    _install(monkeypatch, info, {"en": VTT.encode(), "fr": VTT.encode()})
    t = YtDlpSubtitleProvider().fetch(_meta(), ["fr", "en"])
    assert t is not None and t.language == "fr"


def test_none_when_no_captions(monkeypatch):
    _install(monkeypatch, {"subtitles": {}, "automatic_captions": {}}, {})
    assert YtDlpSubtitleProvider().fetch(_meta(), ["fr"]) is None


def test_none_when_only_unparseable_format(monkeypatch):
    info = {"subtitles": {"fr": [{"ext": "ttml", "url": "x"}]}, "automatic_captions": {}}
    _install(monkeypatch, info, {"x": b"<ttml/>"})
    assert YtDlpSubtitleProvider().fetch(_meta(), ["fr"]) is None


def test_returns_none_on_extractor_error(monkeypatch):
    class Boom(FakeYDL):
        def extract_info(self, url, download=False):
            raise RuntimeError("network down")

    monkeypatch.setattr(sub_mod.yt_dlp, "YoutubeDL", Boom)
    assert YtDlpSubtitleProvider().fetch(_meta(), ["fr"]) is None
