#!/usr/bin/env python3
"""Tests for browser_cookies.py: storage-state persistence and path-traversal protection.

Per-channel BrowserManager context isolation is tested in test_browser_client.py
(TestChannelContexts/TestConnection) - not duplicated here.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nixclaw_agent.tools.browser_cookies import (
    clear_storage_state,
    get_browser_dir,
    load_storage_state,
    save_storage_state,
)


class TestBrowserCookiesModule:
    """Test the utility functions in browser_cookies.py."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.temp_dir = tmp_path
        self.mock_config = MagicMock()
        self.mock_config.paths.data_dir = self.temp_dir

    def test_get_browser_dir_creates_correct_path(self, setup):
        """Verify path is data/{channel_id}/browser/"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            result = get_browser_dir("+000000")

        expected = self.temp_dir / "+000000" / "browser"
        assert result == expected

    def test_get_browser_dir_prevents_path_traversal(self, setup):
        """Verify channel_id with ../ is blocked"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises(ValueError, match="Path traversal detected"):
                get_browser_dir("../../../etc/passwd")

    def test_save_and_load_storage_state(self, setup):
        """Verify state is saved to and loaded from correct file"""
        test_state = {
            "cookies": [{"name": "test", "value": "value"}],
            "origins": [],
        }

        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            save_storage_state("+000001", test_state)
            loaded = load_storage_state("+000001")

        assert loaded == test_state
        expected_file = self.temp_dir / "+000001" / "browser" / "storage_state.json"
        assert expected_file.exists()

    def test_load_storage_state_returns_none_for_new_channel(self, setup):
        """Verify None returned when no stored state exists"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            result = load_storage_state("nonexistent_channel")

        assert result is None

    def test_clear_storage_state_removes_file(self, setup):
        """Verify file is deleted when clearing"""
        test_state = {"cookies": [], "origins": []}

        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            save_storage_state("+000002", test_state)
            browser_dir = self.temp_dir / "+000002" / "browser"
            state_file = browser_dir / "storage_state.json"
            assert state_file.exists()

            clear_storage_state("+000002")
            assert not state_file.exists()

    def test_save_storage_state_atomic_write(self, setup):
        """Verify atomic write - temp file replaces actual file"""
        test_state = {"cookies": [{"name": "atomic", "value": "test"}], "origins": []}

        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            save_storage_state("+000003", test_state)

            state_file = self.temp_dir / "+000003" / "browser" / "storage_state.json"
            temp_file = self.temp_dir / "+000003" / "browser" / "storage_state.json.tmp"

            assert state_file.exists()
            assert not temp_file.exists()


class TestBrowserDirPathTraversal:
    """Test path traversal protection in browser directory functions."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.temp_dir = tmp_path
        self.mock_config = MagicMock()
        self.mock_config.paths.data_dir = self.temp_dir

    def test_path_traversal_with_double_dots(self, setup):
        """Verify channel_id with ../ is blocked at first level"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises(ValueError, match="Path traversal detected"):
                get_browser_dir("../../etc/passwd")

    def test_path_traversal_with_absolute_path(self, setup):
        """Verify channel_id that resolves outside data_dir is blocked"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises(ValueError, match="Path traversal detected"):
                get_browser_dir("/etc/passwd")

    def test_path_traversal_with_null_bytes(self, setup):
        """Verify channel_id with null bytes is blocked"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises((ValueError, OSError)):
                get_browser_dir("../../../etc/passwd\x00")

    def test_valid_channel_id_works(self, setup):
        """Verify valid channel_ids are accepted"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            result = get_browser_dir("+491234567890")
            expected = self.temp_dir / "+491234567890" / "browser"
            assert result == expected

    def test_multiple_traversal_attempts(self, setup):
        """Verify multiple ../ in path are blocked"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises(ValueError, match="Path traversal detected"):
                get_browser_dir("../../../../../../etc/passwd")

    def test_browser_subdirectory_traversal(self, setup):
        """Verify traversal attempt after browser subdir is blocked"""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises(ValueError, match="Path traversal detected"):
                get_browser_dir("channel_id/browser/../../../etc/passwd")

    def test_sibling_prefix_escape_is_blocked(self, setup):
        """A sibling directory sharing a string prefix with data_dir must not
        pass the containment check (str.startswith would incorrectly allow this)."""
        with patch("nixclaw_agent.tools.browser_cookies.config", self.mock_config):
            with pytest.raises(ValueError, match="Path traversal detected"):
                get_browser_dir(f"../{self.temp_dir.name}_escaped")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
