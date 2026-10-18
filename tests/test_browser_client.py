"""Tests for the playwright-direct BrowserManager and the browse.py tool layer."""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nixclaw_agent.tools import browse
from nixclaw_agent.tools.browser_client import (
    BrowserManager,
    SNAPSHOT_MAX_CHARS,
)


def make_page():
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    # Mock response object with status
    response = MagicMock()
    response.status = 200
    page.goto = AsyncMock(return_value=response)
    page.go_back = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.title = AsyncMock(return_value="Example")
    page.url = "https://example.com"
    page.evaluate = AsyncMock(return_value="inner text fallback")
    page.screenshot = AsyncMock(return_value=b"\x89PNG-fake-bytes")
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()

    element = MagicMock()
    element.click = AsyncMock()
    element.fill = AsyncMock()
    locator = MagicMock()
    locator.aria_snapshot = AsyncMock(return_value='- heading "Example"')
    locator.first = element
    page.locator = MagicMock(return_value=locator)
    return page


def make_manager(connected=True):
    """BrowserManager wired to a mock browser; no real playwright involved."""
    manager = BrowserManager()
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=connected)
    browser.close = AsyncMock()

    def make_context(**kwargs):
        context = MagicMock()
        context.new_page = AsyncMock(return_value=make_page())
        context.close = AsyncMock()
        context.storage_state = AsyncMock(
            return_value={"cookies": [{"name": "s", "value": "1"}], "origins": []}
        )
        return context

    browser.new_context = AsyncMock(side_effect=make_context)
    manager._browser = browser
    manager._playwright = MagicMock()
    return manager


def no_stored_state():
    return patch(
        "nixclaw_agent.tools.browser_client.load_storage_state", return_value=None
    )


@pytest.fixture(autouse=True)
def _no_real_state_writes():
    """Keep navigate/interact from writing storage state to the real data dir."""
    with patch("nixclaw_agent.tools.browser_client.save_storage_state"):
        yield


@pytest.mark.asyncio
class TestConnection:
    async def test_connect_failure_raises(self):
        manager = BrowserManager()
        manager._playwright = MagicMock()
        manager._playwright.chromium.connect = AsyncMock(
            side_effect=Exception("connect ECONNREFUSED 127.0.0.1:8931")
        )
        with no_stored_state():
            with pytest.raises(Exception, match="ECONNREFUSED"):
                await manager.get_page("chan")

    async def test_reconnect_after_disconnect(self):
        manager = make_manager()
        with no_stored_state():
            await manager.get_page("chan")
            assert "chan" in manager._contexts
            old_browser = manager._browser
            old_browser.is_connected = MagicMock(return_value=False)

            new_browser = MagicMock()
            new_browser.is_connected = MagicMock(return_value=True)
            new_browser.new_context = old_browser.new_context
            manager._playwright.chromium.connect = AsyncMock(return_value=new_browser)

            page = await manager.get_page("chan")
        assert manager._browser is new_browser
        assert page is not None
        assert manager._playwright.chromium.connect.await_count == 1

    async def test_reset_closes_browser_and_forgets_contexts(self):
        manager = make_manager()
        with no_stored_state():
            await manager.get_page("chan")
        browser = manager._browser
        await manager.reset()
        assert manager._browser is None
        assert manager._contexts == {}
        assert manager._pages == {}
        assert browser.close.await_count == 1


