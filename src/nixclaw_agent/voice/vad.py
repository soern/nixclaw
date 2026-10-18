"""Voice-activity detection and utterance segmentation.

The segmenter is pure (frames + speech flags in, events out) so it is
unit-testable without audio hardware or webrtcvad.
"""

from collections import deque
from dataclasses import dataclass
from typing import Union

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS // 1000  # s16le mono


@dataclass
class SpeechStarted:
    """Enough consecutive speech to open an utterance (barge-in trigger)."""


@dataclass
class Utterance:
    pcm: bytes          # 16 kHz s16le mono, incl. pre-roll
    speech_ms: int      # voiced duration inside it


SegmentEvent = Union[SpeechStarted, Utterance]


class UtteranceSegmenter:
    """Turns a stream of 20 ms frames into utterances.

    idle -> speaking after `start_speech_ms` of consecutive speech (the frames
    before it, up to `pre_roll_ms`, are kept so word onsets aren't clipped);
    speaking -> done after `end_silence_ms` of silence or `max_utterance_ms`.
    Utterances with less than `min_speech_ms` of speech are dropped (clicks,
    coughs).
    """

    def __init__(self, start_speech_ms: int = 60, end_silence_ms: int = 700,
                 min_speech_ms: int = 250, max_utterance_ms: int = 30000,
                 pre_roll_ms: int = 200, frame_ms: int = FRAME_MS):
        self.frame_ms = frame_ms
        self.start_frames = max(1, start_speech_ms // frame_ms)
        self.end_frames = max(1, end_silence_ms // frame_ms)
        self.min_speech_ms = min_speech_ms
        self.max_frames = max(1, max_utterance_ms // frame_ms)
        self._pre_roll: deque[bytes] = deque(maxlen=max(1, pre_roll_ms // frame_ms))
        self._reset()

    def _reset(self) -> None:
        self.speaking = False
        self._run = 0           # consecutive speech frames while idle
        self._frames: list[bytes] = []
        self._speech_frames = 0
        self._silence_run = 0

    def current_pcm(self) -> bytes:
        """Audio of the utterance in progress so far (barge-in check)."""
        return b"".join(self._frames) if self.speaking else b""

    @property
    def current_speech_ms(self) -> int:
        """Voiced duration of the utterance in progress (0 while idle)."""
        return self._speech_frames * self.frame_ms if self.speaking else 0

    @property
    def silence_ms(self) -> int:
        """Silence since the last speech frame of the utterance in progress."""
        return self._silence_run * self.frame_ms if self.speaking else 0

    def push(self, frame: bytes, is_speech: bool) -> list[SegmentEvent]:
        if not self.speaking:
            self._pre_roll.append(frame)
            self._run = self._run + 1 if is_speech else 0
            if self._run < self.start_frames:
                return []
            self.speaking = True
            self._frames = list(self._pre_roll)
            self._pre_roll.clear()
            self._speech_frames = self._run
            self._silence_run = 0
            return [SpeechStarted()]

        self._frames.append(frame)
        if is_speech:
            self._speech_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
        if self._silence_run >= self.end_frames or len(self._frames) >= self.max_frames:
            return self._finish()
        return []

    def _finish(self) -> list[SegmentEvent]:
        # Keep ~200 ms of the trailing silence, drop the rest.
        keep_tail = min(self._silence_run, 200 // self.frame_ms)
        frames = self._frames[:len(self._frames) - self._silence_run + keep_tail]
        speech_ms = self._speech_frames * self.frame_ms
        self._reset()
        if speech_ms < self.min_speech_ms:
            return []
        return [Utterance(pcm=b"".join(frames), speech_ms=speech_ms)]


class WebRtcVad:
    """webrtcvad wrapper for 16 kHz / 20 ms frames (imported lazily)."""

    def __init__(self, aggressiveness: int):
        import webrtcvad  # type: ignore[import-not-found]
        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, frame: bytes) -> bool:
        return bool(self._vad.is_speech(frame, SAMPLE_RATE))
