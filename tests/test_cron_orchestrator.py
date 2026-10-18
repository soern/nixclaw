"""Orchestrator-level tests for cronjob threshold-tripped one-time behaviour.

Tests _handle_one_time_threshold_tripped (the orchestrator's terminal-action handler)
and _send_one_time_threshold_notification in isolation.
"""

import sys
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "src")

import pytest

from nixclaw_agent.db.models import CronJob
from nixclaw_agent.core import AgentLoop
from nixclaw_agent.cron import CronCompleteResult
from nixclaw_agent.auth import UserTier


# ---------------------------------------------------------------------------
# Minimal AgentLoop — no super().__init__(), just enough for the two methods
# ---------------------------------------------------------------------------


class MinimalAgentLoop(AgentLoop):
    """Stripped-down AgentLoop for isolated testing.

    Bypasses __init__ entirely and manually sets only the attributes
    needed by _handle_one_time_threshold_tripped and
    _send_one_time_threshold_notification.
    """

    def __init__(self):
        # Attributes required by _handle_one_time_threshold_tripped
        self._db_ops = MagicMock()
        self._auth_manager = MagicMock()
        self._auth_manager.get_user_tier.return_value = UserTier.KNOWN
        self._send_with_retry = AsyncMock()
        self._flush_channel_queue = AsyncMock()


def make_one_time_job(job_id: str = "test-job", failure_count: int = 3) -> CronJob:
    return CronJob(
        id=job_id,
        channel_id="+0000000001",
        creator_number="+0000000001",
        prompt="test prompt",
        scheduled_at=datetime.now(timezone.utc).isoformat(),
        status="running",
        is_recurring=False,
        interval_minutes=None,
        cancelled=False,
        failure_count=failure_count,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandleOneTimeThresholdTripped:
    """Tests for _handle_one_time_threshold_tripped."""

    @pytest.mark.asyncio
    async def test_sends_notification_and_deletes(self):
        """One-time threshold-tripped → notification sent, delete called."""
        loop = MinimalAgentLoop()
        notification_mock = AsyncMock()
        loop._send_one_time_threshold_notification = notification_mock
        loop._db_ops.delete_cronjob = MagicMock(return_value=True)

        job = make_one_time_job("job-1", failure_count=3)
        await loop._handle_one_time_threshold_tripped(job)

        notification_mock.assert_called_once_with(job)
        loop._db_ops.delete_cronjob.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    async def test_notification_failure_still_calls_delete(self):
        """Case 17: _send_with_retry raises → delete_cronjob is still called."""
        loop = MinimalAgentLoop()
        notification_mock = AsyncMock(side_effect=Exception("send failed"))
        loop._send_one_time_threshold_notification = notification_mock
        loop._db_ops.delete_cronjob = MagicMock(return_value=True)

        job = make_one_time_job("job-2", failure_count=3)
        await loop._handle_one_time_threshold_tripped(job)

        notification_mock.assert_called_once()
        loop._db_ops.delete_cronjob.assert_called_once_with("job-2")

    @pytest.mark.asyncio
    async def test_delete_failure_does_not_propagate(self):
        """delete_cronjob raises → does not propagate to caller."""
        loop = MinimalAgentLoop()
        notification_mock = AsyncMock()
        loop._send_one_time_threshold_notification = notification_mock
        loop._db_ops.delete_cronjob = MagicMock(side_effect=Exception("delete failed"))

        job = make_one_time_job("job-3", failure_count=3)

        # Must not raise
        await loop._handle_one_time_threshold_tripped(job)

        notification_mock.assert_called_once()
        loop._db_ops.delete_cronjob.assert_called_once()


class TestSendOneTimeThresholdNotification:
    """Unit test for _send_one_time_threshold_notification content."""

    @pytest.mark.asyncio
    async def test_notification_uses_utc_hhmm_and_threshold(self):
        loop = MinimalAgentLoop()
        sent_messages: list[str] = []

        async def capture_send(channel_id: str, msg: str, tier):
            sent_messages.append(msg)

        loop._send_with_retry = capture_send
        loop._flush_channel_queue = AsyncMock()

        job = CronJob(
            id="notify-test",
            channel_id="+0000000001",
            creator_number="+0000000001",
            prompt="test",
            scheduled_at="2025-06-15T14:30:00+00:00",
            status="running",
            is_recurring=False,
            interval_minutes=None,
            cancelled=False,
            failure_count=3,
        )

        await loop._send_one_time_threshold_notification(job)

        assert len(sent_messages) == 1
        assert "14:30" in sent_messages[0]
        assert "3 times" in sent_messages[0]