@pytest.mark.asyncio
class TestChannelContexts:
    async def test_channels_are_isolated(self):
        manager = make_manager()
        with no_stored_state():
            page_a = await manager.get_page("chan_a")
            page_b = await manager.get_page("chan_b")
        assert page_a is not page_b
        assert manager._contexts["chan_a"] is not manager._contexts["chan_b"]

    async def test_same_channel_reuses_context(self):
        manager = make_manager()
        with no_stored_state():
            first = await manager.get_page("chan")
            second = await manager.get_page("chan")
        assert first is second
        assert manager._browser.new_context.await_count == 1

    async def test_close_channel_closes_and_forgets(self):
        manager = make_manager()
        with no_stored_state():
            await manager.get_page("chan")
            context = manager._contexts["chan"]
            await manager.close_channel("chan")
            assert "chan" not in manager._contexts
            assert "chan" not in manager._pages
            assert context.close.await_count == 1
            await manager.get_page("chan")
        assert manager._browser.new_context.await_count == 2

    async def test_closed_page_is_replaced(self):
        """A page.is_closed()-detected-closed page triggers auto-replacement,
        distinct from an explicit close_channel() call above."""
        manager = make_manager()
        with no_stored_state():
            first = await manager.get_page("chan")
            old_context = manager._contexts["chan"]
            first.is_closed = MagicMock(return_value=True)
            second = await manager.get_page("chan")
        assert second is not first
        assert old_context.close.await_count == 1


@pytest.mark.asyncio
class TestStorageState:
    async def test_context_created_with_persisted_state(self):
        manager = make_manager()
        stored = {"cookies": [{"name": "session", "value": "abc"}], "origins": []}
        with patch(
            "nixclaw_agent.tools.browser_client.load_storage_state",
            return_value=stored,
        ):
            await manager.get_page("chan")
        kwargs = manager._browser.new_context.await_args.kwargs
        assert kwargs["storage_state"] == stored

    async def test_close_channel_saves_state(self):
        manager = make_manager()
        with no_stored_state():
            await manager.get_page("chan")
            context = manager._contexts["chan"]
            with patch(
                "nixclaw_agent.tools.browser_client.save_storage_state"
            ) as save:
                await manager.close_channel("chan")
        assert context.storage_state.await_count == 1
        save.assert_called_once_with(
            "chan", {"cookies": [{"name": "s", "value": "1"}], "origins": []}
        )
        assert context.close.await_count == 1

    async def test_close_channel_state_save_failure_still_closes(self):
        manager = make_manager()
        with no_stored_state():
            await manager.get_page("chan")
            context = manager._contexts["chan"]
            context.storage_state = AsyncMock(side_effect=Exception("target closed"))
            await manager.close_channel("chan")
        assert context.close.await_count == 1
        assert "chan" not in manager._contexts

    async def test_navigate_saves_state(self):
        manager = make_manager()
        with no_stored_state():
            with patch(
                "nixclaw_agent.tools.browser_client.save_storage_state"
            ) as save:
                await manager.navigate("chan", "https://example.com")
        save.assert_called_once()
        channel_arg, state_arg = save.call_args.args
        assert channel_arg == "chan"
        assert state_arg["cookies"] == [{"name": "s", "value": "1"}]

    async def test_load_failure_does_not_block_page_creation(self):
        manager = make_manager()
        with patch(
            "nixclaw_agent.tools.browser_client.load_storage_state",
            side_effect=OSError("corrupt state file"),
        ):
            page = await manager.get_page("chan")
        assert page is not None
        assert manager._browser.new_context.await_args.kwargs["storage_state"] is None


