"""Channel reference normalization (handle / id / vanity / user paths)."""

from __future__ import annotations

import pytest

from media_ingestion.channel import channel_key, normalize_channel_url

UC = "UC" + "A" * 22  # a well-formed 24-char channel id


@pytest.mark.parametrize(
    "value, key, url",
    [
        ("@Acme", "@acme", "https://www.youtube.com/@acme/videos"),
        ("@Acme.News-1", "@acme.news-1", "https://www.youtube.com/@acme.news-1/videos"),
        (
            "https://www.youtube.com/@Acme",
            "@acme",
            "https://www.youtube.com/@acme/videos",
        ),
        (
            "youtube.com/@Acme/videos",
            "@acme",
            "https://www.youtube.com/@acme/videos",
        ),
        ("m.youtube.com/@Acme", "@acme", "https://www.youtube.com/@acme/videos"),
        (UC, UC, f"https://www.youtube.com/channel/{UC}/videos"),
        (
            f"https://www.youtube.com/channel/{UC}",
            UC,
            f"https://www.youtube.com/channel/{UC}/videos",
        ),
        (
            "https://www.youtube.com/c/SomeName",
            "c/SomeName",
            "https://www.youtube.com/c/SomeName/videos",
        ),
        (
            "https://www.youtube.com/user/SomeUser",
            "user/SomeUser",
            "https://www.youtube.com/user/SomeUser/videos",
        ),
    ],
)
def test_valid_channel_refs(value, key, url):
    assert channel_key(value) == key
    assert normalize_channel_url(value) == url


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not a channel",
        "https://example.com/@acme",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # a video, not a channel
        "UCtooshort",
        "@",  # empty handle
    ],
)
def test_invalid_channel_refs(value):
    assert channel_key(value) is None
    assert normalize_channel_url(value) is None
