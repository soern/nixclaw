"""Tests for mute functionality."""

import json
import tempfile
import os
import pytest
from pathlib import Path

from src.nixclaw_agent.tools.mute import (
    contains_mute_request,
    mute_channel,
    unmute_channel,
    is_channel_muted,
    read_mute_status,
    MUTE_KEYWORDS,
)


class TestMuteKeywords:
    def test_contains_mute_request_exact_mute(self):
        assert contains_mute_request("mute") is True
        assert contains_mute_request("mute channel") is True
        assert contains_mute_request("mute on") is True

    def test_contains_mute_request_quiet(self):
        assert contains_mute_request("be quiet") is True
        assert contains_mute_request("please be quiet") is True
        assert contains_mute_request("can you be quiet") is True

    def test_contains_mute_request_silent(self):
        assert contains_mute_request("be silent") is True
        assert contains_mute_request("be really silent") is True
        assert contains_mute_request("stay silent") is True

    def test_contains_mute_request_dont_respond(self):
        assert contains_mute_request("don't respond") is True
        assert contains_mute_request("don't say anything") is True
        assert contains_mute_request("do not respond") is True

    def test_contains_mute_request_case_insensitive(self):
        assert contains_mute_request("MUTE") is True
        assert contains_mute_request("BE QUIET") is True
        assert contains_mute_request("Be Silent") is True
        assert contains_mute_request("MUTED") is True

    def test_contains_mute_request_no_false_positives(self):
        assert contains_mute_request("hello there") is False
        assert contains_mute_request("what time is it") is False
        assert contains_mute_request("quite nice") is False
        assert contains_mute_request("quietly") is False
        assert contains_mute_request("silencing") is False


class TestMuteChannelPersistence:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()

    def test_mute_channel_creates_config(self):
        from src.nixclaw_agent import config as config_module
        original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)

        try:
            channel_id = "test_channel_123"
            result = mute_channel(channel_id)

            assert result.success is True
            assert "muted" in result.output.lower()

            mute_file = Path(self.temp_dir.name) / channel_id / "mute.json"
            assert mute_file.exists()

            with open(mute_file) as f:
                data = json.load(f)
                assert data["muted"] is True
        finally:
            config_module.config.paths.data_dir = original_data_dir

    def test_unmute_channel_removes_mute(self):
        from src.nixclaw_agent import config as config_module
        original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)

        try:
            channel_id = "test_channel_456"
            mute_channel(channel_id)

            result = unmute_channel(channel_id)

            assert result.success is True
            assert "unmuted" in result.output.lower()

            mute_file = Path(self.temp_dir.name) / channel_id / "mute.json"
            with open(mute_file) as f:
                data = json.load(f)
                assert data["muted"] is False
        finally:
            config_module.config.paths.data_dir = original_data_dir

    def test_is_channel_muted_after_mute(self):
        from src.nixclaw_agent import config as config_module
        original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)

        try:
            channel_id = "test_channel_789"
            assert is_channel_muted(channel_id) is False

            mute_channel(channel_id)
            assert is_channel_muted(channel_id) is True

            unmute_channel(channel_id)
            assert is_channel_muted(channel_id) is False
        finally:
            config_module.config.paths.data_dir = original_data_dir

    def test_mute_channel_rejects_sibling_prefix_escape(self):
        """A channel_id that escapes into a sibling dir sharing a string
        prefix with data_dir must not pass the containment check
        (str.startswith would incorrectly allow this)."""
        from src.nixclaw_agent import config as config_module
        original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)

        try:
            base_name = Path(self.temp_dir.name).name
            channel_id = f"../{base_name}_escaped"
            result = mute_channel(channel_id)

            assert result.success is False
            assert "Path traversal" in result.error
        finally:
            config_module.config.paths.data_dir = original_data_dir

    def test_read_mute_status(self):
        from src.nixclaw_agent import config as config_module
        original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name)

        try:
            channel_id = "test_channel_status"
            
            result = read_mute_status(channel_id)
            assert result.success is True
            data = json.loads(result.output)
            assert data["muted"] is False

            mute_channel(channel_id)

            result = read_mute_status(channel_id)
            assert result.success is True
            data = json.loads(result.output)
            assert data["muted"] is True
        finally:
            config_module.config.paths.data_dir = original_data_dir
