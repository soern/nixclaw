"""Browser scope isolation (fix/browser-scope-isolation).

Chat turns browse in the channel's persistent scope (channel_id); each
cronjob/subagent run browses in its own ephemeral scope
"<channel_id>#run:<turn_id>" (types.Context.browser_scope). Background scopes
are seeded from the channel's storage_state.json but never write it back, and
are closed when the run ends. This replaces the former global browser lease,
whose timer-based eviction let a weather cronjob navigate the page a live chat
turn on the same channel was screenshotting.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent import core
from src.nixclaw_agent.tools import browse, registry
from src.nixclaw_agent.tools.browser_client import BrowserManager
from src.nixclaw_agent.types import Context, UserTier

CLIENT = "src.nixclaw_agent.tools.browser_client"
STORED = {"cookies": [{"name": "session", "value": "abc"}], "origins": []}


def _make_page():
    page = MagicMock()
    page.url = "about:blank"
    page.is_closed = MagicMock(return_value=False)

    async def goto(url, **kwargs):
        page.url = url
        return SimpleNamespace(status=200)

    page.goto = AsyncMock(side_effect=goto)
    page.wait_for_load_state = AsyncMock()
    page.title = AsyncMock(side_effect=lambda: f"Title of {page.url}")
    page.screenshot = AsyncMock(return_value=b"\x89PNG-fake")
    locator = MagicMock()
    locator.aria_snapshot = AsyncMock(return_value="- text")
    page.locator = MagicMock(return_value=locator)
    return page


def _make_manager():
    manager = BrowserManager()
    browser = MagicMock()
    browser.is_connected = MagicMock(return_value=True)

    def new_context(**kwargs):
        context = MagicMock()
        context.new_page = AsyncMock(side_effect=lambda: _make_page())
        context.close = AsyncMock()
        context.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
        return context

    browser.new_context = AsyncMock(side_effect=new_context)
    manager._browser = browser
    manager._playwright = MagicMock()
    return manager


def _ctx(channel_id: str, lane: str = "interactive") -> Context:
    return Context(
        channel_id=channel_id,
        messages_db=Path("/nonexistent/messages.db"),
        attachments_dir=Path("/nonexistent/attachments"),
        lane=lane,
    )


async def _in_turn(context: Context, coro_fn):
    """Run coro_fn() in its own task with `context` as the current turn."""
    async def runner():
        registry._current_context.set(context)
        return await coro_fn()
    return await asyncio.create_task(runner())


class TestContextLaneScope:
    def test_interactive_scope_is_channel(self):
        ctx = _ctx("+491")
        assert ctx.lane == "interactive"
        assert ctx.browser_scope == "+491"

    def test_background_scope_is_ephemeral_per_run(self):
        a = _ctx("+491", lane="background")
        b = _ctx("+491", lane="background")
        assert a.browser_scope == f"+491#run:{a.turn_id}"
        assert a.browser_scope != b.browser_scope
        assert a.browser_scope != "+491"

    def test_get_current_browser_scope_without_context_is_none(self):
        async def check():
            return registry.get_current_browser_scope()
        assert asyncio.run(check()) is None

    def test_context_manager_passes_lane(self, tmp_path):
        from src.nixclaw_agent.context import ContextManager
        cm = ContextManager(SimpleNamespace(data_dir=tmp_path), MagicMock())
        assert cm.get_context("+491").lane == "interactive"
        assert cm.get_context("+491", lane="background").lane == "background"


@pytest.mark.asyncio
class TestManagerScopes:
    async def test_chat_and_background_on_same_channel_see_own_page(self):
        manager = _make_manager()
        chat = _ctx("+491")
        cron = _ctx("+491", lane="background")
        with patch(f"{CLIENT}.load_storage_state", return_value=STORED), \
             patch(f"{CLIENT}.save_storage_state"):
            await asyncio.gather(
                manager.navigate("+491", "https://debug.example", scope=chat.browser_scope),
                manager.navigate("+491", "https://wttr.in", scope=cron.browser_scope),
            )
            chat_info = await manager.page_info("+491", scope=chat.browser_scope)
            cron_info = await manager.page_info("+491", scope=cron.browser_scope)
        assert chat_info == ("https://debug.example", "Title of https://debug.example")
        assert cron_info == ("https://wttr.in", "Title of https://wttr.in")
        assert manager._contexts[chat.browser_scope] is not manager._contexts[cron.browser_scope]

    async def test_two_channels_isolated(self):
        manager = _make_manager()
        with patch(f"{CLIENT}.load_storage_state", return_value=None), \
             patch(f"{CLIENT}.save_storage_state"):
            await manager.navigate("+491", "https://a.example")
            await manager.navigate("+492", "https://b.example")
        assert (await manager.page_info("+491"))[0] == "https://a.example"
        assert (await manager.page_info("+492"))[0] == "https://b.example"

    async def test_background_scope_seeded_from_channel_but_never_saves(self):
        manager = _make_manager()
        scope = _ctx("+491", lane="background").browser_scope
        with patch(f"{CLIENT}.load_storage_state", return_value=STORED) as load, \
             patch(f"{CLIENT}.save_storage_state") as save:
            await manager.navigate("+491", "https://a.example", scope=scope)
            await manager.screenshot_bytes("+491", "https://b.example", scope=scope)
            await manager.save_state("+491", scope=scope)
            context = manager._contexts[scope]
            await manager.close_scope("+491", scope)
        load.assert_called_once_with("+491")
        assert manager._browser.new_context.await_args.kwargs["storage_state"] == STORED
        save.assert_not_called()
        assert context.close.await_count == 1
        assert scope not in manager._contexts and scope not in manager._pages

    async def test_chat_scope_still_saves(self):
        manager = _make_manager()
        with patch(f"{CLIENT}.load_storage_state", return_value=None), \
             patch(f"{CLIENT}.save_storage_state") as save:
            await manager.navigate("+491", "https://a.example")
            await manager.close_channel("+491")
        assert save.call_count == 2
        assert all(call.args[0] == "+491" for call in save.call_args_list)

    async def test_close_saves_only_chat_scopes(self):
        manager = _make_manager()
        bg = _ctx("+491", lane="background").browser_scope
        with patch(f"{CLIENT}.load_storage_state", return_value=None), \
             patch(f"{CLIENT}.save_storage_state") as save:
            await manager.get_page("+491")
            await manager.get_page("+491", scope=bg)
            await manager.close()
        assert save.call_count == 1
        assert save.call_args.args[0] == "+491"


@pytest.mark.asyncio
class TestToolLayerScopes:
    async def test_tools_use_current_context_scope(self, tmp_path):
        manager = _make_manager()
        chat = _ctx("+491")
        cron = _ctx("+491", lane="background")
        mock_config = MagicMock()
        mock_config.paths.data_dir = tmp_path
        with patch.object(browse, "manager", manager), \
             patch(f"{CLIENT}.load_storage_state", return_value=None), \
             patch(f"{CLIENT}.save_storage_state"), \
             patch("src.nixclaw_agent.tools.paths.config", mock_config):
            await _in_turn(chat, lambda: browse.browser_navigate("https://debug.example", channel_id="+491"))
            await _in_turn(cron, lambda: browse.browser_navigate("https://wttr.in", channel_id="+491"))
            shot = await _in_turn(chat, lambda: browse.browser_screenshot(filename="s.png", channel_id="+491"))
        assert shot.success
        lines = shot.output.splitlines()
        assert lines[0] == "s.png"
        assert "URL: https://debug.example" in lines
        assert "Title: Title of https://debug.example" in lines
        assert set(manager._pages) == {"+491", cron.browser_scope}

    async def test_close_browser_scope_drops_lock(self):
        manager = _make_manager()
        scope = _ctx("+491", lane="background").browser_scope
        await browse._get_lock(scope)
        with patch.object(browse, "manager", manager), \
             patch(f"{CLIENT}.load_storage_state", return_value=None):
            await manager.get_page("+491", scope=scope)
            await browse.close_browser_scope("+491", scope)
        assert scope not in browse._lock_map
        assert scope not in manager._pages

    async def test_run_retry_closes_only_background_scope(self):
        """A retriable error in a background run closes that run's scope,
        never the channel's chat scope."""
        scope = _ctx("+491", lane="background").browser_scope
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("Target page, context or browser has been closed")
            return "ok"

        with patch.object(browse.manager, "close_scope", AsyncMock()) as close_scope, \
             patch.object(browse.manager, "close_channel", AsyncMock()) as close_channel, \
             patch.object(browse.manager, "is_connected", MagicMock(return_value=True)), \
             patch.object(browse.manager, "reset", AsyncMock()) as reset:
            assert await browse._run(flaky, "+491", scope) == "ok"
        close_scope.assert_awaited_once_with("+491", scope)
        close_channel.assert_not_awaited()
        reset.assert_not_awaited()

    async def test_page_info_title_timeout_is_best_effort(self):
        manager = _make_manager()
        with patch(f"{CLIENT}.load_storage_state", return_value=None):
            page = await manager.get_page("+491")
        page.url = "https://slow.example"

        async def hang():
            await asyncio.sleep(10)

        page.title = AsyncMock(side_effect=hang)
        # Real 2s cap in page_info; must return, not raise or hang.
        info = await asyncio.wait_for(manager.page_info("+491"), 5)
        assert info == ("https://slow.example", "")

    async def test_subagent_task_does_not_inherit_chat_scope(self):
        chat = _ctx("+491")
        background = _ctx("+491", lane="background")
        seen = {}

        async def chat_turn():
            registry._current_context.set(chat)

            async def subagent():
                seen["before"] = registry.get_current_browser_scope()
                registry._current_context.set(background)
                seen["after"] = registry.get_current_browser_scope()

            await asyncio.create_task(subagent())
            seen["chat"] = registry.get_current_browser_scope()

        await asyncio.create_task(chat_turn())
        assert seen["before"] == "+491"  # contextvars are copied into the task...
        assert seen["after"] == background.browser_scope  # ...so set_context must override
        assert seen["chat"] == "+491"


