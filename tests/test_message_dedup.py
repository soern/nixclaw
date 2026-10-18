"""Regression tests for the message dedup key.

Signal timestamps are truncated to seconds (signal.py), so two different
messages from the same user within the same second used to collide on the
`source:timestamp` dedup key - the second message was silently dropped.
Observed live: 9 mock-injected prompts in one second, 8 discarded.
The key now includes a content hash.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.nixclaw_agent.core import AgentLoop
from src.nixclaw_agent.types import Event


def make_event(message: str, timestamp: int = 1700000000) -> Event:
    return Event(
        source="+000001",
        destination="+4900000000",
        message=message,
        timestamp=timestamp,
        group_id=None,
        attachments=[],
    )


@pytest.fixture
def agent_loop():
    loop = AgentLoop(
        MagicMock(), MagicMock(), MagicMock(),
        MagicMock(), MagicMock(), MagicMock(),
    )
    # Answering True makes process_message return right after the dedup
    # check - the id passed to is_message_processed is what we inspect.
    loop._db_ops = MagicMock()
    loop._db_ops.is_message_processed = MagicMock(return_value=True)
    return loop


async def _dedup_id_for(agent_loop, event: Event) -> str:
    agent_loop._db_ops.is_message_processed.reset_mock()
    await agent_loop.process_message(event)
    args = agent_loop._db_ops.is_message_processed.call_args
    return args.args[0]


@pytest.mark.asyncio
async def test_same_second_different_messages_get_different_ids(agent_loop):
    id_a = await _dedup_id_for(agent_loop, make_event("first question"))
    id_b = await _dedup_id_for(agent_loop, make_event("second question"))
    assert id_a != id_b


@pytest.mark.asyncio
async def test_identical_redelivery_gets_same_id(agent_loop):
    id_a = await _dedup_id_for(agent_loop, make_event("same message"))
    id_b = await _dedup_id_for(agent_loop, make_event("same message"))
    assert id_a == id_b


@pytest.mark.asyncio
async def test_id_still_contains_source_and_timestamp(agent_loop):
    dedup_id = await _dedup_id_for(agent_loop, make_event("hello", timestamp=1712345678))
    assert dedup_id.startswith("+000001:1712345678")
