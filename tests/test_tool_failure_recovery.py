"""A failing tool must never end the turn behind the model's back: failures are
not cached, repeat-guard trips are fed back as tool results, and an empty
completion after a failed tool gets a failure-aware nudge (with tools) instead
of the canned user-facing fallback."""

from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent import prompts
from src.nixclaw_agent.auth import UserTier
from src.nixclaw_agent.tools.registry import ToolRegistry
from src.nixclaw_agent.types import Event, ToolCall, ToolResult

pytestmark = pytest.mark.usefixtures("reset_db")


def _event(msg="make an image"):
    return Event(source="+000000", destination="+491234567890", message=msg,
                 timestamp=1700000010, group_id=None, attachments=[], mentions=[])


def _fail(name="generate_image"):
    return ToolResult(name=name, success=False, output=None, error="HTTP 500: mha_graph")


@pytest.mark.asyncio
async def test_failed_tool_call_is_not_cached_and_reexecutes():
    agent = _make_agent()
    tc = lambda i: ToolCall(id=str(i), name="generate_image", arguments={"prompt": "x"})
    agent._tool_registry.execute_tool = AsyncMock(
        side_effect=[_fail(), ToolResult(name="generate_image", success=True, output="a.png", error=None)]
    )
    agent._llm_client.chat = AsyncMock(side_effect=[("try", [tc(1)]), ("retry", [tc(2)]), ("done", [])])
    await agent.process_message(_event())
    assert agent._tool_registry.execute_tool.await_count == 2


@pytest.mark.asyncio
async def test_repeat_guard_feeds_hint_to_model_instead_of_ending_turn():
    agent = _make_agent()
    tc = lambda i: ToolCall(id=str(i), name="generate_image", arguments={"prompt": "x"})
    agent._tool_registry.execute_tool = AsyncMock(return_value=_fail())
    agent._send_with_retry = AsyncMock()
    # 5 identical calls (limit is 3), then the model changes course with text.
    agent._llm_client.chat = AsyncMock(
        side_effect=[("t", [tc(i)]) for i in range(1, 5)] + [("Sorry, image generation is down.", [])]
    )
    await agent.process_message(_event())
    assert agent._tool_registry.execute_tool.await_count == 3  # 4th call was not executed
    last_msgs = agent._llm_client.chat.await_args_list[-1].args[0]
    hints = [m["content"] for m in last_msgs if m.get("role") == "tool" and "not run again" in m["content"]]
    assert len(hints) == 1
    sent = [c.args[1] for c in agent._send_with_retry.await_args_list]
    assert any("image generation is down" in s for s in sent)
    assert not any("Operation paused" in s for s in sent)


@pytest.mark.asyncio
async def test_empty_response_after_failed_tool_gets_failure_nudge_with_tools():
    agent = _make_agent()
    tc = ToolCall(id="1", name="generate_image", arguments={"prompt": "x"})
    agent._tool_registry.execute_tool = AsyncMock(return_value=_fail())
    agent._send_with_retry = AsyncMock()
    agent._llm_client.chat = AsyncMock(
        side_effect=[("try", [tc]), ("", []), ("Image generation failed, try later?", [])]
    )
    await agent.process_message(_event())
    nudge_call = agent._llm_client.chat.await_args_list[2]
    nudge = nudge_call.args[0][-1]["content"]
    assert "generate_image" in nudge and "mha_graph" in nudge
    sent = [c.args[1] for c in agent._send_with_retry.await_args_list]
    assert sent[-1] == "Image generation failed, try later?"
    assert prompts.current().text("no-summary-fallback") not in sent


@pytest.mark.asyncio
async def test_nudged_tool_call_is_executed_and_fallback_only_after_all_nudges():
    agent = _make_agent()
    tc = lambda i, p: ToolCall(id=str(i), name="generate_image", arguments={"prompt": p})
    agent._tool_registry.execute_tool = AsyncMock(
        side_effect=[_fail(), ToolResult(name="generate_image", success=True, output="a.png", error=None)]
    )
    agent._send_with_retry = AsyncMock()
    agent._llm_client.chat = AsyncMock(
        side_effect=[("t", [tc(1, "x")]), ("", []), ("retrying", [tc(2, "y")]), ("Here it is", [])]
    )
    await agent.process_message(_event())
    assert agent._tool_registry.execute_tool.await_count == 2

    agent2 = _make_agent()
    agent2._tool_registry.execute_tool = AsyncMock(return_value=_fail())
    agent2._send_with_retry = AsyncMock()
    agent2._llm_client.chat = AsyncMock(side_effect=[("t", [tc(1, "x")])] + [("", [])] * 4)
    await agent2.process_message(_event())
    assert agent2._llm_client.chat.await_count == 5  # initial, empty post-tool, 3 nudges
    sent = [c.args[1] for c in agent2._send_with_retry.await_args_list]
    assert sent[-1] == prompts.current().text("no-summary-fallback")


@pytest.mark.asyncio
async def test_registry_rejects_truncation_placeholder_arguments():
    registry = ToolRegistry(auth_manager=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(
        can_use_tool=lambda tier, name: True))
    handler = AsyncMock()
    registry.register("generate_image", handler, {"prompt": {"type": "string"}})
    result = await registry.execute_tool(
        "generate_image", {"_truncated": "past tool call (auto-truncated): {...}"}, UserTier.PRIVILEGED)
    assert not result.success
    assert "placeholder" in result.error
    handler.assert_not_awaited()
