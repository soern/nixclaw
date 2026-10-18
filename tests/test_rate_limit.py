"""Tests for rate limiting and loop detection in core.py."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock

from src.nixclaw_agent.core import AgentLoop, RATE_LIMITS_PER_TIER, LOOP_DETECTION_THRESHOLD, LOOP_DETECTION_WINDOW_SECONDS, _get_rate_limit_for_tier
from src.nixclaw_agent.types import UserTier


class TestRateLimiting:
    @pytest.fixture
    def agent_loop(self):
        mock_signal = MagicMock()
        mock_llm = MagicMock()
        mock_auth = MagicMock()
        mock_context = MagicMock()
        mock_tool_registry = MagicMock()
        mock_cron = MagicMock()
        return AgentLoop(
            mock_signal, mock_llm, mock_auth, mock_context, mock_tool_registry, mock_cron
        )

    def test_rate_limit_allows_first_messages(self, agent_loop):
        channel_id = "test_channel_rate"
        tier = UserTier.UNKNOWN
        assert agent_loop._check_rate_limit(channel_id, tier) is True

    def test_rate_limit_blocks_after_threshold(self, agent_loop):
        channel_id = "test_channel_rate2"
        tier = UserTier.UNKNOWN
        rate_limit = RATE_LIMITS_PER_TIER[tier]
        for _ in range(rate_limit):
            assert agent_loop._check_rate_limit(channel_id, tier) is True
        assert agent_loop._check_rate_limit(channel_id, tier) is False

    def test_rate_limit_resets_after_60_seconds(self, agent_loop):
        channel_id = "test_channel_rate3"
        tier = UserTier.UNKNOWN
        rate_limit = RATE_LIMITS_PER_TIER[tier]
        for _ in range(rate_limit):
            agent_loop._check_rate_limit(channel_id, tier)
        assert agent_loop._check_rate_limit(channel_id, tier) is False
        agent_loop._channel_message_timestamps[channel_id] = [
            datetime.now() - timedelta(seconds=120)
        ]
        assert agent_loop._check_rate_limit(channel_id, tier) is True

    def test_privileged_tier_has_higher_rate_limit(self, agent_loop):
        channel_id = "test_channel_privileged"
        unknown_tier = UserTier.UNKNOWN
        privileged_tier = UserTier.PRIVILEGED
        unknown_limit = RATE_LIMITS_PER_TIER[unknown_tier]
        privileged_limit = RATE_LIMITS_PER_TIER[privileged_tier]
        assert privileged_limit > unknown_limit
        for _ in range(unknown_limit):
            assert agent_loop._check_rate_limit(channel_id, unknown_tier) is True
        assert agent_loop._check_rate_limit(channel_id, unknown_tier) is False
        for _ in range(privileged_limit - unknown_limit):
            assert agent_loop._check_rate_limit(channel_id, privileged_tier) is True


class TestLoopDetection:
    @pytest.fixture
    def agent_loop(self):
        mock_signal = MagicMock()
        mock_llm = MagicMock()
        mock_auth = MagicMock()
        mock_context = MagicMock()
        mock_tool_registry = MagicMock()
        mock_cron = MagicMock()
        return AgentLoop(
            mock_signal, mock_llm, mock_auth, mock_context, mock_tool_registry, mock_cron
        )

    def test_loop_detection_allows_first_requests(self, agent_loop):
        channel_id = "test_channel_loop"
        assert agent_loop._check_loop_detection(channel_id, "stop") is True

    def test_loop_detection_allows_different_messages(self, agent_loop):
        channel_id = "test_channel_loop2"
        agent_loop._check_loop_detection(channel_id, "stop")
        assert agent_loop._check_loop_detection(channel_id, "go") is True

    def test_loop_detection_blocks_repeated_same_message(self, agent_loop):
        channel_id = "test_channel_loop3"
        for _ in range(LOOP_DETECTION_THRESHOLD):
            assert agent_loop._check_loop_detection(channel_id, "stop") is True
        assert agent_loop._check_loop_detection(channel_id, "stop") is False

    def test_loop_detection_resets_after_window(self, agent_loop):
        channel_id = "test_channel_loop4"
        for _ in range(LOOP_DETECTION_THRESHOLD):
            agent_loop._check_loop_detection(channel_id, "stop")
        assert agent_loop._check_loop_detection(channel_id, "stop") is False
        agent_loop._channel_recent_requests[channel_id] = [
            ("stop", datetime.now() - timedelta(seconds=LOOP_DETECTION_WINDOW_SECONDS + 10))
        ]
        assert agent_loop._check_loop_detection(channel_id, "stop") is True

    def test_loop_detection_normalizes_to_lowercase(self, agent_loop):
        channel_id = "test_channel_loop5"
        agent_loop._check_loop_detection(channel_id, "STOP")
        agent_loop._check_loop_detection(channel_id, "Stop")
        agent_loop._check_loop_detection(channel_id, "stop")
        assert agent_loop._check_loop_detection(channel_id, "stop") is False


class TestStopKeywordDetection:
    """Tests for stop keyword detection in process_message.

    The stop detection should only trigger for messages that start with
    a stop keyword followed by at most 3 additional characters.
    Pattern: ^KEYWORD.{0,3}$
    """

    @pytest.fixture
    def agent_loop(self):
        mock_signal = MagicMock()
        mock_llm = MagicMock()
        mock_auth = MagicMock()
        mock_context = MagicMock()
        mock_tool_registry = MagicMock()
        mock_cron = MagicMock()
        mock_db_ops = MagicMock()
        mock_db_ops.is_message_processed.return_value = False
        agent = AgentLoop(
            mock_signal, mock_llm, mock_auth, mock_context, mock_tool_registry, mock_cron
        )
        agent._db_ops = mock_db_ops
        return agent

    @pytest.mark.parametrize("message", [
        "stop",
        "halt",
        "cancel",
        "abort",
        "enough",
        "cease",
        "stop!",
        "halt!",
        "stop?",
        "halt?",
        "stop.",
        "halt.",
        "stop!!",
        "halt!!",
        "STOP",
        "HALT",
    ])
    def test_stop_keyword_triggers_halt(self, agent_loop, message):
        """Messages that are exactly stop keyword (+ up to 3 chars) should trigger halt."""
        tier = UserTier.KNOWN
        mock_event = MagicMock()
        mock_event.message = message
        mock_event.source = "+491234567891"
        mock_event.timestamp = "2026-04-08T00:00:00"
        mock_event.message_id = "test-msg-id"
        agent_loop._auth_manager.get_user_tier.return_value = tier
        agent_loop._send_typing = AsyncMock()
        agent_loop._send_with_retry = AsyncMock()

        import asyncio
        asyncio.run(agent_loop.process_message(mock_event))

        agent_loop._send_with_retry.assert_called_once()
        call_args = agent_loop._send_with_retry.call_args[0]
        assert "Operation halted by user request" in call_args[1]

    @pytest.mark.parametrize("message", [
        "stop the cronjob polling News on Iran",
        "stop everything",
        "halt the process please",
        "cancel the reminder",
        "abort the operation",
        "enough is enough",
        "cease all activity",
        "please stop",
        "can you stop?",
    ])
    def test_compound_message_passes_to_llm(self, agent_loop, message):
        """Messages with more than 3 chars after keyword should NOT trigger halt."""
        tier = UserTier.KNOWN
        mock_event = MagicMock()
        mock_event.message = message
        mock_event.source = "+491234567891"
        mock_event.timestamp = "2026-04-08T00:00:00"
        mock_event.message_id = "test-msg-id"
        agent_loop._auth_manager.get_user_tier.return_value = tier
        agent_loop._send_typing = AsyncMock()
        agent_loop._send_with_retry = AsyncMock()

        import asyncio
        try:
            asyncio.run(agent_loop.process_message(mock_event))
        except Exception:
            pass

        halt_call = [c for c in agent_loop._send_with_retry.call_args_list
                     if "Operation halted" in str(c)]
        assert len(halt_call) == 0, f"Message '{message}' should not trigger halt"
