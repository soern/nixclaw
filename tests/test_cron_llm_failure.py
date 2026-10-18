"""An LLM-call failure during a cronjob run must be treated as a real
failure (CronScheduler.mark_complete(success=False, ...)), not caught
locally and reported to the user as if the run had succeeded.

Regression test for the bug where _process_cronjob caught the LLM error,
substituted "LLM temporarily unavailable" as response_text, and fell
through to the success path - sending AND storing that placeholder to the
user on every failed tick of a recurring job (e.g. every 20 minutes)
instead of retrying quietly via the existing failure-count/threshold
machinery in CronScheduler.mark_complete.
"""
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock

sys.path.insert(0, "src")

import pytest

from nixclaw_agent.cron import CronCompleteResult
from nixclaw_agent.db.models import CronJob
from nixclaw_agent.types import ToolCall, ToolResult

from conftest import build_mock_agent_loop as _make_agent

pytestmark = pytest.mark.usefixtures("reset_db")


def _make_job(job_id: str = "job-1") -> CronJob:
    return CronJob(
        id=job_id,
        channel_id="+000000",
        creator_number="+000000",
        prompt="do the thing",
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        status="running",
        is_recurring=False,
        interval_minutes=None,
        summary="test job",
    )


@pytest.mark.asyncio
async def test_initial_llm_error_marks_failure_not_success():
    agent = _make_agent()
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("backend down"))
    agent._cron_scheduler.mark_complete.return_value = CronCompleteResult("none", "job-1")

    job = _make_job()
    await agent._process_cronjob(job)

    agent._cron_scheduler.mark_complete.assert_called_once_with(
        job.id,
        success=False,
        is_recurring=job.is_recurring,
        interval_minutes=job.interval_minutes,
        is_cancelled=job.cancelled,
        scheduled_at=job.scheduled_at,
    )
    # Nothing sent or stored for the user - this is a quiet retry, not a reply.
    agent._signal_client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_llm_error_mid_tool_loop_marks_failure_not_success():
    agent = _make_agent()
    agent._tool_registry.execute_tool = AsyncMock(
        return_value=ToolResult(name="file_read", success=True, output="ok", error=None)
    )
    agent._llm_client.chat = AsyncMock(
        side_effect=[
            ("", [ToolCall(id="tc1", name="file_read", arguments={"path": "x"})]),
            RuntimeError("backend down"),
        ]
    )
    agent._cron_scheduler.mark_complete.return_value = CronCompleteResult("none", "job-2")

    job = _make_job("job-2")
    await agent._process_cronjob(job)

    agent._cron_scheduler.mark_complete.assert_called_once_with(
        job.id,
        success=False,
        is_recurring=job.is_recurring,
        interval_minutes=job.interval_minutes,
        is_cancelled=job.cancelled,
        scheduled_at=job.scheduled_at,
    )
    agent._signal_client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_successful_run_sends_reply_to_channel():
    """A completed cronjob delivers its reply to the job's channel and is
    marked successful."""
    agent = _make_agent()
    agent._llm_client.chat = AsyncMock(return_value=("Here is your reminder.", []))
    agent._cron_scheduler.mark_complete.return_value = CronCompleteResult("none", "job-3")

    job = _make_job("job-3")
    await agent._process_cronjob(job)

    agent._cron_scheduler.mark_complete.assert_called_once()
    assert agent._cron_scheduler.mark_complete.call_args.kwargs["success"] is True
    agent._signal_client.send_message.assert_awaited()
    sent = agent._signal_client.send_message.await_args_list
    assert any("Here is your reminder." in str(c) for c in sent)
    assert any("+000000" in str(c) for c in sent)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
