"""Provider detection: from a raw URL (best-effort) and from yt-dlp's extractor."""

from __future__ import annotations

import pytest

from media_ingestion.provider import Provider, detect_provider, provider_from_extractor


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.youtube.com/watch?v=abc", Provider.YOUTUBE),
        ("https://youtu.be/abc", Provider.YOUTUBE),
        ("https://vimeo.com/123456", Provider.VIMEO),
        ("https://www.dailymotion.com/video/x9abcd", Provider.DAILYMOTION),
        ("https://www.tiktok.com/@u/video/123", Provider.TIKTOK),
        ("https://rumble.com/v123-title.html", Provider.RUMBLE),
        ("https://odysee.com/@ch:1/video:2", Provider.ODYSEE),
        ("https://www.reddit.com/r/x/comments/abc/title/", Provider.REDDIT),
        ("https://v.redd.it/abc123", Provider.REDDIT),
        # PeerTube is federated -> can't be matched by host.
        ("https://framatube.org/w/abc", Provider.UNKNOWN),
        ("", Provider.UNKNOWN),
        ("not a url", Provider.UNKNOWN),
    ],
)
def test_detect_provider_from_url(url, expected):
    assert detect_provider(url) is expected


@pytest.mark.parametrize(
    "extractor, expected",
    [
        ("Youtube", Provider.YOUTUBE),
        ("youtube", Provider.YOUTUBE),
        ("Vimeo", Provider.VIMEO),
        ("Dailymotion", Provider.DAILYMOTION),
        ("TikTok", Provider.TIKTOK),
        ("Rumble", Provider.RUMBLE),
        ("RumbleEmbed", Provider.RUMBLE),
        ("lbry", Provider.ODYSEE),
        ("PeerTube", Provider.PEERTUBE),
        ("Reddit", Provider.REDDIT),
        ("SomethingElse", Provider.UNKNOWN),
        (None, Provider.UNKNOWN),
    ],
)
def test_provider_from_extractor(extractor, expected):
    assert provider_from_extractor(extractor) is expected
