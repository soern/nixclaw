"""Browser tools for nixclaw-agent, backed by a Playwright server (no MCP)."""

import asyncio
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, TypeVar
from urllib.parse import quote

from .. import prompts
from ..types import ToolResult
from .browser_client import manager
from .paths import get_attachments_dir as _get_attachments_dir
from .registry import get_current_browser_scope

LOG = logging.getLogger(__name__)

CAPTCHA_PATTERNS = [
    "captcha",
    "unusual traffic",
    "prove you're human",
    "verify you are human",
    "human verification",
    "bots use duckduckgo too",
    "select all squares containing",
]

# Errors that indicate the websocket/browser died and a reconnect may help.
RETRIABLE_MARKERS = (
    "has been closed",
    "browser has been closed",
    "target closed",
    "target page, context or browser",
    "disconnected",
    "connection refused",
    "connect econnrefused",
    "websocket error",
)

# Site-level permanent errors that should not trigger retry.
SITE_ERROR_MARKERS = (
    "err_ssl",
    "err_http2_protocol_error",
    "err_connection_reset",
    "err_connection_refused",
    "err_cert",
    "err_name_not_resolved",
)

# One lock per browser scope (= one page): serializes tool calls against the
# same page. Chat turns use the channel scope, each cronjob/subagent run its
# own ephemeral scope (types.Context.browser_scope), so background runs never
# touch the page a chat turn is using.
_locks_guard = asyncio.Lock()
_lock_map: dict[str, asyncio.Lock] = {}

T = TypeVar("T")


async def _get_lock(scope: str) -> asyncio.Lock:
    async with _locks_guard:
        if scope not in _lock_map:
            _lock_map[scope] = asyncio.Lock()
        return _lock_map[scope]


def _current_scope(channel_id: str) -> str:
    """Browser scope of the running turn; the channel (chat) scope if no turn Context is set."""
    return get_current_browser_scope() or channel_id


def _is_captcha_page(content: str) -> bool:
    if not content:
        return False
    content_lower = content.lower()
    return any(pattern in content_lower for pattern in CAPTCHA_PATTERNS)


def _is_site_level_error(error_message: str) -> bool:
    """Check if an error is a permanent site-level issue (SSL, protocol, cert, DNS)."""
    if not error_message:
        return False
    message_lower = error_message.lower()
    return any(marker in message_lower for marker in SITE_ERROR_MARKERS)


def _format_site_error(original_error: str) -> str:
    """Format a site-level error with guidance."""
    return (
        f"{original_error}\n\n"
        "This is a site-level problem (SSL/protocol/connection). "
        "Retrying the same URL will not help - try a different site."
    )


async def _run(op: Callable[[], Awaitable[T]], channel_id: str, scope: str) -> T:
    """Run a browser operation with one reconnect-and-retry on connection loss.

    On a retriable error, close only the failing scope's context first
    (cheap, doesn't disturb other scopes' in-flight browsing) and only
    fall back to a full `manager.reset()` (closes every scope) if that
    wasn't enough to restore a live connection - i.e. the websocket itself
    is gone, not just this scope's page.
    """
    for attempt in range(2):
        try:
            return await op()
        except Exception as e:
            message = str(e).lower()
            if attempt == 0 and any(marker in message for marker in RETRIABLE_MARKERS):
                LOG.warning(f"Browser operation failed ({e}); reconnecting and retrying once")
                await manager.close_scope(channel_id, scope)
                if not manager.is_connected():
                    await manager.reset()
                continue
            raise
    raise RuntimeError("unreachable")


async def close_browser_scope(channel_id: str, scope: str) -> None:
    """Close a (background run's) browser scope and forget its lock."""
    await manager.close_scope(channel_id, scope)
    async with _locks_guard:
        _lock_map.pop(scope, None)


async def web_search(query: str, channel_id: str) -> ToolResult:
    """Search the web using Bing with fallback to DuckDuckGo; return the results page as text."""
    if not query or not query.strip():
        return ToolResult(
            name="web_search",
            success=False,
            output=None,
            error="Search query cannot be empty",
        )

    query_clean = query.strip()
    bing_url = f"https://www.bing.com/search?q={quote(query_clean)}"
    LOG.info(f"web_search: query='{query_clean}' -> trying Bing")

    scope = _current_scope(channel_id)
    async with await _get_lock(scope):
        try:
            content = await _run(lambda: manager.navigate(channel_id, bing_url, scope=scope), channel_id, scope)
            if _is_captcha_page(content):
                LOG.warning("CAPTCHA detected on Bing; trying DuckDuckGo fallback")
                try:
                    ddg_url = f"https://html.duckduckgo.com/html/?q={quote(query_clean)}"
                    content = await _run(lambda: manager.navigate(channel_id, ddg_url, scope=scope), channel_id, scope)
                    if _is_captcha_page(content):
                        LOG.warning("CAPTCHA also detected on DuckDuckGo")
                        return ToolResult(
                            name="web_search",
                            success=False,
                            output=None,
                            error=(
                                "Both search engines blocked the request (CAPTCHA). "
                                "Either solve the captcha on the current page "
                                "(browser_screenshot -> analyze_image -> browser_interact) "
                                "or navigate directly to a relevant website with browser_navigate."
                            ),
                        )
                    LOG.info("DuckDuckGo fallback succeeded")
                    return ToolResult(name="web_search", success=True, output=content, error=None)
                except Exception as ddg_error:
                    LOG.warning(f"DuckDuckGo fallback failed: {ddg_error}")
                    return ToolResult(
                        name="web_search",
                        success=False,
                        output=None,
                        error="Search engine blocked the request (CAPTCHA). Try a different query or wait and retry.",
                    )
            return ToolResult(name="web_search", success=True, output=content, error=None)
        except Exception as e:
            error_str = str(e)
            if _is_site_level_error(error_str):
                error_str = _format_site_error(error_str)
            return ToolResult(name="web_search", success=False, output=None, error=error_str)


