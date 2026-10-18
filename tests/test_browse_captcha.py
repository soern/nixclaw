"""Tests for browser_navigate captcha handling and the _run reconnect-retry helper."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nixclaw_agent.tools import browse


CAPTCHA_PAGE = "Page: Bing\nURL: https://bing.com\n\nPlease verify you are human to continue"
NORMAL_PAGE = "Page: Example\nURL: https://example.com\n\nExample Domain"


@pytest.mark.asyncio
class TestBrowserNavigateCaptcha:
    async def test_browser_navigate_flags_captcha_but_keeps_page_content(self):
        with patch.object(browse.manager, "navigate", AsyncMock(return_value=CAPTCHA_PAGE)):
            result = await browse.browser_navigate("https://bing.com", channel_id="cap_ch")
        # The captcha is flagged with solving guidance, but the snapshot stays
        # available so the agent can screenshot/analyze/click it.
        assert result.success
        assert "CAPTCHA" in result.output
        assert "browser_screenshot" in result.output
        assert "verify you are human" in result.output

    async def test_browser_navigate_succeeds_on_normal_page(self):
        with patch.object(browse.manager, "navigate", AsyncMock(return_value=NORMAL_PAGE)):
            result = await browse.browser_navigate("https://example.com", channel_id="ok_ch")
        assert result.success
        assert "Example Domain" in result.output

    async def test_web_search_returns_captcha_error_on_captcha_page(self):
        with patch.object(browse.manager, "navigate", AsyncMock(return_value=CAPTCHA_PAGE)):
            result = await browse.web_search("anything", channel_id="cap_search_ch")
        assert not result.success
        assert "CAPTCHA" in result.error

    async def test_empty_url_snapshots_current_page(self):
        with patch.object(browse.manager, "snapshot", AsyncMock(return_value=NORMAL_PAGE)) as snap:
            with patch.object(browse.manager, "navigate", AsyncMock()) as nav:
                result = await browse.browser_navigate("", channel_id="snap_ch")
        assert result.success
        assert snap.await_count == 1
        assert nav.await_count == 0

    async def test_scheme_is_prefixed(self):
        nav = AsyncMock(return_value=NORMAL_PAGE)
        with patch.object(browse.manager, "navigate", nav):
            await browse.browser_navigate("example.com", channel_id="scheme_ch")
        assert nav.await_args.args[1] == "https://example.com"

    async def test_browser_navigate_detects_duckduckgo_challenge(self):
        ddg_page = (
            "Page: DuckDuckGo\nURL: https://duckduckgo.com/html/?q=x\n\n"
            "Unfortunately, bots use DuckDuckGo too.\n"
            "Please complete the following challenge to confirm this search was made by a human.\n"
            "Select all squares containing a duck:\nSubmit"
        )
        with patch.object(browse.manager, "navigate", AsyncMock(return_value=ddg_page)):
            result = await browse.browser_navigate("https://duckduckgo.com/html/?q=x", channel_id="ddg_ch")
        assert result.success
        assert "CAPTCHA" in result.output
        assert "browser_screenshot" in result.output


@pytest.mark.asyncio
class TestWebSearchDDGFallback:
    async def test_web_search_falls_back_to_ddg_on_bing_captcha(self):
        """When Bing returns CAPTCHA, web_search should retry with DuckDuckGo."""
        bing_captcha_page = "Page: Bing\nURL: https://bing.com\n\nPlease verify you are human"
        ddg_normal_page = "Page: DuckDuckGo\nURL: https://html.duckduckgo.com/html/?q=test\n\nSearch results"

        call_count = {"n": 0}

        async def navigate_side_effect(channel_id, url, scope=None):
            call_count["n"] += 1
            if "bing.com" in url:
                return bing_captcha_page
            elif "duckduckgo.com" in url:
                return ddg_normal_page
            return "unexpected"

        with patch.object(browse.manager, "navigate", AsyncMock(side_effect=navigate_side_effect)):
            result = await browse.web_search("test query", channel_id="ddg_fallback_ch")

        assert result.success
        assert "Search results" in result.output
        assert call_count["n"] == 2  # Called twice: Bing then DDG

    async def test_web_search_returns_error_if_both_engines_captcha(self):
        """When both Bing and DDG return CAPTCHA, web_search should return error."""
        captcha_page = "Page: Engine\n\nPlease verify you are human"

        with patch.object(browse.manager, "navigate", AsyncMock(return_value=captcha_page)):
            result = await browse.web_search("test query", channel_id="both_cap_ch")

        assert not result.success
        assert "Both search engines" in result.error or "blocked" in result.error.lower()

    async def test_web_search_returns_error_if_ddg_fallback_fails(self):
        """When DDG fallback itself raises an exception, web_search should return error."""
        bing_captcha_page = "Page: Bing\n\nPlease verify you are human"

        async def navigate_side_effect(channel_id, url, scope=None):
            if "bing.com" in url:
                return bing_captcha_page
            elif "duckduckgo.com" in url:
                raise Exception("Network error")
            return "unexpected"

        with patch.object(browse.manager, "navigate", AsyncMock(side_effect=navigate_side_effect)):
            result = await browse.web_search("test query", channel_id="ddg_fail_ch")

        assert not result.success
        assert "CAPTCHA" in result.error or "blocked" in result.error.lower()


@pytest.mark.asyncio
class TestSiteLevelErrors:
    async def test_browser_navigate_formats_ssl_error_helpfully(self):
        """SSL errors should include guidance that retry won't help."""
        with patch.object(browse.manager, "navigate", AsyncMock(side_effect=Exception("ERR_SSL_PROTOCOL_ERROR"))):
            result = await browse.browser_navigate("https://badssl.example.com", channel_id="ssl_ch")

        assert not result.success
        assert "site-level problem" in result.error.lower()
        assert "SSL" in result.error or "ssl" in result.error

    async def test_web_search_formats_cert_error_helpfully(self):
        """Certificate errors should include guidance."""
        with patch.object(browse.manager, "navigate", AsyncMock(side_effect=Exception("ERR_CERT_AUTHORITY_INVALID"))):
            result = await browse.web_search("test query", channel_id="cert_ch")

        assert not result.success
        assert "site-level problem" in result.error.lower() or "SSL" in result.error or "cert" in result.error.lower()

    async def test_dns_error_includes_helpful_message(self):
        """DNS resolution errors should be marked as site-level."""
        with patch.object(browse.manager, "navigate", AsyncMock(side_effect=Exception("ERR_NAME_NOT_RESOLVED"))):
            result = await browse.browser_navigate("https://nonexistent.invalid", channel_id="dns_ch")

        assert not result.success
        assert "site-level problem" in result.error.lower()


