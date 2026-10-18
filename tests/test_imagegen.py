"""Unit tests for src/nixclaw_agent/imagegen.py.

Covers FluxImageClient.generate with a mocked httpx.AsyncClient:
- generation mode: URL, JSON payload shape, decoded PNG bytes returned
- edit mode: multipart shape (files list of ("image", (name, bytes, ctype)) tuples, form data fields)
- HTTP 500, network error, malformed payloads
- shared semaphore: concurrent calls serialized, wait timeout raises
  ImageGenConcurrencyLimitError

Mirrors tests/test_stt.py's _MockResponse / patched-AsyncClient style.
"""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.nixclaw_agent.config import ImageGenConfig
from src.nixclaw_agent.imagegen import (
    FluxImageClient,
    ImageGenConcurrencyLimitError,
    ImageGenError,
    _get_shared_imagegen_semaphore,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-png-bytes"
PNG_B64 = base64.b64encode(PNG_BYTES).decode()


@pytest.fixture(autouse=True)
def _reset_shared_semaphore():
    """Reset the module-level semaphore around every test.

    pytest-asyncio creates a fresh event loop per test, and an
    asyncio.Semaphore binds to the loop it was first awaited in - a
    semaphore created in a previous test's loop would raise RuntimeError
    in this one. Production runs one event loop for the whole process,
    so the global is fine there (same as llm.py's vLLM semaphore).
    """
    import src.nixclaw_agent.imagegen as imagegen
    imagegen._shared_imagegen_semaphore = None
    imagegen._shared_imagegen_semaphore_size = None
    yield
    imagegen._shared_imagegen_semaphore = None
    imagegen._shared_imagegen_semaphore_size = None


def _make_cfg(**overrides) -> ImageGenConfig:
    kwargs = dict(
        enabled=True,
        base_url="http://flux.test:8002",
        model="black-forest-labs/FLUX.2-klein-9B",
        timeout=180.0,
        default_size="1024x1024",
        default_steps=4,
        max_concurrent_requests=1,
        max_concurrent_wait_seconds=5.0,
    )
    kwargs.update(overrides)
    return ImageGenConfig(**kwargs)


class _MockResponse:
    def __init__(self, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self._json = json_payload
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=MagicMock(), response=MagicMock(status_code=self.status_code)
            )


def _mock_client_ctx(resp):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=None)
    ctx.post = AsyncMock(return_value=resp)
    return ctx


def _ok_resp() -> _MockResponse:
    return _MockResponse(200, {"created": 1, "data": [{"b64_json": PNG_B64}]})


