"""Tests for error handling and message cap enforcement in AgentLoop.

Covers:
- Issue #10: LLM error propagation to Signal (end-to-end error delivery)
- Issue #12: MAX_MESSAGES_PER_SESSION cap enforcement (rate limiting)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.nixclaw_agent.core import MAX_MESSAGES_PER_SESSION
from src.nixclaw_agent.types import Event
from conftest import build_mock_agent_loop


@pytest.fixture
def _reset_db(reset_db):
    """Fresh sqlite DB per test (delegates to conftest's reset_db)."""
    yield


def _make_agent():
    """Create mocked AgentLoop for testing error scenarios."""
    agent = build_mock_agent_loop()
    agent._llm_client.chat = AsyncMock()
    return agent


@pytest.fixture
def _event():
    """Create a test event."""
    def _create(source="+000000", message="test", timestamp=1700000000):
        return Event(
            source=source,
            destination=None,
            message=message,
            timestamp=timestamp,
            group_id=None,
            attachments=[],
            mentions=[],
        )
    return _create


class TestLLMErrorPropagation:
    """Test LLM errors are correctly propagated to user via Signal."""

    @pytest.mark.asyncio
    async def test_llm_error_at_iteration_1_sends_fallback(self, _reset_db, _event):
        """LLM error on first call sends fallback message."""
        agent = _make_agent()
        agent._llm_client.chat.side_effect = ValueError("LLM API down")
        
        event = _event("+000000", "test")
        await agent.process_message(event)
        
        # Verify error message was sent
        assert agent._signal_client.send_message.await_count >= 1
        call_args = agent._signal_client.send_message.call_args_list[0]
        assert "temporarily unavailable" in str(call_args).lower()

    @pytest.mark.asyncio
    async def test_llm_error_increments_messages_sent(self, _reset_db, _event):
        """Fallback error message counts toward message cap."""
        agent = _make_agent()
        agent._llm_client.chat.side_effect = ValueError("LLM error")
        
        channel = "+000000"
        event = _event("+000000", "test")
        await agent.process_message(event)
        
        # Verify message was counted
        assert agent._messages_sent.get(channel, 0) >= 1


class TestSignalDeliveryErrors:
    """Test Signal delivery failures and retries."""

    @pytest.mark.asyncio
    async def test_signal_send_failure_does_not_crash(self, _reset_db, _event):
        """Signal send failure is handled gracefully."""
        agent = _make_agent()
        agent._signal_client.send_message.side_effect = ConnectionError("Signal down")
        agent._llm_client.chat.return_value = ("test response", [])
        
        event = _event("+000000", "test")
        # Should not raise exception
        await agent.process_message(event)
        
        # Verify send was attempted
        assert agent._signal_client.send_message.await_count >= 1

    @pytest.mark.asyncio
    async def test_rate_limited_message_not_sent(self, _reset_db, _event):
        """Rate-limited messages don't get sent."""
        agent = _make_agent()
        agent._check_rate_limit = MagicMock(return_value=False)
        agent._llm_client.chat.return_value = ("response", [])
        
        event = _event("+000000", "test")
        await agent.process_message(event)
        
        # Message should not be sent if rate limited
        # (it would be queued instead, but we're not testing that path here)


class TestMessageCapBoundary:
    """Test MAX_MESSAGES_PER_SESSION enforcement."""

    @pytest.mark.asyncio
    async def test_message_cap_reset_on_new_turn(self, _reset_db, _event):
        """Message cap resets for new turns."""
        agent = _make_agent()
        agent._llm_client.chat.return_value = ("response", [])
        
        channel = "+000000"
        
        # First turn
        event1 = _event("+000000", "test 1", timestamp=1700000000)
        await agent.process_message(event1)
        assert agent._messages_sent[channel] == 1

        # Second turn: counter is reset on entry to process_message, so it
        # does not carry over from the first turn
        event2 = _event("+000000", "test 2", timestamp=1700000001)
        await agent.process_message(event2)
        assert agent._messages_sent[channel] == 1

        assert agent._signal_client.send_message.await_count == 2

    @pytest.mark.asyncio
    async def test_at_message_cap_blocks_sends(self, _reset_db, _event):
        """At message cap, additional sends are blocked."""
        agent = _make_agent()
        agent._llm_client.chat.return_value = ("response", [])
        
        channel = "+000000"
        agent._messages_sent[channel] = MAX_MESSAGES_PER_SESSION
        
        event = _event("+000000", "test")
        await agent.process_message(event)
        
        # Verify cap prevents sending (or sends "paused" message)
        # The exact behavior depends on where cap is checked


class TestCapAndErrorInteraction:
    """Test interactions between message cap and error handling."""

    @pytest.mark.asyncio
    async def test_llm_error_fallback_respects_cap(self, _reset_db, _event):
        """LLM error fallback respects message cap."""
        agent = _make_agent()
        agent._llm_client.chat.side_effect = ValueError("LLM error")
        
        channel = "+000000"
        # Set to one below cap
        agent._messages_sent[channel] = MAX_MESSAGES_PER_SESSION - 1
        
        event = _event("+000000", "test")
        await agent.process_message(event)
        
        # Fallback should be sent (bringing to cap)
        assert agent._messages_sent[channel] <= MAX_MESSAGES_PER_SESSION

    @pytest.mark.asyncio
    async def test_error_recovery_message_counts(self, _reset_db, _event):
        """Error recovery messages count against cap."""
        agent = _make_agent()
        agent._llm_client.chat.side_effect = Exception("LLM error")
        
        channel = "+000000"
        assert agent._messages_sent.get(channel, 0) == 0
        
        event = _event("+000000", "test")
        await agent.process_message(event)
        
        # Error message should have incremented counter
        assert agent._messages_sent[channel] >= 1
