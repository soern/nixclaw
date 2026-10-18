"""Regression test: process_message must not send a whitespace-only
response_text (e.g. "\\n\\n") to the user as a standalone Signal message
when the LLM returns that text alongside tool_calls.

vLLM/Qwen routinely returns response_text="\\n\\n" together with a tool
call before it has produced a real answer. Because "\\n\\n" is truthy in
Python, a plain `if response_text:` check (as opposed to
`if response_text and response_text.strip():`) sends it as a real,
visually-empty message - the "agent sends an empty first reply, then the
real response later" symptom, seen live in data/debug-agent.json starting
~2026-08-10 (145 occurrences). See core.py's pre-tool-loop send checks
(the same `.strip()` guard already used at the mid-loop send, core.py:943).

Uses the shared AgentLoop-construction harness in conftest.py (also used by
test_core_tool_result_wiring.py and test_channel_turn_serialization.py).
"""

from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent.types import Event, ToolCall, ToolResult

pytestmark = pytest.mark.usefixtures("reset_db")


@pytest.mark.asyncio
async def test_whitespace_text_with_tool_call_is_not_sent_as_message():
    """First chat() call returns response_text="\\n\\n" alongside a tool
    call (the observed vLLM pattern). No message should reach Signal until
    the second chat() call produces the real answer.
    """
    agent = _make_agent()

    tool_call = ToolCall(id="1", name="analyze_image", arguments={"path": "x.jpg"})

    agent._tool_registry.execute_tool = AsyncMock(
        return_value=ToolResult(name="analyze_image", success=True, output="a 3D printer", error=None)
    )

    agent._llm_client.chat = AsyncMock(
        side_effect=[
            ("\n\n", [tool_call]),
            ("It's a 3D printer.", []),
        ]
    )

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="what is this?",
        timestamp=1700000010,
        group_id=None,
        attachments=[],
        mentions=[],
    )

    await agent.process_message(event)

    sent_texts = [call.args[1] for call in agent._signal_client.send_message.await_args_list]
    assert sent_texts == ["It's a 3D printer."], (
        f"expected exactly one, non-whitespace message to be sent, got {sent_texts!r}"
    )
