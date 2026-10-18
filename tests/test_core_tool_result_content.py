"""Unit tests for llm._tool_result_content (bounded tool-result rendering)."""

from src.nixclaw_agent.llm import _tool_result_content, truncate_tool_result
from src.nixclaw_agent.types import ToolResult


class TestToolResultContent:
    def test_success_under_limit_returned_unchanged(self):
        result = ToolResult(name="echo", success=True, output="short output", error=None)
        assert _tool_result_content(result, 1024) == "short output"

    def test_success_over_limit_is_truncated(self):
        long_output = "x" * 5000
        result = ToolResult(name="echo", success=True, output=long_output, error=None)
        rendered = _tool_result_content(result, 1024)
        assert len(rendered) < len(long_output)
        assert "[truncated," in rendered
        assert rendered.startswith("x" * 100)

    def test_failure_returns_error_message(self):
        result = ToolResult(name="echo", success=False, output=None, error="boom")
        assert _tool_result_content(result, 1024) == "Error: boom"

    def test_failure_over_limit_is_truncated(self):
        result = ToolResult(name="echo", success=False, output=None, error="e" * 5000)
        rendered = _tool_result_content(result, 1024)
        assert len(rendered) < 5000 + len("Error: ")
        assert "[truncated," in rendered

    def test_success_with_none_output_returns_empty_string(self):
        result = ToolResult(name="echo", success=True, output=None, error=None)
        assert _tool_result_content(result, 1024) == ""


class TestTruncateToolResult:
    def test_no_truncation_when_under_limit(self):
        assert truncate_tool_result("short result", 100) == "short result"

    def test_no_truncation_at_exact_limit(self):
        assert truncate_tool_result("a" * 100, 100) == "a" * 100

    def test_truncation_adds_marker(self):
        truncated = truncate_tool_result("a" * 1000, 500)
        assert truncated == "a" * 500 + "\n...[truncated, 500 of 1000 chars omitted]"

    def test_truncation_preserves_first_chars(self):
        assert truncate_tool_result("PREFIX" + "x" * 1000, 100).startswith("PREFIX")
