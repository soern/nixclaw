"""Image generation via the FLUX.2 [klein] sidecar server.

This module wraps the image generation service (see
deploy/dgx-spark/flux2-klein/), which exposes an OpenAI-shaped
`POST /v1/images/generations` and `POST /v1/images/edits` endpoint and
returns base64-encoded PNGs.

The GPU is shared with vLLM (llm.py), so concurrent diffusion runs would
thrash both services. A module-level semaphore (see
_get_shared_imagegen_semaphore) caps in-flight generations across the
whole process, mirroring the vLLM concurrency cap.
"""

import asyncio
import base64
import logging
import mimetypes
import time
from typing import Optional

import httpx

from .config import ImageGenConfig

LOG = logging.getLogger(__name__)


class ImageGenError(Exception):
    """Generic image generation failure (HTTP, parse, payload)."""


class ImageGenConcurrencyLimitError(ImageGenError):
    """Timed out waiting for an image generation concurrency slot."""


# Module-level (not a FluxImageClient instance attribute) so the concurrency
# cap is shared across every FluxImageClient in the process. A fresh client
# is created per tool call and a per-instance semaphore would give every
# call its own full-capacity semaphore, bypassing the cap entirely.
# (Same rationale as _shared_vllm_semaphore in llm.py.)
_shared_imagegen_semaphore: Optional[asyncio.Semaphore] = None
_shared_imagegen_semaphore_size: Optional[int] = None


def _get_shared_imagegen_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    """Return the process-wide image generation concurrency semaphore,
    creating it on first use. Later instantiations with a different
    `max_concurrent_requests` do not resize the semaphore - only warn -
    since resizing an in-use asyncio.Semaphore isn't safe to do at runtime."""
    global _shared_imagegen_semaphore, _shared_imagegen_semaphore_size
    if _shared_imagegen_semaphore is None:
        _shared_imagegen_semaphore = asyncio.Semaphore(max_concurrent)
        _shared_imagegen_semaphore_size = max_concurrent
    elif _shared_imagegen_semaphore_size != max_concurrent:
        LOG.warning(
            f"Image generation semaphore already created with size "
            f"{_shared_imagegen_semaphore_size}; ignoring different size "
            f"{max_concurrent} from a later FluxImageClient instantiation"
        )
    return _shared_imagegen_semaphore


def _extract_png(payload) -> bytes:
    """Validate an OpenAI-shaped response and return the decoded PNG bytes."""
    if not isinstance(payload, dict):
        raise ImageGenError(f"Malformed response from image generation service: {payload!r}")
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0].get("b64_json"), str):
        raise ImageGenError(f"Malformed response from image generation service: {payload!r}")
    return base64.b64decode(data[0]["b64_json"])


class FluxImageClient:
    """Thin httpx wrapper around the OpenAI-shaped image generation endpoints."""

    def __init__(self, cfg: ImageGenConfig):
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")
        self._semaphore = _get_shared_imagegen_semaphore(cfg.max_concurrent_requests)

    async def generate(
        self,
        prompt: str,
        *,
        input_images: Optional[list] = None,  # list of (filename, bytes) tuples
        size: Optional[str] = None,
        steps: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> bytes:
        """Generate (or edit, when `input_images` is given) an image.

        Returns the decoded PNG bytes. Raises ImageGenError /
        ImageGenConcurrencyLimitError on any failure.
        """
        effective_steps = steps if steps is not None else self.cfg.default_steps
        if input_images:
            files = []
            for name, img_bytes in input_images:
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                files.append(("image", (name, img_bytes, content_type)))
            data: dict = {
                "prompt": prompt,
                "n": "1",
                "response_format": "b64_json",
                "steps": str(effective_steps),
            }
            if seed is not None:
                data["seed"] = str(seed)
            if size:
                data["size"] = size
            url = f"{self._base}/v1/images/edits"
        else:
            json_body: dict = {
                "prompt": prompt,
                "size": size or self.cfg.default_size,
                "n": 1,
                "response_format": "b64_json",
                "steps": effective_steps,
            }
            if seed is not None:
                json_body["seed"] = seed
            files = None
            url = f"{self._base}/v1/images/generations"

        wait_started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self.cfg.max_concurrent_wait_seconds
            )
        except asyncio.TimeoutError:
            raise ImageGenConcurrencyLimitError(
                f"Timed out after {self.cfg.max_concurrent_wait_seconds}s waiting for an "
                f"image generation concurrency slot "
                f"(max_concurrent_requests={self.cfg.max_concurrent_requests})"
            )

        waited = time.monotonic() - wait_started
        if waited > 0.5:
            LOG.info(
                f"Image generation request queued for {waited:.1f}s "
                f"(max_concurrent_requests={self.cfg.max_concurrent_requests})"
            )

        try:
            # Scale timeout with step count: each pass costs roughly linear
            # time, so high step counts need proportionally more time.
            timeout = httpx.Timeout(
                max(self.cfg.timeout, self.cfg.timeout * effective_steps / max(self.cfg.default_steps, 1)),
                connect=10.0,
            )
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    if files is not None:
                        resp = await client.post(url, files=files, data=data)
                    else:
                        resp = await client.post(url, json=json_body)
            except httpx.TimeoutException as exc:
                # str() of a read timeout is empty, which read as "service
                # down" to the agent; the service was busy, not unreachable.
                raise ImageGenError(
                    f"Image generation service did not answer within {timeout.read:.0f}s "
                    f"({type(exc).__name__}). It is reachable but was too slow - do not retry "
                    f"the same size; omit size or use a smaller one."
                ) from exc
            except httpx.HTTPError as exc:
                raise ImageGenError(
                    f"HTTP error calling image generation service ({url}): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            if resp.status_code != 200:
                raise ImageGenError(
                    f"Image generation service returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            try:
                payload = resp.json()
            except Exception as exc:
                raise ImageGenError(
                    f"Non-JSON response from image generation service: {exc}"
                ) from exc
            return _extract_png(payload)
        finally:
            self._semaphore.release()
