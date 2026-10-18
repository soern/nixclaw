"""Chat lane: a turn pauses on an approval poll (core.AgentLoop._pause_for_consent).

Uses the shared AgentLoop harness from conftest.py with a fake LLM, a fake
tool registry and a fake Signal client; ConsentManager is real.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop
from src.nixclaw_agent import prompts
from src.nixclaw_agent.config import config
from src.nixclaw_agent.db.operations import DatabaseOperations
from src.nixclaw_agent.turn_output import TurnOutput
from src.nixclaw_agent.types import ConsentRequest, Event, PollVote, ToolCall, ToolResult

pytestmark = pytest.mark.usefixtures("reset_db")

CHANNEL = "+000000"
POLL_TS = 4242
QUESTION = "Print 9 pages?"
BASH_CALL = ToolCall(id="c1", name="bash", arguments={"cmd": "print.sh"})
OTHER_CALL = ToolCall(id="c2", name="file_list", arguments={})


def _event(message, ts=1700000000, source=CHANNEL, group_id=None, destination="+4912345678", mentions=None):
    return Event(source=source, destination=destination, message=message, timestamp=ts,
                 group_id=group_id, attachments=[], mentions=mentions or [],
                 signal_timestamp_ms=ts * 1000)


def _consent_result():
    return ToolResult(
        name="bash", success=False, output=None, error="NOT RUN: needs approval",
        consent_request=ConsentRequest(question=QUESTION, user_text="9 pages, A4",
                                       command="print.sh", arguments={"cmd": "print.sh", "timeout": 30}),
    )


def _agent(llm_replies, monkeypatch):
    monkeypatch.setattr(config.chat, "read_receipts", True)
    agent = build_mock_agent_loop()
    sc = agent._signal_client
    sc.create_poll = AsyncMock(return_value=POLL_TS)
    sc.delete_message = AsyncMock(return_value=True)
    sc.send_message = AsyncMock(return_value=1)
    agent._llm_client.chat = AsyncMock(side_effect=list(llm_replies))
    calls = []

    async def execute_tool(name, args, tier, *, consent_question=None):
        calls.append((name, consent_question))
        if name == "bash" and consent_question is None:
            return _consent_result()
        if name == "bash":
            return ToolResult(name="bash", success=True, output="job 645 submitted", error=None)
        return ToolResult(name=name, success=True, output="files", error=None)

    agent._tool_registry.execute_tool = AsyncMock(side_effect=execute_tool)
    agent.tool_calls_made = calls
    return agent


async def _until_paused(agent):
    for _ in range(200):
        if agent._consent._pending:
            return next(iter(agent._consent._pending.values()))
        await asyncio.sleep(0.005)
    raise AssertionError("turn never paused on a poll")


def _vote(index, ts=POLL_TS, channel=CHANNEL):
    return PollVote(channel_id=channel, voter="+000000", voter_name="Bob", target_ts=ts,
                    option_indexes=[index], vote_count=1)


def _tool_contents(messages):
    return {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}


def _assert_valid_order(messages):
    """Every assistant tool_calls message is followed directly by one tool
    result per call (S1)."""
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            following = messages[i + 1:i + 1 + len(ids)]
            assert [f.get("role") for f in following] == ["tool"] * len(ids), messages
            assert [f["tool_call_id"] for f in following] == ids


@pytest.mark.asyncio
async def test_yes_reruns_with_consent_and_continues(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("Printed.", [])], monkeypatch)
    resets = []
    orig_reset = TurnOutput.reset_target
    monkeypatch.setattr(TurnOutput, "reset_target", lambda self: (resets.append(1), orig_reset(self))[1])

    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    # Info text + poll posted directly; read receipts flushed; DB row stored.
    agent._signal_client.send_message.assert_any_await(CHANNEL, "9 pages, A4")
    agent._signal_client.create_poll.assert_awaited_once_with(CHANNEL, QUESTION, ["Yes", "No"])
    agent._signal_client.send_read_receipt.assert_awaited_once_with(CHANNEL, 1700000000 * 1000)
    assert DatabaseOperations().get_consent_polls() == [(CHANNEL, POLL_TS)]
    assert resets

    await agent._consent.on_vote(_vote(0))
    await asyncio.wait_for(turn, 5)

    assert agent.tool_calls_made == [("bash", None), ("bash", QUESTION)]
    messages = agent._llm_client.chat.await_args_list[1].args[0]
    _assert_valid_order(messages)
    content = _tool_contents(messages)["c1"]
    assert "job 645 submitted" in content and "(approved by Bob)" in content
    assert DatabaseOperations().get_consent_polls() == []
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)


@pytest.mark.asyncio
async def test_no_gives_rejected_result_and_skips_remaining_calls(monkeypatch):
    agent = _agent([("", [BASH_CALL, OTHER_CALL]), ("OK, not printed.", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    await agent._consent.on_vote(_vote(1))
    await asyncio.wait_for(turn, 5)

    assert agent.tool_calls_made == [("bash", None)]
    messages = agent._llm_client.chat.await_args_list[1].args[0]
    _assert_valid_order(messages)
    contents = _tool_contents(messages)
    assert contents["c1"] == "Error: " + prompts.current().render("consent-rejected", voter="Bob", question=QUESTION)
    assert contents["c2"] == "Error: " + prompts.current().text("consent-skipped")


@pytest.mark.asyncio
async def test_after_yes_remaining_calls_run(monkeypatch):
    agent = _agent([("", [BASH_CALL, OTHER_CALL]), ("Done.", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    await agent._consent.on_vote(_vote(0))
    await asyncio.wait_for(turn, 5)
    assert agent.tool_calls_made == [("bash", None), ("bash", QUESTION), ("file_list", None)]


@pytest.mark.asyncio
async def test_message_instead_of_vote_goes_into_tool_result(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("Which printer? The Brother.", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    agent._enqueue_inbound(CHANNEL, _event("which printer?", ts=1700000100))
    await asyncio.wait_for(turn, 5)

    assert agent.tool_calls_made == [("bash", None)]
    messages = agent._llm_client.chat.await_args_list[1].args[0]
    _assert_valid_order(messages)
    assert messages[-1]["role"] == "tool"  # no separate user turn
    assert _tool_contents(messages)["c1"] == "Error: " + prompts.current().render(
        "consent-not-run-message", question=QUESTION, message="which printer?")
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    assert DatabaseOperations().get_consent_polls() == []
    assert agent._consent._pending == {}


@pytest.mark.asyncio
async def test_timeout_gives_expired_result_and_deletes_poll(monkeypatch):
    monkeypatch.setattr(config.consent, "timeout_minutes", 0.002)  # 0.12 s
    agent = _agent([("", [BASH_CALL]), ("Nobody answered.", [])], monkeypatch)
    await asyncio.wait_for(agent.process_message(_event("print it")), 5)

    messages = agent._llm_client.chat.await_args_list[1].args[0]
    assert _tool_contents(messages)["c1"] == "Error: " + prompts.current().render("consent-expired", question=QUESTION)
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    assert DatabaseOperations().get_consent_polls() == []


@pytest.mark.asyncio
async def test_stop_while_paused_ends_turn_and_deletes_poll(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("never", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    agent._enqueue_inbound(CHANNEL, _event("stop", ts=1700000100))
    agent._enqueue_inbound(CHANNEL, _event("and something else", ts=1700000101))
    await asyncio.wait_for(turn, 5)

    assert agent._llm_client.chat.await_count == 1
    # Like the normal checkpoint abort: the rest of the queue is dropped.
    assert CHANNEL not in agent._channel_inbound_queue
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    assert agent._consent._pending == {}


@pytest.mark.asyncio
async def test_unmentioned_group_chatter_keeps_waiting(monkeypatch):
    group = "signal-group:g1"
    agent = _agent([("", [BASH_CALL]), ("Printed.", [])], monkeypatch)

    def group_event(message, ts, mentions=None):
        return _event(message, ts=ts, group_id="g1", destination=group, mentions=mentions)

    turn = asyncio.create_task(agent.process_message(
        group_event("print it", 1700000000, mentions=[config.signal.phone_number])))
    pending = await _until_paused(agent)
    agent._enqueue_inbound(group, group_event("nice weather today", 1700000100))
    for _ in range(10):
        await asyncio.sleep(0.01)
    assert not turn.done()
    assert not pending.future.done()
    await agent._consent.on_vote(_vote(0, channel=group))
    await asyncio.wait_for(turn, 5)
    assert agent.tool_calls_made == [("bash", None), ("bash", QUESTION)]


@pytest.mark.asyncio
async def test_vote_and_message_together_vote_wins_message_stays_queued(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("Printed.", [OTHER_CALL]), ("Done.", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    agent._enqueue_inbound(CHANNEL, _event("also staple it", ts=1700000100))
    await agent._consent.on_vote(_vote(0))  # no await point before the turn sees both
    await asyncio.wait_for(turn, 5)

    assert agent.tool_calls_made[:2] == [("bash", None), ("bash", QUESTION)]
    second = agent._llm_client.chat.await_args_list[1].args[0]
    assert "job 645 submitted" in _tool_contents(second)["c1"]
    # The queued message reached the next normal checkpoint.
    third = agent._llm_client.chat.await_args_list[2].args[0]
    assert any(m.get("role") == "user" and "also staple it" in m.get("content", "") for m in third)


@pytest.mark.asyncio
async def test_poll_send_failure_is_a_tool_error_without_pause(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("Could not ask.", [])], monkeypatch)
    agent._signal_client.create_poll = AsyncMock(return_value=None)
    await asyncio.wait_for(agent.process_message(_event("print it")), 5)

    messages = agent._llm_client.chat.await_args_list[1].args[0]
    assert "Could not ask the users for approval" in _tool_contents(messages)["c1"]
    assert DatabaseOperations().get_consent_polls() == []
    assert agent.tool_calls_made == [("bash", None)]


@pytest.mark.asyncio
async def test_shutdown_ends_paused_turn(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("never", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    agent._signal_client.send_message.reset_mock()
    await agent.shutdown()
    await asyncio.wait_for(turn, 5)
    assert agent._llm_client.chat.await_count == 1
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    # Same notice as the startup cleanup after a crash, posted once.
    agent._signal_client.send_message.assert_awaited_once_with(
        CHANNEL, prompts.current().text("consent-expired-restart"))
    assert DatabaseOperations().get_consent_polls() == []


@pytest.mark.asyncio
async def test_cancelled_turn_deletes_poll(monkeypatch):
    agent = _agent([("", [BASH_CALL]), ("never", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    assert DatabaseOperations().get_consent_polls() == []


@pytest.mark.asyncio
async def test_vote_arriving_while_message_is_preprocessed_wins(monkeypatch):
    """The message is already consumed; the vote still wins and the message
    is injected as a normal inbound-drain turn after the step's tool results."""
    agent = _agent([("", [BASH_CALL]), ("Printed, and noted.", [])], monkeypatch)
    original = agent._preprocess_incoming

    async def preprocess(event, channel_id, reply_destination):
        result = await original(event, channel_id, reply_destination)
        if event.message == "also staple it":
            await agent._consent.on_vote(_vote(0))
        return result

    monkeypatch.setattr(agent, "_preprocess_incoming", preprocess)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    await _until_paused(agent)
    agent._enqueue_inbound(CHANNEL, _event("also staple it", ts=1700000100))
    await asyncio.wait_for(turn, 5)

    assert agent.tool_calls_made == [("bash", None), ("bash", QUESTION)]
    messages = agent._llm_client.chat.await_args_list[1].args[0]
    _assert_valid_order(messages)
    assert "(approved by Bob)" in _tool_contents(messages)["c1"]
    assert messages[-1] == {"role": "user", "content": prompts.current().render("inbound-drain", message="also staple it")}


