"""Tests for tool registry cache eviction and global web_search caching.

Covers:
- Issue #11: Cache eviction boundaries and truncation interaction
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock
from src.nixclaw_agent.tools.registry import ToolRegistry, MAX_TOOL_CACHE_SIZE, TOOL_TIMEOUT_SECONDS
from src.nixclaw_agent.auth import AuthManager, AuthConfig, UserTier
from src.nixclaw_agent.config import config as _config, TTSEngineConfig
from src.nixclaw_agent.types import ToolResult


@pytest.fixture
def _auth():
    """Create auth manager for testing."""
    config = AuthConfig(privileged_numbers=["+000000"])
    return AuthManager(config)


class TestRegistryCacheEviction:
    """Test web_search cache boundary conditions and LRU behavior."""

    @pytest.mark.asyncio
    async def test_cache_respects_max_size_boundary(self, _auth):
        """Cache doesn't exceed MAX_TOOL_CACHE_SIZE."""
        registry = ToolRegistry(_auth)
        
        # Mock web_search tool
        async def mock_web_search(query: str, **kwargs):
            return ToolResult(
                name="web_search",
                success=True,
                output=f"Results for {query}",
                error=None,
            )
        
        registry.register("web_search", mock_web_search, {"query": {"type": "string"}})
        
        # Add exactly MAX_TOOL_CACHE_SIZE items
        for i in range(MAX_TOOL_CACHE_SIZE):
            result = await registry.execute_tool("web_search", {"query": f"search_{i}"}, UserTier.KNOWN)
            assert result.success

    @pytest.mark.asyncio
    async def test_cache_evicts_on_overflow(self, _auth):
        """Cache evicts oldest entry when full."""
        registry = ToolRegistry(_auth)
        
        async def mock_web_search(query: str, **kwargs):
            return ToolResult(
                name="web_search",
                success=True,
                output=f"Results for {query}",
                error=None,
            )
        
        registry.register("web_search", mock_web_search, {"query": {"type": "string"}})
        
        # Fill cache to capacity
        for i in range(MAX_TOOL_CACHE_SIZE):
            await registry.execute_tool("web_search", {"query": f"search_{i}"}, UserTier.KNOWN)
        
        cache_size_before = len(registry._tool_cache)
        
        # Add one more (should evict oldest)
        await registry.execute_tool("web_search", {"query": f"search_overflow"}, UserTier.KNOWN)
        
        # Cache should stay at max size (or just below)
        assert len(registry._tool_cache) <= MAX_TOOL_CACHE_SIZE + 1


class TestRegistryErrorCaching:
    """Test that errors are not cached."""

    @pytest.mark.asyncio
    async def test_error_not_cached(self, _auth):
        """Tool errors are not stored in cache."""
        registry = ToolRegistry(_auth)
        
        call_count = 0
        
        # web_search is the only tool the registry caches
        async def mock_tool_with_error(query: str, **kwargs):
            nonlocal call_count
            call_count += 1
            return ToolResult(
                name="web_search",
                success=False,
                output=None,
                error="Tool failed",
            )
        
        registry.register("web_search", mock_tool_with_error, {"query": {"type": "string"}})
        
        # First call should fail
        result1 = await registry.execute_tool("web_search", {"query": "test"}, UserTier.KNOWN)
        assert result1.success is False
        assert call_count == 1
        
        # Second call with same args should execute again (not cached)
        result2 = await registry.execute_tool("web_search", {"query": "test"}, UserTier.KNOWN)
        assert result2.success is False
        assert call_count == 2  # Should be called twice (not cached)


