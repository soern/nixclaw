"""Webhook authentication: signal.webhook_secret (SIGNAL_WEBHOOK_SECRET).

Anyone who can reach the webhook could fake a message from any number, so with
a secret configured every request must carry it (?token= or header), and
without one the listener binds to 127.0.0.1 only.
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.nixclaw_agent import signal as signal_mod
from src.nixclaw_agent.signal import SignalClient

SECRET = "s3cret-token"


def _request(headers=None, query=None):
    async def _json():
        return {"envelope": {"source": "+000000", "timestamp": 1, "dataMessage": {"message": "hi"}}}

    return SimpleNamespace(headers=headers or {}, query=query or {}, json=_json)


async def _call(client: SignalClient, request) -> tuple[int, list]:
    received: list = []

    async def on_message(event):
        received.append(event)

    client._on_message = on_message
    response = await client._webhook_handler(request)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    return response.status, received


class TestWebhookSecret:
    @pytest.mark.asyncio
    async def test_no_secret_configured_accepts(self):
        status, received = await _call(SignalClient(), _request())
        assert status == 200
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_query_token_accepted(self):
        status, received = await _call(SignalClient(webhook_secret=SECRET), _request(query={"token": SECRET}))
        assert status == 200
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_header_accepted(self):
        status, received = await _call(
            SignalClient(webhook_secret=SECRET), _request(headers={"X-Signal-Hook-Secret": SECRET})
        )
        assert status == 200
        assert len(received) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", [{}, {"token": ""}, {"token": "wrong"}, {"token": SECRET + "x"}])
    async def test_missing_or_wrong_token_rejected(self, query):
        status, received = await _call(SignalClient(webhook_secret=SECRET), _request(query=query))
        assert status == 401
        assert received == []


class TestWebhookBindAddress:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("secret,expected_host", [(None, "127.0.0.1"), (SECRET, "0.0.0.0")])
    async def test_bind_host(self, monkeypatch, secret, expected_host):
        bound: list = []

        class FakeRunner:
            def __init__(self, app, **kwargs):
                self.kwargs = kwargs

            async def setup(self):
                pass

        class FakeSite:
            def __init__(self, runner, host, port):
                bound.append((host, port, runner.kwargs))

            async def start(self):
                pass

        monkeypatch.setattr(signal_mod.web, "AppRunner", FakeRunner)
        monkeypatch.setattr(signal_mod.web, "TCPSite", FakeSite)

        async def on_message(event):
            pass

        await SignalClient(webhook_port=3999, webhook_secret=secret).start_webhook(on_message)
        assert bound == [(expected_host, 3999, {"access_log": None})]
