"""Tests for playlist id extraction."""

from __future__ import annotations

from media_ingestion.playlist import extract_playlist_id, playlist_url

PL = "PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb"


def test_extract_from_playlist_url():
    assert extract_playlist_id(f"https://www.youtube.com/playlist?list={PL}") == PL


def test_extract_from_watch_url_with_list():
    vid = "dQw4w9WgXcQ"
    assert extract_playlist_id(f"https://www.youtube.com/watch?v={vid}&list={PL}") == PL


def test_extract_from_schemeless_url():
    assert extract_playlist_id(f"youtube.com/playlist?list={PL}") == PL


def test_extract_from_youtu_be_with_list():
    assert extract_playlist_id(f"https://youtu.be/dQw4w9WgXcQ?list={PL}") == PL


def test_extract_bare_id():
    assert extract_playlist_id(PL) == PL
    assert extract_playlist_id("UUxxxxxxxxxxxxxxxxxxxxxx") == "UUxxxxxxxxxxxxxxxxxxxxxx"


def test_no_playlist_returns_none():
    # plain video URL, no list param
    assert extract_playlist_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    # non-YouTube host
    assert extract_playlist_id(f"https://example.com/playlist?list={PL}") is None
    assert extract_playlist_id("") is None
    # a bare id with an illegal char
    assert extract_playlist_id("not a playlist!") is None


def test_playlist_url_roundtrips():
    assert extract_playlist_id(playlist_url(PL)) == PL
