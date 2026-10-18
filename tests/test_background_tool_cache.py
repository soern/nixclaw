"""Per-run tool cache + repeat guard of cronjob and subagent runs
(core._ToolCallCache), same rules as the chat turn's loop: failures are
never cached, successes are, and past MAX_REPEATED_TOOL_CALLS identical
calls the model gets a hint instead of another execution.

Regression test for the background loops caching every result, so a retry
after a transient error (timeout, backend hiccup) replayed the error.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop
from src.nixclaw_agent.core import MAX_REPEATED_TOOL_CALLS, MAX_TOOL_CACHE_SIZE, _ToolCallCache
from src.nixclaw_agent.cron import CronCompleteResult
from src.nixclaw_agent.db.models import CronJob
from src.nixclaw_agent.types import ToolCall, ToolResult, UserTier

pytestmark = pytest.mark.usefixtures("reset_db")

CALL = ToolCall(id="tc", name="file_read", arguments={"path": "x"})
OK = ToolResult(name="file_read", success=True, output="ok", error=None)
FAIL = ToolResult(name="file_read", success=False, output=None, error="timeout")


async def _run(lane: str, tool_results: list[ToolResult], n_calls: int):
    """Run one background run whose model makes CALL n_calls times, then
    stops. Returns (agent, number of tool executions)."""
    agent = build_mock_agent_loop()
    agent._tool_registry.execute_tool = AsyncMock(side_effect=tool_results)
    agent._llm_client.chat = AsyncMock(side_effect=[("", [CALL])] * n_calls + [("done", [])])
    if lane == "cronjob":
        agent._cron_scheduler.mark_complete.return_value = CronCompleteResult("none", "job-1")
        await agent._process_cronjob(CronJob(
            id="job-1",
            channel_id="+000000",
            creator_number="+000000",
            prompt="do the thing",
            scheduled_at=datetime.now(timezone.utc).isoformat(),
            status="running",
            is_recurring=False,
            interval_minutes=None,
            summary="test job",
        ))
    else:
        await agent._process_subagent("do the thing", "+000000", UserTier.PRIVILEGED)
    return agent, agent._tool_registry.execute_tool.await_count


def _tool_messages(agent) -> list[str]:
    messages = agent._llm_client.chat.await_args_list[-1].args[0]
    return [m["content"] for m in messages if m.get("role") == "tool"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["cronjob", "subagent"])
async def test_failure_is_retried_not_replayed(lane):
    agent, executions = await _run(lane, [FAIL, OK], n_calls=2)
    assert executions == 2
    assert "ok" in _tool_messages(agent)[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["cronjob", "subagent"])
async def test_success_is_served_from_cache(lane):
    agent, executions = await _run(lane, [OK], n_calls=2)
    assert executions == 1
    assert all("ok" in c for c in _tool_messages(agent))


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["cronjob", "subagent"])
async def test_repeat_guard_hints_instead_of_executing(lane):
    n_calls = MAX_REPEATED_TOOL_CALLS + 2
    agent, executions = await _run(lane, [FAIL] * n_calls, n_calls=n_calls)
    assert executions == MAX_REPEATED_TOOL_CALLS
    contents = _tool_messages(agent)
    assert len(contents) == n_calls
    assert all("timeout" not in c for c in contents[MAX_REPEATED_TOOL_CALLS:])


def test_cache_evicts_oldest_past_max_size():
    cache = _ToolCallCache()
    calls = [ToolCall(id=str(i), name="file_read", arguments={"path": str(i)}) for i in range(MAX_TOOL_CACHE_SIZE + 1)]
    for tc in calls:
        assert cache.lookup(tc) is None
        cache.store(tc, OK)
    assert cache.lookup(calls[0]) is None  # evicted
    assert cache.lookup(calls[-1]) is OK