@pytest.mark.asyncio
class TestScreenshotFilenameSandbox:
    """browser_screenshot must not write outside the channel attachments dir."""

    def _mock_config(self, tmp_path):
        mock_config = MagicMock()
        mock_config.paths.data_dir = tmp_path
        return mock_config

    async def test_path_traversal_filename_rejected(self, tmp_path):
        png = b"\x89PNG fake"
        with patch.object(browse.manager, "screenshot_bytes", AsyncMock(return_value=png)):
            with patch("nixclaw_agent.tools.paths.config", self._mock_config(tmp_path)):
                result = await browse.browser_screenshot(
                    filename="../evil.png", channel_id="shot_ch"
                )
        assert not result.success
        assert "Invalid filename" in result.error
        assert not (tmp_path / "evil.png").exists()

    async def test_absolute_filename_rejected(self, tmp_path):
        png = b"\x89PNG fake"
        outside = tmp_path / "outside.png"
        with patch.object(browse.manager, "screenshot_bytes", AsyncMock(return_value=png)):
            with patch("nixclaw_agent.tools.paths.config", self._mock_config(tmp_path / "data")):
                result = await browse.browser_screenshot(
                    filename=str(outside), channel_id="shot_ch"
                )
        assert not result.success
        assert "Invalid filename" in result.error
        assert not outside.exists()

    async def test_sibling_prefix_channel_id_rejected(self, tmp_path):
        """A channel_id that escapes into a sibling dir sharing a string
        prefix with data_dir must not pass the containment check
        (str.startswith would incorrectly allow this)."""
        png = b"\x89PNG fake"
        sibling_channel_id = f"../{tmp_path.name}_escaped"
        with patch.object(browse.manager, "screenshot_bytes", AsyncMock(return_value=png)):
            with patch("nixclaw_agent.tools.paths.config", self._mock_config(tmp_path)):
                result = await browse.browser_screenshot(
                    filename="ok.png", channel_id=sibling_channel_id
                )
        assert not result.success
        assert "Path traversal" in result.error
        assert not (tmp_path.parent / f"{tmp_path.name}_escaped" / "ok.png").exists()

    async def test_plain_filename_accepted(self, tmp_path):
        png = b"\x89PNG fake"
        with patch.object(browse.manager, "screenshot_bytes", AsyncMock(return_value=png)):
            with patch("nixclaw_agent.tools.paths.config", self._mock_config(tmp_path)):
                result = await browse.browser_screenshot(
                    filename="ok.png", channel_id="shot_ch"
                )
        assert result.success
        assert result.output.splitlines()[0] == "ok.png"
        assert (tmp_path / "shot_ch" / "attachments" / "ok.png").read_bytes() == png


