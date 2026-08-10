"""Provider detection from yt-dlp's extractor key."""

from __future__ import annotations

import pytest

from media_ingestion.provider import Provider, provider_from_extractor


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
