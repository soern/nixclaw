"""Tests for the shared path-containment check in path_safety.py.

The bug being guarded against: `str.startswith` is a plain string-prefix
test, so a sibling directory that happens to share a string prefix with the
base directory (e.g. base=/tmp/xyz_data, candidate=/tmp/xyz_data_escaped)
was incorrectly treated as "inside" the base directory.
"""

import tempfile
from pathlib import Path

from src.nixclaw_agent.path_safety import is_within_directory


class TestIsWithinDirectory:
    def test_sibling_prefix_escape_is_rejected(self):
        """The actual bug: a sibling dir sharing a string prefix must be False."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "xyz_data"
            base.mkdir()
            sibling = Path(tmp) / "xyz_data_escaped"
            sibling.mkdir()

            assert is_within_directory(sibling, base) is False

    def test_legitimate_subdirectory_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir()
            sub = base / "channel_123"
            sub.mkdir()

            assert is_within_directory(sub, base) is True

    def test_path_equal_to_base_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir()

            assert is_within_directory(base, base) is True

    def test_classic_dotdot_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir()
            (Path(tmp) / "etc").mkdir()
            candidate = base / ".." / "etc" / "passwd"

            assert is_within_directory(candidate, base) is False

    def test_already_resolved_and_unresolved_paths_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir()
            sub = base / "channel_abc"
            sub.mkdir()

            resolved_candidate = sub.resolve()
            unresolved_candidate = base / "." / "channel_abc"

            assert is_within_directory(resolved_candidate, base) is True
            assert is_within_directory(unresolved_candidate, base) is True


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