@pytest.mark.asyncio
class TestSnapshot:
    async def test_snapshot_uses_aria_snapshot(self):
        manager = make_manager()
        with no_stored_state():
            output = await manager.snapshot("chan")
        assert output.startswith("Page: Example\nURL: https://example.com")
        assert '- heading "Example"' in output

    async def test_snapshot_falls_back_to_inner_text_on_error(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.locator("body").aria_snapshot = AsyncMock(
                side_effect=Exception("aria not supported")
            )
            output = await manager.snapshot("chan")
        assert "inner text fallback" in output

    async def test_snapshot_falls_back_to_inner_text_when_empty(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.locator("body").aria_snapshot = AsyncMock(return_value="")
            output = await manager.snapshot("chan")
        assert "inner text fallback" in output

    async def test_snapshot_truncated_with_marker(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.locator("body").aria_snapshot = AsyncMock(return_value="x" * 50000)
            output = await manager.snapshot("chan")
        assert len(output) <= SNAPSHOT_MAX_CHARS
        assert output.endswith("[truncated]")

    async def test_snapshot_includes_http_status_for_4xx_5xx(self):
        manager = make_manager()
        with no_stored_state():
            output = await manager.snapshot("chan", http_status=404)
        assert "HTTP status: 404" in output
        assert output.startswith("Page: Example\nURL: https://example.com\nHTTP status: 404")

    async def test_snapshot_excludes_http_status_for_2xx_3xx(self):
        manager = make_manager()
        with no_stored_state():
            output = await manager.snapshot("chan", http_status=200)
        assert "HTTP status" not in output

    async def test_snapshot_excludes_http_status_when_none(self):
        manager = make_manager()
        with no_stored_state():
            output = await manager.snapshot("chan", http_status=None)
        assert "HTTP status" not in output

    async def test_snapshot_uses_settle_timeout_for_aria(self):
        """aria_snapshot should use SETTLE_TIMEOUT_MS (15000) not LOCATOR_TIMEOUT_MS (5000)."""
        from nixclaw_agent.tools.browser_client import SETTLE_TIMEOUT_MS
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.locator("body").aria_snapshot = AsyncMock(return_value="test")
            await manager.snapshot("chan")
        page.locator("body").aria_snapshot.assert_awaited_with(timeout=SETTLE_TIMEOUT_MS)


@pytest.mark.asyncio
class TestNavigate:
    async def test_navigate_captures_response_status(self):
        """navigate() should capture the response status and pass it to snapshot()."""
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            response = MagicMock()
            response.status = 404
            page.goto = AsyncMock(return_value=response)
            page.wait_for_load_state = AsyncMock()
            output = await manager.navigate("chan", "https://example.com/notfound")
        assert "HTTP status: 404" in output


@pytest.mark.asyncio
class TestEvaluate:
    async def test_expression_gets_return_wrapping(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.evaluate = AsyncMock(return_value="Example Domain")
            result = await manager.evaluate("chan", "document.title")
        assert page.evaluate.await_args.args[0] == (
            "async () => { return (document.title); }"
        )
        assert result == "Example Domain"

    async def test_statement_body_wrapped_verbatim(self):
        manager = make_manager()
        script = "const x = 1; return x"
        with no_stored_state():
            page = await manager.get_page("chan")
            page.evaluate = AsyncMock(return_value=1)
            await manager.evaluate("chan", script)
        assert page.evaluate.await_args.args[0] == f"async () => {{ {script} }}"

    async def test_non_string_result_is_json(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.evaluate = AsyncMock(return_value={"links": 3, "title": "t"})
            result = await manager.evaluate("chan", "return summary()")
        assert json.loads(result) == {"links": 3, "title": "t"}

    async def test_none_result_reports_no_value(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.evaluate = AsyncMock(return_value=None)
            result = await manager.evaluate("chan", "console.log('hi')")
        assert result == "(script returned no value)"


@pytest.mark.asyncio
class TestInteract:
    async def test_unknown_action_raises_with_valid_list(self):
        manager = make_manager()
        with no_stored_state():
            with pytest.raises(ValueError, match="click, type, press, back"):
                await manager.interact("chan", "hover", "#el", "")

    async def test_click_requires_target(self):
        manager = make_manager()
        with no_stored_state():
            with pytest.raises(ValueError, match="requires 'target'"):
                await manager.interact("chan", "click", "", "")

    async def test_press_requires_value(self):
        manager = make_manager()
        with no_stored_state():
            with pytest.raises(ValueError, match="requires 'value'"):
                await manager.interact("chan", "press", "", "")

    async def test_click_returns_updated_snapshot(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            output = await manager.interact("chan", "click", "#submit", "")
        assert page.locator("#submit").first.click.await_count == 1
        assert output.startswith("Page: Example")

    async def test_type_fills_value(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            await manager.interact("chan", "type", "input[name=q]", "hello")
        page.locator("input[name=q]").first.fill.assert_awaited_once_with(
            "hello", timeout=5000
        )

    async def test_back_goes_back(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            await manager.interact("chan", "back", "", "")
        assert page.go_back.await_count == 1

    async def test_selector_timeout_gives_selector_help(self):
        pytest.importorskip("playwright")
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.locator("#missing").first.click = AsyncMock(
                side_effect=PlaywrightTimeoutError("timeout 5000ms exceeded")
            )
            with pytest.raises(ValueError, match="Accepted selector forms"):
                await manager.interact("chan", "click", "#missing", "")


@pytest.mark.asyncio
class TestNoPageGuard:
    """interact/evaluate on a fresh context (still about:blank) give a clear error."""

    async def test_interact_on_blank_page_gives_friendly_error(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.url = "about:blank"
            with pytest.raises(ValueError, match="No page loaded yet"):
                await manager.interact("chan", "click", "#btn", "")
        assert page.locator("#btn").first.click.await_count == 0

    async def test_evaluate_on_blank_page_gives_friendly_error(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.url = "about:blank"
            with pytest.raises(ValueError, match="No page loaded yet"):
                await manager.evaluate("chan", "return document.title")

    async def test_interact_allowed_after_navigation(self):
        manager = make_manager()
        with no_stored_state():
            page = await manager.get_page("chan")
            page.url = "https://example.com"
            output = await manager.interact("chan", "click", "#btn", "")
        assert output.startswith("Page: Example")


@pytest.mark.asyncio
class TestBrowseToolLayer:
    async def test_screenshot_writes_bytes_to_attachments(self, tmp_path):
        mock_config = MagicMock()
        mock_config.paths.data_dir = tmp_path
        png = b"\x89PNG-fake-bytes"
        with patch("nixclaw_agent.tools.paths.config", mock_config):
            with patch.object(
                browse.manager, "screenshot_bytes", AsyncMock(return_value=png)
            ):
                result = await browse.browser_screenshot(
                    filename="shot.png", channel_id="chan"
                )
        assert result.success
        assert result.output.splitlines()[0] == "shot.png"
        written = tmp_path / "chan" / "attachments" / "shot.png"
        assert written.read_bytes() == png

    async def test_screenshot_empty_data_fails(self):
        with patch.object(
            browse.manager, "screenshot_bytes", AsyncMock(return_value=b"")
        ):
            result = await browse.browser_screenshot(channel_id="chan")
        assert not result.success
        assert "Empty screenshot" in result.error

    async def test_evaluate_rejects_empty_script(self):
        result = await browse.browser_evaluate("   ", channel_id="chan")
        assert not result.success

    async def test_different_channels_still_get_isolated_manager_contexts(self):
        """BrowserManager itself gives each channel_id an independent
        BrowserContext/page (see TestChannelContexts above, which exercises
        this directly)."""
        with no_stored_state():
            page_a = await browse.manager.get_page("iso_conc_a")
            page_b = await browse.manager.get_page("iso_conc_b")
        assert page_a is not page_b

    async def test_different_channels_browse_concurrently(self):
        """There is no global browser lease any more: two browser_navigate
        calls for *different* channels (different scopes, different pages)
        run concurrently instead of one queueing behind / failing on the
        other."""
        both_started = asyncio.Event()
        started = set()

        async def nav(channel_id, url, scope=None):
            started.add(channel_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1.0)
            return f"Page: {channel_id}\nURL: {url}\n\nok"

        with patch.object(browse.manager, "navigate", side_effect=nav):
            result_a, result_b = await asyncio.wait_for(
                asyncio.gather(
                    browse.browser_navigate("https://a.example", channel_id="conc_a"),
                    browse.browser_navigate("https://b.example", channel_id="conc_b"),
                ),
                timeout=2.0,
            )

        assert result_a.success and result_b.success
        assert "conc_a" in result_a.output
        assert "conc_b" in result_b.output

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
