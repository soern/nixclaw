"""Read receipts mark the end of a turn (core.py _queue_read_receipt /
_flush_read_receipts, signal.py send_read_receipt).

An inbound message gets a read receipt exactly when the agent is done with
it: after the last reply of the turn that handled it, never mid-turn.
Turns are driven through _on_signal_message (the real dispatch path, which
flushes in process_with_semaphore), not process_message directly.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent import signal as signal_mod
from src.nixclaw_agent.config import config, load_config, _validate_reloaded
from src.nixclaw_agent.signal import SignalClient
from src.nixclaw_agent.types import Event, ToolCall, ToolResult

pytestmark = pytest.mark.usefixtures("reset_db")

BOT_NUMBER = "+491234567890"


@pytest.fixture(autouse=True)
def _receipt_config(monkeypatch):
    monkeypatch.setattr(config.chat, "read_receipts", True)
    monkeypatch.setattr(config.signal, "phone_number", BOT_NUMBER)


def _event(source: str, message: str, ts_ms: int, group_id=None, mentions=None) -> Event:
    return Event(
        source=source,
        destination=f"signal-group:{group_id}" if group_id else BOT_NUMBER,
        message=message,
        timestamp=ts_ms // 1000,
        group_id=group_id,
        attachments=[],
        mentions=mentions or [],
        signal_timestamp_ms=ts_ms,
    )


def _track_calls(agent) -> list:
    """Record sends and receipts in one ordered list."""
    calls: list = []

    async def _send(recipient, text, edit_timestamp=None):
        calls.append(("send", text))
        return None

    async def _receipt(sender, ts):
        calls.append(("receipt", sender, ts))

    agent._signal_client.send_message = AsyncMock(side_effect=_send)
    agent._signal_client.send_read_receipt = AsyncMock(side_effect=_receipt)
    return calls


async def _run_turn(agent, event: Event) -> None:
    await agent._on_signal_message(event)
    channel_id = event.destination if event.group_id else event.source
    task = agent._channel_active_task[channel_id]
    await task


def _receipts(calls: list) -> list:
    return [c[1:] for c in calls if c[0] == "receipt"]


class TestWebhookParse:
    @pytest.mark.asyncio
    async def test_webhook_keeps_original_ms_timestamp(self):
        client = SignalClient(api_url="http://x", phone_number=BOT_NUMBER)
        received: list[Event] = []

        async def _on_message(ev):
            received.append(ev)

        client._on_message = _on_message
        payload = {"envelope": {
            "sourceNumber": "+000000",
            "timestamp": 1791131093291,
            "dataMessage": {"message": "hi"},
        }}

        async def _json():
            return payload

        request = SimpleNamespace(headers={}, json=_json)
        await client._webhook_handler(request)  # type: ignore[arg-type]
        await asyncio.sleep(0)

        assert received[0].timestamp == 1791131093
        assert received[0].signal_timestamp_ms == 1791131093291


class TestSendReadReceipt:
    @pytest.mark.asyncio
    async def test_posts_receipt_and_routes_test_numbers_to_mock(self, monkeypatch):
        monkeypatch.setattr(config.signal, "mock_api_url", "http://mock")
        posted: list = []

        class _Client:
            def __init__(self, timeout):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                posted.append((url, json))
                return SimpleNamespace(status_code=204, text="")

        monkeypatch.setattr(signal_mod.httpx, "AsyncClient", _Client)
        client = SignalClient(api_url="http://live", phone_number=BOT_NUMBER)
        await client.send_read_receipt("+000000", 1791131093291)

        assert posted == [(
            f"http://mock/v1/receipts/{BOT_NUMBER}",
            {"receipt_type": "read", "recipient": "+000000", "timestamp": 1791131093291},
        )]

    @pytest.mark.asyncio
    async def test_failure_never_raises(self, monkeypatch):
        class _Client:
            def __init__(self, timeout):
                pass

            async def __aenter__(self):
                raise RuntimeError("connection refused")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(signal_mod.httpx, "AsyncClient", _Client)
        client = SignalClient(api_url="http://live", phone_number=BOT_NUMBER)
        await client.send_read_receipt("+000000", 1791131093291)  # no exception


class TestReceiptAtTurnEnd:
    @pytest.mark.asyncio
    async def test_normal_turn_receipt_after_last_reply(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event("+000000", "hello", 1791131093291))

        assert calls[-1] == ("receipt", "+000000", 1791131093291)
        assert _receipts(calls) == [("+000000", 1791131093291)]
        assert ("send", "Done") in calls

    @pytest.mark.asyncio
    async def test_text_only_colon_turn_still_marks_read(self):
        """The motivating case: a turn ending in "Fixing now:" is over, so
        the message is marked read (the user can tell nothing is running)."""
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._llm_client.chat = AsyncMock(side_effect=[("Fixing now:", [])])

        await _run_turn(agent, _event("+000000", "fix it", 1791131093292))

        assert calls == [("send", "Fixing now:"), ("receipt", "+000000", 1791131093292)]

    @pytest.mark.asyncio
    async def test_no_receipt_mid_turn(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        tool_call = ToolCall(id="1", name="noop_tool", arguments={})
        seen_during_tool: list = []

        async def _exec(name, args, tier):
            seen_during_tool.append(list(_receipts(calls)))
            return ToolResult(name=name, success=True, output="ok", error=None)

        agent._tool_registry.execute_tool = AsyncMock(side_effect=_exec)
        agent._llm_client.chat = AsyncMock(side_effect=[
            ("Let me check", [tool_call]),
            ("Done", []),
        ])

        await _run_turn(agent, _event("+000000", "check", 1791131093293))

        assert seen_during_tool == [[]]
        assert calls[-1] == ("receipt", "+000000", 1791131093293)
        assert len(_receipts(calls)) == 1

    @pytest.mark.asyncio
    async def test_exception_in_turn_still_sends_receipt(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._context_manager.get_context.side_effect = RuntimeError("boom")

        await _run_turn(agent, _event("+000000", "hello", 1791131093294))

        assert _receipts(calls) == [("+000000", 1791131093294)]

    @pytest.mark.asyncio
    async def test_stop_shortcut_marked_read(self):
        agent = _make_agent()
        calls = _track_calls(agent)

        await _run_turn(agent, _event("+000000", "stop", 1791131093295))

        assert calls[0][0] == "send"
        assert calls[-1] == ("receipt", "+000000", 1791131093295)

    @pytest.mark.asyncio
    async def test_cancelled_turn_sends_no_receipt(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        started = asyncio.Event()

        async def _hang(*a, **kw):
            started.set()
            await asyncio.Event().wait()

        agent._llm_client.chat = AsyncMock(side_effect=_hang)
        event = _event("+000000", "hello", 1791131093296)
        await agent._on_signal_message(event)
        task = agent._channel_active_task["+000000"]
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _receipts(calls) == []
        assert "+000000" not in agent._pending_read_receipts


class TestDrainedAndQueued:
    @pytest.mark.asyncio
    async def test_drained_marked_at_turn_end_still_queued_not(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        channel = "+000000"
        drained = _event(channel, "also the logs", 1791131093301)
        still_queued = _event(channel, "and later this", 1791131093302)
        tool_call = ToolCall(id="1", name="noop_tool", arguments={})

        async def _exec(name, args, tier):
            if agent._tool_registry.execute_tool.await_count == 1:
                agent._enqueue_inbound(channel, drained)  # drained at the next checkpoint
            return ToolResult(name=name, success=True, output="ok", error=None)

        responses = [
            ("Checking", [tool_call]),
            ("Checking more", [ToolCall(id="2", name="noop_tool", arguments={"x": 1})]),
            ("Done", []),
            ("Next turn", []),
        ]

        async def _chat(*a, **kw):
            if len(responses) == 2:
                # Arrives after the last checkpoint: starts the next turn.
                agent._enqueue_inbound(channel, still_queued)
            return responses.pop(0)

        agent._tool_registry.execute_tool = AsyncMock(side_effect=_exec)
        agent._llm_client.chat = AsyncMock(side_effect=_chat)

        await _run_turn(agent, _event(channel, "check", 1791131093300))

        # First turn's flush: trigger + drained, not the still-queued one,
        # and only after the turn's last reply (no mid-turn flush).
        assert sorted(_receipts(calls)) == [(channel, 1791131093300), (channel, 1791131093301)]
        last_send = calls.index(("send", "Done"))
        assert all(c[0] == "send" for c in calls[:last_send + 1])
        assert all(c[0] == "receipt" for c in calls[last_send + 1:])

        # The still-queued message starts the next turn and is marked at its end.
        await asyncio.sleep(0)
        next_task = agent._channel_active_task[channel]
        await next_task
        assert _receipts(calls)[-1] == (channel, 1791131093302)

    @pytest.mark.asyncio
    async def test_drained_stop_marked_read(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        channel = "+000000"
        stop = _event(channel, "stop", 1791131093311)
        tool_call = ToolCall(id="1", name="noop_tool", arguments={})

        async def _exec(name, args, tier):
            agent._enqueue_inbound(channel, stop)
            return ToolResult(name=name, success=True, output="ok", error=None)

        agent._tool_registry.execute_tool = AsyncMock(side_effect=_exec)
        agent._llm_client.chat = AsyncMock(side_effect=[
            ("Working", [tool_call]),
            ("Still working", [ToolCall(id="2", name="noop_tool", arguments={"x": 1})]),
            ("Should not happen", []),
        ])

        await _run_turn(agent, _event(channel, "do work", 1791131093310))

        # Aborted at the checkpoint before the third LLM call
        assert agent._llm_client.chat.await_count == 2
        assert sorted(_receipts(calls)) == [(channel, 1791131093310), (channel, 1791131093311)]
        # Receipts only after the canned "halted" reply, the turn's last send
        last_send = max(i for i, c in enumerate(calls) if c[0] == "send")
        assert "halted" in calls[last_send][1].lower()
        assert all(c[0] == "receipt" for c in calls[last_send + 1:])
        assert all(c[0] == "send" for c in calls[:last_send + 1])


class TestSkipped:
    @pytest.mark.asyncio
    async def test_unmentioned_group_message_not_marked(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event("+000000", "chatter", 1791131093320, group_id="grp1"))

        assert _receipts(calls) == []
        agent._llm_client.chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mentioned_group_message_marked(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event(
            "+000000", "hey bot", 1791131093321, group_id="grp1", mentions=[BOT_NUMBER],
        ))

        assert _receipts(calls) == [("+000000", 1791131093321)]

    @pytest.mark.asyncio
    async def test_disabled_switch_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(config.chat, "read_receipts", False)
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event("+000000", "hello", 1791131093330))

        assert _receipts(calls) == []

    @pytest.mark.asyncio
    async def test_own_sync_message_not_marked(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event(BOT_NUMBER, "note to self", 1791131093340))

        assert _receipts(calls) == []

    @pytest.mark.asyncio
    async def test_duplicate_message_not_marked(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._db_ops.is_message_processed = lambda message_id, channel_id: True
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event("+000000", "hello", 1791131093350))

        assert _receipts(calls) == []


class TestFlush:
    @pytest.mark.asyncio
    async def test_duplicate_pending_pairs_send_one_receipt(self):
        agent = _make_agent()
        calls = _track_calls(agent)
        agent._pending_read_receipts["+000000"] = [
            ("+000000", 1791131093370), ("+000000", 1791131093370),
        ]

        await agent._flush_read_receipts("+000000")

        assert _receipts(calls) == [("+000000", 1791131093370)]
        assert "+000000" not in agent._pending_read_receipts


class TestFailures:
    @pytest.mark.asyncio
    async def test_failed_receipt_does_not_break_turn(self):
        agent = _make_agent()
        agent._signal_client.send_read_receipt = AsyncMock(side_effect=RuntimeError("api down"))
        agent._llm_client.chat = AsyncMock(side_effect=[("Done", [])])

        await _run_turn(agent, _event("+000000", "hello", 1791131093360))

        agent._signal_client.send_read_receipt.assert_awaited_once()
        assert "+000000" not in agent._pending_read_receipts


class TestConfig:
    def test_default_on(self):
        assert load_config({}).chat.read_receipts is True

    def test_reload_rejects_non_bool(self):
        cfg = load_config({
            "auth": {"privileged_numbers": ["+000000"]},
            "chat": {"read_receipts": "false"},
        })
        assert any("read_receipts" in e for e in _validate_reloaded(cfg))
        cfg.chat.read_receipts = False
        assert not any("read_receipts" in e for e in _validate_reloaded(cfg))
