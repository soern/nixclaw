"""Unit tests for VLLMClient's streaming chat() implementation: SSE reassembly
(content + tool-call deltas), stall-watchdog retry with linear backoff, and
that non-stall errors (context length) are not retried.

Uses a scripted httpx.AsyncBaseTransport instead of a live vLLM backend, so
these run offline and fast (asyncio.sleep is patched out for backoff delays).
"""
import asyncio
import base64
import io
import json
import time

import httpx
import pytest
from PIL import Image

import src.nixclaw_agent.llm as llm_module
from src.nixclaw_agent.config import VllmConfig, VllmOptionsConfig
from src.nixclaw_agent.llm import (
    IMAGE_SHRINK_LADDER_PIXELS,
    ContextLengthExceededError,
    ImageProcessingError,
    ModelNotFoundError,
    StreamStallError,
    VLLMClient,
    VLLMConcurrencyLimitError,
)
from src.nixclaw_agent.types import Tool


@pytest.fixture(autouse=True)
def _reset_shared_vllm_semaphore():
    """The concurrency semaphore is a module-level singleton in llm.py (shared
    across every VLLMClient instance in the process - see llm.py's comment on
    _get_shared_vllm_semaphore), not a per-instance attribute. Reset it around
    every test so one test's `max_concurrent_requests` sizing/state can't leak
    into the next test's `make_client()` calls."""
    llm_module._shared_vllm_semaphore = None
    llm_module._shared_vllm_semaphore_size = None
    yield
    llm_module._shared_vllm_semaphore = None
    llm_module._shared_vllm_semaphore_size = None


@pytest.fixture(autouse=True)
def _reset_image_cap_cache():
    """_vllm_image_max_dimension_cache is module-level (keyed by model id, so
    it survives VLLMClient re-instantiation - see llm.py's comment on it).
    Reset around every test so one test's learned cap can't leak into the
    next test's make_client() calls."""
    llm_module._vllm_image_max_dimension_cache.clear()
    yield
    llm_module._vllm_image_max_dimension_cache.clear()


def _image_b64(width: int, height: int, fmt: str = "JPEG") -> str:
    img = Image.new("RGB", (width, height), color=(120, 40, 200))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _image_size_from_b64(b64_data: str) -> tuple[int, int]:
    with Image.open(io.BytesIO(base64.b64decode(b64_data))) as img:
        return img.size


def _image_size_from_request(request: httpx.Request) -> tuple[int, int]:
    """Decode the first image_url data URL out of a captured vLLM chat
    request body, and return its (width, height)."""
    body = json.loads(request.content)
    for msg in body["messages"]:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") == "image_url":
                data_url = part["image_url"]["url"]
                b64_data = data_url.split(",", 1)[1]
                return _image_size_from_b64(b64_data)
    raise AssertionError("no image_url found in request body")


IMAGE_PROCESSOR_ERROR_BODY = (
    b'{"error":{"message":"Failed to apply Qwen3VLProcessor on data='
    b"{'text': '<|vision_start|><|image_pad|><|vision_end|>', 'images': "
    b"[<PIL.Image.Image image mode=RGB size=4080x1836 at 0xDEADBEEF>]} "
    b"with kwargs={'return_tensors': 'pt'}\","
    b'"type":"BadRequestError","param":null,"code":400}}'
)


