"""Tests for channel mode administration feature."""

import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch

from src.nixclaw_agent.tools.channel_mode import (
    is_channel_mode_always_process,
    set_channel_mode,
    get_channel_mode,
    ChannelMode,
    _get_context_dir,
)
from src.nixclaw_agent.types import Event, Attachment


class TestIsChannelModeAlwaysProcess:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        from src.nixclaw_agent import config as config_module
        self.original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()
        from src.nixclaw_agent import config as config_module
        config_module.config.paths.data_dir = self.original_data_dir

    def test_returns_false_when_no_config_exists(self):
        result = is_channel_mode_always_process("nonexistent_channel")
        assert result is False

    def test_returns_true_when_mode_is_always_process(self):
        channel_id = "test_channel_always"
        set_channel_mode(channel_id, "always_process")
        result = is_channel_mode_always_process(channel_id)
        assert result is True

    def test_returns_false_when_mode_is_mention_required(self):
        channel_id = "test_channel_mention"
        set_channel_mode(channel_id, "mention_required")
        result = is_channel_mode_always_process(channel_id)
        assert result is False

    def test_handles_errors_gracefully(self):
        with patch("builtins.open", side_effect=OSError("Mocked error")):
            result = is_channel_mode_always_process("any_channel")
        assert result is False


class TestSetChannelMode:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        from src.nixclaw_agent import config as config_module
        self.original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()
        from src.nixclaw_agent import config as config_module
        config_module.config.paths.data_dir = self.original_data_dir

    def test_sets_mode_to_always_process(self):
        channel_id = "test_set_always"
        result = set_channel_mode(channel_id, "always_process")

        assert result.success is True
        assert "always_process" in result.output
        assert result.error is None

        config_file = Path(self.temp_dir.name) / channel_id / "channel_mode.json"
        assert config_file.exists()
        with open(config_file) as f:
            data = json.load(f)
            assert data["mode"] == "always_process"

    def test_sets_mode_to_mention_required(self):
        channel_id = "test_set_mention"
        result = set_channel_mode(channel_id, "mention_required")

        assert result.success is True
        assert "mention_required" in result.output
        assert result.error is None

        config_file = Path(self.temp_dir.name) / channel_id / "channel_mode.json"
        assert config_file.exists()
        with open(config_file) as f:
            data = json.load(f)
            assert data["mode"] == "mention_required"

    def test_handles_errors_gracefully(self):
        with patch("pathlib.Path.mkdir", side_effect=OSError("Mocked error")):
            result = set_channel_mode("any_channel", "always_process")
        assert result.success is False
        assert result.error is not None

    def test_path_traversal_protection_rejects_invalid_channel_id(self):
        result = set_channel_mode("../traversal", "always_process")
        assert result.success is False
        assert "Path traversal" in result.error


class TestGetChannelMode:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        from src.nixclaw_agent import config as config_module
        self.original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()
        from src.nixclaw_agent import config as config_module
        config_module.config.paths.data_dir = self.original_data_dir

    def test_returns_mention_required_when_no_config_exists(self):
        result = get_channel_mode("nonexistent_channel")

        assert result.success is True
        data = json.loads(result.output)
        assert data["mode"] == "mention_required"

    def test_returns_current_mode_from_config(self):
        channel_id = "test_get_mode"
        set_channel_mode(channel_id, "always_process")

        result = get_channel_mode(channel_id)

        assert result.success is True
        data = json.loads(result.output)
        assert data["mode"] == "always_process"

    def test_handles_errors_gracefully(self):
        channel_id = "test_error_channel"
        set_channel_mode(channel_id, "always_process")
        with patch("builtins.open", side_effect=OSError("Mocked error")):
            result = get_channel_mode(channel_id)
        assert result.success is False
        assert result.error is not None

    def test_path_traversal_protection_rejects_invalid_channel_id(self):
        result = get_channel_mode("../traversal")
        assert result.success is False
        assert "Path traversal" in result.error