@pytest.mark.asyncio
class TestRunRetry:
    async def test_retriable_error_triggers_reset_and_retry(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("Target page, context or browser has been closed")
            return "ok"

        with patch.object(browse.manager, "reset", AsyncMock()) as reset:
            result = await browse._run(flaky, "chan", "chan")
        assert result == "ok"
        assert calls["n"] == 2
        assert reset.await_count == 1

    async def test_non_retriable_error_raises_immediately(self):
        calls = {"n": 0}

        async def failing():
            calls["n"] += 1
            raise ValueError("No element matched the selector")

        with patch.object(browse.manager, "reset", AsyncMock()) as reset:
            with pytest.raises(ValueError):
                await browse._run(failing, "chan", "chan")
        assert calls["n"] == 1
        assert reset.await_count == 0

    async def test_persistent_retriable_error_raises_after_one_retry(self):
        calls = {"n": 0}

        async def always_down():
            calls["n"] += 1
            raise Exception("connect ECONNREFUSED 127.0.0.1:8931")

        with patch.object(browse.manager, "reset", AsyncMock()):
            with pytest.raises(Exception):
                await browse._run(always_down, "chan", "chan")
        assert calls["n"] == 2

    async def test_retriable_error_closes_only_failing_scope_first(self):
        """The failing scope's context should be closed before any full reset."""
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("target closed")
            return "ok"

        with patch.object(browse.manager, "close_scope", AsyncMock()) as close_scope:
            with patch.object(browse.manager, "reset", AsyncMock()):
                result = await browse._run(flaky, "the_failing_channel", "the_failing_channel")
        assert result == "ok"
        close_scope.assert_awaited_once_with("the_failing_channel", "the_failing_channel")

    async def test_retriable_error_skips_full_reset_when_still_connected(self):
        """If closing just the failing channel restores connectivity, don't
        blow away every other channel's context with a full reset()."""
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("target closed")
            return "ok"

        with patch.object(browse.manager, "close_scope", AsyncMock()):
            with patch.object(browse.manager, "is_connected", MagicMock(return_value=True)):
                with patch.object(browse.manager, "reset", AsyncMock()) as reset:
                    result = await browse._run(flaky, "chan", "chan")
        assert result == "ok"
        assert reset.await_count == 0

    async def test_retriable_error_falls_back_to_full_reset_when_disconnected(self):
        """If the shared connection itself is gone, fall back to a full reset()."""
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("target closed")
            return "ok"

        with patch.object(browse.manager, "close_scope", AsyncMock()):
            with patch.object(browse.manager, "is_connected", MagicMock(return_value=False)):
                with patch.object(browse.manager, "reset", AsyncMock()) as reset:
                    result = await browse._run(flaky, "chan", "chan")
        assert result == "ok"
        assert reset.await_count == 1
