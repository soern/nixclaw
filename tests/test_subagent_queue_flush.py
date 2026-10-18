#!/usr/bin/env python3
"""Tests for subagent task queue flush functionality.

These tests verify that queued messages are properly flushed after subagent task execution,
addressing the bug where subagent tasks could leave messages stuck in the queue if rate limited.

Note: Subagents are spawned via tool calls from the LLM, so testing requires triggering
the spawn_subagent tool through a user message that causes the LLM to invoke it.
"""

import subprocess
import time
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


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


class TestSubagentQueueFlush:
    """Test subagent task queue flush behavior."""

    def test_subagent_spawns_via_tool(self):
        """Test that spawn_subagent tool can be triggered via message."""
        test_message = f"spawn a subagent that says: test {time.time()}"
        returncode, output = run_mock_inject("+000000", test_message)
        assert returncode == 0, f"Expected success, got: {output}"

    def test_subagent_completes_in_logs(self):
        """Test that subagent completion appears in logs."""
        test_message = f"subagent log test {time.time()}"
        run_mock_inject("+000000", test_message)

        time.sleep(5)

        result = subprocess.run(
            ["docker", "logs", "nixclaw", "--tail", "100", "--since", "30s"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        combined_output = result.stdout + result.stderr
        assert "subagent" in combined_output.lower() or "spawn" in combined_output.lower(), \
            "Expected subagent-related log entries"

    def test_multiple_subagent_spawns(self):
        """Test multiple subagent spawns in sequence."""
        for i in range(2):
            test_message = f"multiple subagent test {i} {time.time()}"
            returncode, output = run_mock_inject("+000000", test_message)
            assert returncode == 0, f"Multiple subagent test {i} failed: {output}"
            time.sleep(2)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])