def _real_context_manager(agent):
    agent._context_manager.get_context = MagicMock(
        side_effect=lambda cid, lane="interactive": _ctx(cid, lane=lane)
    )


async def _run_and_cancel(agent, coro, channel_id, meta):
    task = asyncio.create_task(coro)
    agent._register_channel_task(channel_id, task, lane="background", meta=meta)
    await asyncio.sleep(0)
    agent.cancel_background_activity(channel_id, meta["source_id"])
    await task


@pytest.mark.asyncio
class TestCoreBackgroundRuns:
    async def _agent(self, monkeypatch):
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        _real_context_manager(agent)
        hang = asyncio.Event()

        async def hanging_chat(messages, tools, channel_id_arg):
            await hang.wait()

        agent._llm_client.chat = AsyncMock(side_effect=hanging_chat)
        close = AsyncMock()
        monkeypatch.setattr(core, "close_browser_scope", close)
        return agent, close

    async def test_cronjob_runs_in_background_scope_and_closes_it(self, monkeypatch):
        agent, close = await self._agent(monkeypatch)
        agent._cron_scheduler.mark_complete = MagicMock(
            return_value=SimpleNamespace(terminal_action="none", job_id="job-1")
        )
        job = SimpleNamespace(
            id="job-1", channel_id="+000000", creator_number="+000000",
            prompt="weather", scheduled_at="2026-01-01T00:00:00+00:00",
            is_recurring=True, interval_minutes=60, cancelled=False,
        )
        await _run_and_cancel(
            agent, agent._process_cronjob(job), "+000000",
            {"source_type": "cronjob", "source_id": "job-1"},
        )
        agent._context_manager.get_context.assert_called_once_with("+000000", lane="background")
        context = agent._tool_registry.set_context.call_args.args[0]
        assert context.lane == "background"
        close.assert_awaited_once_with("+000000", context.browser_scope)
        assert context.browser_scope != "+000000"

    async def test_subagent_runs_in_background_scope_and_closes_it(self, monkeypatch):
        agent, close = await self._agent(monkeypatch)
        await _run_and_cancel(
            agent, agent._process_subagent("do it", "+000000", UserTier.PRIVILEGED), "+000000",
            {"source_type": "subagent", "source_id": "sub-1"},
        )
        agent._context_manager.get_context.assert_called_once_with("+000000", lane="background")
        context = agent._tool_registry.set_context.call_args.args[0]
        assert context.lane == "background"
        close.assert_awaited_once_with("+000000", context.browser_scope)

    async def test_scope_close_failure_does_not_raise(self, monkeypatch):
        agent, close = await self._agent(monkeypatch)
        close.side_effect = Exception("browser gone")
        await _run_and_cancel(
            agent, agent._process_subagent("do it", "+000000", UserTier.PRIVILEGED), "+000000",
            {"source_type": "subagent", "source_id": "sub-2"},
        )
        assert close.await_count == 1
