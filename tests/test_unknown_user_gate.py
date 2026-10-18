"""Unknown-sender gate (core.py _unknown_sender_allowed, auth.unknown_users)
and the group-membership lookup it uses (web/channels.py group_members)."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent import config as config_module
from src.nixclaw_agent.config import AuthConfig, UnknownUsersConfig, config, load_config
from src.nixclaw_agent.web import channels as channels_mod
from src.nixclaw_agent.web.channels import GroupInfo, MembershipCache
from src.nixclaw_agent.types import Event

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("reset_db")]

BOT = "+000999"
PRIV = "+000000"
KNOWN = "+000001"
UNKNOWN = "+000002"
STRANGER = "+000003"
GROUP_ID = "grp=="
GROUP = f"signal-group:{GROUP_ID}"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config.signal, "phone_number", BOT)
    monkeypatch.setattr(config, "auth", AuthConfig(privileged_numbers=[PRIV], known_numbers=[KNOWN]))


def _cache(groups: dict[str, set[str]]) -> MembershipCache:
    cache = MembershipCache(BOT, "")
    cache.set_groups({cid: GroupInfo(channel_id=cid, name=cid, members=m) for cid, m in groups.items()})
    cache._fetched_at = time.monotonic()
    return cache


def _agent(membership=None):
    agent = _make_agent()
    agent._auth_manager.update(config.auth)
    agent._membership = membership
    agent._dispatch_or_queue = MagicMock()
    agent._chat_bus = MagicMock()
    agent._chat_bus.publish_inbound = MagicMock(return_value=1)
    return agent


def _direct(source: str) -> Event:
    return Event(source=source, destination=BOT, message="hi", timestamp=1, group_id=None)


def _group(source: str) -> Event:
    return Event(source=source, destination=GROUP, message="hi", timestamp=1, group_id=GROUP_ID)


def _passed(agent) -> bool:
    called = agent._dispatch_or_queue.call_count > 0
    # Publishing (web history) and dispatch happen together or not at all.
    assert called == (agent._chat_bus.publish_inbound.call_count > 0)
    return called


@pytest.fixture
def reload_yaml(tmp_path, monkeypatch):
    """Isolated agent.yaml; live config sections touched by reload are restored."""
    yaml_path = tmp_path / "agent.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    for name in ("auth", "chat", "agent", "consent", "webui"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config.logging, "level", config.logging.level)
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    return yaml_path


# --- config ------------------------------------------------------------------

async def test_config_defaults():
    cfg = load_config({})
    assert cfg.auth.unknown_users == UnknownUsersConfig(allow_direct=False, allow_in_groups=True)
    cfg = load_config({"auth": {"privileged_numbers": [PRIV]}})
    assert cfg.auth.unknown_users.allow_direct is False
    assert cfg.auth.unknown_users.allow_in_groups is True


async def test_config_parses_values():
    cfg = load_config({"auth": {"unknown_users": {"allow_direct": True, "allow_in_groups": False}}})
    assert cfg.auth.unknown_users.allow_direct is True
    assert cfg.auth.unknown_users.allow_in_groups is False


@pytest.mark.parametrize("data", [
    {"allow_direct": "false"},
    {"allow_in_groups": "no"},
    {"allow_direct": 1},
    "yes",
])
async def test_config_rejects_non_bool_at_startup(data):
    with pytest.raises(ValueError, match="unknown_users"):
        load_config({"auth": {"unknown_users": data}})


async def test_reload_rejects_non_bool(reload_yaml):
    yaml_path = reload_yaml
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\n'
                         '  unknown_users:\n    allow_direct: "false"\n')
    agent = _agent()
    result = agent.reload("test")
    assert not result.ok
    assert any("unknown_users.allow_direct" in e for e in result.errors)
    assert config.auth.unknown_users.allow_direct is False


# --- membership lookup ----------------------------------------------------------

async def test_group_members_cached():
    cache = _cache({GROUP: {KNOWN, UNKNOWN}})
    cache._fetch = AsyncMock()
    assert await cache.group_members(GROUP) == {KNOWN, UNKNOWN}
    cache._fetch.assert_not_called()


async def test_group_members_stale_cache_is_refreshed():
    cache = _cache({GROUP: {UNKNOWN}})
    cache._fetched_at = time.monotonic() - channels_mod.CACHE_TTL_SECONDS - 1
    cache._fetch = AsyncMock(return_value={GROUP: GroupInfo(GROUP, "g", {KNOWN})})
    assert await cache.group_members(GROUP) == {KNOWN}
    cache._fetch.assert_awaited_once()


async def test_group_members_uncached_one_rate_limited_refresh():
    cache = _cache({})
    calls = 0

    async def _fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {}

    cache._fetch = _fetch
    # A burst of lookups for an unknown group causes one fetch.
    results = await asyncio.gather(*(cache.group_members(GROUP) for _ in range(5)))
    assert results == [None] * 5 and calls == 1
    # Still within the rate limit: no further fetch.
    assert await cache.group_members(GROUP) is None and calls == 1
    # After the rate limit, a miss refreshes again and finds the new group.
    cache._last_miss_refresh -= channels_mod.RETRY_AFTER_FAILURE_SECONDS

    async def _fetch_found():
        nonlocal calls
        calls += 1
        return {GROUP: GroupInfo(GROUP, "g", {KNOWN})}

    cache._fetch = _fetch_found
    assert await cache.group_members(GROUP) == {KNOWN} and calls == 2


async def test_group_members_burst_waits_for_inflight_refresh():
    cache = _cache({})
    started = asyncio.Event()

    async def _fetch():
        started.set()
        await asyncio.sleep(0.01)
        return {GROUP: GroupInfo(GROUP, "g", {KNOWN})}

    cache._fetch = _fetch
    first = asyncio.create_task(cache.group_members(GROUP))
    await started.wait()
    assert await cache.group_members(GROUP) == {KNOWN}
    assert await first == {KNOWN}


async def test_group_members_fetch_failure_fails_closed():
    cache = _cache({})
    cache._fetch = AsyncMock(return_value=None)
    assert await cache.group_members(GROUP) is None
    cache._fetch.assert_awaited_once()


# --- gate ----------------------------------------------------------------------

async def test_direct_unknown_dropped_by_default(caplog):
    agent = _agent()
    event = _direct(UNKNOWN)
    event.message = "secret text"
    with caplog.at_level("INFO"):
        await agent._on_signal_message(event)
    assert not _passed(agent)
    assert "Dropped message from unknown sender" in caplog.text
    assert "secret text" not in caplog.text


async def test_direct_unknown_dropped_nothing_stored_or_sent():
    """Real dispatch path: no task, no DB row, no reply/typing/receipt."""
    agent = _make_agent()
    agent._auth_manager.update(config.auth)
    agent._signal_client.send_typing_indicator = AsyncMock()
    await agent._on_signal_message(_direct(UNKNOWN))
    assert agent._channel_active_task == {} and not agent._tasks
    assert agent._db_ops.get_messages(UNKNOWN) == []
    agent._signal_client.send_message.assert_not_called()
    agent._signal_client.send_typing_indicator.assert_not_called()
    agent._signal_client.send_read_receipt.assert_not_called()
    agent._signal_client.save_attachment.assert_not_called()


async def test_direct_unknown_allowed_with_allow_direct(monkeypatch):
    monkeypatch.setattr(config.auth, "unknown_users", UnknownUsersConfig(allow_direct=True))
    agent = _agent()
    await agent._on_signal_message(_direct(UNKNOWN))
    assert _passed(agent)


@pytest.mark.parametrize("sender", [PRIV, KNOWN])
async def test_trusted_senders_unaffected(sender):
    agent = _agent(_cache({GROUP: {sender}}))
    await agent._on_signal_message(_direct(sender))
    await agent._on_signal_message(_group(sender))
    assert agent._dispatch_or_queue.call_count == 2


async def test_trusted_senders_in_unknown_group_unaffected():
    agent = _agent(None)
    await agent._on_signal_message(_group(KNOWN))
    assert _passed(agent)


async def test_own_number_sync_message_unaffected():
    agent = _agent(None)
    await agent._on_signal_message(Event(source=BOT, destination=KNOWN, message="echo", timestamp=1,
                                         group_id=None))
    await agent._on_signal_message(_group(BOT))
    assert agent._dispatch_or_queue.call_count == 2
    agent._chat_bus.publish_inbound.assert_not_called()  # own-number echoes stay unpublished


async def test_group_with_known_member_allowed():
    agent = _agent(_cache({GROUP: {KNOWN, UNKNOWN, BOT}}))
    await agent._on_signal_message(_group(UNKNOWN))
    assert _passed(agent)


async def test_group_with_privileged_member_allowed():
    agent = _agent(_cache({GROUP: {PRIV, UNKNOWN}}))
    await agent._on_signal_message(_group(UNKNOWN))
    assert _passed(agent)


async def test_group_without_trusted_member_dropped():
    agent = _agent(_cache({GROUP: {UNKNOWN, STRANGER, BOT}}))
    await agent._on_signal_message(_group(UNKNOWN))
    assert not _passed(agent)


async def test_bot_number_in_member_list_does_not_count(monkeypatch):
    # Even if the bot's own number were configured as a trusted number.
    monkeypatch.setattr(config, "auth", AuthConfig(privileged_numbers=[PRIV, BOT]))
    agent = _agent(_cache({GROUP: {BOT, UNKNOWN}}))
    await agent._on_signal_message(_group(UNKNOWN))
    assert not _passed(agent)


async def test_allow_in_groups_false_drops(monkeypatch):
    monkeypatch.setattr(config.auth, "unknown_users", UnknownUsersConfig(allow_in_groups=False))
    agent = _agent(_cache({GROUP: {KNOWN, UNKNOWN}}))
    await agent._on_signal_message(_group(UNKNOWN))
    assert not _passed(agent)


async def test_group_unknown_without_cache_fails_closed():
    agent = _agent(None)
    await agent._on_signal_message(_group(UNKNOWN))
    assert not _passed(agent)


async def test_group_not_cached_and_fetch_fails_drops():
    cache = _cache({})
    cache._fetch = AsyncMock(return_value=None)
    agent = _agent(cache)
    await agent._on_signal_message(_group(UNKNOWN))
    assert not _passed(agent)


async def test_membership_lookup_error_fails_closed():
    cache = _cache({})
    cache.group_members = AsyncMock(side_effect=RuntimeError("boom"))
    agent = _agent(cache)
    await agent._on_signal_message(_group(UNKNOWN))
    assert not _passed(agent)


async def test_reload_applies_setting_immediately(reload_yaml):
    yaml_path = reload_yaml
    agent = _agent()
    await agent._on_signal_message(_direct(UNKNOWN))
    assert agent._dispatch_or_queue.call_count == 0
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\n'
                         '  unknown_users:\n    allow_direct: true\n')
    result = agent.reload("test")
    assert result.ok, result.errors
    await agent._on_signal_message(_direct(UNKNOWN))
    assert agent._dispatch_or_queue.call_count == 1
    # And reloading a newly known number lets that number through as well.
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\n  known_numbers: ["{STRANGER}"]\n')
    assert agent.reload("test").ok
    await agent._on_signal_message(_direct(UNKNOWN))
    await agent._on_signal_message(_direct(STRANGER))
    assert [c.args[0] for c in agent._dispatch_or_queue.call_args_list] == [UNKNOWN, STRANGER]
