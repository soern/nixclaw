"""Unit tests for src/nixclaw_agent/tts.py.

Covers KokoroTTSClient.synthesize with a mocked httpx.AsyncClient:
- success path (JSON payload shape, returned (bytes, extension) tuple)
- HTTP 500 (plain-text body, not JSON) -> TTSError
- network error -> TTSError
- max_input_chars rejection happens before any HTTP call
- shared semaphore keyed by base_url: same base_url -> same semaphore
  object, different base_url -> different semaphore objects; wait-timeout
  raises TTSConcurrencyLimitError without leaking the slot

Mirrors tests/test_imagegen.py's _MockResponse / patched-AsyncClient style.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.nixclaw_agent.config import TTSEngineConfig
from src.nixclaw_agent.tts import (
    KokoroTTSClient,
    TTSConcurrencyLimitError,
    TTSError,
    _get_shared_tts_semaphore,
)

AUDIO_BYTES = b"\x00\x00\x00\x18ftypM4A -fake-m4a-bytes"


@pytest.fixture(autouse=True)
def _reset_shared_semaphores():
    """Reset the module-level semaphore registry around every test.

    pytest-asyncio creates a fresh event loop per test, and an
    asyncio.Semaphore binds to the loop it was first awaited in - a
    semaphore created in a previous test's loop would raise RuntimeError
    in this one. Production runs one event loop for the whole process, so
    the global dict is fine there (same as imagegen.py's semaphore).
    """
    import src.nixclaw_agent.tts as tts
    tts._shared_tts_semaphores = {}
    tts._shared_tts_semaphore_sizes = {}
    yield
    tts._shared_tts_semaphores = {}
    tts._shared_tts_semaphore_sizes = {}


def _make_cfg(**overrides) -> TTSEngineConfig:
    kwargs = dict(
        language="de",
        base_url="http://tts.test:8003",
        model="Godelaune/Kokoro-82M-ONNX-German-Martin",
        voice="martin",
        speed=1.0,
        response_format="aac",
        timeout=120.0,
        max_input_chars=20000,
        max_concurrent_requests=1,
        max_concurrent_wait_seconds=5.0,
    )
    kwargs.update(overrides)
    return TTSEngineConfig(**kwargs)


class _MockResponse:
    def __init__(self, status_code=200, content=b"", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def _mock_client_ctx(resp):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=None)
    ctx.post = AsyncMock(return_value=resp)
    return ctx


def _ok_resp() -> _MockResponse:
    return _MockResponse(200, content=AUDIO_BYTES)


class TestSynthesize:
    @pytest.mark.asyncio
    async def test_success_returns_bytes_and_extension(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            audio, ext = await client.synthesize("Guten Morgen.")
        assert audio == AUDIO_BYTES
        assert ext == "aac"
        call = ctx.post.call_args
        assert call.args[0] == "http://tts.test:8003/v1/audio/speech"
        body = call.kwargs["json"]
        assert body["input"] == "Guten Morgen."
        assert body["voice"] == "martin"  # default from cfg
        assert body["language"] == "de"
        assert body["response_format"] == "aac"  # default from cfg
        assert body["speed"] == 1.0  # default from cfg

    @pytest.mark.asyncio
    async def test_explicit_overrides_used_over_cfg_defaults(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _mock_client_ctx(_ok_resp())
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            audio, ext = await client.synthesize(
                "Hallo", voice="other", speed=1.5, response_format="wav"
            )
        assert ext == "wav"
        body = ctx.post.call_args.kwargs["json"]
        assert body["voice"] == "other"
        assert body["speed"] == 1.5
        assert body["response_format"] == "wav"

    @pytest.mark.asyncio
    async def test_http_500_raises_with_text_body(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(500, text="model not loaded: boom"))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(TTSError, match="HTTP 500") as excinfo:
                await client.synthesize("x")
        # Confirm the plain-text body (not a .json() call) made it into the message -
        # the server returns binary/plain-text bodies on error, not JSON.
        assert "model not loaded: boom" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_network_error_raises_tts_error(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _mock_client_ctx(None)
        ctx.post = AsyncMock(side_effect=httpx.ConnectError("nope"))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(TTSError, match="HTTP error"):
                await client.synthesize("x")

    @pytest.mark.asyncio
    async def test_max_input_chars_rejected_before_any_http_call(self):
        client = KokoroTTSClient(_make_cfg(max_input_chars=10))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient") as mock_cls:
            with pytest.raises(TTSError, match="too long"):
                await client.synthesize("this text is way over the limit")
            mock_cls.assert_not_called()


class TestSharedSemaphore:
    @pytest.mark.asyncio
    async def test_same_base_url_shares_semaphore(self):
        cfg_a = _make_cfg(base_url="http://same:8003")
        cfg_b = _make_cfg(base_url="http://same:8003")
        client_a = KokoroTTSClient(cfg_a)
        client_b = KokoroTTSClient(cfg_b)
        assert client_a._semaphore is client_b._semaphore

    @pytest.mark.asyncio
    async def test_different_base_url_gets_independent_semaphore(self):
        cfg_a = _make_cfg(base_url="http://engine-a:8003")
        cfg_b = _make_cfg(base_url="http://engine-b:8004")
        client_a = KokoroTTSClient(cfg_a)
        client_b = KokoroTTSClient(cfg_b)
        assert client_a._semaphore is not client_b._semaphore

    @pytest.mark.asyncio
    async def test_concurrent_calls_serialized_per_base_url(self):
        client = KokoroTTSClient(_make_cfg(max_concurrent_requests=1))
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
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            results = await asyncio.gather(
                client.synthesize("a"), client.synthesize("b")
            )
        assert [r[0] for r in results] == [AUDIO_BYTES, AUDIO_BYTES]
        assert max_in_flight == 1, "two syntheses ran in flight despite cap of 1"

    @pytest.mark.asyncio
    async def test_wait_timeout_raises_concurrency_limit_without_leaking_slot(self):
        sem = _get_shared_tts_semaphore("http://tts.test:8003", 1)
        await sem.acquire()
        try:
            client = KokoroTTSClient(_make_cfg(max_concurrent_wait_seconds=0.2))
            with patch("src.nixclaw_agent.tts.httpx.AsyncClient"):
                with pytest.raises(TTSConcurrencyLimitError, match="Timed out"):
                    await client.synthesize("x")
        finally:
            sem.release()
        assert sem._value == 1

    @pytest.mark.asyncio
    async def test_semaphore_released_after_error(self):
        sem = _get_shared_tts_semaphore("http://tts.test:8003", 1)
        client = KokoroTTSClient(_make_cfg())
        ctx = _mock_client_ctx(_MockResponse(500, text="boom"))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(TTSError):
                await client.synthesize("x")
        assert sem._value == 1, "semaphore not released after a failed synthesis"

    @pytest.mark.asyncio
    async def test_size_mismatch_on_same_base_url_warns_not_resizes(self, caplog):
        client_a = KokoroTTSClient(_make_cfg(base_url="http://warn-test:8003", max_concurrent_requests=1))
        with caplog.at_level("WARNING"):
            client_b = KokoroTTSClient(
                _make_cfg(base_url="http://warn-test:8003", max_concurrent_requests=5)
            )
        assert client_a._semaphore is client_b._semaphore
        assert client_a._semaphore._value == 1
        assert any("already created with size" in rec.message for rec in caplog.records)


class _JsonResponse(_MockResponse):
    def __init__(self, status_code=200, data=None, text=""):
        super().__init__(status_code, text=text)
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def _get_ctx(resp=None, side_effect=None):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=None)
    ctx.get = AsyncMock(return_value=resp, side_effect=side_effect)
    return ctx


class TestListVoices:
    CATALOG = {"presets": ["aiden", "ryan"], "cloned": {"de-voiceclip": {"language": "de"}}}

    @pytest.mark.asyncio
    async def test_success_returns_catalog(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _get_ctx(_JsonResponse(200, self.CATALOG))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            assert await client.list_voices() == self.CATALOG
        assert ctx.get.call_args.args[0] == "http://tts.test:8003/v1/voices"

    @pytest.mark.asyncio
    async def test_404_means_no_listing(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _get_ctx(_JsonResponse(404, text="Not Found"))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            assert await client.list_voices() is None

    @pytest.mark.asyncio
    async def test_http_500_raises(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _get_ctx(_JsonResponse(500, text="boom"))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(TTSError, match="HTTP 500"):
                await client.list_voices()

    @pytest.mark.asyncio
    async def test_network_error_raises(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _get_ctx(side_effect=httpx.ConnectError("nope"))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(TTSError, match="HTTP error"):
                await client.list_voices()

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self):
        client = KokoroTTSClient(_make_cfg())
        ctx = _get_ctx(_JsonResponse(200, ValueError("bad json")))
        with patch("src.nixclaw_agent.tts.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(TTSError, match="invalid JSON"):
                await client.list_voices()