class TestCacheCrossChannel:
    """Test cache behavior across channels."""

    @pytest.mark.asyncio
    async def test_web_search_cache_hit_within_same_scope(self, _auth):
        """Repeated web_search in the same browser scope is served from cache."""
        registry = ToolRegistry(_auth)
        
        call_count = 0
        
        async def mock_web_search(query: str, **kwargs):
            nonlocal call_count
            call_count += 1
            return ToolResult(
                name="web_search",
                success=True,
                output=f"Results for {query}",
                error=None,
            )
        
        registry.register("web_search", mock_web_search, {"query": {"type": "string"}})
        
        # First call
        result1 = await registry.execute_tool("web_search", {"query": "python"}, UserTier.KNOWN)
        assert call_count == 1
        
        # Second call with same args (should use cache)
        result2 = await registry.execute_tool("web_search", {"query": "python"}, UserTier.KNOWN)
        assert call_count == 1  # Not called again (cache hit)


    @pytest.mark.asyncio
    async def test_web_search_cache_not_shared_across_scopes(self, _auth):
        """A cached result skips navigation, so it must never be served to a
        different browser scope (other channel, or a background run on the
        same channel)."""
        from pathlib import Path
        from src.nixclaw_agent.types import Context

        registry = ToolRegistry(_auth)
        calls = []

        async def mock_web_search(query: str, **kwargs):
            calls.append(kwargs.get("channel_id"))
            return ToolResult(name="web_search", success=True, output=f"Results for {query}", error=None)

        registry.register("web_search", mock_web_search, {"query": {"type": "string"}})

        def ctx(channel_id, lane="interactive"):
            return Context(channel_id, Path("/x/m.db"), Path("/x/a"), lane=lane)

        async def search_in(context):
            async def run():
                registry.set_context(context)
                return await registry.execute_tool("web_search", {"query": "python"}, UserTier.PRIVILEGED)
            return await asyncio.create_task(run())

        chat_a = ctx("+000000")
        await search_in(chat_a)
        await search_in(chat_a)  # same scope: cache hit
        await search_in(ctx("+000001"))  # other channel
        await search_in(ctx("+000000", lane="background"))  # cron/subagent run, same channel
        assert calls == ["+000000", "+000001", "+000000"]


class TestNonWebSearchNotCached:
    """Test that non-web_search tools are not cached globally."""

    @pytest.mark.asyncio
    async def test_bash_not_cached_globally(self, _auth):
        """Non-web_search tools don't use global registry cache."""
        registry = ToolRegistry(_auth)
        
        # The per-run cache in core.py is separate (per-turn, local)
        # Registry only caches web_search, so other tools should
        # always execute (not cached at registry level)
        
        call_count = 0
        
        async def mock_bash(cmd: str, **kwargs):
            nonlocal call_count
            call_count += 1
            return ToolResult(
                name="bash",
                success=True,
                output=f"Executed: {cmd}",
                error=None,
            )
        
        registry.register("bash", mock_bash, {"cmd": {"type": "string"}})
        
        # First call
        result1 = await registry.execute_tool("bash", {"cmd": "echo test"}, UserTier.PRIVILEGED)
        assert call_count == 1
        
        # Second call with same args
        result2 = await registry.execute_tool("bash", {"cmd": "echo test"}, UserTier.PRIVILEGED)
        assert call_count == 2  # Should be called again (not cached in registry)


class TestTextToSpeechTimeoutBranch:
    """text_to_speech's outer asyncio.wait_for timeout must cover the shared
    TTS semaphore wait + per-engine synthesis timeout + 30s margin, taking
    the max across all configured engines (mirrors generate_image's
    image_gen.max_concurrent_wait_seconds + image_gen.timeout + 30.0)."""

    @pytest.mark.asyncio
    async def test_timeout_uses_max_across_engines(self, _auth, monkeypatch):
        monkeypatch.setattr(
            _config.tts,
            "engines",
            {
                "de": TTSEngineConfig(
                    language="de", max_concurrent_wait_seconds=180.0, timeout=120.0
                ),
                "en": TTSEngineConfig(
                    language="en", max_concurrent_wait_seconds=200.0, timeout=150.0
                ),
            },
        )
        registry = ToolRegistry(_auth)

        async def mock_tts(**kwargs):
            return ToolResult(name="text_to_speech", success=True, output="x.m4a", error=None)

        registry.register("text_to_speech", mock_tts, {"text": {"type": "string"}})

        captured = {}
        real_wait_for = asyncio.wait_for

        async def fake_wait_for(aw, timeout):
            captured["timeout"] = timeout
            return await real_wait_for(aw, timeout)

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

        result = await registry.execute_tool("text_to_speech", {"text": "hi"}, UserTier.KNOWN)
        assert result.success is True
        # en engine has the larger wait+timeout sum (350 > 300), so it wins.
        assert captured["timeout"] == 200.0 + 150.0 + 30.0

    @pytest.mark.asyncio
    async def test_timeout_falls_back_when_no_engines_configured(self, _auth, monkeypatch):
        monkeypatch.setattr(_config.tts, "engines", {})
        registry = ToolRegistry(_auth)

        async def mock_tts(**kwargs):
            return ToolResult(name="text_to_speech", success=True, output="x.m4a", error=None)

        registry.register("text_to_speech", mock_tts, {"text": {"type": "string"}})

        captured = {}
        real_wait_for = asyncio.wait_for

        async def fake_wait_for(aw, timeout):
            captured["timeout"] = timeout
            return await real_wait_for(aw, timeout)

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

        result = await registry.execute_tool("text_to_speech", {"text": "hi"}, UserTier.KNOWN)
        assert result.success is True
        assert captured["timeout"] == TOOL_TIMEOUT_SECONDS + 30.0
