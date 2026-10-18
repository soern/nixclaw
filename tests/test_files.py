"""Minimal call-through sandbox tests for src/nixclaw_agent/tools/files.py.

These are not a full test suite for files.py (out of scope here) - just proof
that the containment fix in path_safety.is_within_directory actually holds
through the real public file_read/file_write functions, for a sibling-prefix
channel_id (e.g. "../data_escaped") which the old str.startswith-based check
would have incorrectly treated as inside the sandbox.
"""

import os
import tempfile
from pathlib import Path

import pytest

from src.nixclaw_agent.tools.files import file_read, file_write


class TestFileToolsSandbox:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.original_dir = os.getcwd()
        self.temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self.temp_dir.name)
        from src.nixclaw_agent import config as config_module
        self.original_data_dir = config_module.config.paths.data_dir
        config_module.config.paths.data_dir = Path(self.temp_dir.name) / "data"
        config_module.config.paths.data_dir.mkdir(parents=True, exist_ok=True)
        yield
        os.chdir(self.original_dir)
        self.temp_dir.cleanup()
        from src.nixclaw_agent import config as config_module
        config_module.config.paths.data_dir = self.original_data_dir

    def test_file_write_rejects_sibling_prefix_channel_id(self):
        """A channel_id resolving to a sibling of data_dir (sharing its string
        prefix, e.g. data_escaped) must be rejected, not treated as sandboxed."""
        channel_id = "../data_escaped"
        result = file_write("evil.txt", "pwned", channel_id)

        assert result.success is False
        assert "Path traversal" in result.error

        escaped_dir = Path(self.temp_dir.name) / "data_escaped"
        assert not (escaped_dir / "evil.txt").exists()

    def test_file_read_rejects_sibling_prefix_channel_id(self):
        channel_id = "../data_escaped"
        result = file_read("some.txt", channel_id)

        assert result.success is False
        assert "Path traversal" in result.error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