class _RecordingScriptedTransport(httpx.AsyncBaseTransport):
    """Like _ScriptedTransport, but also records each request so tests can
    inspect what was actually sent (e.g. the image size after a shrink)."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.call_count = 0
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.requests.append(request)
        return self._responses[min(self.call_count - 1, len(self._responses) - 1)]


def make_client(**opts) -> VLLMClient:
    opts.setdefault("stream_idle_timeout", 0.2)
    opts.setdefault("max_retries", 3)
    options = VllmOptionsConfig(**opts)
    config = VllmConfig(host="http://test-vllm", model="test-model", options=options)
    return VLLMClient(config)


class _FakeAsyncStream(httpx.AsyncByteStream):
    """Yields pre-built SSE byte chunks, optionally sleeping before each."""

    def __init__(self, chunks: list[bytes], delays: "list[float] | None" = None):
        self._chunks = chunks
        self._delays = delays or [0.0] * len(chunks)

    async def __aiter__(self):
        for chunk, delay in zip(self._chunks, self._delays):
            if delay:
                await _REAL_SLEEP(delay)
            yield chunk

    async def aclose(self) -> None:
        return None


def sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


DONE = b"data: [DONE]\n\n"

# Captured before any test monkeypatches asyncio.sleep, so simulated stall
# delays inside _FakeAsyncStream stay real even while backoff sleeps are faked.
_REAL_SLEEP = asyncio.sleep


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Returns one canned httpx.Response per call, in order."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        return self._responses[min(self.call_count - 1, len(self._responses) - 1)]


def install_transport(monkeypatch, transport: httpx.AsyncBaseTransport):
    class _ClientWithTransport(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _ClientWithTransport)


def patch_sleep(monkeypatch) -> list[float]:
    calls: list[float] = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


@pytest.mark.asyncio
async def test_content_deltas_reassembled_across_chunks(monkeypatch):
    stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "Hel"}}]}),
        sse({"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]}),
        DONE,
    ])
    transport = _ScriptedTransport([httpx.Response(200, stream=stream)])
    install_transport(monkeypatch, transport)

    client = make_client()
    text, tool_calls = await client.chat([{"role": "user", "content": "hi"}], [])

    assert text == "Hello"
    assert tool_calls == []
    assert transport.call_count == 1


@pytest.mark.asyncio
async def test_tool_call_deltas_reassembled_across_chunks(monkeypatch):
    stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "foo", "arguments": '{"a":'}}
        ]}}]}),
        sse({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}}
        ]}}]}),
        sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
        DONE,
    ])
    transport = _ScriptedTransport([httpx.Response(200, stream=stream)])
    install_transport(monkeypatch, transport)

    client = make_client()
    tool = Tool(name="foo", description="d", parameters={"type": "object", "properties": {}})
    text, tool_calls = await client.chat([{"role": "user", "content": "hi"}], [tool])

    assert text == ""
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "call_1"
    assert tool_calls[0].name == "foo"
    assert tool_calls[0].arguments == {"a": 1}


@pytest.mark.asyncio
async def test_stall_then_retry_succeeds(monkeypatch):
    stalling_stream = _FakeAsyncStream([sse({"choices": [{"index": 0, "delta": {"content": "late"}}]})], delays=[0.5])
    healthy_stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        DONE,
    ])
    transport = _ScriptedTransport([
        httpx.Response(200, stream=stalling_stream),
        httpx.Response(200, stream=healthy_stream),
    ])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client()
    text, tool_calls = await client.chat([{"role": "user", "content": "hi"}], [])

    assert text == "ok"
    assert transport.call_count == 2
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_backoff_is_linear_in_attempt_number(monkeypatch):
    stall = lambda: httpx.Response(200, stream=_FakeAsyncStream(
        [sse({"choices": [{"index": 0, "delta": {"content": "x"}}]})], delays=[0.5]
    ))
    healthy = httpx.Response(200, stream=_FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        DONE,
    ]))
    transport = _ScriptedTransport([stall(), stall(), stall(), healthy])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client(max_retries=5)
    text, _ = await client.chat([{"role": "user", "content": "hi"}], [])

    assert text == "ok"
    assert sleeps == [3.0, 6.0, 9.0]


@pytest.mark.asyncio
async def test_retries_exhausted_raises_stream_stall_error(monkeypatch):
    stall = lambda: httpx.Response(200, stream=_FakeAsyncStream(
        [sse({"choices": [{"index": 0, "delta": {"content": "x"}}]})], delays=[0.5]
    ))
    transport = _ScriptedTransport([stall(), stall(), stall()])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client(max_retries=2)
    with pytest.raises(StreamStallError):
        await client.chat([{"role": "user", "content": "hi"}], [])

    assert transport.call_count == 3
    assert sleeps == [3.0, 6.0]


class _HangingThenAnsweringServer:
    """Real TCP server on 127.0.0.1: the first `hang_count` connections are
    accepted but never answered (no response headers - SGLang's hung
    scheduler on 2026-10-09), later ones get a complete SSE reply. Needs a
    real socket, because httpx's read timeout is enforced by its network
    backend, which the scripted mock transports above bypass."""

    def __init__(self, hang_count: int):
        self.hang_count = hang_count
        self.connections = 0
        self._release = asyncio.Event()
        self._server: "asyncio.base_events.Server | None" = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        try:
            if self.connections <= self.hang_count:
                await self._release.wait()
                return
            await reader.readuntil(b"\r\n\r\n")
            body = sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}) + DONE
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    async def __aenter__(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aexit__(self, *exc):
        self._release.set()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


def make_client_for(host: str, **opts) -> VLLMClient:
    opts.setdefault("stream_idle_timeout", 0.2)
    opts.setdefault("max_retries", 3)
    return VLLMClient(VllmConfig(host=host, model="test-model", options=VllmOptionsConfig(**opts)))


@pytest.mark.asyncio
async def test_no_response_headers_is_retried_like_a_stall(monkeypatch):
    sleeps = patch_sleep(monkeypatch)
    async with _HangingThenAnsweringServer(hang_count=1) as host:
        client = make_client_for(host)
        text, _ = await asyncio.wait_for(client.chat([{"role": "user", "content": "hi"}], []), timeout=10)

    assert text == "ok"
    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_no_response_headers_exhausts_retries_with_stream_stall_error(monkeypatch):
    sleeps = patch_sleep(monkeypatch)
    server = _HangingThenAnsweringServer(hang_count=99)
    async with server as host:
        client = make_client_for(host, max_retries=2)
        with pytest.raises(StreamStallError, match="no response headers"):
            await asyncio.wait_for(client.chat([{"role": "user", "content": "hi"}], []), timeout=10)

    assert server.connections == 3
    assert sleeps == [3.0, 6.0]


@pytest.mark.asyncio
async def test_context_length_error_is_not_retried(monkeypatch):
    transport = _ScriptedTransport([
        httpx.Response(400, content=b"This model's maximum context length is exceeded"),
    ])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client(max_retries=5)
    with pytest.raises(ContextLengthExceededError):
        await client.chat([{"role": "user", "content": "hi"}], [])

    assert transport.call_count == 1
    assert sleeps == []


class _TrackingByteStream(httpx.AsyncByteStream):
    """Like _FakeAsyncStream, but bumps a shared in-flight counter for the
    duration it is actively being iterated, so tests can observe peak
    concurrency at the point requests are actually streaming.

    Decrements in `aclose()`, not in `__aiter__`'s own generator teardown:
    `_stream_chat_once` breaks its read loop as soon as it sees the SSE
    "[DONE]" sentinel, without asking the underlying async generator for a
    further item, so that generator is left suspended mid-`yield` and never
    naturally reaches its own `finally`. `httpx.AsyncClient.stream()`'s
    context manager, however, unconditionally calls `response.aclose()` in
    its own `finally` (see httpx source), which propagates down to this
    class's `aclose()` - the reliable place to observe "this request's
    stream is done".
    """

    def __init__(self, chunks: list[bytes], delays: list[float], counter: list[int], peak: list[int]):
        self._chunks = chunks
        self._delays = delays
        self._counter = counter
        self._peak = peak
        self._counted = False

    async def __aiter__(self):
        self._counter[0] += 1
        self._peak[0] = max(self._peak[0], self._counter[0])
        self._counted = True
        for chunk, delay in zip(self._chunks, self._delays):
            if delay:
                await _REAL_SLEEP(delay)
            yield chunk

    async def aclose(self) -> None:
        if self._counted:
            self._counter[0] -= 1
            self._counted = False


class _FactoryTransport(httpx.AsyncBaseTransport):
    """Unlike _ScriptedTransport's fixed response list, builds a fresh
    tracked SSE response on every call - needed so N concurrent chat()
    calls each get their own independent stream/iterator."""

    def __init__(self, counter: list[int], peak: list[int], delay: float = 0.1):
        self._counter = counter
        self._peak = peak
        self._delay = delay
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        stream = _TrackingByteStream(
            [sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}), DONE],
            delays=[self._delay, 0.0],
            counter=self._counter,
            peak=self._peak,
        )
        return httpx.Response(200, stream=stream)


@pytest.mark.asyncio
async def test_concurrency_capped_across_multiple_client_instances(monkeypatch):
    """The critical case from plan review: main.py's shared client and
    tools/files.py's per-call `VLLMClient(config.vllm)` are two separate
    instances. Both must share one process-wide concurrency limit."""
    counter = [0]
    peak = [0]
    transport = _FactoryTransport(counter, peak, delay=0.15)
    install_transport(monkeypatch, transport)

    client_a = make_client(max_concurrent_requests=2)
    client_b = make_client(max_concurrent_requests=2)

    tasks = [
        asyncio.create_task(client_a.chat([{"role": "user", "content": "hi"}], []))
        for _ in range(2)
    ] + [
        asyncio.create_task(client_b.chat([{"role": "user", "content": "hi"}], []))
        for _ in range(2)
    ]

    results = await asyncio.gather(*tasks)

    assert transport.call_count == 4
    assert all(text == "ok" for text, _ in results)
    assert peak[0] == 2, f"expected peak in-flight of 2, got {peak[0]}"


@pytest.mark.asyncio
async def test_wait_timeout_raises_concurrency_limit_error(monkeypatch):
    holder_delay = 0.6
    wait_timeout = 0.2

    holder_stream = _FakeAsyncStream(
        [sse({"choices": [{"index": 0, "delta": {"content": "slow"}, "finish_reason": "stop"}]}), DONE],
        delays=[holder_delay, 0.0],
    )
    second_stream = _FakeAsyncStream(
        [sse({"choices": [{"index": 0, "delta": {"content": "fast"}, "finish_reason": "stop"}]}), DONE],
    )
    transport = _ScriptedTransport([
        httpx.Response(200, stream=holder_stream),
        httpx.Response(200, stream=second_stream),
    ])
    install_transport(monkeypatch, transport)

    client = make_client(
        max_concurrent_requests=1,
        max_concurrent_wait_seconds=wait_timeout,
        stream_idle_timeout=5.0,  # well above holder_delay so this test isn't about stall retries
    )

    holder_task = asyncio.create_task(client.chat([{"role": "user", "content": "hi"}], []))
    await _REAL_SLEEP(0.05)  # let the holder task acquire the slot first

    start = time.monotonic()
    with pytest.raises(VLLMConcurrencyLimitError):
        await client.chat([{"role": "user", "content": "hi"}], [])
    elapsed = time.monotonic() - start

    assert elapsed < holder_delay, "should time out on the wait, not on the full held request"
    assert elapsed >= wait_timeout * 0.5

    text, _ = await holder_task
    assert text == "slow"


@pytest.mark.asyncio
async def test_model_not_found_not_retried_when_pinned(monkeypatch):
    """An operator-pinned model (make_client's default) rejected by the backend
    is a misconfiguration, not a runtime model swap - must not be retried."""
    transport = _ScriptedTransport([
        httpx.Response(404, content=b'{"message": "The model `test-model` does not exist."}'),
    ])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client(max_retries=5)
    with pytest.raises(ModelNotFoundError):
        await client.chat([{"role": "user", "content": "hi"}], [])

    assert transport.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_model_not_found_reprobes_and_retries_when_auto_detected(monkeypatch):
    """Backend restarted serving a different model than the one previously
    cached; auto-detect mode should re-query /v1/models and retry with the
    freshly discovered id instead of failing the whole request."""
    healthy_stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        DONE,
    ])
    transport = _ScriptedTransport([
        httpx.Response(404, content=b'{"message": "The model `stale-model` does not exist."}'),
        httpx.Response(200, json={"data": [{"id": "new-model", "object": "model"}]}),
        httpx.Response(200, stream=healthy_stream),
    ])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    options = VllmOptionsConfig(stream_idle_timeout=0.2, max_retries=3)
    config = VllmConfig(host="http://test-vllm", model=None, options=options)
    client = VLLMClient(config)
    client.model = "stale-model"  # simulate a model cached from an earlier, now-outdated backend

    text, _ = await client.chat([{"role": "user", "content": "hi"}], [])

    assert text == "ok"
    assert client.model == "new-model"
    assert transport.call_count == 3
    assert sleeps == []


@pytest.mark.asyncio
async def test_chat_lazily_initializes_when_model_unknown(monkeypatch):
    """Simulates the backend being down at nixclaw startup (self.model stays
    None) and coming back up before the first user message arrives - chat()
    should discover the model itself rather than requiring a restart."""
    healthy_stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        DONE,
    ])
    transport = _ScriptedTransport([
        httpx.Response(200, json={"data": [{"id": "discovered-model", "object": "model"}]}),
        httpx.Response(200, stream=healthy_stream),
    ])
    install_transport(monkeypatch, transport)

    options = VllmOptionsConfig(stream_idle_timeout=0.2, max_retries=3)
    config = VllmConfig(host="http://test-vllm", model=None, options=options)
    client = VLLMClient(config)
    assert client.model is None

    text, _ = await client.chat([{"role": "user", "content": "hi"}], [])

    assert text == "ok"
    assert client.model == "discovered-model"


@pytest.mark.asyncio
async def test_chat_raises_when_backend_still_down_at_first_message(monkeypatch):
    """If the backend is still unreachable when the first message finally
    arrives, chat() should raise the same RuntimeError initialize() would -
    callers (core.py) already turn that into a user-facing 'LLM
    temporarily unavailable' message, same as any other LLM outage."""
    class _AlwaysFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

    install_transport(monkeypatch, _AlwaysFailTransport())

    options = VllmOptionsConfig(stream_idle_timeout=0.2, max_retries=3)
    config = VllmConfig(host="http://test-vllm", model=None, options=options)
    client = VLLMClient(config)

    with pytest.raises(RuntimeError, match="Failed to discover models"):
        await client.chat([{"role": "user", "content": "hi"}], [])


@pytest.mark.asyncio
async def test_image_processing_error_shrinks_and_retries_then_caches(monkeypatch):
    """Reproduces the real-world failure: the backend's multimodal processor
    rejects a full-resolution image outright (400, matching the observed
    'Failed to apply Qwen3VLProcessor ...' body). chat() should downscale
    to the first IMAGE_SHRINK_LADDER_PIXELS step and resend, succeed, and
    cache that cap for the model so later calls don't repeat the failure."""
    healthy_stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        DONE,
    ])
    transport = _RecordingScriptedTransport([
        httpx.Response(400, content=IMAGE_PROCESSOR_ERROR_BODY),
        httpx.Response(200, stream=healthy_stream),
    ])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client(max_retries=3)
    big_image = _image_b64(4080, 1836)
    messages = [{"role": "user", "content": "what's this?", "images": [big_image]}]

    text, _ = await client.chat(messages, [])

    assert text == "ok"
    assert transport.call_count == 2
    assert sleeps == []  # image-shrink retries don't use the stall backoff path

    first_size = _image_size_from_request(transport.requests[0])
    assert first_size == (4080, 1836)  # first attempt sent at native resolution

    second_size = _image_size_from_request(transport.requests[1])
    assert max(second_size) == IMAGE_SHRINK_LADDER_PIXELS[0]

    assert llm_module._vllm_image_max_dimension_cache["test-model"] == IMAGE_SHRINK_LADDER_PIXELS[0]