async def browser_navigate(url: str = "", channel_id: str = "") -> ToolResult:
    """Navigate to a URL and return a text snapshot of the page.

    With an empty url, returns a snapshot of the current page without navigating.
    Pages that render content via JavaScript may show only navigation/empty content in the snapshot;
    use browser_evaluate to extract dynamic content (e.g. `document.querySelector('.results').innerText`).
    """
    if url and not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url

    scope = _current_scope(channel_id)
    async with await _get_lock(scope):
        try:
            if url:
                content = await _run(lambda: manager.navigate(channel_id, url, scope=scope), channel_id, scope)
            else:
                content = await _run(lambda: manager.snapshot(channel_id, scope=scope), channel_id, scope)
            if _is_captcha_page(content):
                LOG.warning(f"CAPTCHA detected in response from {url or 'current page'}")
                return ToolResult(
                    name="browser_navigate",
                    success=True,
                    output=prompts.current().text("captcha-guidance") + "\n\n" + content,
                    error=None,
                )
            return ToolResult(name="browser_navigate", success=True, output=content, error=None)
        except Exception as e:
            error_str = str(e)
            if _is_site_level_error(error_str):
                error_str = _format_site_error(error_str)
            return ToolResult(name="browser_navigate", success=False, output=None, error=error_str)


async def browser_interact(action: str, target: str = "", value: str = "", channel_id: str = "") -> ToolResult:
    """Interact with the current page: click, type, press a key, or go back.

    Returns the updated page snapshot on success.
    """
    scope = _current_scope(channel_id)
    async with await _get_lock(scope):
        try:
            content = await _run(lambda: manager.interact(channel_id, action, target, value, scope=scope), channel_id, scope)
            return ToolResult(name="browser_interact", success=True, output=content, error=None)
        except Exception as e:
            return ToolResult(name="browser_interact", success=False, output=None, error=str(e))


async def browser_screenshot(url: str = "", filename: str = "", channel_id: str = "") -> ToolResult:
    """Screenshot the current page (or a URL) and save as PNG to attachments.

    On success the output's first line is the bare filename, followed by
    the URL and title of the captured page so the model can tell whether
    it screenshotted the page it expected.
    """
    scope = _current_scope(channel_id)
    async with await _get_lock(scope):
        try:
            image_data = await _run(lambda: manager.screenshot_bytes(channel_id, url, scope=scope), channel_id, scope)
            if not image_data:
                return ToolResult(name="browser_screenshot", success=False, output=None, error="Empty screenshot data")

            if not filename:
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                random_suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=4))
                filename = f"screenshot_{timestamp}_{random_suffix}.png"

            attachments_dir = _get_attachments_dir(channel_id)
            attachments_dir.mkdir(parents=True, exist_ok=True)
            filepath = (attachments_dir / filename).resolve()
            if filepath.parent != attachments_dir:
                return ToolResult(
                    name="browser_screenshot",
                    success=False,
                    output=None,
                    error="Invalid filename: must be a plain filename without directory parts",
                )
            filepath.write_bytes(image_data)
            filename = filepath.name
            page_url, page_title = await manager.page_info(channel_id, scope=scope)

            return ToolResult(
                name="browser_screenshot",
                success=True,
                output=f"{filename}\nURL: {page_url}\nTitle: {page_title}",
                error=None,
            )
        except Exception as e:
            return ToolResult(name="browser_screenshot", success=False, output=None, error=str(e))


async def browser_evaluate(script: str, channel_id: str) -> ToolResult:
    """Run JavaScript inside the current page (document/window scope only)."""
    if not script or not script.strip():
        return ToolResult(
            name="browser_evaluate",
            success=False,
            output=None,
            error="Script cannot be empty",
        )

    scope = _current_scope(channel_id)
    async with await _get_lock(scope):
        try:
            result = await _run(lambda: manager.evaluate(channel_id, script, scope=scope), channel_id, scope)
            return ToolResult(name="browser_evaluate", success=True, output=result, error=None)
        except Exception as e:
            return ToolResult(name="browser_evaluate", success=False, output=None, error=str(e))
