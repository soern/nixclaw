"""Single-message mode wiring: Signal timestamps/edits, message_mode tools,
config validation, user_visible DB flag + history marker, replace-mode
prompt, attachment hook and the core tool loop (see turn_output.py for the
state machine itself, tested in test_turn_output.py)."""

import json
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
import yaml

from src.nixclaw_agent import config as config_module
from src.nixclaw_agent import core as core_module
from src.nixclaw_agent import prompts
from src.nixclaw_agent import signal as signal_module
from src.nixclaw_agent.auth import TOOL_PERMISSIONS
from src.nixclaw_agent.config import ChatConfig, SingleMessageConfig, config
from src.nixclaw_agent.db import operations as db_ops
from src.nixclaw_agent.llm import REPLACED_TEXT_PREFIX, LLMClient, _build_history_messages
from src.nixclaw_agent.config import OllamaConfig
from src.nixclaw_agent.signal import SignalClient, parse_send_timestamp
from src.nixclaw_agent.tools import message_mode, registry as registry_module, signal_ops
from src.nixclaw_agent.tools.registry import TOOLS_NEEDING_CHANNEL_ID
from src.nixclaw_agent.turn_output import TurnOutput
from src.nixclaw_agent.types import Context, Event, Message, ToolCall, ToolResult, UserTier


def _set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(config.chat, "single_message", SingleMessageConfig(mode=mode))


# --- signal.py --------------------------------------------------------------

@pytest.mark.parametrize("body, expected", [
    ([{"timestamp": "1791131093291"}], 1791131093291),   # real signal-cli-rest-api
    ({"status": "ok", "timestamp": 1791131093291}, 1791131093291),  # mock
    ([], None),
    ({"status": "ok"}, None),
    ({"timestamp": "abc"}, None),
    ({"timestamp": True}, None),
    ("garbage", None),
    (None, None),
])
def test_parse_send_timestamp(body, expected):
    assert parse_send_timestamp(body) == expected


class _FakeResponse:
    status_code = 201

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _FakeHttpx:
    """Stands in for httpx.AsyncClient; records posted bodies."""
    posted: list[dict] = []
    reply: object = [{"timestamp": "42"}]

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeHttpx.posted.append(json)
        return _FakeResponse(_FakeHttpx.reply)


@pytest.fixture
def fake_httpx(monkeypatch):
    _FakeHttpx.posted = []
    _FakeHttpx.reply = [{"timestamp": "42"}]
    monkeypatch.setattr(signal_module.httpx, "AsyncClient", _FakeHttpx)
    return _FakeHttpx


@pytest.mark.asyncio
async def test_send_message_returns_timestamp_and_omits_edit_field(fake_httpx):
    client = SignalClient(api_url="http://x", phone_number="+49100")
    assert await client.send_message("+4911", "hi") == 42
    assert "edit_timestamp" not in fake_httpx.posted[0]


@pytest.mark.asyncio
async def test_send_message_edit_body_for_group(fake_httpx):
    fake_httpx.reply = {"status": "ok", "timestamp": 7}
    client = SignalClient(api_url="http://x", phone_number="+49100")
    assert await client.send_message("signal-group:abc", "edited", edit_timestamp=1234) == 7
    body = fake_httpx.posted[0]
    assert body["edit_timestamp"] == 1234
    assert body["message"] == "edited"
    assert body["recipients"][0].startswith("group.")


@pytest.mark.asyncio
async def test_send_attachment_returns_timestamp(fake_httpx, tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"png")
    client = SignalClient(api_url="http://x", phone_number="+49100")
    assert await client.send_attachment("+4911", str(f), "image/png", caption="c") == 42


# --- tools/message_mode.py ------------------------------------------------------

def test_message_mode_tools_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(config.paths, "data_dir", tmp_path)
    _set_mode(monkeypatch, "replace")
    got = json.loads(message_mode.get_message_mode("+000000").output)
    assert got == {"mode": "replace", "source": "global default"}
    assert message_mode.resolve_message_mode("+000000") == "replace"

    assert message_mode.set_message_mode("+000000", "append").success
    got = json.loads(message_mode.get_message_mode("+000000").output)
    assert got == {"mode": "append", "source": "channel"}
    assert message_mode.resolve_message_mode("+000000") == "append"
    # other channels keep the global default
    assert message_mode.resolve_message_mode("+000001") == "replace"


