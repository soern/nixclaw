"""Text-to-speech synthesis via the Kokoro-82M ONNX sidecar (see
deploy/cpu/kokoro-tts-german/), which exposes an OpenAI-shaped
`POST /v1/audio/speech` endpoint and returns a raw binary audio body (not
JSON) on success. On failure it returns either a FastAPI `{"detail": ...}`
JSON body (validation errors) or a plain-text body (synthesis/encoding
failures) - the client below never calls `.json()`, only `resp.text[:200]`,
so both shapes render fine in the resulting TTSError message.

Mirrors imagegen.py's shared-semaphore-per-process pattern, with one key
difference: TTS can have multiple engines (one per language, each with its
own `base_url` - see `TTSConfig.engines` in config.py), so the concurrency
cap is keyed by `base_url` rather than being a single process-wide semaphore.
If two language entries share a `base_url` (one multilingual server), they
correctly share the same semaphore slots; different `base_url`s each get
their own independent cap. If a future deployment needs per-language slots
on a single shared server, the key would need to become a
`(base_url, language)` tuple instead.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from .config import TTSEngineConfig

LOG = logging.getLogger(__name__)


class TTSError(Exception):
    """Generic TTS synthesis failure (HTTP, timeout, non-200 response)."""


class TTSConcurrencyLimitError(TTSError):
    """Timed out waiting for a TTS concurrency slot."""


# Module-level (not a KokoroTTSClient instance attribute) so the concurrency
# cap is shared across every KokoroTTSClient in the process, same rationale
# as imagegen.py's _shared_imagegen_semaphore. Keyed by base_url (not a
# single global) because multiple language engines can point at different
# servers, each needing its own independent cap - see module docstring.
_shared_tts_semaphores: dict[str, asyncio.Semaphore] = {}
_shared_tts_semaphore_sizes: dict[str, int] = {}


def _get_shared_tts_semaphore(base_url: str, max_concurrent: int) -> asyncio.Semaphore:
    """Return the process-wide TTS concurrency semaphore for `base_url`,
    creating it on first use. Later instantiations for the same base_url
    with a different `max_concurrent_requests` do not resize the semaphore -
    only warn - since resizing an in-use asyncio.Semaphore isn't safe to do
    at runtime (same rationale as _get_shared_imagegen_semaphore)."""
    existing = _shared_tts_semaphores.get(base_url)
    if existing is None:
        existing = asyncio.Semaphore(max_concurrent)
        _shared_tts_semaphores[base_url] = existing
        _shared_tts_semaphore_sizes[base_url] = max_concurrent
    elif _shared_tts_semaphore_sizes.get(base_url) != max_concurrent:
        LOG.warning(
            f"TTS semaphore for base_url={base_url!r} already created with size "
            f"{_shared_tts_semaphore_sizes.get(base_url)}; ignoring different size "
            f"{max_concurrent} from a later KokoroTTSClient instantiation"
        )
    return existing


class KokoroTTSClient:
    """Thin httpx wrapper around the OpenAI-shaped /v1/audio/speech endpoint."""

    def __init__(self, cfg: TTSEngineConfig):
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._semaphore = _get_shared_tts_semaphore(cfg.base_url, cfg.max_concurrent_requests)

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        response_format: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> tuple[bytes, str]:
        """Synthesize `text` to speech. `extra`: further request fields
        (sampling settings of the Qwen3-TTS server; other servers ignore them).

        Returns (audio_bytes, file_extension). Raises TTSError /
        TTSConcurrencyLimitError on any failure.
        """
        # Fail fast on oversized input, before acquiring the semaphore or
        # making any HTTP call - no point queueing for a request that's
        # certain to be rejected server-side anyway.
        if len(text) > self.cfg.max_input_chars:
            raise TTSError(
                f"Input too long: {len(text)} chars > max_input_chars={self.cfg.max_input_chars}"
            )

        effective_voice = voice if voice is not None else self.cfg.voice
        effective_speed = speed if speed is not None else self.cfg.speed
        effective_format = response_format if response_format is not None else self.cfg.response_format

        wait_started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self.cfg.max_concurrent_wait_seconds
            )
        except asyncio.TimeoutError:
            raise TTSConcurrencyLimitError(
                f"Timed out after {self.cfg.max_concurrent_wait_seconds}s waiting for a "
                f"TTS concurrency slot (max_concurrent_requests={self.cfg.max_concurrent_requests})"
            )

        waited = time.monotonic() - wait_started
        if waited > 0.5:
            LOG.info(
                f"TTS request queued for {waited:.1f}s "
                f"(max_concurrent_requests={self.cfg.max_concurrent_requests})"
            )

        try:
            json_body = {
                "input": text,
                "voice": effective_voice,
                "language": self.cfg.language,
                "response_format": effective_format,
                "speed": effective_speed,
                **(extra or {}),
            }
            url = f"{self._base}/v1/audio/speech"
            timeout = httpx.Timeout(self.cfg.timeout, connect=10.0)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=json_body)
            except httpx.HTTPError as exc:
                raise TTSError(f"HTTP error calling TTS service ({url}): {exc}") from exc

            if resp.status_code != 200:
                raise TTSError(
                    f"TTS service returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            return resp.content, effective_format
        finally:
            self._semaphore.release()

    async def list_voices(self) -> Optional[dict]:
        """Fetch the server's voice catalog (`GET /v1/voices`).

        Returns the parsed JSON body, or None if the server has no voice
        listing (HTTP 404, e.g. the Kokoro sidecars). Raises TTSError on any
        other failure. Not gated by the synthesis semaphore: a cheap metadata
        call shouldn't queue behind minutes-long synthesis requests.
        """
        url = f"{self._base}/v1/voices"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            raise TTSError(f"HTTP error calling TTS service ({url}): {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise TTSError(f"TTS service returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TTSError(f"TTS service returned invalid JSON from {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise TTSError(f"TTS service returned unexpected voice listing from {url}")
        return data
