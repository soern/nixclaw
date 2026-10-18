"""__DISCARD_OUTPUT__ is only meaningful for cronjob/subagent output
(AgentLoop._should_discard_output returns False for source="user"/"chat").
If it leaks into an interactive chat reply - e.g. because a tool result
handed the model cron-prompt text that itself said to use the marker - it
must be stripped, never forwarded to the user as raw text.
"""
import sys

sys.path.insert(0, "src")

from conftest import build_mock_agent_loop as _make_agent


class TestStripLeakedDiscardMarker:
    def test_marker_only_response_becomes_empty(self):
        agent = _make_agent()
        out = agent._strip_leaked_discard_marker("\n__DISCARD_OUTPUT__", "+000000")
        assert out == ""

    def test_marker_stripped_case_insensitive(self):
        agent = _make_agent()
        out = agent._strip_leaked_discard_marker("done. __discard_output__", "+000000")
        assert "discard_output" not in out.lower()
        assert out == "done."

    def test_no_marker_passes_through_unchanged(self):
        agent = _make_agent()
        out = agent._strip_leaked_discard_marker("Here are your cronjobs.", "+000000")
        assert out == "Here are your cronjobs."

    def test_should_discard_output_still_false_for_chat_source(self):
        """The marker is stripped, but chat never silently discards - the
        (now-empty) text just isn't sent, same as any other empty reply."""
        agent = _make_agent()
        assert agent._should_discard_output("__DISCARD_OUTPUT__", "+000000") is False


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
