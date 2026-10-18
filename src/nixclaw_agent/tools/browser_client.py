"""Playwright-direct browser backend.

Connects to a Playwright server (`npx playwright run-server`) over a
websocket and drives it with the native Playwright API. Replaces the
former playwright-mcp container + hand-rolled SSE/JSON-RPC MCP client.

Isolation model: one shared Browser connection, one BrowserContext +
Page per *scope*. Contexts are independent (cookies, storage), so scopes
can browse concurrently. A chat turn's scope is its channel_id (persistent,
cookies saved back to the channel's storage_state.json); a cronjob/subagent
run gets an ephemeral scope "<channel_id>#run:<turn_id>" (see
types.Context.browser_scope) that is seeded from the channel's cookies but
never writes them back, and is closed when the run ends. Every method takes
the owning channel_id plus an optional scope that defaults to channel_id.
"""

import asyncio
import json
import logging
import os
from typing import Any, Optional

from .. import prompts
from .browser_cookies import load_storage_state, save_storage_state

LOG = logging.getLogger(__name__)

DEFAULT_WS_URL = "ws://localhost:8931"
CONNECT_TIMEOUT_MS = 15000
NAVIGATE_TIMEOUT_MS = 30000
LOCATOR_TIMEOUT_MS = 5000
SETTLE_TIMEOUT_MS = 15000
SCREENSHOT_TIMEOUT_MS = 60000
SNAPSHOT_MAX_CHARS = 15000

INTERACT_ACTIONS = ("click", "type", "press", "back")


def _has_page_loaded(page: Any) -> bool:
    return page.url not in ("", "about:blank")