def test_set_message_mode_rejects_invalid_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config.paths, "data_dir", tmp_path)
    result = message_mode.set_message_mode("+000000", "loud")
    assert not result.success
    assert not (tmp_path / "+000000" / "message_mode.json").exists()


def test_corrupt_channel_file_falls_back_to_global(monkeypatch, tmp_path):
    monkeypatch.setattr(config.paths, "data_dir", tmp_path)
    _set_mode(monkeypatch, "append")
    (tmp_path / "+000000").mkdir()
    (tmp_path / "+000000" / "message_mode.json").write_text("{not json")
    assert message_mode.resolve_message_mode("+000000") == "append"


def test_message_mode_path_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(config.paths, "data_dir", tmp_path / "data")
    assert not message_mode.set_message_mode("../escape", "off").success


def test_message_mode_tool_tiers():
    for tool in ("set_message_mode", "get_message_mode"):
        assert tool in TOOL_PERMISSIONS[UserTier.PRIVILEGED]
        assert tool in TOOL_PERMISSIONS[UserTier.KNOWN]
        assert tool not in TOOL_PERMISSIONS[UserTier.UNKNOWN]
        assert tool in TOOLS_NEEDING_CHANNEL_ID


# --- config ------------------------------------------------------------------

def test_single_message_config_defaults_and_from_dict():
    assert ChatConfig.from_dict({}).single_message == SingleMessageConfig()
    sm = ChatConfig.from_dict({"single_message": {"mode": "append", "separator": " / "}}).single_message
    assert sm.effective_mode == "append" and sm.separator == " / "
    assert SingleMessageConfig().effective_mode == "replace"
    assert SingleMessageConfig().max_edits_per_message == 9


def test_invalid_mode_falls_back_at_startup():
    sm = SingleMessageConfig.from_dict({"mode": "loud"})
    assert sm.effective_mode == "replace"


@pytest.mark.parametrize("sm, error", [
    ({"mode": "loud"}, "chat.single_message.mode"),
    ({"max_edits_per_message": 0}, "chat.single_message.max_edits_per_message"),
    ({"max_message_chars": -5}, "chat.single_message.max_message_chars"),
])
def test_reload_validation_rejects_bad_single_message(sm, error):
    new = config_module.load_config({
        "auth": {"privileged_numbers": ["+000000"]},
        "chat": {"single_message": sm},
    })
    errors = config_module._validate_reloaded(new)
    assert any(error in e for e in errors), errors


def test_unquoted_yaml_off_means_off_at_startup():
    """YAML 1.1 parses an unquoted `mode: off` as False - it must still mean off."""
    raw = yaml.safe_load("auth:\n  privileged_numbers: ['+000000']\nchat:\n  single_message:\n    mode: off\n")
    assert raw["chat"]["single_message"]["mode"] is False
    new = config_module.load_config(raw)
    assert new.chat.single_message.effective_mode == "off"
    assert config_module._validate_reloaded(new) == []


