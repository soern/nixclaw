"""Web interface sync: chat bus, signal.py outbound listener, core.py hooks."""

import asyncio
import dataclasses
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import build_mock_agent_loop
from src.nixclaw_agent import config as config_module
from src.nixclaw_agent import core
from src.nixclaw_agent import signal as signal_mod
from src.nixclaw_agent.config import WebUIConfig, config
from src.nixclaw_agent.db import operations as db
from src.nixclaw_agent.signal import SignalClient
from src.nixclaw_agent.tools import signal_ops
from src.nixclaw_agent.types import Attachment, Event
from webui_support import BOT, GROUP, KNOWN, PRIV, UNKNOWN, make_env, webui_config  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("reset_db", "webui_config")]

GROUP_ID = GROUP[len("signal-group:"):]


class _FakeHttpx:
    """Stand-in for httpx.AsyncClient: records requests, answers with a timestamp."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.ts = 1000
        rec = self

        class _Client:
            def __init__(self, timeout: float) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def _resp(self):
                rec.ts += 1
                ts = rec.ts
                return SimpleNamespace(status_code=201, text="", json=lambda: {"timestamp": ts},
                                       raise_for_status=lambda: None)

            async def post(self, url, json=None, headers=None):
                rec.calls.append(("POST", url, json))
                return self._resp()

            async def put(self, url, json=None):
                rec.calls.append(("PUT", url, json))
                return self._resp()

            async def request(self, method, url, json=None):
                rec.calls.append((method, url, json))
                return self._resp()

        self.client_cls = _Client


@pytest.fixture
def httpx_fake(monkeypatch):
    fake = _FakeHttpx()
    monkeypatch.setattr(signal_mod.httpx, "AsyncClient", fake.client_cls)
    monkeypatch.setattr(config.signal, "mock_api_url", None)
    return fake


@pytest.fixture
def listener():
    events: list[tuple[str, dict]] = []
    signal_mod.set_outbound_listener(lambda kind, payload: events.append((kind, payload)))
    yield events
    signal_mod.set_outbound_listener(None)


def _rows(channel_id: str) -> list:
    return db.chat_log_history(channel_id, None, 100)


# --- signal.py outbound listener ------------------------------------------------

async def test_listener_called_for_both_signal_clients(httpx_fake, listener, tmp_path):
    main_client = SignalClient(api_url="http://live", phone_number=BOT)
    ts = await main_client.send_message(KNOWN, "hi")
    await main_client.send_message(KNOWN, "hi again", edit_timestamp=ts)
    f = tmp_path / "pic.png"
    f.write_bytes(b"png")
    await signal_ops.SIGNAL_CLIENT.send_attachment(KNOWN, str(f), "image/png", caption="look")
    poll_ts = await main_client.create_poll(KNOWN, "Print?", ["Yes", "No"])
    await main_client.delete_message(KNOWN, poll_ts)
    await main_client.send_typing_indicator(KNOWN, True)
    assert [k for k, _ in listener] == ["message", "edit", "attachment", "poll", "delete", "typing"]
    assert listener[0][1] == {"recipient": KNOWN, "text": "hi", "ts": ts}
    assert listener[1][1]["edit_ts"] == ts
    assert listener[2][1]["file_path"] == str(f) and listener[2][1]["caption"] == "look"


async def test_listener_not_called_with_publish_false(httpx_fake, listener, tmp_path):
    client = SignalClient(api_url="http://live", phone_number=BOT)
    await client.send_message(KNOWN, "mirror", publish=False)
    f = tmp_path / "a.txt"
    f.write_text("x")
    await signal_ops.SIGNAL_CLIENT.send_attachment(KNOWN, str(f), "text/plain", publish=False)
    await client.create_poll(KNOWN, "q", ["a"], publish=False)
    await client.delete_message(KNOWN, 5, publish=False)
    assert listener == []
    assert len(httpx_fake.calls) == 4


async def test_listener_errors_never_reach_send_path(httpx_fake):
    def boom(kind, payload):
        raise RuntimeError("boom")
    signal_mod.set_outbound_listener(boom)
    try:
        ts = await SignalClient(api_url="http://live", phone_number=BOT).send_message(KNOWN, "hi")
        assert ts is not None
    finally:
        signal_mod.set_outbound_listener(None)


# --- bus ----------------------------------------------------------------------------

async def test_bus_publish_edit_delete_and_rev_replay():
    e = make_env()
    bus = e.bus
    bus.on_outbound("message", {"recipient": KNOWN, "text": "one", "ts": 10})
    bus.on_outbound("poll", {"recipient": KNOWN, "question": "Print?", "answers": ["Yes", "No"], "ts": 11})
    rows = _rows(KNOWN)
    assert [r["origin"] for r in rows] == ["agent", "agent"]
    assert rows[0]["sender"] == BOT and rows[0]["sender_name"] == config.agent.name
    rev_after_insert = rows[1]["rev"]
    bus.on_outbound("edit", {"recipient": KNOWN, "text": "one, edited", "edit_ts": 10})
    bus.on_outbound("delete", {"recipient": KNOWN, "ts": 11})
    rows = _rows(KNOWN)
    assert len(rows) == 2
    assert rows[0]["text"] == "one, edited" and rows[0]["edited"] == 1
    assert rows[1]["deleted"] == 1 and json.loads(rows[1]["poll"])["closed"] is True
    assert rows[0]["rev"] > rev_after_insert and rows[1]["rev"] > rows[0]["rev"]
    replay = bus.replay(KNOWN, rev_after_insert)
    assert [r["id"] for r in replay] == [rows[0]["id"], rows[1]["id"]]
    # another user's channel is not replayed
    bus.on_outbound("message", {"recipient": PRIV, "text": "private", "ts": 12})
    assert [r["text"] for r in bus.replay(KNOWN, 0)] == ["one, edited", ""]
    # unknown edit target -> new row; unknown delete -> ignored
    bus.on_outbound("edit", {"recipient": KNOWN, "text": "orphan", "edit_ts": 999})
    bus.on_outbound("delete", {"recipient": KNOWN, "ts": 12345})
    assert [r["text"] for r in _rows(KNOWN)][-1] == "orphan"


async def test_bus_outbound_from_other_thread_hops_to_loop():
    e = make_env()
    t = threading.Thread(target=e.bus.on_outbound, args=("message", {"recipient": KNOWN, "text": "x", "ts": 1}))
    t.start()
    t.join()
    assert _rows(KNOWN) == []
    await asyncio.sleep(0)
    assert [r["text"] for r in _rows(KNOWN)] == ["x"]


async def test_bus_attachment_and_transcript_updates():
    e = make_env()
    event = Event(source=KNOWN, destination=BOT, message="[Attachment]", timestamp=1, group_id=None,
                  attachments=[Attachment(id="sig1", content_type="audio/aac", filename=None)],
                  source_name="Alice", signal_timestamp_ms=1000)
    row_id = e.bus.publish_inbound(event, KNOWN, origin="signal")
    row = db.chat_log_get(row_id)
    assert row["text"] == "" and row["sender_name"] == "Alice" and row["origin"] == "signal"
    assert json.loads(row["attachments"])[0]["name"] == ""
    from src.nixclaw_agent.web.bus import message_json
    assert message_json(row)["attachments"][0]["exists"] is False
    e.bus.update_attachments(row_id, [Attachment(id="sig1", content_type="audio/aac", filename=None,
                                                 local_path="/x/sig1.aac", size=5)])
    e.bus.set_transcript(row_id, "sig1", "hello world")
    att = json.loads(db.chat_log_get(row_id)["attachments"])[0]
    assert (att["name"], att["size"], att["transcript"]) == ("sig1.aac", 5, "hello world")
    assert e.bus.display_name(KNOWN) == "Alice"


async def test_auth_code_not_published(httpx_fake):
    e = make_env()
    e.web_auth._signal = SignalClient(api_url="http://live", phone_number=BOT)
    signal_mod.set_outbound_listener(e.bus.on_outbound)
    try:
        assert e.web_auth.request_code(KNOWN, "1.2.3.4")
        await asyncio.gather(*e.web_auth._tasks)
    finally:
        signal_mod.set_outbound_listener(None)
    assert len(httpx_fake.calls) == 1 and "login code" in httpx_fake.calls[0][2]["message"]
    assert _rows(KNOWN) == []


# --- core.py --------------------------------------------------------------------------

def _agent_with_web(httpx_fake=None):
    e = make_env()
    agent = build_mock_agent_loop()
    agent._auth_manager = e.auth_manager
    agent.attach_web(e.bus, e.web_auth)
    agent._dispatch_or_queue = MagicMock()
    if httpx_fake is not None:
        agent._signal_client = SignalClient(api_url="http://live", phone_number=BOT)
    return agent, e


async def test_web_message_dispatched_before_mirror_and_mirror_errors_logged(httpx_fake):
    agent, e = _agent_with_web(httpx_fake)
    agent._signal_client.send_message = AsyncMock(side_effect=RuntimeError("signal down"))
    agent.submit_web_message(KNOWN, KNOWN, "hello", [])
    # dispatched synchronously, before the mirror task ran
    assert agent._dispatch_or_queue.call_count == 1
    agent._signal_client.send_message.assert_not_called()
    await asyncio.gather(*agent._web_tasks)
    agent._signal_client.send_message.assert_awaited_once()


async def test_web_messages_same_second_not_deduped():
    agent, e = _agent_with_web()
    ev1 = Event(source=KNOWN, destination=BOT, message="[Attachment]", timestamp=5, group_id=None, origin="web")
    ev2 = Event(source=KNOWN, destination=BOT, message="[Attachment]", timestamp=5, group_id=None, origin="web")
    ev1.chat_log_id = e.bus.publish_inbound(ev1, KNOWN, origin="web")
    ev2.chat_log_id = e.bus.publish_inbound(ev2, KNOWN, origin="web")
    assert (await agent._preprocess_incoming(ev1, KNOWN, KNOWN)).event is not None
    assert (await agent._preprocess_incoming(ev2, KNOWN, KNOWN)).event is not None
    # the dedup key is the row id: checked against exactly that key
    seen: list[str] = []
    agent._db_ops.is_message_processed = lambda mid, cid: seen.append(mid) or False  # type: ignore[method-assign]
    await agent._preprocess_incoming(dataclasses.replace(ev2), KNOWN, KNOWN)
    assert seen == [f"web:{ev2.chat_log_id}"]


async def test_deauth_argument_kept_as_typed_in_1to1():
    agent, e = _agent_with_web()
    name = f"{config.agent.name} box"
    e.login(KNOWN, name)
    r = await agent._preprocess_incoming(
        Event(source=KNOWN, destination=BOT, message=f"/deauth {name}", timestamp=1, group_id=None),
        KNOWN, KNOWN)
    assert r.event is None and db.web_endpoints_for(KNOWN) == []


async def test_web_message_mirrored_but_not_published(httpx_fake, tmp_path):
    agent, e = _agent_with_web(httpx_fake)
    signal_mod.set_outbound_listener(e.bus.on_outbound)
    try:
        row_id = agent.submit_web_message(KNOWN, KNOWN, "hello", [])
        await asyncio.gather(*agent._web_tasks)
        e.bus.note_name(KNOWN, "Alice")
        f = tmp_path / "p.png"
        f.write_bytes(b"png")
        att = Attachment(id="p.png", content_type="image/png", filename="p.png", local_path=str(f), size=3)
        agent.submit_web_message(KNOWN, GROUP, "@Nixclaw look", [att])
        await asyncio.gather(*agent._web_tasks)
    finally:
        signal_mod.set_outbound_listener(None)
    sends = [c[2] for c in httpx_fake.calls]
    assert sends[0]["message"] == "🌐 hello" and sends[0]["recipients"] == [KNOWN]
    assert sends[1]["message"] == "🌐 Alice: @Nixclaw look"
    assert sends[1]["base64_attachments"][0].startswith("data:image/png;filename=p.png;base64,")
    # only the two inbound web rows, no mirror rows
    assert [(r["origin"], r["text"]) for r in _rows(KNOWN)] == [("web", "hello")]
    assert [(r["origin"], r["text"]) for r in _rows(GROUP)] == [("web", "@Nixclaw look")]
    assert db.chat_log_get(row_id)["sender"] == KNOWN
    dispatched = [c.args for c in agent._dispatch_or_queue.call_args_list]
    assert dispatched[0][0] == KNOWN and dispatched[0][1].origin == "web"
    ev = dispatched[1][1]
    assert dispatched[1][0] == GROUP and ev.group_id == GROUP_ID and ev.destination == GROUP
    assert ev.attachments[0].local_path == str(f) and ev.chat_log_id is not None


async def test_signal_entry_publishes_and_skips_own_number():
    agent, e = _agent_with_web()
    await agent._on_signal_message(Event(source=KNOWN, destination=BOT, message="hi", timestamp=1,
                                         group_id=None, source_name="Alice"))
    await agent._on_signal_message(Event(source=BOT, destination=KNOWN, message="echo", timestamp=2,
                                         group_id=None))
    rows = _rows(KNOWN)
    assert [(r["origin"], r["text"], r["sender_name"]) for r in rows] == [("signal", "hi", "Alice")]
    assert agent._dispatch_or_queue.call_count == 2


async def test_local_path_attachments_not_redownloaded():
    agent, e = _agent_with_web()
    agent._signal_client.save_attachment = AsyncMock(return_value=Attachment(
        id="sig", content_type="image/jpeg", filename="x.jpeg", local_path="/d/sig.jpeg", size=4))
    local = Attachment(id="w.png", content_type="image/png", filename="w.png", local_path="/d/w.png", size=1)
    remote = Attachment(id="sig", content_type="image/jpeg", filename=None)
    event = Event(source=KNOWN, destination=BOT, message="x", timestamp=1, group_id=None,
                  attachments=[local, remote])
    event.chat_log_id = e.bus.publish_inbound(event, KNOWN, origin="signal")
    result = await agent._download_attachments(event, KNOWN)
    assert result[0] is local and result[1].local_path == "/d/sig.jpeg"
    agent._signal_client.save_attachment.assert_awaited_once()
    names = [a["name"] for a in json.loads(db.chat_log_get(event.chat_log_id)["attachments"])]
    assert names == ["w.png", "sig.jpeg"]


async def test_transcript_stored_in_chat_log(monkeypatch, tmp_path):
    agent, e = _agent_with_web()
    monkeypatch.setattr(config.stt, "enabled", True)
    monkeypatch.setattr(core, "transcribe_attachment", AsyncMock(return_value="hello world"))
    audio = tmp_path / "v.webm"
    audio.write_bytes(b"x")
    att = Attachment(id="v.webm", content_type="audio/webm", filename="v.webm", local_path=str(audio), size=1)
    event = Event(source=KNOWN, destination=BOT, message="[Attachment]", timestamp=5, group_id=None,
                  attachments=[att], origin="web")
    event.chat_log_id = e.bus.publish_inbound(event, KNOWN, origin="web")
    result = await agent._preprocess_incoming(event, KNOWN, KNOWN)
    assert "[Voice message: v.webm] hello world" in result.event.message
    entry = json.loads(db.chat_log_get(event.chat_log_id)["attachments"])[0]
    assert entry["transcript"] == "hello world"


async def test_unmentioned_group_attachment_downloaded_without_stt(monkeypatch):
    agent, e = _agent_with_web()
    monkeypatch.setattr(config.stt, "enabled", True)
    transcribe = AsyncMock(return_value="nope")
    monkeypatch.setattr(core, "transcribe_attachment", transcribe)
    agent._signal_client.save_attachment = AsyncMock(return_value=Attachment(
        id="a1", content_type="audio/aac", filename=None, local_path="/d/a1.aac", size=9))
    event = Event(source=KNOWN, destination=GROUP, message="[Attachment]", timestamp=7, group_id=GROUP_ID,
                  attachments=[Attachment(id="a1", content_type="audio/aac", filename=None)])
    event.chat_log_id = e.bus.publish_inbound(event, GROUP, origin="signal")
    result = await agent._preprocess_incoming(event, GROUP, GROUP)
    assert result.event is None
    agent._signal_client.save_attachment.assert_awaited_once()
    transcribe.assert_not_awaited()
    assert json.loads(db.chat_log_get(event.chat_log_id)["attachments"])[0]["name"] == "a1.aac"


def _sent_texts(agent) -> list[str]:
    return [c.args[1] for c in agent._signal_client.send_message.call_args_list]


async def test_status_and_deauth_commands_in_signal_1to1():
    agent, e = _agent_with_web()
    e.login(KNOWN, "Laptop")
    r = await agent._preprocess_incoming(
        Event(source=KNOWN, destination=BOT, message="/status", timestamp=1, group_id=None), KNOWN, KNOWN)
    assert r.event is None
    assert "1. Laptop" in _sent_texts(agent)[-1]
    r = await agent._preprocess_incoming(
        Event(source=KNOWN, destination=BOT, message="/deauth 1", timestamp=2, group_id=None), KNOWN, KNOWN)
    assert r.event is None and "Laptop" in _sent_texts(agent)[-1]
    assert db.web_endpoints_for(KNOWN) == []
    # UNKNOWN sender: not handled here (goes on to the normal path)
    r = await agent._preprocess_incoming(
        Event(source=UNKNOWN, destination=BOT, message="/status", timestamp=3, group_id=None), UNKNOWN, UNKNOWN)
    assert r.event is not None


async def test_status_in_group_gives_hint_only_when_mentioned():
    agent, e = _agent_with_web()
    r = await agent._preprocess_incoming(
        Event(source=KNOWN, destination=GROUP, message=f"@{config.agent.name} /status", timestamp=1, group_id=GROUP_ID),
        GROUP, GROUP)
    assert r.event is None and _sent_texts(agent) == ["Please send /status and /deauth to me in a 1:1 chat."]
    r = await agent._preprocess_incoming(
        Event(source=KNOWN, destination=GROUP, message="/status", timestamp=2, group_id=GROUP_ID),
        GROUP, GROUP)
    assert r.event is None and len(_sent_texts(agent)) == 1


async def test_reload_hot_applies_webui_and_closes_unknown_streams(tmp_path, monkeypatch):
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        f'auth:\n  privileged_numbers: ["{PRIV}"]\n'
        "webui:\n  enabled: true\n  api_port: 4000\n  code_ttl_seconds: 100\n"
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    for name in ("auth", "chat", "agent", "consent"):
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(config.logging, "level", config.logging.level)
    monkeypatch.setattr(config, "webui", WebUIConfig(enabled=True))
    agent, e = _agent_with_web()
    known_sub = e.bus.subscribe(KNOWN, 1)
    priv_sub = e.bus.subscribe(PRIV, 2)
    result = agent.reload("test")
    assert result.ok, result.errors
    assert "webui" in result.applied and "webui.api_port" in result.restart_required
    assert config.webui.code_ttl_seconds == 100 and config.webui.api_port == 3003
    assert known_sub.queue.get_nowait()[0] == "deauthed" and known_sub.closed
    assert priv_sub.queue.empty() and not priv_sub.closed


async def test_invalid_webui_rejected_at_load_and_reload(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="max_upload_mb.*hidden_paths"):
        config_module.load_config({"webui": {"max_upload_mb": 0, "hidden_paths": "browser"}})
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(f'auth:\n  privileged_numbers: ["{PRIV}"]\nwebui:\n  token_expiry_days: -1\n')
    monkeypatch.setattr(config_module, "CONFIG_PATH", yaml_path)
    plan = config_module.plan_config_reload(config)
    assert plan.errors and "token_expiry_days" in plan.errors[0]


async def test_mirror_attachment_failure_continues_and_group_caption_has_name(tmp_path):
    agent, e = _agent_with_web()
    e.bus.note_name(KNOWN, "Alice")
    files = []
    for n in ("a.png", "b.png"):
        f = tmp_path / n
        f.write_bytes(b"png")
        files.append(Attachment(id=n, content_type="image/png", filename=n, local_path=str(f), size=3))
    agent._signal_client.send_attachment = AsyncMock(side_effect=[RuntimeError("down"), 2])
    agent.submit_web_message(KNOWN, GROUP, "", files)
    await asyncio.gather(*agent._web_tasks)
    calls = agent._signal_client.send_attachment.call_args_list
    assert [c.args[1] for c in calls] == [f.local_path for f in files]
    assert [c.kwargs["caption"] for c in calls] == ["🌐 Alice:", "🌐 Alice:"]


async def test_unchanged_attachment_and_transcript_updates_not_published():
    e = make_env()
    att = Attachment(id="v.webm", content_type="audio/webm", filename="v.webm", local_path="/d/v.webm", size=1)
    event = Event(source=KNOWN, destination=BOT, message="[Attachment]", timestamp=1, group_id=None,
                  attachments=[att])
    row_id = e.bus.publish_inbound(event, KNOWN, origin="web")
    rev = db.chat_log_get(row_id)["rev"]
    e.bus.update_attachments(row_id, [att])
    assert db.chat_log_get(row_id)["rev"] == rev
    e.bus.set_transcript(row_id, "v.webm", "hi")
    rev2 = db.chat_log_get(row_id)["rev"]
    assert rev2 > rev
    e.bus.set_transcript(row_id, "v.webm", "hi")
    assert db.chat_log_get(row_id)["rev"] == rev2