class BrowserManager:
    """Owns the websocket connection and per-scope contexts/pages."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        # All three keyed by scope; _channels maps scope -> owning channel_id.
        self._contexts: dict[str, Any] = {}
        self._pages: dict[str, Any] = {}
        self._channels: dict[str, str] = {}
        self._connect_lock = asyncio.Lock()

    @property
    def ws_url(self) -> str:
        env_url = os.environ.get("BROWSER_WS_URL")
        if env_url:
            return env_url
        try:
            from ..config import config
            return config.browser.ws_url
        except Exception:
            return DEFAULT_WS_URL

    async def _ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            # Stale contexts belong to a dead connection
            self._contexts.clear()
            self._pages.clear()
            self._channels.clear()
            if self._playwright is None:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
            LOG.info(f"Connecting to browser server at {self.ws_url}")
            self._browser = await self._playwright.chromium.connect(
                self.ws_url, timeout=CONNECT_TIMEOUT_MS
            )

    def is_connected(self) -> bool:
        """Safely report whether there's a live browser connection."""
        return self._browser is not None and self._browser.is_connected()

    async def reset(self) -> None:
        """Drop the connection so the next operation reconnects fresh."""
        async with self._connect_lock:
            self._contexts.clear()
            self._pages.clear()
            self._channels.clear()
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

    async def get_page(self, channel_id: str, scope: Optional[str] = None) -> Any:
        if not channel_id:
            raise ValueError("channel_id is required")
        scope = scope or channel_id
        await self._ensure_connected()
        page = self._pages.get(scope)
        if page is not None and not page.is_closed():
            return page
        old_context = self._contexts.pop(scope, None)
        self._pages.pop(scope, None)
        if old_context is not None:
            try:
                await old_context.close()
            except Exception:
                pass
        state = None
        try:
            state = load_storage_state(channel_id)
        except Exception as e:
            LOG.warning(f"Could not load storage state for {channel_id}: {e}")
        context = await self._browser.new_context(storage_state=state)
        try:
            page = await context.new_page()
        except Exception:
            try:
                await context.close()
            except Exception:
                pass
            raise
        self._contexts[scope] = context
        self._pages[scope] = page
        self._channels[scope] = channel_id
        return page

    async def save_state(self, channel_id: str, scope: Optional[str] = None) -> None:
        # Only the chat scope persists cookies; background run scopes are
        # seeded from the channel's state read-only.
        if (scope or channel_id) != channel_id:
            return
        context = self._contexts.get(channel_id)
        if context is None:
            return
        try:
            state = await context.storage_state()
            if state:
                save_storage_state(channel_id, state)
        except Exception as e:
            LOG.warning(f"Could not persist storage state for {channel_id}: {e}")

    async def close_scope(self, channel_id: str, scope: Optional[str] = None) -> None:
        scope = scope or channel_id
        context = self._contexts.pop(scope, None)
        self._pages.pop(scope, None)
        self._channels.pop(scope, None)
        if context is not None:
            # Best-effort: cookies set since the last navigation (e.g. via
            # evaluate) would otherwise be lost. Chat scope only.
            if scope == channel_id:
                try:
                    state = await context.storage_state()
                    if state:
                        save_storage_state(channel_id, state)
                except Exception:
                    pass
            try:
                await context.close()
            except Exception:
                pass

    async def close_channel(self, channel_id: str) -> None:
        """Close the channel's chat scope."""
        await self.close_scope(channel_id, channel_id)

    async def close(self) -> None:
        for scope in list(self._contexts):
            await self.close_scope(self._channels.get(scope, scope), scope)
        await self.reset()

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def navigate(self, channel_id: str, url: str, scope: Optional[str] = None) -> str:
        page = await self.get_page(channel_id, scope)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
        # Best-effort wait for network idle; don't fail if it times out.
        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        await self.save_state(channel_id, scope)
        status = response.status if response else None
        return await self.snapshot(channel_id, http_status=status, scope=scope)

    async def snapshot(
        self, channel_id: str, http_status: Optional[int] = None, scope: Optional[str] = None
    ) -> str:
        """Text rendering of the current page: title, URL, accessibility tree."""
        page = await self.get_page(channel_id, scope)
        text = ""
        try:
            text = await page.locator("body").aria_snapshot(timeout=SETTLE_TIMEOUT_MS)
        except Exception as e:
            LOG.debug(f"aria_snapshot failed, falling back to innerText: {e}")
        if not text:
            try:
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            except Exception:
                text = ""
        try:
            title = await page.title()
        except Exception:
            title = ""
        output = f"Page: {title}\nURL: {page.url}"
        if http_status is not None and http_status >= 400:
            output += f"\nHTTP status: {http_status}"
        output += f"\n\n{text or '(page has no readable content)'}"
        marker = "\n[truncated]"
        if len(output) > SNAPSHOT_MAX_CHARS:
            output = output[:SNAPSHOT_MAX_CHARS - len(marker)] + marker
        return output

    async def interact(
        self, channel_id: str, action: str, target: str, value: str, scope: Optional[str] = None
    ) -> str:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        def _selector_error(verb: str, exc: Exception) -> ValueError:
            # Invalid selector syntax (e.g. 'button "square 2"') raises a
            # generic parse error; surface the accepted forms instead.
            return ValueError(f"{verb} '{target}': invalid selector syntax. {prompts.current().text('selector-help')}")

        page = await self.get_page(channel_id, scope)
        if not _has_page_loaded(page):
            raise ValueError(prompts.current().text("no-page-error"))
        if action == "click":
            if not target:
                raise ValueError("action 'click' requires 'target'")
            try:
                await self._locate(page, target).click(timeout=LOCATOR_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                raise ValueError(f"click '{target}': {prompts.current().text('selector-help')}")
            except PlaywrightError as e:
                if "selector" in str(e).lower() or "unexpected token" in str(e).lower():
                    raise _selector_error("click", e)
                raise
        elif action == "type":
            if not target:
                raise ValueError("action 'type' requires 'target'")
            try:
                await self._locate(page, target).fill(value, timeout=LOCATOR_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                raise ValueError(f"type '{target}': {prompts.current().text('selector-help')}")
            except PlaywrightError as e:
                if "selector" in str(e).lower() or "unexpected token" in str(e).lower():
                    raise _selector_error("type", e)
                raise
        elif action == "press":
            if not value:
                raise ValueError("action 'press' requires 'value' (a key name like 'Enter')")
            await page.keyboard.press(value)
        elif action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
        else:
            raise ValueError(
                f"Unknown action '{action}'. Valid actions: {', '.join(INTERACT_ACTIONS)}"
            )
        # Interactions often trigger navigation; give the page a moment to settle.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass
        await self.save_state(channel_id, scope)
        return await self.snapshot(channel_id, scope=scope)

    def _locate(self, page: Any, target: str) -> Any:
        locator = page.locator(target)
        return locator.first

    async def screenshot_bytes(self, channel_id: str, url: str = "", scope: Optional[str] = None) -> bytes:
        page = await self.get_page(channel_id, scope)
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATE_TIMEOUT_MS)
            # Best-effort wait for network idle; don't fail if it times out.
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            await self.save_state(channel_id, scope)
        data: bytes = await page.screenshot(type="png", timeout=SCREENSHOT_TIMEOUT_MS)
        return data

    async def page_info(self, channel_id: str, scope: Optional[str] = None) -> tuple[str, str]:
        """(url, title) of the scope's current page, or empty strings.

        Never opens a page or raises: only reads an already-open page.
        """
        page = self._pages.get(scope or channel_id)
        if page is None:
            return "", ""
        try:
            url = page.url or ""
        except Exception:
            url = ""
        try:
            title = await asyncio.wait_for(page.title(), 2)
        except Exception:
            title = ""
        return url, title

    async def evaluate(self, channel_id: str, script: str, scope: Optional[str] = None) -> str:
        """Run JavaScript inside the page (document/window scope only)."""
        page = await self.get_page(channel_id, scope)
        if not _has_page_loaded(page):
            raise ValueError(prompts.current().text("no-page-error"))
        s = script.strip()
        if "return" not in s and ";" not in s and "\n" not in s:
            wrapped = f"async () => {{ return ({s}); }}"
        else:
            wrapped = f"async () => {{ {s} }}"
        result = await page.evaluate(wrapped)
        if result is None:
            return "(script returned no value)"
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, default=str)
        except (TypeError, ValueError):
            return str(result)


# Module-level singleton shared by all browser tools.
manager = BrowserManager()
