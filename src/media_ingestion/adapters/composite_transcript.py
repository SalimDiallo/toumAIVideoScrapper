"""TranscriptProviderPort that routes to the right backend per platform.

YouTube has a dedicated, higher-quality caption API (youtube-transcript-api);
every other platform pulls captions through yt-dlp. This composite inspects
``metadata.provider`` and dispatches accordingly, so the use case stays unaware
of which platform it's ingesting.

As a safety net, if the provider is unknown (yt-dlp reported no recognised
extractor) we still try the yt-dlp subtitle path — it works for anything yt-dlp
can read, and simply returns None when there are no captions.
"""

from __future__ import annotations

from ..domain.models import Transcript, VideoMetadata
from ..domain.ports import TranscriptProviderPort
from ..provider import Provider


class CompositeTranscriptProvider:
    def __init__(
        self,
        *,
        youtube: TranscriptProviderPort,
        subtitle: TranscriptProviderPort,
    ) -> None:
        self._youtube = youtube
        self._subtitle = subtitle

    def fetch(self, metadata: VideoMetadata, languages: list[str]) -> Transcript | None:
        if metadata.provider == Provider.YOUTUBE.value:
            return self._youtube.fetch(metadata, languages)
        return self._subtitle.fetch(metadata, languages)
