"""End-to-end wiring test: process_message caps oversized tool results before
they re-enter the message list sent back to the LLM (see _tool_result_content
in core.py). Uses the shared AgentLoop-construction harness in conftest.py
(also used by test_channel_turn_serialization.py,
test_whitespace_only_response_not_sent.py, and, in a specialized form, by
test_stt_core_integration.py).
"""

from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent.config import config as config_module
from src.nixclaw_agent.types import Event, ToolCall, ToolResult

pytestmark = pytest.mark.usefixtures("reset_db")


@pytest.mark.asyncio
async def test_oversized_tool_result_is_capped_before_next_llm_call(monkeypatch):
    """The main chat loop (process_message) must truncate a tool result via
    _tool_result_content before it is appended to `messages` and sent back
    to the LLM on the follow-up chat() call.
    """
    monkeypatch.setattr(config_module.chat, "_effective_tool_result_max_chars", 1024)

    agent = _make_agent()

    tool_call = ToolCall(id="1", name="grep_files", arguments={"pattern": "x"})
    oversized_output = "z" * 50_000

    agent._tool_registry.execute_tool = AsyncMock(
        return_value=ToolResult(name="grep_files", success=True, output=oversized_output, error=None)
    )

    # First chat() call: model asks to call the tool.
    # Second chat() call: model is done (no more tool calls) - loop exits.
    agent._llm_client.chat = AsyncMock(
        side_effect=[
            ("Let me check", [tool_call]),
            ("Done", []),
        ]
    )

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="please grep",
        timestamp=1700000010,
        group_id=None,
        attachments=[],
        mentions=[],
    )

    await agent.process_message(event)

    assert agent._llm_client.chat.await_count == 2
    second_call_messages = agent._llm_client.chat.await_args_list[1].args[0]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    rendered = tool_messages[0]["content"]
    assert len(rendered) < len(oversized_output)
    assert "[truncated," in rendered
