"""Streaming STT client for calls: one WebSocket per call, one utterance at
a time (deploy/dgx-spark/qwen3-asr/server.py, /v1/audio/transcriptions/stream).

The caller audio is sent while the caller is still speaking, so at the end
of an utterance only a short final decode remains; partial transcripts feed
the barge-in check. `flush()` (the caller went quiet) lets the server decode
before the end of the utterance is certain, `set_partials()` switches the
periodic partial decodes on and off. Every method fails soft: `finish()`
returns None when the stream is unavailable and the session falls back to
the HTTP endpoint; an older server without flush/partials just transcribes
at finish, with partials always on.
"""

import asyncio
import json
import logging
from collections import deque
from typing import Callable, Optional

import aiohttp

LOG = logging.getLogger(__name__)

STREAM_PATH = "/v1/audio/transcriptions/stream"
CONNECT_TIMEOUT_SECONDS = 5.0
FINAL_TIMEOUT_SECONDS = 15.0
# After a failed connect, don't retry for this long (HTTP fallback meanwhile).
RETRY_AFTER_SECONDS = 30.0


def stream_url(base_url: str) -> str:
    """ws(s):// URL of the streaming endpoint for an http(s):// STT base URL."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + STREAM_PATH


class StreamingTranscriber:
    def __init__(self, url: str, language: Optional[str], detect_speech: bool,
                 chunk_ms: int = 1000, on_partial: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = lambda: asyncio.get_running_loop().time()):
        self.url = url
        self.language = language
        self.detect_speech = detect_speech
        self.chunk_ms = chunk_ms
        self.on_partial = on_partial
        self._clock = clock
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._reader: Optional[asyncio.Task] = None
        self._finals: deque[asyncio.Future] = deque()   # one per finish() sent
        self._open = False          # an utterance is being streamed
        self._failed_at: Optional[float] = None
        self._lock = asyncio.Lock()
        self._controls = True       # server understands flush/partials

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def begin(self, pcm: bytes = b"", partials: bool = True) -> bool:
        """Start an utterance (with already captured audio). False = unavailable."""
        async with self._lock:
            if not await self._ensure_connected():
                return False
            try:
                await self._send_json({
                    "type": "start", "language": self.language or None,
                    "chunk_ms": self.chunk_ms, "detect_speech": self.detect_speech,
                    "partials": partials,
                })
                self._open = True
                if pcm:
                    await self._ws.send_bytes(pcm)  # type: ignore[union-attr]
                return True
            except (aiohttp.ClientError, ConnectionError, RuntimeError) as e:
                await self._fail(f"start failed: {e}")
                return False

    async def feed(self, pcm: bytes) -> None:
        if not self._open or not self.connected:
            return
        try:
            await self._ws.send_bytes(pcm)  # type: ignore[union-attr]
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as e:
            await self._fail(f"send failed: {e}")

    async def flush(self) -> None:
        """The caller went quiet: decode what was said so far now."""
        await self._control({"type": "flush"})

    async def set_partials(self, enabled: bool) -> None:
        await self._control({"type": "partials", "enabled": enabled})

    async def _control(self, msg: dict) -> None:
        if not self._open or not self.connected or not self._controls:
            return
        try:
            await self._send_json(msg)
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as e:
            await self._fail(f"{msg['type']} failed: {e}")

    async def finish(self, tail_silence_ms: int = 0) -> Optional[dict]:
        """End the utterance; the final message, or None if the stream failed.

        `tail_silence_ms`: the end of the streamed audio known to be silence
        (lets the server reuse its last partial decode).
        """
        if not self._open or not self.connected:
            self._open = False
            return None
        self._open = False
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._finals.append(fut)
        try:
            await self._send_json({"type": "finish", "tail_silence_ms": tail_silence_ms})
            result: Optional[dict] = await asyncio.wait_for(asyncio.shield(fut), FINAL_TIMEOUT_SECONDS)
            return result
        except (aiohttp.ClientError, ConnectionError, RuntimeError, asyncio.TimeoutError) as e:
            await self._fail(f"no final transcript: {e!r}")
            return None

    async def cancel(self) -> None:
        """Drop the utterance in progress (too short to count)."""
        if not self._open:
            return
        self._open = False
        if self.connected:
            try:
                await self._send_json({"type": "cancel"})
            except (aiohttp.ClientError, ConnectionError, RuntimeError) as e:
                await self._fail(f"cancel failed: {e}")

    async def close(self) -> None:
        self._open = False
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._ws = self._session = self._reader = None
        self._fail_pending()

    async def _ensure_connected(self) -> bool:
        if self.connected:
            return True
        if self._failed_at is not None and self._clock() - self._failed_at < RETRY_AFTER_SECONDS:
            return False
        await self.close()
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT_SECONDS))
            self._ws = await self._session.ws_connect(self.url, heartbeat=20.0)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            detail = getattr(e, "status", None) or e    # handshake errors: just the HTTP status
            await self._fail(f"connect to {self.url} failed: {type(e).__name__} {detail}")
            return False
        self._failed_at = None
        self._reader = asyncio.create_task(self._read_loop())
        LOG.info(f"voice: streaming STT connected ({self.url})")
        return True

    async def _send_json(self, msg: dict) -> None:
        if self._ws is None:
            raise ConnectionError("not connected")
        await self._ws.send_str(json.dumps(msg))

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "final":
                if self._finals:
                    fut = self._finals.popleft()
                    if not fut.done():
                        fut.set_result(data)
            elif kind == "partial":
                # Partials of an utterance arrive before its final; only the
                # one being streamed (no final pending) is of interest.
                if self._open and not self._finals and self.on_partial is not None:
                    self.on_partial(str(data.get("text") or ""))
            elif kind == "error":
                detail = str(data.get("detail"))
                if self._controls and ("'flush'" in detail or "'partials'" in detail):
                    self._controls = False
                    LOG.info("voice: STT server without flush/partials, decoding at the end")
                else:
                    LOG.warning(f"voice: streaming STT error: {detail}")
        self._fail_pending()

    def _fail_pending(self) -> None:
        while self._finals:
            fut = self._finals.popleft()
            if not fut.done():
                fut.set_result(None)    # finish() -> None -> HTTP fallback

    async def _fail(self, why: str) -> None:
        LOG.warning(f"voice: streaming STT unavailable, using HTTP: {why}")
        failed_at = self._clock()
        await self.close()
        self._failed_at = failed_at
