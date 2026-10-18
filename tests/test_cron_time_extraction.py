"""Tests for cron time extraction functionality."""

import pytest
from datetime import datetime, timedelta, timezone
import sys
sys.path.insert(0, 'src')

# TestScheduleCronValidation calls the real schedule_cron(), which persists
# via db.operations - route it at a throwaway sqlite DB instead of the
# default /data/messages.db path.
pytestmark = pytest.mark.usefixtures("reset_db")


class TestParseCronWhen:
    """Tests for _parse_cron_when function."""

    def test_bare_time_converts_to_utc(self):
        """Test that bare time is converted to UTC."""
        from nixclaw_agent.tools.cron import _parse_cron_when

        result = _parse_cron_when("16:40")
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_today_converts_to_utc(self):
        """Test that 'today 16:40' is converted to UTC."""
        from nixclaw_agent.tools.cron import _parse_cron_when

        result = _parse_cron_when("today 16:40")
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_relative_time_plus_minutes(self):
        """Test that '+5minutes' still works."""
        from nixclaw_agent.tools.cron import _parse_cron_when

        result = _parse_cron_when("+5minutes")
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_every_weekday_still_works(self):
        """Test that 'every Monday' still works."""
        from nixclaw_agent.tools.cron import _parse_cron_when

        result = _parse_cron_when("every Monday")
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc
        assert result.weekday() == 0

    def test_iso_format_still_works(self):
        """Test that ISO format datetime still works."""
        from nixclaw_agent.tools.cron import _parse_cron_when

        result = _parse_cron_when("2026-04-10T16:40:00")
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc


class TestScheduleCronValidation:
    """Tests for schedule_cron validation."""

    def test_past_date_rejected(self):
        """Test that scheduling more than 24 hours in the past is rejected."""
        from src.nixclaw_agent.tools.cron import schedule_cron

        result = schedule_cron("test prompt", "2025-05-22 16:40", "+000000", "+000000", summary="test")
        assert result.success is False
        assert "past" in result.error.lower()

    def test_valid_date_succeeds(self):
        """Test that scheduling a valid future date succeeds."""
        from src.nixclaw_agent.tools.cron import schedule_cron

        future_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        result = schedule_cron("test prompt", f"{future_date} 16:40", "+000000", "+000000", summary="test")
        assert result.success is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])