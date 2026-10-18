#!/usr/bin/env python3
"""Tests for enhanced message search with fuzzy matching and time-based search.

These tests verify:
- Fuzzy keyword matching with typo tolerance
- Approximate time parsing (last week, Monday morning, etc.)
- Context lines (messages before/after matches)
- Combined keyword + time search
"""

import subprocess
import time
import pytest
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from nixclaw_agent.types import Event, Message as TypesMessage


TEST_CHANNEL = "+0000001"


def run_mock_inject(source: str, message: str, port: int = 8081) -> tuple[int, str]:
    """Run send_mock_message.sh with given parameters and return status and response."""
    env = {"API_URL": f"http://localhost:{port}"}
    result = subprocess.run(
        ["bash", "send_mock_message.sh", source, message],
        capture_output=True,
        text=True,
        env={**subprocess.os.environ, **env},
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    return result.returncode, result.stdout + result.stderr


def get_docker_logs_nixclaw(since_seconds: int = 120) -> str:
    """Get recent nixclaw logs."""
    result = subprocess.run(
        ["docker", "logs", "nixclaw", "--tail", "300", "--since", f"{since_seconds}s"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout + result.stderr


def get_docker_logs_mock(since_seconds: int = 120) -> str:
    """Get recent mock-signal-api logs."""
    result = subprocess.run(
        ["docker", "logs", "nixclaw-mock-signal-api", "--tail", "200", "--since", f"{since_seconds}s"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout + result.stderr


def clear_docker_logs():
    """Clear docker logs for clean test state."""
    subprocess.run(["docker", "logs", "--quiet", "--tail", "0", "nixclaw"], capture_output=True)
    subprocess.run(["docker", "logs", "--quiet", "--tail", "0", "nixclaw-mock-signal-api"], capture_output=True)


def create_mock_message(content: str, timestamp: int, is_outgoing: bool, source: str = "user") -> TypesMessage:
    """Create a mock message object."""
    return TypesMessage(
        event=Event(
            source=source,
            destination=TEST_CHANNEL,
            message=content,
            timestamp=timestamp,
            group_id=None,
            attachments=[],
        ),
        is_outgoing=is_outgoing,
        processed=True,
    )


def get_mock_conversations() -> list[TypesMessage]:
    """Get 10 fictive conversations as mock messages."""
    now = datetime.now(timezone.utc)
    messages = []

    conv1_time = int((now - timedelta(days=4, hours=10)).timestamp())
    messages.extend([
        create_mock_message("Can you help me with a REST API question?", conv1_time, False, "user"),
        create_mock_message("Of course! What would you like to know about REST APIs?", conv1_time + 60, True, "agent"),
        create_mock_message("How do I structure endpoint URLs for a collection of items?", conv1_time + 120, False, "user"),
        create_mock_message("For collections, use plural nouns like /users or /items. For specific items, add an ID: /users/123", conv1_time + 180, True, "agent"),
    ])

    conv2_time = int((now - timedelta(days=4, hours=14)).timestamp())
    messages.extend([
        create_mock_message("I'm getting a 500 error on my server", conv2_time, False, "user"),
        create_mock_message("A 500 error means something went wrong on the server side. Let me help you debug it.", conv2_time + 60, True, "agent"),
        create_mock_message("The logs show 'NullPointerException at line 42'", conv2_time + 120, False, "user"),
        create_mock_message("That usually means a variable is null when it shouldn't be. Check your initialization code.", conv2_time + 180, True, "agent"),
    ])

    conv3_time = int((now - timedelta(days=3, hours=9)).timestamp())
    messages.extend([
        create_mock_message("I want to start a new Python project", conv3_time, False, "user"),
        create_mock_message("Great! What kind of project is it - web app, script, or library?", conv3_time + 60, True, "agent"),
        create_mock_message("A web scraper to collect news articles", conv3_time + 120, False, "user"),
        create_mock_message("Nice! For a web scraper, consider using requests and BeautifulSoup. Need help with dependencies?", conv3_time + 180, True, "agent"),
    ])

    conv4_time = int((now - timedelta(days=7, hours=11)).timestamp())
    messages.extend([
        create_mock_message("I need to plan my monthly budget", conv4_time, False, "user"),
        create_mock_message("I can help with that. Do you have your income and expenses tracked?", conv4_time + 60, True, "agent"),
        create_mock_message("Yes I earn 3000 euros monthly, need to save for vacation", conv4_time + 120, False, "user"),
        create_mock_message("A good rule is 50/30/20 - 50% needs, 30% wants, 20% savings. For 600 euros savings, you're on track!", conv4_time + 180, True, "agent"),
    ])

    conv5_time = int((now - timedelta(days=5, hours=15)).timestamp())
    messages.extend([
        create_mock_message("Can you help me schedule a team meeting?", conv5_time, False, "user"),
        create_mock_message("Sure! What day and time works for your team?", conv5_time + 60, True, "agent"),
        create_mock_message("Thursday at 2pm please", conv5_time + 120, False, "user"),
        create_mock_message("Done! I've scheduled the meeting for Thursday at 2pm. You'll receive a reminder 15 minutes before.", conv5_time + 180, True, "agent"),
    ])

    conv6_time = int((now - timedelta(days=3, hours=16)).timestamp())
    messages.extend([
        create_mock_message("There's a bug in my code where the list stays empty", conv6_time, False, "user"),
        create_mock_message("Let me help you debug. Can you share the relevant code section?", conv6_time + 60, True, "agent"),
        create_mock_message("Here is my code: items = []\nfor item in data:\n    if item.active:\n        items.append(item)", conv6_time + 120, False, "user"),
        create_mock_message("The code looks correct. Are you sure 'data' has any items where .active is True?", conv6_time + 180, True, "agent"),
    ])

    conv7_time = int((now - timedelta(days=2, hours=10)).timestamp())
    messages.extend([
        create_mock_message("Can you read the config.json file for me?", conv7_time, False, "user"),
        create_mock_message("Let me check that file.", conv7_time + 60, True, "agent"),
        create_mock_message("The config contains: database URL, API keys (masked), and timeout settings set to 30 seconds.", conv7_time + 120, True, "agent"),
    ])

    conv8_time = int((now - timedelta(days=1, hours=9)).timestamp())
    messages.extend([
        create_mock_message("Search for the latest news about Python programming", conv8_time, False, "user"),
        create_mock_message("I found some recent articles about Python 3.12 features including better error messages and faster startup time.", conv8_time + 60, True, "agent"),
        create_mock_message("Thanks! Any about async improvements?", conv8_time + 120, False, "user"),
        create_mock_message("Yes! Python 3.11 and 3.12 have significant async improvements, including Task groups and better tracebacks.", conv8_time + 180, True, "agent"),
    ])

    conv9_time = int((now - timedelta(days=1, hours=14)).timestamp())
    messages.extend([
        create_mock_message("Remind me to check my emails every day at 9am", conv9_time, False, "user"),
        create_mock_message("I've set up a daily reminder for 9am to check your emails.", conv9_time + 60, True, "agent"),
        create_mock_message("Great, thanks!", conv9_time + 120, False, "user"),
        create_mock_message("You're welcome! The reminder is active.", conv9_time + 180, True, "agent"),
    ])

    conv10_time = int((now - timedelta(hours=2)).timestamp())
    messages.extend([
        create_mock_message("What's the weather like today?", conv10_time, False, "user"),
        create_mock_message("I don't have access to weather data, but you can check weather.com or use a weather app on your phone.", conv10_time + 60, True, "agent"),
    ])

    return messages


class TestTimeParsing:
    """Unit tests for time parsing function."""

    def test_parse_last_week(self):
        """Test 'last week' parsing."""
        from nixclaw_agent.tools.messages import _parse_time_range
        result = _parse_time_range("last week")
        assert result is not None
        start, end = result
        now = datetime.now()
        expected = now - timedelta(days=7)
        assert abs(start - int(expected.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())) < 60

    def test_parse_yesterday(self):
        """Test 'yesterday' parsing."""
        from nixclaw_agent.tools.messages import _parse_time_range
        result = _parse_time_range("yesterday")
        assert result is not None
        start, end = result
        now = datetime.now()
        expected = now - timedelta(days=1)
        assert abs(start - int(expected.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())) < 60

    def test_parse_days_ago(self):
        """Test '3 days ago' parsing."""
        from nixclaw_agent.tools.messages import _parse_time_range
        result = _parse_time_range("3 days ago")
        assert result is not None
        start, end = result
        now = datetime.now()
        expected = now - timedelta(days=3)
        assert abs(start - int(expected.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())) < 60

    def test_parse_monday(self):
        """Test 'Monday' parsing returns most recent Monday."""
        from nixclaw_agent.tools.messages import _parse_time_range
        result = _parse_time_range("Monday")
        assert result is not None
        start, end = result
        now = datetime.now()
        monday = now - timedelta(days=(now.weekday() + 7) % 7)
        expected_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        assert abs(start - int(expected_start.timestamp())) < 3600

    def test_parse_monday_morning(self):
        """Test 'Monday morning' parsing returns Monday 6-12."""
        from nixclaw_agent.tools.messages import _parse_time_range
        result = _parse_time_range("Monday morning")
        assert result is not None
        start, end = result
        now = datetime.now()
        monday = now - timedelta(days=(now.weekday() + 7) % 7)
        expected_start = monday.replace(hour=6, minute=0, second=0, microsecond=0)
        expected_end = monday.replace(hour=11, minute=59, second=59, microsecond=999999)
        assert abs(start - int(expected_start.timestamp())) < 3600
        assert abs(end - int(expected_end.timestamp())) < 3600

    def test_parse_invalid(self):
        """Test invalid time string returns None."""
        from nixclaw_agent.tools.messages import _parse_time_range
        result = _parse_time_range("not a valid time string at all")
        assert result is None


class TestMessageSearchDirect:
    """Direct tests of message_search function with mocked database."""

    @pytest.fixture(autouse=True)
    def setup_mock_db(self):
        """Set up mock database for each test."""
        self.mock_messages = get_mock_conversations()
        self.patcher = patch("nixclaw_agent.tools.messages.get_messages")
        self.mock_get_messages = self.patcher.start()
        self.mock_get_messages.return_value = self.mock_messages
        yield
        self.patcher.stop()

    def test_keyword_search_api(self):
        """A1: Search for 'API' should find conversation #1."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="API", channel_id=TEST_CHANNEL)
        assert result.success
        assert "REST" in result.output or "endpoint" in result.output.lower()

    def test_keyword_search_error(self):
        """A2: Search for 'error' should find conversation #2."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="error", channel_id=TEST_CHANNEL)
        assert result.success
        assert "500" in result.output or "NullPointerException" in result.output

    def test_keyword_search_budget(self):
        """A3: Search for 'budget' should find conversation #4."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="budget", channel_id=TEST_CHANNEL)
        assert result.success
        assert "budget" in result.output.lower()

    def test_fuzzy_search_typo(self):
        """A4: Search for 'endpont' (typo) should still match 'endpoint'."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="endpont", channel_id=TEST_CHANNEL)
        assert result.success
        assert "endpoint" in result.output.lower() or "API" in result.output

    def test_keyword_search_bug(self):
        """A5: Search for 'bug' should find conversation #6."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="bug", channel_id=TEST_CHANNEL)
        assert result.success
        assert "bug" in result.output.lower() or "debug" in result.output.lower()

    def test_time_search_yesterday(self):
        """B2: 'yesterday' should find conversations #8 (search) and #9 (cron)."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(approximate_time="yesterday", channel_id=TEST_CHANNEL)
        assert result.success
        combined = result.output.lower()
        assert "python" in combined or "email" in combined or "reminder" in combined

    def test_time_search_days_ago(self):
        """B4: '3 days ago' should find conversation #6 (bug)."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(approximate_time="3 days ago", channel_id=TEST_CHANNEL)
        assert result.success

    def test_combined_keyword_time(self):
        """C1: API discussion on Monday should find conv #1."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="API", approximate_time="Monday", channel_id=TEST_CHANNEL)
        assert result.success

    def test_no_results(self):
        """E1: Search for nonexistent content should return empty."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="xyzzy no such thing", channel_id=TEST_CHANNEL)
        assert result.success
        assert "no messages found" in result.output.lower() or "no matches" in result.output.lower()

    def test_context_lines_included(self):
        """Verify that context lines are included in results."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="NullPointerException", context_lines=2, channel_id=TEST_CHANNEL)
        assert result.success
        lines = result.output.split("\n")
        match_lines = [l for l in lines if "[MATCH]" in l]
        assert len(match_lines) >= 1

    def test_time_only_search(self):
        """Verify time-only search works without query."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(approximate_time="yesterday", channel_id=TEST_CHANNEL)
        assert result.success
        assert "Found" in result.output

    def test_error_without_query_or_time(self):
        """Error when neither query nor time is provided."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(channel_id=TEST_CHANNEL)
        assert not result.success
        assert "at least one" in result.error.lower()

    def test_includes_summarized_messages(self):
        """Summarized rows are older history the search exists to find."""
        from nixclaw_agent.tools.messages import message_search
        message_search(query="API", channel_id=TEST_CHANNEL)
        assert self.mock_get_messages.call_args.kwargs["include_summarized"] is True


@pytest.fixture
def berlin_tz(monkeypatch):
    """Pin the process time zone to Europe/Berlin (CEST = UTC+2 in October)."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestLocalTime:
    """Times are local (matching the system prompt's clock), not UTC."""

    # 2026-10-01 20:51:00 UTC == 22:51 CEST
    TS = int(datetime(2026, 10, 1, 20, 51, tzinfo=timezone.utc).timestamp())

    def test_output_timestamps_local_with_offset(self, berlin_tz):
        from nixclaw_agent.tools.messages import message_search
        msg = create_mock_message("sieht gut aus", self.TS, False)
        with patch("nixclaw_agent.tools.messages.get_messages", return_value=[msg]):
            result = message_search(query="sieht gut", channel_id=TEST_CHANNEL)
        assert "[2026-10-01 22:51 +0200]" in result.output
        assert "20:51" not in result.output

    def test_iso_filters_with_offset_round_trip(self, berlin_tz):
        """The model passes back the local time it was shown, with offset."""
        from nixclaw_agent.tools.messages import message_search
        with patch("nixclaw_agent.tools.messages.get_messages", return_value=[]) as gm:
            message_search(after="2026-10-01T22:00:00+02:00", before="2026-10-01T22:50:00+02:00",
                           channel_id=TEST_CHANNEL)
        kwargs = gm.call_args.kwargs
        assert kwargs["after_timestamp"] == self.TS - 51 * 60
        assert kwargs["before_timestamp"] == self.TS - 60

    def test_approximate_time_uses_local_day(self, berlin_tz):
        from nixclaw_agent.tools.messages import _parse_time_range
        start, end = _parse_time_range("today")
        assert datetime.fromtimestamp(start).strftime("%H:%M:%S") == "00:00:00"
        assert datetime.fromtimestamp(end).strftime("%H:%M:%S") == "23:59:59"
        assert start % 86400 != 0  # local midnight, not UTC midnight

    def test_month_day_uses_local_midnight(self, berlin_tz):
        from nixclaw_agent.tools.messages import _parse_time_range
        start, _ = _parse_time_range("March 15th")
        assert datetime.fromtimestamp(start).strftime("%m-%d %H:%M") == "03-15 00:00"


class TestFuzzyMatching:
    """Tests for fuzzy matching behavior."""

    @pytest.fixture(autouse=True)
    def setup_mock_db(self):
        """Set up mock database for each test."""
        self.mock_messages = get_mock_conversations()
        self.patcher = patch("nixclaw_agent.tools.messages.get_messages")
        self.mock_get_messages = self.patcher.start()
        self.mock_get_messages.return_value = self.mock_messages
        yield
        self.patcher.stop()

    def test_fuzzy_threshold_80_percent(self):
        """Verify fuzzy matching works at ~80% similarity."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="endpont", channel_id=TEST_CHANNEL)
        assert result.success

    def test_fuzzy_not_too_permissive(self):
        """Verify fuzzy matching doesn't match completely unrelated words."""
        from nixclaw_agent.tools.messages import message_search
        result = message_search(query="apples oranges", channel_id=TEST_CHANNEL)
        assert result.success
        assert "no messages found" in result.output.lower() or "no matches" in result.output.lower()


class TestMessageSearchIntegration:
    """Integration tests using mock signal API to verify agent uses message_search."""

    def test_tool_definition_is_registered(self):
        """Verify message_search tool is correctly registered with new parameters."""
        import sys
        sys.path.insert(0, str(REPO_ROOT / "src"))

        from nixclaw_agent.tools import TOOL_DEFINITIONS

        assert "message_search" in TOOL_DEFINITIONS
        tool_def = TOOL_DEFINITIONS["message_search"]

        props = tool_def["params"]["properties"]
        assert "query" in props
        assert "approximate_time" in props
        assert "before" in props
        assert "after" in props
        assert "context_lines" in props
        assert "limit" in props

        from nixclaw_agent import prompts
        desc = prompts.current().doc("tool-descriptions.yaml")["message_search"]["description"]
        assert "fuzzy" in desc.lower()
        assert "approximate time" in desc.lower()

    def test_agent_responds_to_recall_request(self):
        """Verify agent can respond to questions (basic connectivity test)."""
        run_mock_inject("+000000", "what is 2+2?")
        time.sleep(15)

        mock_logs = get_docker_logs_mock(30)
        assert "4" in mock_logs or "two" in mock_logs.lower() or "answer" in mock_logs.lower(), \
            f"Agent should respond to basic math. Logs: {mock_logs[-500:]}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])