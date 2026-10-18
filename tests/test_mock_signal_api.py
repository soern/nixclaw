#!/usr/bin/env python3
"""Tests for mock signal API endpoint functionality."""

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


def _poll_logs_for_message(container_name: str, message: str, max_attempts: int = 10, backoff_seconds: float = 0.2) -> bool:
    """Poll docker logs with exponential backoff until message found or timeout.
    
    Args:
        container_name: Name of the docker container to check logs for
        message: The message string to search for
        max_attempts: Maximum number of retry attempts
        backoff_seconds: Initial backoff seconds (doubles each attempt)
    
    Returns:
        True if message found, False if not found after max_attempts
    """
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                ["docker", "logs", container_name, "--tail", "100"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if message in result.stdout or message in result.stderr:
                return True
        except subprocess.TimeoutExpired:
            pass
        
        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds * (2 ** attempt))
    return False


class TestMockSignalAPI:
    """Test mock signal API endpoint."""

    def test_mock_endpoint_default_port_8081(self):
        """Test that mock endpoint works with default port 8081."""
        test_message = f"test default port {time.time()}"
        returncode, output = run_mock_inject("+000000", test_message, port=8081)
        
        assert returncode == 0, f"Expected success, got: {output}"
        assert "status" in output.lower(), f"Expected JSON response: {output}"
        assert "ok" in output.lower() or "message_uuid" in output.lower(), f"Expected success response: {output}"

    def test_mock_endpoint_port_8080_returns_404(self):
        """Test that real signal-api (port 8080) returns 404 for mock endpoint."""
        test_message = f"should fail {time.time()}"
        returncode, output = run_mock_inject("+000000", test_message, port=8080)
        
        assert "404" in output, f"Expected 404 error, got: {output}"

    def test_mock_endpoint_injection_logs_forwarded(self):
        """Test that injected messages are logged as forwarded to webhook."""
        test_message = f"forward test {time.time()}"
        run_mock_inject("+000000", test_message, port=8081)
        
        # Use exponential backoff polling instead of fixed sleep (0.2s, 0.4s, 0.8s, ... up to ~102s total)
        # This makes the test more robust to slow log propagation while still completing quickly in normal cases
        assert _poll_logs_for_message(
            "nixclaw-mock-signal-api",
            test_message,
            max_attempts=10,
            backoff_seconds=0.2
        ), f"Expected message in logs after polling: {test_message}"

    def test_multiple_injections(self):
        """Test multiple sequential injections."""
        for i in range(3):
            test_message = f"batch test {i} {time.time()}"
            returncode, output = run_mock_inject("+000000", test_message, port=8081)
            assert returncode == 0, f"Batch test {i} failed: {output}"

    def test_message_uuid_is_unique(self):
        """Test that each injection gets a unique message UUID."""
        uuids = []
        for _ in range(3):
            _, output = run_mock_inject("+000000", f"unique uuid test {time.time()}", port=8081)
            assert "message_uuid" in output, f"Expected message_uuid in response: {output}"
            uuids.append(output.split("message_uuid")[1].split(":")[1].strip().strip('"').strip(','))
        
        assert len(set(uuids)) == 3, f"Expected 3 unique UUIDs, got: {uuids}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
