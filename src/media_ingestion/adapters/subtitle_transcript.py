"""TranscriptProviderPort backed by yt-dlp subtitles (non-YouTube platforms).

youtube-transcript-api only speaks YouTube. Every other platform we target
(Vimeo, Dailymotion, TikTok, PeerTube, ...) exposes its caption tracks through
yt-dlp itself, split into two buckets on the extracted info dict:

  * ``subtitles``           — author-provided tracks (human, higher trust)
  * ``automatic_captions``  — machine-generated (ASR, lower trust)

Each bucket maps ``lang -> [{"ext": ..., "url": ...}, ...]``. This adapter lists
those tracks (one metadata request, no audio download), picks the most
trustworthy acceptable one — mirroring the YouTube selection order — then fetches
and parses that single subtitle file.

Selection order: author track in a preferred language, then author in any
language, then (if accept_asr) ASR in a preferred language, then ASR in any.
Returns None when nothing acceptable is found or on any error — the use case
then marks the video ``unavailable``. Many platforms (TikTok clips, Rumble,
Reddit, Odysee) simply carry no captions, which is the None path.

Only WebVTT and SubRip (.srt) tracks are parsed; if a platform only offers other
formats the track is skipped. yt-dlp does the network I/O so cookies/proxies and
platform quirks are handled the same way as the audio download.
"""

from __future__ import annotations

import re

import structlog
import yt_dlp

from ..domain.models import Transcript, TranscriptSegment, TranscriptSource, VideoMetadata

log = structlog.get_logger(__name__)

# Subtitle formats we know how to parse into segments, best first.
_PARSEABLE_EXTS = ("vtt", "srt")


class YtDlpSubtitleProvider:
    def __init__(self, *, accept_asr: bool = True, proxy: str | None = None) -> None:
        # Accept auto-generated captions. Turn off to mark ASR-only videos as
        # unavailable rather than keeping machine captions.
        self._accept_asr = accept_asr
        self._proxy = proxy or None

    def fetch(self, metadata: VideoMetadata, languages: list[str]) -> Transcript | None:
        opts: dict = {"quiet": True, "no_warnings": True, "skip_download": True}
        if self._proxy:
            opts["proxy"] = self._proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(metadata.url, download=False)
                selected = self._select(info, languages)
                if selected is None:
                    return None
                entry, language, source = selected
                raw = ydl.urlopen(entry["url"]).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - any failure means "no transcript"
            log.info("subtitle.unavailable", url=metadata.url, error=str(exc))
            return None

        segments = _parse(raw, entry["ext"])
        if not segments:
            return None
        log.info(
            "subtitle.selected",
            url=metadata.url,
            provider=metadata.provider,
            source=source.value,
            language=language,
            segments=len(segments),
        )
        return Transcript(language=language, source=source, segments=segments)

    # -- selection strategy --------------------------------------------------

    def _select(
        self, info: dict, languages: list[str]
    ) -> tuple[dict, str, TranscriptSource] | None:
        """Pick the most trustworthy parseable track, or None."""
        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}

        # 1-2. author-provided subtitles win, preferred language first.
        chosen = self._first_track(manual, languages)
        if chosen is not None:
            language, entry = chosen
            return entry, language, TranscriptSource.PROVIDER_SUBTITLE

        # 3-4. machine captions, if we accept them.
        if self._accept_asr:
            chosen = self._first_track(auto, languages)
            if chosen is not None:
                language, entry = chosen
                return entry, language, TranscriptSource.PROVIDER_ASR

        return None

    @staticmethod
    def _first_track(
        tracks: dict[str, list[dict]], languages: list[str]
    ) -> tuple[str, dict] | None:
        """First parseable track, honouring language preference then any language."""
        # Preferred languages first (exact code or a "fr-FR" regional variant).
        for lang in languages:
            for code, entries in tracks.items():
                if code == lang or code.startswith(f"{lang}-"):
                    entry = _pick_format(entries)
                    if entry is not None:
                        return code, entry
        # Fallback: any language with a parseable format.
        for code, entries in tracks.items():
            entry = _pick_format(entries)
            if entry is not None:
                return code, entry
        return None


def _pick_format(entries: list[dict]) -> dict | None:
    """Choose a parseable subtitle format (vtt preferred, then srt), or None."""
    for ext in _PARSEABLE_EXTS:
        for entry in entries:
            if entry.get("ext") == ext and entry.get("url"):
                return entry
    return None


# -- parsing -----------------------------------------------------------------

# "00:00:01.000 --> 00:00:04.000" (VTT) or "00:00:01,000 --> 00:00:04,000" (SRT).
_CUE_TIMING_RE = re.compile(
    r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3}"
)
_TIMESTAMP_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{3})")
# Inline VTT markup: cue tags <c>, <00:00:01.000> timestamps, etc.
_TAG_RE = re.compile(r"<[^>]+>")


def _parse(raw: str, ext: str) -> list[TranscriptSegment]:
    """Parse WebVTT / SubRip text into segments. Unknown formats -> empty list."""
    if ext not in _PARSEABLE_EXTS:
        return []
    # VTT and SRT share the same cue shape (timing line + text lines separated by
    # blank lines); the only differences (WEBVTT header, numeric SRT indices,
    # comma vs dot in timestamps) are absorbed by the timing regex below.
    segments: list[TranscriptSegment] = []
    seen: set[tuple[float, str]] = set()
    for block in re.split(r"\r?\n\r?\n", raw):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        timing_idx = next((i for i, ln in enumerate(lines) if _CUE_TIMING_RE.search(ln)), None)
        if timing_idx is None:
            continue
        start, end = _parse_timing(lines[timing_idx])
        if start is None:
            continue
        text = _TAG_RE.sub("", " ".join(lines[timing_idx + 1 :])).strip()
        if not text:
            continue
        # Rolling ASR captions repeat lines across cues; de-dup on (start, text).
        key = (round(start, 2), text)
        if key in seen:
            continue
        seen.add(key)
        duration = max(0.0, end - start) if end is not None else 0.0
        segments.append(TranscriptSegment(start_s=start, duration_s=duration, text=text))
    return segments


def _parse_timing(line: str) -> tuple[float | None, float | None]:
    stamps = _TIMESTAMP_RE.findall(line)
    if len(stamps) < 2:
        return None, None
    return _to_seconds(stamps[0]), _to_seconds(stamps[1])


def _to_seconds(groups: tuple[str, str, str, str]) -> float:
    hours, minutes, seconds, millis = groups
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0