class TestGenerate:
    @pytest.mark.asyncio
    async def test_generation_success(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            out = await client.generate("a red bicycle")
        assert out == PNG_BYTES
        call = ctx.post.call_args
        assert call.args[0] == "http://flux.test:8002/v1/images/generations"
        assert "json" in call.kwargs
        assert "files" not in call.kwargs
        body = call.kwargs["json"]
        assert body["prompt"] == "a red bicycle"
        assert body["size"] == "1024x1024"  # default from cfg
        assert body["n"] == 1
        assert body["response_format"] == "b64_json"
        assert body["steps"] == 4  # default from cfg
        assert "seed" not in body

    @pytest.mark.asyncio
    async def test_generation_explicit_size_steps_seed(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            await client.generate("x", size="1536x1024", steps=8, seed=42)
        body = ctx.post.call_args.kwargs["json"]
        assert body["size"] == "1536x1024"
        assert body["steps"] == 8
        assert body["seed"] == 42

    @pytest.mark.asyncio
    async def test_edit_mode_multipart_shape(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            out = await client.generate(
                "make it blue",
                input_images=[("photo.jpg", b"JPEGDATA")],
            )
        assert out == PNG_BYTES
        call = ctx.post.call_args
        assert call.args[0] == "http://flux.test:8002/v1/images/edits"
        assert "files" in call.kwargs
        assert "json" not in call.kwargs
        # files is a list so multiple reference images can share the "image" field
        assert call.kwargs["files"][0][0] == "image"
        name, fbytes, ctype = call.kwargs["files"][0][1]
        assert name == "photo.jpg"
        assert fbytes == b"JPEGDATA"
        assert ctype == "image/jpeg"
        data = call.kwargs["data"]
        assert data["prompt"] == "make it blue"
        assert data["n"] == "1"
        assert data["response_format"] == "b64_json"
        assert data["steps"] == "4"
        assert "seed" not in data
        # size omitted -> server keeps the input image's dimensions
        assert "size" not in data

    @pytest.mark.asyncio
    async def test_edit_mode_multiple_images_same_field(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            await client.generate(
                "combine these",
                input_images=[("a.png", b"AAAA"), ("b.jpg", b"BBBB")],
            )
        files = ctx.post.call_args.kwargs["files"]
        assert [f[0] for f in files] == ["image", "image"]
        assert files[0][1] == ("a.png", b"AAAA", "image/png")
        assert files[1][1] == ("b.jpg", b"BBBB", "image/jpeg")

    @pytest.mark.asyncio
    async def test_edit_mode_unknown_extension_fallback_ctype(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            await client.generate("x", input_images=[("img.unknown", b"XX")])
        _, _, ctype = ctx.post.call_args.kwargs["files"][0][1]
        assert ctype == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_http_500_raises(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(500, text="boom"))
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(ImageGenError, match="HTTP 500"):
                await client.generate("x")

    @pytest.mark.asyncio
    async def test_network_error(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(None)
        ctx.post = AsyncMock(side_effect=httpx.ConnectError("nope"))
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(ImageGenError, match="HTTP error"):
                await client.generate("x")

    @pytest.mark.asyncio
    async def test_malformed_payload_no_data(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(200, {"foo": "bar"}))
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(ImageGenError, match="Malformed"):
                await client.generate("x")

    @pytest.mark.asyncio
    async def test_malformed_payload_missing_b64(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(200, {"data": [{"url": "http://x"}]}))
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(ImageGenError, match="Malformed"):
                await client.generate("x")

    @pytest.mark.asyncio
    async def test_non_dict_payload(self):
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(200, ["data"]))
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(ImageGenError, match="Malformed"):
                await client.generate("x")


class TestSemaphore:
    @pytest.mark.asyncio
    async def test_shared_semaphore_is_process_wide(self):
        sem1 = _get_shared_imagegen_semaphore(1)
        sem2 = _get_shared_imagegen_semaphore(1)
        assert sem1 is sem2

    @pytest.mark.asyncio
    async def test_concurrent_calls_serialized(self):
        client = FluxImageClient(_make_cfg(max_concurrent_requests=1))
        in_flight = 0
        max_in_flight = 0

        async def fake_post(url, *args, **kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return _ok_resp()

        ctx = _mock_client_ctx(None)
        ctx.post = AsyncMock(side_effect=fake_post)
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            results = await asyncio.gather(
                client.generate("a"), client.generate("b")
            )
        assert results == [PNG_BYTES, PNG_BYTES]
        assert max_in_flight == 1, "two generations ran in flight despite cap of 1"

    @pytest.mark.asyncio
    async def test_wait_timeout_raises_concurrency_limit(self):
        sem = _get_shared_imagegen_semaphore(1)
        await sem.acquire()
        try:
            client = FluxImageClient(_make_cfg(max_concurrent_wait_seconds=0.2))
            with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient"):
                with pytest.raises(ImageGenConcurrencyLimitError, match="Timed out"):
                    await client.generate("x")
        finally:
            sem.release()
        # Semaphore must not be leaked: release it back to the pool state.
        assert sem._value == 1

    @pytest.mark.asyncio
    async def test_semaphore_released_after_error(self):
        sem = _get_shared_imagegen_semaphore(1)
        client = FluxImageClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(500, text="boom"))
        with patch("src.nixclaw_agent.imagegen.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(ImageGenError):
                await client.generate("x")
        assert sem._value == 1, "semaphore not released after a failed generation"
