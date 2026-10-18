"""Signal polls: create_poll/delete_message requests and pollVote webhook parsing."""

import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import pytest

from src.nixclaw_agent import signal as signal_mod
from src.nixclaw_agent.config import config
from src.nixclaw_agent.signal import SignalClient, api_recipient
from src.nixclaw_agent.types import Event, PollVote

BOT_NUMBER = "+491234567890"
GROUP_ID = "abcGroupId123=="
GROUP_API = "group." + base64.b64encode(GROUP_ID.encode()).decode()


class _Recorder:
    """Stand-in for httpx.AsyncClient that records requests."""

    def __init__(self, status: int = 201, body: Any = None, raise_exc: Exception | None = None):
        self.calls: list[tuple[str, str, Any]] = []
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        rec = self

        class _Client:
            def __init__(self, timeout):
                assert timeout <= 10.0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def _response(self):
                if rec.raise_exc:
                    raise rec.raise_exc

                def _json():
                    if rec.body is None:
                        raise ValueError("no json")
                    return rec.body

                return SimpleNamespace(status_code=rec.status, text=str(rec.body), json=_json)

            async def post(self, url, json):
                rec.calls.append(("POST", url, json))
                return self._response()

            async def request(self, method, url, json):
                rec.calls.append((method, url, json))
                return self._response()

        self.client_cls = _Client


@pytest.fixture
def recorder(monkeypatch):
    def _make(**kw):
        rec = _Recorder(**kw)
        monkeypatch.setattr(signal_mod.httpx, "AsyncClient", rec.client_cls)
        return rec
    monkeypatch.setattr(config.signal, "mock_api_url", "http://mock")
    return _make


def _client() -> SignalClient:
    return SignalClient(api_url="http://live", phone_number=BOT_NUMBER)


def test_api_recipient():
    assert api_recipient("+4912345678") == "+4912345678"
    assert api_recipient(f"signal-group:{GROUP_ID}") == GROUP_API


class TestCreatePoll:
    @pytest.mark.asyncio
    async def test_one_to_one_body_and_timestamp(self, recorder):
        rec = recorder(body={"timestamp": "1791131093291"})
        ts = await _client().create_poll("+4912345678", "Run it?", ["Yes", "No"])
        assert ts == 1791131093291
        assert rec.calls == [("POST", f"http://live/v1/polls/{BOT_NUMBER}", {
            "recipient": "+4912345678",
            "question": "Run it?",
            "answers": ["Yes", "No"],
            "allow_multiple_selections": False,
        })]

    @pytest.mark.asyncio
    async def test_group_recipient_converted(self, recorder):
        rec = recorder(body={"timestamp": "42"})
        ts = await _client().create_poll(f"signal-group:{GROUP_ID}", "Q", ["Yes", "No"])
        assert ts == 42
        assert rec.calls[0][1] == f"http://live/v1/polls/{BOT_NUMBER}"
        assert rec.calls[0][2]["recipient"] == GROUP_API

    @pytest.mark.asyncio
    async def test_test_number_routes_to_mock(self, recorder):
        rec = recorder(body={"timestamp": "7"})
        assert await _client().create_poll("+000000", "Q", ["Yes", "No"]) == 7
        assert rec.calls[0][1] == f"http://mock/v1/polls/{BOT_NUMBER}"

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self, recorder):
        recorder(status=400, body={"error": "bad"})
        assert await _client().create_poll("+000000", "Q", ["Yes", "No"]) is None

    @pytest.mark.asyncio
    async def test_missing_timestamp_returns_none(self, recorder):
        recorder(body={"foo": 1})
        assert await _client().create_poll("+000000", "Q", ["Yes", "No"]) is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, recorder):
        recorder(raise_exc=RuntimeError("connection refused"))
        assert await _client().create_poll("+000000", "Q", ["Yes", "No"]) is None


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_body_and_success(self, recorder):
        rec = recorder(status=201, body={"timestamp": "1"})
        ok = await _client().delete_message(f"signal-group:{GROUP_ID}", 1791131093291)
        assert ok is True
        assert rec.calls == [("DELETE", f"http://live/v1/remote-delete/{BOT_NUMBER}", {
            "recipient": GROUP_API, "timestamp": 1791131093291,
        })]

    @pytest.mark.asyncio
    async def test_test_number_routes_to_mock(self, recorder):
        rec = recorder(status=204)
        assert await _client().delete_message("+000000", 5) is True
        assert rec.calls[0][1] == f"http://mock/v1/remote-delete/{BOT_NUMBER}"

    @pytest.mark.asyncio
    async def test_failure_returns_false(self, recorder):
        recorder(status=400, body={"error": "x"})
        assert await _client().delete_message("+000000", 5) is False
        recorder(raise_exc=RuntimeError("down"))
        assert await _client().delete_message("+000000", 5) is False