@pytest.mark.asyncio
async def test_identical_call_after_approval_asks_again(monkeypatch):
    again = ToolCall(id="c3", name="bash", arguments={"cmd": "print.sh"})
    agent = _agent([("", [BASH_CALL]), ("", [again]), ("Done.", [])], monkeypatch)
    turn = asyncio.create_task(agent.process_message(_event("print it")))
    first = await _until_paused(agent)
    await agent._consent.on_vote(_vote(0))
    for _ in range(200):
        if agent._signal_client.create_poll.await_count == 2:
            break
        await asyncio.sleep(0.005)
    second = await _until_paused(agent)
    assert second is not first
    await agent._consent.on_vote(_vote(1))
    await asyncio.wait_for(turn, 5)

    assert agent._signal_client.create_poll.await_count == 2
    assert agent.tool_calls_made == [("bash", None), ("bash", QUESTION), ("bash", None)]
    contents = _tool_contents(agent._llm_client.chat.await_args_list[2].args[0])
    assert "rejected" in contents["c3"]


@pytest.mark.asyncio
async def test_real_registry_and_bash_end_to_end(monkeypatch, tmp_path):
    """consent_question travels core -> ToolRegistry -> bash: the re-run sees
    NIXCLAW_CONSENT=1."""
    from src.nixclaw_agent.auth import AuthConfig, AuthManager
    from src.nixclaw_agent.tools.bash import bash
    from src.nixclaw_agent.tools.registry import ToolRegistry
    from src.nixclaw_agent.types import Context

    agent = _agent([("", [BASH_CALL]), ("Printed.", [])], monkeypatch)
    registry = ToolRegistry(AuthManager(AuthConfig(privileged_numbers=[CHANNEL])))
    registry.register("bash", bash, {})
    agent._tool_registry = registry
    channel_dir = tmp_path / CHANNEL  # reset_db points data_dir at tmp_path
    channel_dir.mkdir()
    agent._context_manager.get_context.return_value = Context(
        channel_id=CHANNEL, messages_db=tmp_path / "m.db", attachments_dir=channel_dir / "attachments")
    (channel_dir / "print.sh").write_text(
        'm="NIXCLAW_REQUIRE_CONSENT_QUESTION_${NIXCLAW_CHANNEL_ID}=\\"Print 9 pages?\\""\n'
        'echo "9 pages, A4"\n'
        'echo "$m" >&2\n'
        'if [ "$NIXCLAW_CONSENT" = 1 ]; then echo printed > approved.txt; echo "job 7"; fi\n'
    )
    call = ToolCall(id="c1", name="bash", arguments={"cmd": "bash print.sh"})
    agent._llm_client.chat = AsyncMock(side_effect=[("", [call]), ("Printed.", [])])

    turn = asyncio.create_task(agent.process_message(_event("print it")))
    pending = await _until_paused(agent)
    assert pending.question == "Print 9 pages?"
    assert not (channel_dir / "approved.txt").exists()
    agent._signal_client.send_message.assert_any_await(CHANNEL, "9 pages, A4")
    await agent._consent.on_vote(_vote(0))
    await asyncio.wait_for(turn, 10)

    assert (channel_dir / "approved.txt").read_text().strip() == "printed"
    content = _tool_contents(agent._llm_client.chat.await_args_list[1].args[0])["c1"]
    assert "job 7" in content and "(approved by Bob)" in content and "NIXCLAW" not in content
