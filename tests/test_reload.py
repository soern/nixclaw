"""Tests for AgentLoop.reload(), config reload planning, and the /reload chat command."""

import asyncio
import logging
import shutil

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent import config as config_module
from src.nixclaw_agent import core, prompts
from src.nixclaw_agent.config import config
from src.nixclaw_agent.types import Event

pytestmark = pytest.mark.usefixtures("reset_db")

PRIV = "+000000"
OTHER = "+000001"
_DEFAULT_SOUL = (prompts.DEFAULT_DIR / "soul.md").read_text().strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated agent.yaml + prompt dirs; live config sections restored after the test."""
    yaml_path = tmp_path / "agent.yaml"
    default = tmp_path / "default"
    custom = tmp_path / "custom"
    shutil.copytree(prompts.DEFAULT_DIR, default)  # real registry needs the full shipped set
    custom.mkdir()
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    monkeypatch.setattr(prompts, "DEFAULT_DIR", default)
    monkeypatch.setattr(prompts, "CUSTOM_DIR", custom)
    monkeypatch.setattr(prompts, "_current", prompts._current)  # restored after the test
    for name in ("auth", "chat", "agent"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config.logging, "level", config.logging.level)
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    prompts.set_current(prompts.load_prompts(default, custom))
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\n')
    monkeypatch.setattr(config, "auth", config_module.AuthConfig(privileged_numbers=[PRIV]))
    return yaml_path, custom


def _event(source: str, message: str, group_id=None, ts: int = 1) -> Event:
    return Event(source=source, destination="+000000", message=message, timestamp=ts, group_id=group_id)


def test_reload_applies_auth_and_prompts(env):
    yaml_path, custom = env
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\n  known_numbers: ["{OTHER}"]\n')
    (custom / "soul.md").write_text("new soul")
    agent = _make_agent()
    result = agent.reload("test")
    assert result.ok, result.errors
    assert "auth" in result.applied and "prompts" in result.applied
    assert config.auth.known_numbers == [OTHER]
    assert agent._auth_manager.get_user_tier(OTHER).name == "KNOWN"
    assert prompts.current().text("soul") == "new soul"
    assert result.format_reply().startswith("Reload OK")


def test_broken_prompt_keeps_old_config_and_prompts(env):
    yaml_path, custom = env
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\n  known_numbers: ["{OTHER}"]\n')
    (custom / "soul.md").write_text("bad {{bogus}} placeholder")
    agent = _make_agent()
    result = agent.reload("test")
    assert not result.ok and "bogus" in result.format_reply()
    assert config.auth.known_numbers == []
    assert agent._auth_manager.get_user_tier(OTHER).name == "UNKNOWN"
    assert prompts.current().text("soul") == _DEFAULT_SOUL


def test_empty_privileged_numbers_rejected(env):
    yaml_path, custom = env
    yaml_path.write_text("auth:\n  privileged_numbers: []\n")
    (custom / "soul.md").write_text("should not apply")
    result = _make_agent().reload("test")
    assert not result.ok
    assert any("privileged_numbers" in e for e in result.errors)
    assert config.auth.privileged_numbers == [PRIV]
    assert prompts.current().text("soul") == _DEFAULT_SOUL


@pytest.mark.parametrize("content", [None, "", "# only a comment\n"])
def test_missing_or_empty_agent_yaml_rejected(env, content):
    yaml_path, _ = env
    if content is None:
        yaml_path.unlink()
    else:
        yaml_path.write_text(content)
    result = _make_agent().reload("test")
    assert not result.ok and "missing or empty" in result.errors[0]
    assert config.auth.privileged_numbers == [PRIV]


def test_bad_types_rejected(env):
    yaml_path, _ = env
    yaml_path.write_text(f'auth:\n  privileged_numbers: "{PRIV}"\nchat:\n  sliding_window_size: 0\n')
    result = _make_agent().reload("test")
    assert not result.ok
    joined = " ".join(result.errors)
    assert "privileged_numbers" in joined and "sliding_window_size" in joined


def test_agent_name_reload_updates_mentions(env):
    yaml_path, _ = env
    group_event = _event("+000005", "hey Botty are you there", group_id="grp")
    assert not core.is_mentioned(group_event)
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\nagent:\n  name: Botty\n')
    assert _make_agent().reload("test").ok
    assert core.is_mentioned(group_event)
    assert core.strip_mention("@Botty hi") == "hi"


def test_non_whitelisted_change_reports_restart_required(env):
    yaml_path, _ = env
    yaml_path.write_text(f'backend: some-other-backend\nauth:\n  privileged_numbers: ["{PRIV}"]\n')
    before = config.backend
    result = _make_agent().reload("test")
    assert result.ok
    assert "backend" in result.restart_required
    assert config.backend == before
    assert "restart required" in result.format_reply()


def test_log_level_hot_applied(env):
    yaml_path, _ = env
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\nlogging:\n  level: WARNING\n')
    result = _make_agent().reload("test")
    assert result.ok and "logging.level" in result.applied
    assert logging.getLogger().level == logging.WARNING


def test_reload_command_privileged_dm(env):
    asyncio.run(_test_reload_command_privileged_dm(env))


async def _test_reload_command_privileged_dm(env):
    yaml_path, custom = env
    (custom / "soul.md").write_text("via chat")
    agent = _make_agent()
    pre = await agent._preprocess_incoming(_event(PRIV, "/reload"), PRIV, PRIV)
    assert pre.event is None and not pre.abort_loop
    sent = agent._signal_client.send_message.await_args_list
    assert sent and "Reload OK" in str(sent[-1])
    assert prompts.current().text("soul") == "via chat"


def test_reload_command_reports_errors(env):
    asyncio.run(_test_reload_command_reports_errors(env))


async def _test_reload_command_reports_errors(env):
    yaml_path, _ = env
    yaml_path.write_text("auth:\n  privileged_numbers: []\n")
    agent = _make_agent()
    await agent._preprocess_incoming(_event(PRIV, "/reload"), PRIV, PRIV)
    assert "Reload failed" in str(agent._signal_client.send_message.await_args_list[-1])


def test_reload_command_from_group_is_ordinary_text(env):
    asyncio.run(_test_reload_command_from_group_is_ordinary_text(env))


async def _test_reload_command_from_group_is_ordinary_text(env):
    agent = _make_agent()
    ev = _event(PRIV, "/reload", group_id="grp")
    pre = await agent._preprocess_incoming(ev, "grp", "grp")
    # Unmentioned group text: stored, no LLM, and crucially no reload reply.
    agent._signal_client.send_message.assert_not_awaited()
    assert pre.event is None


def test_reload_command_from_non_privileged_is_ordinary_text(env):
    asyncio.run(_test_reload_command_from_non_privileged_is_ordinary_text(env))


async def _test_reload_command_from_non_privileged_is_ordinary_text(env):
    agent = _make_agent()
    pre = await agent._preprocess_incoming(_event(OTHER, "/reload"), OTHER, OTHER)
    agent._signal_client.send_message.assert_not_awaited()
    assert pre.event is not None and pre.event.message == "/reload"


def test_reload_command_is_exact_match(env):
    asyncio.run(_test_reload_command_is_exact_match(env))


async def _test_reload_command_is_exact_match(env):
    _, custom = env
    (custom / "soul.md").write_text("must not be loaded")
    agent = _make_agent()
    before = prompts.current()
    pre = await agent._preprocess_incoming(_event(PRIV, "/reload now", ts=2), PRIV, PRIV)
    assert pre.event is not None
    pre = await agent._preprocess_incoming(_event(PRIV, "/Reload", ts=3), PRIV, PRIV)
    assert pre.event is not None
    agent._signal_client.send_message.assert_not_awaited()
    assert prompts.current() is before
    assert prompts.current().text("soul") == _DEFAULT_SOUL


def test_chat_section_hot_applied(env):
    yaml_path, _ = env
    old = config.chat.sliding_window_size
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\nchat:\n  sliding_window_size: {old + 7}\n')
    result = _make_agent().reload("test")
    assert result.ok and "chat" in result.applied
    assert config.chat.sliding_window_size == old + 7


def test_reload_via_drain_path_does_not_abort_turn(env):
    asyncio.run(_test_reload_via_drain_path(env))


async def _test_reload_via_drain_path(env):
    _, custom = env
    (custom / "soul.md").write_text("via drain")
    agent = _make_agent()
    agent._channel_inbound_queue[PRIV] = [_event(PRIV, "/reload")]
    messages: list[dict] = []
    abort = await agent._drain_inbound_checkpoint(PRIV, PRIV, messages)
    assert abort is False and messages == []
    assert "Reload OK" in str(agent._signal_client.send_message.await_args_list[-1])
    assert prompts.current().text("soul") == "via drain"


@pytest.mark.parametrize("fname", ["soul.md", "tool-descriptions.yaml"])
def test_invalid_utf8_custom_file_fails_reload_and_keeps_old(env, fname):
    _, custom = env
    (custom / fname).write_bytes(b"\xff\xfe bad \x80")
    result = _make_agent().reload("test")
    assert not result.ok
    assert any(fname in e for e in result.errors)
    assert "previous config and prompts are still active" in result.format_reply()
    assert prompts.current().text("soul") == _DEFAULT_SOUL


def test_unexpected_error_during_validation_becomes_failed_reload(env, monkeypatch):
    def boom(_config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(core, "plan_config_reload", boom)
    result = _make_agent().reload("test")
    assert not result.ok and "kaboom" in result.format_reply()
    assert prompts.current().text("soul") == _DEFAULT_SOUL