class TestPathTraversalProtection:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        from src.nixclaw_agent import config as config_module
        self.original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()
        from src.nixclaw_agent import config as config_module
        config_module.config.paths.data_dir = self.original_data_dir

    def test_get_context_dir_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Path traversal"):
            _get_context_dir("../traversal")

    def test_get_context_dir_rejects_absolute_path(self):
        with pytest.raises(ValueError, match="Path traversal"):
            _get_context_dir("/etc/passwd")

    def test_get_context_dir_rejects_sibling_prefix_escape(self):
        """A sibling directory sharing a string prefix with data_dir must not
        pass the containment check (str.startswith would incorrectly allow this)."""
        base_name = Path(self.temp_dir.name).name
        with pytest.raises(ValueError, match="Path traversal"):
            _get_context_dir(f"../{base_name}_escaped")


class TestCoreMentionCheckLogic:
    """Test the mention check logic in core.py at line 374."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        from src.nixclaw_agent import config as config_module
        self.original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()
        from src.nixclaw_agent import config as config_module
        config_module.config.paths.data_dir = self.original_data_dir

    def _should_skip(self, event: Event, channel_id: str) -> bool:
        """Helper to determine if message should be skipped based on the logic at core.py:374"""
        from src.nixclaw_agent.tools.channel_mode import is_channel_mode_always_process
        from src.nixclaw_agent.core import is_mentioned

        return (
            event.group_id is not None
            and not is_mentioned(event)
            and not is_channel_mode_always_process(channel_id)
        )

    def test_should_skip_when_group_not_mentioned_not_always_process(self):
        """When group_id is not None and NOT mentioned and NOT always_process -> should skip"""
        event = Event(
            source="+000001",
            destination="+100000",
            message="hello everyone",
            timestamp=0,
            group_id="group123",
            mentions=[],
        )
        channel_id = "test_channel"
        set_channel_mode(channel_id, "mention_required")

        should_skip = self._should_skip(event, channel_id)
        assert should_skip is True

    def test_should_not_skip_when_group_not_mentioned_but_always_process(self):
        """When group_id is not None and NOT mentioned but IS always_process -> should NOT skip"""
        event = Event(
            source="+000001",
            destination="+100000",
            message="hello everyone",
            timestamp=0,
            group_id="group123",
            mentions=[],
        )
        channel_id = "test_channel_always"
        set_channel_mode(channel_id, "always_process")

        should_skip = self._should_skip(event, channel_id)
        assert should_skip is False

    def test_should_not_skip_when_group_id_is_none(self):
        """When group_id is None (1:1 chat) -> should NOT skip regardless of mention"""
        event = Event(
            source="+000001",
            destination="+100000",
            message="hello there",
            timestamp=0,
            group_id=None,
            mentions=[],
        )
        channel_id = "test_channel_1to1"

        should_skip = self._should_skip(event, channel_id)
        assert should_skip is False

    def test_should_not_skip_when_mentioned_in_group(self):
        """When mentioned in group -> should NOT skip"""
        from src.nixclaw_agent import config as config_module

        event = Event(
            source="+000001",
            destination="+100000",
            message="hello @bot",
            timestamp=0,
            group_id="group123",
            mentions=[config_module.config.signal.phone_number],
        )
        channel_id = "test_channel_mentioned"

        should_skip = self._should_skip(event, channel_id)
        assert should_skip is False

    def test_should_not_skip_when_group_no_config_and_mentioned(self):
        """When no config exists but mentioned -> should NOT skip"""
        from src.nixclaw_agent import config as config_module

        event = Event(
            source="+000001",
            destination="+100000",
            message="hello @bot",
            timestamp=0,
            group_id="group123",
            mentions=[config_module.config.signal.phone_number],
        )
        channel_id = "test_channel_no_config"

        should_skip = self._should_skip(event, channel_id)
        assert should_skip is False

    def test_should_skip_when_no_config_and_not_mentioned(self):
        """When no config (defaults to mention_required) and not mentioned -> should skip"""
        event = Event(
            source="+000001",
            destination="+100000",
            message="hello everyone",
            timestamp=0,
            group_id="group123",
            mentions=[],
        )
        channel_id = "test_channel_no_config"

        should_skip = self._should_skip(event, channel_id)
        assert should_skip is True
