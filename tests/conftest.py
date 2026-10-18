"""Shared pytest fixtures for the whole tests/ suite."""

import sys
import time
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.nixclaw_agent.auth import AuthManager, AuthConfig
from src.nixclaw_agent.config import config as config_module
from src.nixclaw_agent.context import ContextManager
from src.nixclaw_agent.core import AgentLoop
from src.nixclaw_agent.cron import CronScheduler
from src.nixclaw_agent.db import operations as db_ops
from src.nixclaw_agent.tools.registry import ToolRegistry


# Register custom markers here so the test suite doesn't warn about
# `pytest.mark.integration` etc. in a pytest.ini-less environment.
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: end-to-end tests that require live services "
        "(self-skip if the services are not reachable)",
    )


@pytest.fixture(autouse=True)
def _default_prompts(tmp_path_factory):
    """Use only config/prompts/default/ (empty custom dir) so a local
    config/prompts/custom/ can never change test results.

    Installs the set into every already-imported prompts module spelling
    (src.nixclaw_agent.prompts and nixclaw_agent.prompts: some test files
    import the package as `src.nixclaw_agent...`, others insert `src/` onto
    sys.path and import bare `nixclaw_agent...`, giving two distinct
    sys.modules entries).
    """
    empty_custom = tmp_path_factory.mktemp("no_custom_prompts")
    mods = [
        m
        for name, m in list(sys.modules.items())
        if name.endswith("nixclaw_agent.prompts") and hasattr(m, "set_current")
    ]
    for m in mods:
        m.set_current(m.load_prompts(m.DEFAULT_DIR, empty_custom))
    yield


@pytest.fixture(autouse=True)
def _single_message_mode_off(monkeypatch):
    """Pin chat.single_message.mode to "off" (one Signal message per text,
    the pre-feature behaviour) so the local config/agent.yaml never changes
    test results. Tests of append/replace mode opt in explicitly.

    The per-turn resolver in core.py is pinned to the global mode too, so a
    developer's data/<channel>/message_mode.json can't leak in (tests of the
    resolver call tools.message_mode.resolve_message_mode directly).

    Patched on every imported module spelling (see _default_prompts).
    """
    for name, m in list(sys.modules.items()):
        if name.endswith("nixclaw_agent.config") and hasattr(m, "SingleMessageConfig"):
            monkeypatch.setattr(m.config.chat, "single_message", m.SingleMessageConfig(mode="off"))
    for name, m in list(sys.modules.items()):
        if name.endswith("nixclaw_agent.core") and hasattr(m, "resolve_message_mode"):
            cfg = m.config
            monkeypatch.setattr(
                m, "resolve_message_mode",
                lambda channel_id, cfg=cfg: cfg.chat.single_message.effective_mode,
            )
    yield


class MockTimeContext:
    """Simple mock time context for testing without external dependencies.
    
    Allows tests to provide a custom time function instead of real time.time().
    """

    def __init__(self):
        self._current_time = time.time()

    def tick(self, seconds: float) -> None:
        """Advance mock time by seconds."""
        self._current_time += seconds

    def time(self) -> float:
        """Get current mock time."""
        return self._current_time


@pytest.fixture
def mock_time():
    """Fixture providing deterministic time control without external dependencies.

    Usage:
        async def test_something(mock_time):
            registry = ToolRegistry(auth, _get_current_time=mock_time.time)
            mock_time.tick(65.0)  # Advance 65 seconds
    """
    return MockTimeContext()


@pytest.fixture
def reset_db(monkeypatch, tmp_path):
    """Force a fresh sqlite DB (and data dir) for each test.

    Opt in per-file with `pytestmark = pytest.mark.usefixtures("reset_db")` at
    module level - do NOT make this autouse here, since that would apply it to
    every file in the suite regardless of whether it touches the DB. Shared
    from test_core_tool_result_wiring.py/test_channel_turn_serialization.py/
    test_whitespace_only_response_not_sent.py/test_stt_core_integration.py,
    which all had this verbatim as a local fixture.
    """
    db_file = tmp_path / "messages.db"
    monkeypatch.setattr(db_ops, "_connection", None)
    monkeypatch.setattr(config_module.paths, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.database, "db_path", db_file)
    db_ops.init_database()
    yield
    monkeypatch.setattr(db_ops, "_connection", None)


def build_mock_agent_loop() -> AgentLoop:
    """Construct an AgentLoop with signal/llm clients mocked and everything
    else either real (AuthManager) or MagicMock(spec=...) (ToolRegistry,
    ContextManager, CronScheduler) - the harness shared verbatim across
    test_core_tool_result_wiring.py, test_channel_turn_serialization.py, and
    test_whitespace_only_response_not_sent.py. Import as e.g.
    `from conftest import build_mock_agent_loop as _make_agent`.

    test_stt_core_integration.py's own _make_agent() is a genuinely different,
    parameterized variant (stt_config/transcript/fail + STT-client stubbing) and
    is NOT this function - it stays local to that file.
    """
    signal_client = MagicMock()
    signal_client.save_attachment = AsyncMock()
    signal_client.send_message = AsyncMock()
    signal_client.send_typing_indicator = AsyncMock()
    signal_client.send_read_receipt = AsyncMock()

    llm_client = MagicMock()
    llm_client.build_system_prompt = MagicMock(return_value="sys")
    llm_client.build_messages = MagicMock(return_value=[{"role": "user", "content": "hi"}])

    auth = AuthManager(AuthConfig(privileged_numbers=["+000000"]))

    tool_registry = MagicMock(spec=ToolRegistry)
    tool_registry.set_context = MagicMock()
    tool_registry.get_tools_for_tier = MagicMock(return_value=[])
    tool_registry.invalidate_file_list_cache = MagicMock()

    context_manager = MagicMock(spec=ContextManager)
    context_manager.get_context = MagicMock()
    context_manager.get_messages_for_llm = MagicMock(return_value=[])
    context_manager.get_message_count = MagicMock(return_value=0)
    context_manager.get_summary = MagicMock(return_value=(None, None))
    context_manager.get_soul_content = MagicMock(return_value=None)

    cron = MagicMock(spec=CronScheduler)

    return AgentLoop(
        signal_client=signal_client,
        llm_client=llm_client,
        auth_manager=auth,
        context_manager=context_manager,
        tool_registry=tool_registry,
        cron_scheduler=cron,
    )