def test_unquoted_yaml_off_accepted_by_reload(monkeypatch, tmp_path):
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("auth:\n  privileged_numbers: ['+000000']\nchat:\n  single_message:\n    mode: off\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    plan = config_module.plan_config_reload(config)
    assert plan.errors == []
    assert plan.new is not None and plan.new.chat.single_message.effective_mode == "off"


def test_yaml_true_mode_is_invalid():
    new = config_module.load_config({"auth": {"privileged_numbers": ["+000000"]}, "chat": {"single_message": {"mode": True}}})
    assert new.chat.single_message.effective_mode == "replace"
    assert any("chat.single_message.mode" in e for e in config_module._validate_reloaded(new))


def test_reload_validation_accepts_good_single_message():
    new = config_module.load_config({
        "auth": {"privileged_numbers": ["+000000"]},
        "chat": {"single_message": {"mode": "off"}},
    })
    assert config_module._validate_reloaded(new) == []


# --- DB user_visible + history marker ---------------------------------------------

def _agent_msg(text: str, ts: int) -> Message:
    ev = Event(source="agent", destination="+000000", message=text, timestamp=ts, group_id=None)
    return Message(event=ev, is_outgoing=True, processed=True)


@pytest.mark.usefixtures("reset_db")
def test_user_visible_column_roundtrip():
    db_ops.init_database()  # second run: migration must be idempotent
    ops = db_ops.DatabaseOperations()
    id1 = ops.store_message(_agent_msg("first", 100), "+000000")
    id2 = ops.store_message(_agent_msg("second", 101), "+000000")
    assert id1 == "agent:100" and id2 == "agent:101"
    ops.set_message_user_visible(id1, "+000000", False)
    msgs = ops.get_messages("+000000", limit=10)
    assert [(m.event.message, m.user_visible) for m in msgs] == [("first", False), ("second", True)]


def test_history_marks_only_replaced_assistant_rows():
    hidden = _agent_msg("draft", 1)
    hidden.user_visible = False
    visible = _agent_msg("final", 2)
    user = Message(
        event=Event(source="+000000", destination="+000000", message="q", timestamp=0, group_id=None),
        is_outgoing=False, processed=True,
    )
    out = _build_history_messages([user, hidden, visible], None, None, 15, 50)
    assert [m["content"] for m in out] == ["q", REPLACED_TEXT_PREFIX + "draft", "final"]


# --- prompt -------------------------------------------------------------------

@pytest.mark.parametrize("mode, present", [("replace", True), ("append", False), ("off", False), (None, False)])
def test_replace_prompt_only_in_replace_mode(mode, present):
    client = LLMClient(OllamaConfig())
    prompt = client.build_system_prompt(tools=[], user_tier="Privileged", message_mode=mode)
    assert ("MESSAGE DISPLAY" in prompt) is present
    if present:
        assert REPLACED_TEXT_PREFIX.strip() in prompt


# --- attachment hook (tools/signal_ops.py) -----------------------------------

@pytest.mark.asyncio
async def test_send_signal_attachment_sets_turn_target(monkeypatch, tmp_path):
    monkeypatch.setattr(config.paths, "data_dir", tmp_path)
    (tmp_path / "+000000" / "attachments").mkdir(parents=True)
    (tmp_path / "+000000" / "attachments" / "pic.png").write_bytes(b"png")
    fake_client = MagicMock()
    fake_client.send_attachment = AsyncMock(return_value=5000)
    monkeypatch.setattr(signal_ops, "SIGNAL_CLIENT", fake_client)

    edits = []

    async def send_new(text):
        return 1

    async def send_edit(ts, text):
        edits.append((ts, text))
        return True

    out = TurnOutput("append", send_new, send_edit)
    ctx = Context(channel_id="+000000", messages_db=tmp_path / "m.db", attachments_dir=tmp_path, turn_output=out)
    token = registry_module._current_context.set(ctx)
    try:
        result = await signal_ops.send_signal_attachment("pic.png", "a picture", "+000000")
    finally:
        registry_module._current_context.reset(token)
    assert result.success
    await out.send_text("done")
    assert edits == [(5000, "a picture\n\ndone")]


# --- core tool loop ---------------------------------------------------------------

def _loop_agent(monkeypatch):
    agent = _make_agent()
    ts = count(1000)

    async def send_message(recipient, message, edit_timestamp=None):
        return next(ts)

    agent._signal_client.send_message = AsyncMock(side_effect=send_message)
    tc1 = ToolCall(id="1", name="file_list", arguments={"path": "a"})
    tc2 = ToolCall(id="2", name="file_list", arguments={"path": "b"})
    agent._tool_registry.execute_tool = AsyncMock(
        return_value=ToolResult(name="file_list", success=True, output="ok", error=None)
    )
    agent._llm_client.chat = AsyncMock(side_effect=[
        ("Looking.", [tc1]),
        ("Still checking.", [tc2]),
        ("Final answer.", []),
    ])
    # Distinct row ids (real ones are second-resolution and would collide here).
    real_store = agent._db_ops.store_message
    row_ids = count(1)
    monkeypatch.setattr(agent._db_ops, "store_message", lambda *a, **k: f"{real_store(*a, **k)}#{next(row_ids)}")
    agent._db_ops.set_message_user_visible = MagicMock()
    return agent


def _event() -> Event:
    return Event(source="+000000", destination="+491234567890", message="check it",
                 timestamp=1700000010, group_id=None, attachments=[], mentions=[])


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
@pytest.mark.parametrize("mode, edited_texts", [
    ("append", ["Looking.\n\nStill checking.", "Looking.\n\nStill checking.\n\nFinal answer."]),
    ("replace", ["Still checking.", "Final answer."]),
])
async def test_tool_loop_sends_one_message_then_edits(monkeypatch, mode, edited_texts):
    _set_mode(monkeypatch, mode)
    agent = _loop_agent(monkeypatch)

    await agent.process_message(_event())

    calls = agent._signal_client.send_message.await_args_list
    new = [c for c in calls if c.kwargs.get("edit_timestamp") is None]
    edits = [c for c in calls if c.kwargs.get("edit_timestamp") is not None]
    assert [c.args[1] for c in new] == ["Looking."]
    assert [c.args[1] for c in edits] == edited_texts
    assert all(c.kwargs["edit_timestamp"] == 1000 for c in edits)
    assert agent._messages_sent["+000000"] == 3  # edits count toward the session cap
    if mode == "replace":
        # "Still checking." (first loop row) was replaced by the final text.
        hidden = [c.args[0] for c in agent._db_ops.set_message_user_visible.call_args_list]
        assert len(hidden) == 1 and hidden[0].endswith("#2")
        assert "message_mode='replace'" in repr(agent._llm_client.build_system_prompt.call_args)
    else:
        agent._db_ops.set_message_user_visible.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_tool_loop_mode_off_sends_every_text(monkeypatch):
    agent = _loop_agent(monkeypatch)  # conftest pins mode "off"

    await agent.process_message(_event())

    calls = agent._signal_client.send_message.await_args_list
    assert [c.args[1] for c in calls] == ["Looking.", "Still checking.", "Final answer."]
    assert all(c.kwargs == {} for c in calls)  # exact pre-feature call shape


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_channel_override_beats_global_mode(monkeypatch):
    _set_mode(monkeypatch, "off")
    # conftest pins core's resolver to the global mode; use the real one here.
    monkeypatch.setattr(core_module, "resolve_message_mode", message_mode.resolve_message_mode)
    assert message_mode.set_message_mode("+000000", "replace").success
    agent = _loop_agent(monkeypatch)

    await agent.process_message(_event())

    calls = agent._signal_client.send_message.await_args_list
    assert sum(1 for c in calls if c.kwargs.get("edit_timestamp") is None) == 1


def _split(agent):
    calls = agent._signal_client.send_message.await_args_list
    new = [c.args[1] for c in calls if c.kwargs.get("edit_timestamp") is None]
    edits = [(c.kwargs["edit_timestamp"], c.args[1]) for c in calls if c.kwargs.get("edit_timestamp") is not None]
    return new, edits


def _enqueue_during_first_tool(agent, queued: Event) -> None:
    """Queue `queued` while the first tool runs, so the next loop checkpoint drains it."""
    async def execute_tool(*args, **kwargs):
        if not getattr(execute_tool, "done", False):
            execute_tool.done = True
            agent._channel_inbound_queue.setdefault("+000000", []).append(queued)
        return ToolResult(name="file_list", success=True, output="ok", error=None)

    agent._tool_registry.execute_tool = AsyncMock(side_effect=execute_tool)


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_drained_user_message_starts_new_message(monkeypatch):
    _set_mode(monkeypatch, "append")
    agent = _loop_agent(monkeypatch)
    _enqueue_during_first_tool(agent, Event(
        source="+000000", destination="+491234567890", message="also check b",
        timestamp=1700000020, group_id=None, attachments=[], mentions=[]))

    await agent.process_message(_event())

    new, edits = _split(agent)
    assert new == ["Looking.", "Final answer."]
    assert edits == [(1000, "Looking.\n\nStill checking.")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_unmentioned_group_message_keeps_target(monkeypatch):
    _set_mode(monkeypatch, "append")
    agent = _loop_agent(monkeypatch)
    _enqueue_during_first_tool(agent, Event(
        source="+000001", destination="+491234567890", message="unrelated chatter",
        timestamp=1700000020, group_id="g1", attachments=[], mentions=[]))

    await agent.process_message(_event())

    new, edits = _split(agent)
    assert new == ["Looking."]
    assert len(edits) == 2 and all(ts == 1000 for ts, _ in edits)


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_canned_reply_mid_turn_starts_new_message(monkeypatch):
    """A queued /reload is answered by _preprocess_incoming itself (no injection)."""
    _set_mode(monkeypatch, "append")
    agent = _loop_agent(monkeypatch)
    agent.reload = MagicMock(return_value=SimpleNamespace(format_reply=lambda: "Reload OK"))
    _enqueue_during_first_tool(agent, Event(
        source="+000000", destination="+491234567890", message="/reload",
        timestamp=1700000020, group_id=None, attachments=[], mentions=[]))

    await agent.process_message(_event())

    new, edits = _split(agent)
    assert new == ["Looking.", "Reload OK", "Final answer."]
    assert edits == [(1000, "Looking.\n\nStill checking.")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_internal_limit_notice_keeps_partial_answer_in_replace_mode(monkeypatch):
    _set_mode(monkeypatch, "replace")
    monkeypatch.setattr(core_module, "_get_max_tool_iterations_for_tier", lambda tier: 1)
    agent = _loop_agent(monkeypatch)

    await agent.process_message(_event())

    new, edits = _split(agent)
    notice = prompts.current().text("internal-limit")
    assert new == ["Looking."]
    assert edits == [(1000, "Still checking."), (1000, "Still checking.\n\n" + notice)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_cronjob_uses_turn_output_and_message_mode(monkeypatch):
    _set_mode(monkeypatch, "replace")
    agent = _loop_agent(monkeypatch)
    agent._llm_client.chat = AsyncMock(return_value=("Cron result.", []))
    job = SimpleNamespace(
        id="job1", channel_id="+000000", creator_number="+000000", prompt="do it",
        is_recurring=False, interval_minutes=None, cancelled=False, scheduled_at=None,
    )

    await agent._process_cronjob(job)

    context = agent._context_manager.get_context.return_value
    assert isinstance(context.turn_output, TurnOutput) and context.turn_output.mode == "replace"
    assert agent._llm_client.build_system_prompt.call_args.kwargs["message_mode"] == "replace"
    new, edits = _split(agent)
    assert new == ["Cron result."] and edits == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_subagent_llm_failure_is_appended_notice(monkeypatch):
    """Attachment sent by a tool, then the LLM fails: in replace mode the
    failure text is appended to the attachment's caption, not replacing it."""
    _set_mode(monkeypatch, "replace")
    agent = _loop_agent(monkeypatch)
    context = agent._context_manager.get_context.return_value
    tc = ToolCall(id="1", name="send_signal_attachment", arguments={"filename": "p.png"})

    async def execute_tool(*args, **kwargs):
        context.turn_output.note_attachment(5000, "pic")
        return ToolResult(name="send_signal_attachment", success=True, output="sent", error=None)

    agent._tool_registry.execute_tool = AsyncMock(side_effect=execute_tool)
    agent._llm_client.chat = AsyncMock(side_effect=[("", [tc]), RuntimeError("down")])

    await agent._process_subagent("do it", "+000000", UserTier.PRIVILEGED)

    assert agent._llm_client.build_system_prompt.call_args.kwargs["message_mode"] == "replace"
    new, edits = _split(agent)
    assert new == []
    assert edits == [(5000, "pic\n\n" + prompts.current().text("llm-unavailable"))]