async def _deliver(client: SignalClient, envelope: dict) -> None:
    async def _json():
        return {"envelope": envelope}

    request = SimpleNamespace(headers={}, json=_json)
    response = await client._webhook_handler(request)  # type: ignore[arg-type]
    assert response.status == 200
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _vote_envelope(indexes: list[int], group_id: str | None = None, sync: bool = False) -> dict:
    data: dict[str, Any] = {
        "message": None,
        "pollVote": {
            "author": BOT_NUMBER,
            "authorNumber": BOT_NUMBER,
            "targetSentTimestamp": 1791131093291,
            "optionIndexes": indexes,
            "voteCount": 2,
        },
    }
    if group_id:
        data["groupInfo"] = {"groupId": group_id, "type": "DELIVER"}
    env: dict[str, Any] = {
        "source": "+000001",
        "sourceNumber": "+000001",
        "sourceName": "Alice",
        "timestamp": 1791131099999,
    }
    if sync:
        env["syncMessage"] = {"sentMessage": data}
    else:
        env["dataMessage"] = data
    return env


class TestWebhookPollVote:
    def _client_with_callbacks(self):
        client = _client()
        votes: list[PollVote] = []
        messages: list[Event] = []

        async def _on_vote(v):
            votes.append(v)

        async def _on_message(ev):
            messages.append(ev)

        client._on_message = _on_message
        client._on_poll_vote = _on_vote
        return client, votes, messages

    @pytest.mark.asyncio
    async def test_one_to_one_vote(self):
        client, votes, messages = self._client_with_callbacks()
        await _deliver(client, _vote_envelope([0]))
        assert messages == []
        assert votes == [PollVote(
            channel_id="+000001", voter="+000001", voter_name="Alice",
            target_ts=1791131093291, option_indexes=[0], vote_count=2,
        )]

    @pytest.mark.asyncio
    async def test_group_vote_channel(self):
        client, votes, messages = self._client_with_callbacks()
        await _deliver(client, _vote_envelope([1], group_id=GROUP_ID))
        assert messages == []
        assert votes[0].channel_id == f"signal-group:{GROUP_ID}"
        assert votes[0].voter == "+000001"
        assert votes[0].option_indexes == [1]

    @pytest.mark.asyncio
    async def test_group_vote_channel_matches_chat_event_channel(self):
        """Vote channel_id equals what core derives for a chat message in the group."""
        from src.nixclaw_agent.core import _channel_id_for_event

        client, votes, messages = self._client_with_callbacks()
        await _deliver(client, _vote_envelope([0], group_id=GROUP_ID))
        env = _vote_envelope([0], group_id=GROUP_ID)
        env["dataMessage"].pop("pollVote")
        env["dataMessage"]["message"] = "hi"
        await _deliver(client, env)
        assert votes[0].channel_id == _channel_id_for_event(messages[0])[0]

    @pytest.mark.asyncio
    async def test_unvote_delivered(self):
        client, votes, _ = self._client_with_callbacks()
        await _deliver(client, _vote_envelope([]))
        assert len(votes) == 1 and votes[0].option_indexes == []

    @pytest.mark.asyncio
    async def test_voter_falls_back_to_source(self):
        client, votes, _ = self._client_with_callbacks()
        env = _vote_envelope([0])
        del env["sourceNumber"]
        env["source"] = "+000002"
        del env["sourceName"]
        await _deliver(client, env)
        assert votes[0].voter == "+000002" and votes[0].voter_name is None

    @pytest.mark.asyncio
    async def test_no_voter_dropped(self):
        client, votes, messages = self._client_with_callbacks()
        env = _vote_envelope([0])
        del env["sourceNumber"]
        env["source"] = ""
        await _deliver(client, env)
        assert votes == [] and messages == []

    @pytest.mark.asyncio
    async def test_raising_callback_logged(self, caplog):
        client = _client()

        async def _boom(v):
            raise RuntimeError("boom")

        client._on_poll_vote = _boom
        with caplog.at_level("ERROR"):
            await _deliver(client, _vote_envelope([0]))  # asserts 200
            await asyncio.sleep(0)
        assert "Poll vote callback failed" in caplog.text
        assert not client._poll_vote_tasks

    @pytest.mark.asyncio
    async def test_sync_vote_ignored(self):
        client, votes, messages = self._client_with_callbacks()
        await _deliver(client, _vote_envelope([0], sync=True))
        assert votes == [] and messages == []

    @pytest.mark.asyncio
    async def test_no_callback_ignored(self):
        client = _client()
        messages: list[Event] = []

        async def _on_message(ev):
            messages.append(ev)

        client._on_message = _on_message
        await _deliver(client, _vote_envelope([0]))
        assert messages == []

    @pytest.mark.asyncio
    async def test_malformed_vote_ignored(self):
        client, votes, messages = self._client_with_callbacks()
        env = _vote_envelope([0])
        env["dataMessage"]["pollVote"]["targetSentTimestamp"] = "nope"
        await _deliver(client, env)
        assert votes == [] and messages == []

    @pytest.mark.asyncio
    async def test_poll_create_and_terminate_ignored(self):
        client, votes, messages = self._client_with_callbacks()
        for key in ("pollCreate", "pollTerminate"):
            env = _vote_envelope([0])
            env["dataMessage"] = {"message": "Run it?", key: {"question": "Run it?"}}
            await _deliver(client, env)
        assert votes == [] and messages == []

    @pytest.mark.asyncio
    async def test_text_message_unaffected(self):
        client, votes, messages = self._client_with_callbacks()
        await _deliver(client, {
            "sourceNumber": "+000000",
            "timestamp": 1791131093291,
            "dataMessage": {"message": "hi", "pollVote": None},
        })
        assert votes == []
        assert len(messages) == 1 and messages[0].message == "hi"
