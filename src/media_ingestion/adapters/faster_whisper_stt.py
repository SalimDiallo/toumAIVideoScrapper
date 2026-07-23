"""SpeechToTextPort — Phase 2 plug point for videos without YouTube captions.

Not enabled in the MVP. Install the extra (`pip install .[stt]`) and flip
TOUMAI_ENABLE_STT=true to use it. The implementation below is ready to wire:
uncomment the faster-whisper import and the body.
"""

from __future__ import annotations

from ..domain.models import AudioAsset, Transcript, TranscriptSegment, TranscriptSource


class FasterWhisperStt:
    def __init__(self, model_size: str = "base", device: str = "cpu", compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None  # lazy-loaded

    def _ensure_model(self) -> None:
        if self._model is None:
            from faster_whisper import WhisperModel  # imported lazily (optional dep)

            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type
            )

    def transcribe(self, audio: AudioAsset, language: str | None = None) -> Transcript:
        self._ensure_model()
        assert self._model is not None
        segments_iter, info = self._model.transcribe(str(audio.path), language=language)
        segments = [
            TranscriptSegment(start_s=s.start, duration_s=s.end - s.start, text=s.text)
            for s in segments_iter
        ]
        return Transcript(
            language=info.language or (language or "unknown"),
            source=TranscriptSource.STT,
            segments=segments,
        )
