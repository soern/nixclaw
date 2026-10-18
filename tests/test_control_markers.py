"""Tests for control-marker detection/stripping in core.py.

Regression coverage for the bug where re.sub's positional 4th argument is
`count`, not `flags`: `re.sub(pattern, repl, string, re.IGNORECASE)` silently
passed re.IGNORECASE (value 2) as `count=2` instead of enabling
case-insensitivity, so a lowercase marker was detected (re.search's 3rd
positional arg genuinely is `flags`, so detection was already correct) but
NOT stripped from the response text - leaking it into the user-visible
output. This asserts both detection AND stripping work for lowercase markers.
"""

from src.nixclaw_agent.core import (
    _extract_continue_keyword,
    _extract_discard_keyword,
    _extract_mute_keyword,
    _strip_continue_keyword,
    _strip_discard_keyword,
    _strip_mute_keyword,
)


class TestMuteKeywordCaseInsensitivity:
    def test_lowercase_mute_is_detected(self):
        assert _extract_mute_keyword("hi __mute__ bye") == "MUTE"

    def test_lowercase_mute_is_stripped(self):
        result = _strip_mute_keyword("hi __mute__ bye")
        assert "__mute__" not in result.lower()
        assert result == "hi bye"

    def test_lowercase_unmute_is_detected(self):
        assert _extract_mute_keyword("hi __unmute__ bye") == "UNMUTE"

    def test_lowercase_unmute_is_stripped(self):
        result = _strip_mute_keyword("hi __unmute__ bye")
        assert "__unmute__" not in result.lower()
        assert result == "hi bye"

    def test_mixed_case_mute_is_stripped(self):
        result = _strip_mute_keyword("hi __Mute__ bye")
        assert "mute" not in result.lower()
        assert result == "hi bye"


class TestDiscardKeywordCaseInsensitivity:
    def test_lowercase_discard_is_detected(self):
        assert _extract_discard_keyword("hi __discard_output__ bye") is True

    def test_lowercase_discard_is_stripped(self):
        result = _strip_discard_keyword("hi __discard_output__ bye")
        assert "__discard_output__" not in result.lower()
        assert result == "hi bye"


class TestContinueKeywordCaseInsensitivity:
    def test_lowercase_continue_is_detected(self):
        assert _extract_continue_keyword("hi __agent_continue__ bye") is True

    def test_lowercase_continue_is_stripped(self):
        result = _strip_continue_keyword("hi __agent_continue__ bye")
        assert "__agent_continue__" not in result.lower()
        assert result == "hi bye"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