@pytest.mark.asyncio
async def test_cached_image_cap_is_reused_skipping_native_attempt(monkeypatch):
    """Once a model's working image cap is learned (e.g. by an earlier
    analyze_image call from a different VLLMClient instance - the cache is
    module-level precisely so that's shared), a fresh client for the same
    model id should go straight to the capped size instead of re-failing at
    native resolution first."""
    llm_module._vllm_image_max_dimension_cache["test-model"] = 1024

    healthy_stream = _FakeAsyncStream([
        sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        DONE,
    ])
    transport = _RecordingScriptedTransport([
        httpx.Response(200, stream=healthy_stream),
    ])
    install_transport(monkeypatch, transport)

    client = make_client(max_retries=3)
    messages = [{"role": "user", "content": "what's this?", "images": [_image_b64(4080, 1836)]}]

    text, _ = await client.chat(messages, [])

    assert text == "ok"
    assert transport.call_count == 1  # no failed native-resolution attempt first
    assert max(_image_size_from_request(transport.requests[0])) == 1024


@pytest.mark.asyncio
async def test_image_shrink_ladder_exhausted_raises_image_processing_error(monkeypatch):
    """If the backend rejects the image at every rung of the shrink ladder,
    chat() should give up and raise ImageProcessingError (which core.py's
    generic handler turns into the 'LLM temporarily unavailable' reply)
    rather than retrying forever."""
    transport = _ScriptedTransport([
        httpx.Response(400, content=IMAGE_PROCESSOR_ERROR_BODY),
    ])
    install_transport(monkeypatch, transport)
    sleeps = patch_sleep(monkeypatch)

    client = make_client(max_retries=3)
    messages = [{"role": "user", "content": "what's this?", "images": [_image_b64(4080, 1836)]}]

    with pytest.raises(ImageProcessingError):
        await client.chat(messages, [])

    assert transport.call_count == 1 + len(IMAGE_SHRINK_LADDER_PIXELS)
    assert sleeps == []
    assert "test-model" not in llm_module._vllm_image_max_dimension_cache


@pytest.mark.asyncio
async def test_image_processing_error_without_images_not_retried(monkeypatch):
    """Defensive case: if the backend somehow raises an image-processor-shaped
    error for a request that carried no images, there's nothing to shrink -
    chat() should raise immediately rather than looping."""
    transport = _ScriptedTransport([
        httpx.Response(400, content=IMAGE_PROCESSOR_ERROR_BODY),
    ])
    install_transport(monkeypatch, transport)

    client = make_client(max_retries=3)

    with pytest.raises(ImageProcessingError):
        await client.chat([{"role": "user", "content": "hi"}], [])

    assert transport.call_count == 1
