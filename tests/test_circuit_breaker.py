#!/usr/bin/env python3
"""Tests for circuit breaker param-aware tracking."""

import time
import pytest
from unittest.mock import AsyncMock, patch
from src.nixclaw_agent.auth import AuthManager
from src.nixclaw_agent.config import AuthConfig
from src.nixclaw_agent.tools.registry import (
    ToolRegistry,
    MAX_REPEATED_TOOL_FAILURES,
    TOOL_FAILURE_WINDOW_SECONDS,
)
from src.nixclaw_agent.tools import register_all_tools
from src.nixclaw_agent.types import UserTier


class TestCircuitBreakerParamAware:
    """Test that circuit breaker tracks by (tool_name, param_hash) not just tool_name."""

    @pytest.mark.asyncio
    async def test_01_different_params_dont_trigger_circuit_breaker(self):
        """Test that failures with different parameters are tracked separately.

        Different exit codes produce different param hashes, so circuit breaker
        should NOT trigger even after many failures.
        """
        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)
        registry = ToolRegistry(auth)
        register_all_tools(registry)

        for i in range(MAX_REPEATED_TOOL_FAILURES + 2):
            result = await registry.execute_tool(
                "bash",
                {"cmd": f"exit {i + 1}"},
                UserTier.PRIVILEGED
            )
            assert result.success is False

        result = await registry.execute_tool(
            "bash",
            {"cmd": "echo test"},
            UserTier.PRIVILEGED
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_02_different_tools_have_independent_circuit_breakers(self):
        """Test that different tools have independent failure tracking."""
        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)
        registry = ToolRegistry(auth)
        register_all_tools(registry)

        for _ in range(MAX_REPEATED_TOOL_FAILURES):
            result = await registry.execute_tool(
                "bash",
                {"cmd": "exit 1"},
                UserTier.PRIVILEGED
            )
            assert result.success is False

        result = await registry.execute_tool(
            "bash",
            {"cmd": "echo test"},
            UserTier.PRIVILEGED
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_03_circuit_breaker_resets_after_window(self, mock_time):
        """Test that circuit breaker resets after TOOL_FAILURE_WINDOW_SECONDS."""
        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)
        # Inject mock_time.time as the clock for deterministic testing
        registry = ToolRegistry(auth, _get_current_time=mock_time.time)
        register_all_tools(registry)

        arguments = {"cmd": "exit 1"}
        for i in range(MAX_REPEATED_TOOL_FAILURES):
            result = await registry.execute_tool("bash", arguments, UserTier.PRIVILEGED)
            assert result.success is False

        # Advance mock time instead of real sleep (sub-millisecond instead of 60+ seconds)
        mock_time.tick(TOOL_FAILURE_WINDOW_SECONDS + 1)

        result = await registry.execute_tool("bash", arguments, UserTier.PRIVILEGED)
        assert result.success is False
        assert "circuit breaker" not in result.error.lower()

    @pytest.mark.asyncio
    async def test_04_different_selectors_for_browser_interact(self):
        """Test the original issue: different browser_interact selectors are tracked separately.

        This is the scenario from the debug log where the agent tried multiple
        selectors for the same element and got blocked incorrectly.
        """
        from src.nixclaw_agent.tools import browse

        config = AuthConfig(
            privileged_numbers=["+000000"],
            known_numbers=["+000001"]
        )
        auth = AuthManager(config)
        registry = ToolRegistry(auth)
        register_all_tools(registry)

        selectors = [
            'button:has-text("Akzeptieren")',
            'text="Akzeptieren"',
            'button:text("Akzeptieren")',
            '//button[contains(text(), "Akzeptieren")]',
        ]

        with patch.object(browse, "_run", AsyncMock(side_effect=ValueError("No element matched the selector"))):
            for selector in selectors:
                result = await registry.execute_tool(
                    "browser_interact",
                    {"action": "click", "target": selector},
                    UserTier.PRIVILEGED
                )
                assert result.success is False

            result = await registry.execute_tool(
                "browser_interact",
                {"action": "click", "target": ".cmp-footer__button"},
                UserTier.PRIVILEGED
            )
        assert result.success is False
        assert "circuit breaker" not in result.error.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])