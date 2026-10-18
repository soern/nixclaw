"""Speech output of a call: queued texts -> sentences -> TTS -> playback.

Synthesis runs ahead of playback (up to `lookahead` finished clips, across
queued texts), so the TTS server is never idle while something plays and
speech only lags the text replies when TTS is slower than real time.
`interrupt()` (barge-in) stops playback and drops everything not yet spoken;
every queued item carries a generation number so in-flight results from
before the interrupt are discarded.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, Optional, Protocol

from .text import split_sentences, to_speakable

LOG = logging.getLogger(__name__)

Clip = tuple[bytes, int, int]  # pcm, rate, channels
Synthesize = Callable[[str], Awaitable[Optional[Clip]]]


class Player(Protocol):
    @property
    def playing(self) -> bool: ...
    async def play(self, pcm: bytes, rate: int, channels: int = 1) -> bool: ...
    async def interrupt(self) -> None: ...


class SpeechPipeline:
    def __init__(self, synthesize: Synthesize, player: Player, lookahead: int = 2):
        self._synthesize = synthesize
        self._player = player
        self._gen = 0
        self._sentences: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._clips: asyncio.Queue[tuple[int, str, Clip]] = asyncio.Queue(maxsize=lookahead)
        self._synthesizing = False
        self._played: deque[tuple[float, str]] = deque(maxlen=20)  # (start time, sentence)

    def say(self, text: str) -> None:
        """Queue `text` after what is already queued (non-blocking). From
        silence the first piece is kept short, so playback starts sooner."""
        for sentence in split_sentences(to_speakable(text), short_first=not self.busy):
            self._sentences.put_nowait((self._gen, sentence))

    @property
    def busy(self) -> bool:
        """Something is playing, being synthesized or waiting to be."""
        return (self._player.playing or self._synthesizing
                or not self._sentences.empty() or not self._clips.empty())

    async def run(self) -> None:
        synth = asyncio.create_task(self._synth_loop())
        try:
            await self._play_loop()
        finally:
            synth.cancel()

    async def interrupt(self) -> None:
        self._gen += 1
        for q in (self._sentences, self._clips):
            while not q.empty():
                q.get_nowait()
        await self._player.interrupt()

    def recently_spoken(self, seconds: float = 20.0) -> list[str]:
        """Sentences whose playback started within the last `seconds`."""
        cutoff = time.monotonic() - seconds
        return [text for t, text in self._played if t >= cutoff]

    async def wait_idle(self, poll: float = 0.1) -> None:
        while self.busy:
            await asyncio.sleep(poll)

    async def _synth_loop(self) -> None:
        while True:
            gen, sentence = await self._sentences.get()
            if gen != self._gen:
                continue
            self._synthesizing = True
            try:
                clip = await self._synthesize(sentence)
            except Exception:
                LOG.exception("voice: synthesis failed")
                clip = None
            finally:
                self._synthesizing = False
            if clip is not None and gen == self._gen:
                await self._clips.put((gen, sentence, clip))  # blocks at `lookahead`

    async def _play_loop(self) -> None:
        while True:
            gen, sentence, clip = await self._clips.get()
            if gen != self._gen:
                continue
            self._played.append((time.monotonic(), sentence))
            await self._player.play(*clip)
