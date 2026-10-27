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
from webui_support import (  # noqa: F401
    BOT, CONTACT_UUIDS, GROUP, KNOWN, OTHER_GROUP, PRIV, UNKNOWN, make_env, vote, webui_config,
)

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
    """AgentLoop wired to a fresh web env; the API's ContactBook is injected
    so contact-backed senders take the pill rung (like main.py's wiring)."""
    e = make_env()
    agent = build_mock_agent_loop(contacts=e.contacts)
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
        f = tmp_path / "p.png"
        f.write_bytes(b"png")
        att = Attachment(id="p.png", content_type="image/png", filename="p.png", local_path=str(f), size=3)
        agent.submit_web_message(KNOWN, GROUP, "@Nixclaw look", [att])
        await asyncio.gather(*agent._web_tasks)
    finally:
        signal_mod.set_outbound_listener(None)
    sends = [c[2] for c in httpx_fake.calls]
    assert sends[0]["message"] == "🌐 hello" and sends[0]["recipients"] == [KNOWN]
    # KNOWN is a seeded contact -> the sender is a pill span, never the number
    assert sends[1]["message"] == f"🌐 {PLACEHOLDER}: @Nixclaw look"
    assert sends[1]["mentions"] == [{"start": 3, "length": 1, "author": CONTACT_UUIDS[KNOWN]}]
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


async def test_mirror_attachment_failure_continues_and_group_caption_has_pill(tmp_path):
    agent, e = _agent_with_web()
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
    # every caption of the call uses the same ladder branch (pill-￼ + own span)
    assert [c.kwargs["caption"] for c in calls] == [f"🌐 {PLACEHOLDER}:"] * 2
    for c in calls:
        assert [m.author for m in c.kwargs["mentions"]] == [CONTACT_UUIDS[KNOWN]]


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


# --- @-mentions on the 🌐 mirror ------------------------------------------------------------

from src.nixclaw_agent.mentions import PLACEHOLDER, MentionEntry  # noqa: E402

KID_UUID = "22222222-2222-4222-8222-222222222222"


def _kid(token: str = "@Known Kid") -> MentionEntry:
    return MentionEntry(author=KID_UUID, token=token)


async def test_mirror_text_message_gets_placeholder_and_spans():
    agent, e = _agent_with_web()
    agent.submit_web_message(KNOWN, GROUP, "😀 @Known Kid ping", [], mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    assert call.args[0] == GROUP
    # contact sender -> sender pill; display text -> transfer body: recipient ￼,
    # the typed space after the token kept
    assert call.args[1] == f"🌐 {PLACEHOLDER}: 😀 {PLACEHOLDER}  ping"
    assert call.kwargs["publish"] is False
    assert call.kwargs["mentions"] is not None
    assert [m.author for m in call.kwargs["mentions"]] == [CONTACT_UUIDS[KNOWN], KID_UUID]
    assert call.kwargs["mentions"][0].start == call.args[1].index(PLACEHOLDER)


async def test_mirror_span_survives_the_utf16_conversion_on_the_wire(httpx_fake):
    """The real send path puts UTF-16 units into the REST body (label rung:
    a sanitized chat_log name - never the number - keeps the offsets pinned)."""
    agent, e = _agent_with_web(httpx_fake)
    agent._signal_client = SignalClient(api_url="http://live", phone_number=BOT)
    _seed_channel_name(e, GROUP, SENDER, "Alice")
    e.bus._names.clear()
    agent.submit_web_message(SENDER, GROUP, "😀 @Known Kid ping", [], mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    body = httpx_fake.calls[-1][2]
    assert body["message"] == f"🌐 Alice: 😀 {PLACEHOLDER}  ping"
    assert SENDER not in body["message"]
    # 🌐(2) + " "(1) + "Alice"(5) + ":"(1) + " "(1) + 😀(2) + " "(1) = 13
    assert body["mentions"] == [{"start": 13, "length": 1, "author": KID_UUID}]


async def test_mirror_sender_named_like_a_token_cannot_steal_the_span():
    """scan_from anchors the scan behind the wrapper: the sender's chat_log
    name (attacker-influenceable via their own Signal profile) is "@Alice",
    the mentioned member's token is "@Alice" too - the prefix copy stays
    plain text, only the body copy gets the span."""
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, SENDER, "@Alice")
    e.bus._names.clear()
    agent.submit_web_message(SENDER, GROUP, "@Alice ping", [],
                             mentions=[MentionEntry(author=KID_UUID, token="@Alice")])
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    raw = call.args[1]
    assert raw == f"🌐 @Alice: {PLACEHOLDER}  ping"
    assert raw.count(PLACEHOLDER) == 1
    assert len(call.kwargs["mentions"]) == 1
    assert call.kwargs["mentions"][0].start == raw.index(PLACEHOLDER)


async def test_mirror_label_containing_a_recipient_token_verbatim_keeps_only_the_body_span():
    """Plan Tests 7: label branch, label embeds the recipient token verbatim
    with a valid boundary ("@Alice Jr" -> "@Alice" + space) - without the
    scan_from anchor the prefix copy would steal the span."""
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, SENDER, "@Alice Jr")
    e.bus._names.clear()
    agent.submit_web_message(SENDER, GROUP, "@Alice ping", [],
                             mentions=[MentionEntry(author=KID_UUID, token="@Alice")])
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    raw = call.args[1]
    assert raw == f"🌐 @Alice Jr: {PLACEHOLDER}  ping"
    assert raw.count(PLACEHOLDER) == 1
    assert call.kwargs["mentions"][0].start == raw.index(PLACEHOLDER) > raw.index(":")


async def test_mirror_without_a_matching_token_sends_plain_text():
    agent, e = _agent_with_web()
    agent.submit_web_message(SENDER, GROUP, "no tokens here", [], mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    # unseeded sender, no chat_log name -> label-free rung: plain text, no spans
    assert call.args[1] == "🌐 no tokens here"
    assert not call.kwargs["mentions"]


async def test_mirror_mention_scan_failure_sends_plain_caption_and_loses_nothing(monkeypatch):
    """Settled failure mode: a broken RECIPIENT scan degrades to "no pill,
    plain text" - the mirror itself (and the sender pill from the ladder)
    must still be sent (display caption, recipients gone)."""
    agent, e = _agent_with_web()

    def boom(*args, **kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(core, "apply_mentions", boom)
    agent.submit_web_message(KNOWN, GROUP, "hi @Known Kid", [], mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    assert call.args[0] == GROUP
    assert call.args[1] == f"🌐 {PLACEHOLDER}: hi @Known Kid"
    assert call.kwargs["publish"] is False
    # recipient pills lost, sender span from the ladder intact
    assert [m.author for m in call.kwargs["mentions"]] == [CONTACT_UUIDS[KNOWN]]
    # the degraded mirror is still sent exactly once (no double-send on failure)
    assert agent._signal_client.send_message.await_count == 1


async def test_mirror_attachment_branch_puts_mentions_on_the_first_caption_only(tmp_path):
    agent, e = _agent_with_web()
    files = []
    for n in ("a.png", "b.png"):
        f = tmp_path / n
        f.write_bytes(b"png")
        files.append(Attachment(id=n, content_type="image/png", filename=n, local_path=str(f), size=3))
    agent._signal_client.send_attachment = AsyncMock(return_value=1)
    agent.submit_web_message(KNOWN, GROUP, "@Known Kid look", files, mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    calls = agent._signal_client.send_attachment.call_args_list
    # RECIPIENTS ride the first caption only; the sender pill spans every caption
    assert calls[0].kwargs["caption"] == f"🌐 {PLACEHOLDER}: {PLACEHOLDER}  look"
    assert [m.author for m in calls[0].kwargs["mentions"]] == [CONTACT_UUIDS[KNOWN], KID_UUID]
    assert calls[1].kwargs["caption"] == f"🌐 {PLACEHOLDER}:"
    assert [m.author for m in calls[1].kwargs["mentions"]] == [CONTACT_UUIDS[KNOWN]]


async def test_mirror_raw_caption_never_reaches_the_chat_log(httpx_fake):
    """publish=False keeps the ￼-carrying transfer body out of web history."""
    agent, e = _agent_with_web(httpx_fake)
    e.bus.note_name(KNOWN, "Alice")
    agent.submit_web_message(KNOWN, GROUP, "hi @Known Kid", [], mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    rows = _rows(GROUP)
    assert [(r["origin"], r["text"]) for r in rows] == [("web", "hi @Known Kid")]
    assert PLACEHOLDER not in rows[0]["text"]


async def test_chat_log_stores_no_placeholder_for_any_origin():
    """U+FFFC is wire storage only: history shows the plain display text, and
    the agent-visible event is not modified."""
    e = make_env()
    event = Event(source=KNOWN, destination=BOT, message=f"hi {PLACEHOLDER} there {PLACEHOLDER}",
                  timestamp=1, group_id=None, source_name="Alice")
    row_id = e.bus.publish_inbound(event, KNOWN, origin="signal")
    row = db.chat_log_get(row_id)
    assert row["text"] == "hi  there "
    from src.nixclaw_agent.web.bus import message_json
    assert message_json(row)["text"] == "hi  there "
    assert event.message == f"hi {PLACEHOLDER} there {PLACEHOLDER}"


# --- Sender-label ladder: 🌐 mirrors never carry a phone number -------------------------
# (tmp/plan-webui-mirror-privacy.md: pill > last-seen chat name (DB) > nothing;
# harness-composed sent content is number-free; the consent pair the same way.)

SENDER = "+000003"  # never in the mock contacts feed, no chat_log name by default


def _seed_channel_name(e, channel_id: str, number: str, name) -> None:
    """Store a chat_log row giving `number` display name `name` in `channel_id`
    (the webui render source and the ladder's DB rung; also warms ChatBus._names)."""
    ev = Event(source=number, destination=channel_id, message="earlier", timestamp=2,
               group_id=channel_id[len("signal-group:"):] if channel_id.startswith("signal-group:") else None,
               source_name=name)
    e.bus.publish_inbound(ev, channel_id, origin="web")


def _assert_placeholders_all_spanned(body: str, spans) -> None:
    """Tofu conservation: every ￼ in `body` carries exactly one span and every
    span points at a ￼ (per-caption to_wire validity, plan Tests 8)."""
    from src.nixclaw_agent.mentions import to_wire
    assert body.count(PLACEHOLDER) == len(spans or []), (body, spans)
    to_wire(body, spans or [])  # raises ValueError on a span not sitting on a ￼


async def test_mirror_text_that_is_only_a_mention_keeps_its_span():
    """Canary (round-2 Critical-1): the whole text is one recipient token and
    the sender is a contact -> sender pill span AND recipient span survive."""
    agent, e = _agent_with_web()
    agent.submit_web_message(KNOWN, GROUP, "@Known Kid", [], mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    body, spans = call.args[1], call.kwargs["mentions"]
    # prefix "🌐 ￼:" then the mentioned body; the typed space after the token stays
    assert body == f"🌐 {PLACEHOLDER}: {PLACEHOLDER} "
    assert [m.author for m in spans] == [CONTACT_UUIDS[KNOWN], KID_UUID]
    assert spans[0].start == body.index(PLACEHOLDER)  # sender span indexes its own body
    assert spans[1].start > spans[0].start
    _assert_placeholders_all_spanned(body, spans)
    assert KNOWN not in body and SENDER not in body


async def test_mirror_pill_ladder_spans_and_no_number_anywhere(tmp_path):
    agent, e = _agent_with_web()
    assert agent._web_mirror_rungs(GROUP, KNOWN)[0] == (PLACEHOLDER, CONTACT_UUIDS[KNOWN])
    files = []
    for n in ("a.png", "b.png"):
        f = tmp_path / n
        f.write_bytes(b"png")
        files.append(Attachment(id=n, content_type="image/png", filename=n, local_path=str(f), size=3))
    agent._signal_client.send_attachment = AsyncMock(return_value=1)
    agent.submit_web_message(KNOWN, GROUP, "hey @Known Kid", files, mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    sent_fields = [agent._signal_client.send_attachment.call_args_list[0].kwargs["caption"],
                   agent._signal_client.send_attachment.call_args_list[1].kwargs["caption"]]
    for field in sent_fields:
        assert PLACEHOLDER in field and KNOWN not in field  # number never in a sent text field
    spans0 = agent._signal_client.send_attachment.call_args_list[0].kwargs["mentions"]
    assert spans0[0].author == CONTACT_UUIDS[KNOWN]


async def test_mirror_name_ladder_from_chat_log_survives_bus_restart():
    agent, e = _agent_with_web()
    from src.nixclaw_agent.db.operations import chat_log_sender_name
    _seed_channel_name(e, GROUP, SENDER, "Neighbor")
    assert chat_log_sender_name(GROUP, SENDER) == "Neighbor"
    e.bus._names.clear()  # restart: the in-memory ChatBus name cache is gone
    agent.submit_web_message(SENDER, GROUP, "hi", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 Neighbor: hi"
    assert SENDER not in call.args[1] and PLACEHOLDER not in call.args[1]
    assert call.kwargs["mentions"] is None


async def test_mirror_no_label_sends_bare_prefix_once():
    agent, e = _agent_with_web()
    assert agent._web_mirror_rungs(GROUP, SENDER)[-1] == ("", None)
    agent.submit_web_message(SENDER, GROUP, "hi there", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    assert agent._signal_client.send_message.await_count == 1
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 hi there"
    assert PLACEHOLDER not in call.args[1] and SENDER not in call.args[1]
    assert call.kwargs["mentions"] is None


async def test_mirror_1to1_skips_the_ladder_and_never_logs_degradation(caplog):
    """1:1 renders web-mirror-direct only: pill/label composition is skipped
    board-wide, so even a SEEDED contact never burns the pill rung down the
    ladder (one send, direct body, no mentions, zero 'degraded' warnings)."""
    import logging
    agent, e = _agent_with_web()
    assert KNOWN in CONTACT_UUIDS  # seeded contact: the ladder WOULD pill
    with caplog.at_level(logging.WARNING):
        agent.submit_web_message(KNOWN, KNOWN, "hello", [], mentions=None)
        await asyncio.gather(*agent._web_tasks)
    assert agent._signal_client.send_message.await_count == 1
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 hello"  # direct render, untouched by any rung
    assert call.kwargs["mentions"] is None
    assert "degraded" not in caplog.text
    assert PLACEHOLDER not in call.args[1] and KNOWN not in call.args[1]


async def test_mirror_strips_user_pasted_placeholder():
    """Raw ￼ in the user's text never reaches the wire (nor does it threaten
    sender-span uniqueness)."""
    agent, e = _agent_with_web()
    agent.submit_web_message(KNOWN, GROUP, f"hi {PLACEHOLDER} there", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == f"🌐 {PLACEHOLDER}: hi  there"
    _assert_placeholders_all_spanned(call.args[1], call.kwargs["mentions"])


async def test_mirror_span_cap_degrades_the_pill_rather_than_dropping_the_sender_span():
    """10 entries x 2 occurrences = 20 recipient spans; pill would need 21 ->
    the len(spans)+1 <= MAX_MENTIONS rule degrades the RUNG (never silently
    drops the sender span, never sends 21)."""
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, KNOWN, "Alice")  # degraded rung resolves to the name
    tokens = [f"@A{c}" for c in "ABCDEFGHIJ"]
    text = "".join(f" {t} x {t}" for t in tokens)
    entries = [MentionEntry(author=f"uuid-{i}", token=t) for i, t in enumerate(tokens)]
    agent.submit_web_message(KNOWN, GROUP, text, [], mentions=entries)
    await asyncio.gather(*agent._web_tasks)
    assert agent._signal_client.send_message.await_count == 1
    call = agent._signal_client.send_message.call_args
    body, spans = call.args[1], call.kwargs["mentions"]
    assert len(spans) == 20  # not 21, and not 20-minus-the-sender either
    assert body.startswith("🌐 Alice:")  # pill rung was abandoned for the name rung
    _assert_placeholders_all_spanned(body, spans)


async def test_mirror_non_first_captions_carry_their_own_sender_span(tmp_path):
    agent, e = _agent_with_web()
    files = []
    for n in ("a.png", "b.png"):
        f = tmp_path / n
        f.write_bytes(b"png")
        files.append(Attachment(id=n, content_type="image/png", filename=n, local_path=str(f), size=3))
    agent._signal_client.send_attachment = AsyncMock(return_value=1)
    agent.submit_web_message(KNOWN, GROUP, "@Known Kid look", files, mentions=[_kid()])
    await asyncio.gather(*agent._web_tasks)
    calls = agent._signal_client.send_attachment.call_args_list
    cap0, sp0 = calls[0].kwargs["caption"], calls[0].kwargs["mentions"]
    cap1, sp1 = calls[1].kwargs["caption"], calls[1].kwargs["mentions"]
    # recipients only on the first caption; the sender pill on EVERY caption,
    # each body-indexed (closes the v3 tofu hole)
    assert [m.author for m in sp0] == [CONTACT_UUIDS[KNOWN], KID_UUID]
    assert cap1 == f"🌐 {PLACEHOLDER}:"
    assert [m.author for m in sp1] == [CONTACT_UUIDS[KNOWN]]
    assert sp1[0].start < cap1.index(":")
    _assert_placeholders_all_spanned(cap0, sp0)
    _assert_placeholders_all_spanned(cap1, sp1)


async def test_mirror_pill_with_zero_recipient_entries_sends_one_sender_span():
    """Round-3 W1: no recipient entries -> the sender span is still sent."""
    agent, e = _agent_with_web()
    agent.submit_web_message(KNOWN, GROUP, "ping", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    body, spans = call.args[1], call.kwargs["mentions"]
    assert body == f"🌐 {PLACEHOLDER}: ping"
    assert len(spans) == 1 and spans[0].author == CONTACT_UUIDS[KNOWN]
    assert spans[0].start == body.index(PLACEHOLDER) < body.index(":")
    _assert_placeholders_all_spanned(body, spans)


async def test_mirror_bot_number_sender_is_nolabel():
    """Round-3 C2: the bot never pills/labels itself in its own mirror."""
    agent, e = _agent_with_web()
    agent.submit_web_message(BOT, GROUP, "hi", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    assert agent._signal_client.send_message.await_count == 1
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 hi"
    assert PLACEHOLDER not in call.args[1] and BOT not in call.args[1]
    assert call.kwargs["mentions"] is None


async def test_mirror_label_resolver_crash_still_sends_the_mirror(monkeypatch):
    """Ladder-safe: a resolver explosion must not lose the mirror."""
    agent, e = _agent_with_web()

    def boom(*args, **kwargs):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(agent, "_web_mirror_rungs", boom)
    agent.submit_web_message(SENDER, GROUP, "hi", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    assert agent._signal_client.send_message.await_count == 1
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 hi"
    assert call.kwargs["mentions"] is None


async def test_mirror_broken_sender_template_degrades_and_sends_once(tmp_path, monkeypatch):
    """{{sender}} swallowed by a customized template -> pill composition
    raises -> pill->label->... ladder; every caption sent EXACTLY once, no
    unspanned tofu."""
    from src.nixclaw_agent import prompts as prompts_mod
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, KNOWN, "Alice")
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "user-messages.yaml").write_text('web_mirror_group: "🌐 {{text}}"\n')
    broken = prompts_mod.load_prompts(prompts_mod.DEFAULT_DIR, custom)
    monkeypatch.setattr(core.prompts, "current", lambda: broken)
    files = []
    for n in ("a.png", "b.png"):
        f = tmp_path / n
        f.write_bytes(b"png")
        files.append(Attachment(id=n, content_type="image/png", filename=n, local_path=str(f), size=3))
    agent._signal_client.send_attachment = AsyncMock(return_value=1)
    agent.submit_web_message(KNOWN, GROUP, "", files, mentions=None)
    await asyncio.gather(*agent._web_tasks)
    calls = agent._signal_client.send_attachment.call_args_list
    assert len(calls) == 2  # never a double-send per caption while degrading
    for c in calls:
        assert PLACEHOLDER not in c.kwargs["caption"]
        assert not c.kwargs["mentions"]


async def test_mirror_sender_label_db_rung_skips_number_shaped_name():
    """A number-shaped chat_log display name is no label -> next rung (nothing)."""
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, SENDER, "+491761234567")
    e.bus._names.clear()
    agent.submit_web_message(SENDER, GROUP, "hi", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 hi"
    assert "+491761234567" not in call.args[1]


async def test_chat_log_sender_name_unit():
    from src.nixclaw_agent.db.operations import chat_log_insert, chat_log_sender_name

    def ins(channel, sender, name, ts):
        chat_log_insert({"channel_id": channel, "sender": sender, "sender_name": name,
                         "origin": "web", "text": "x", "signal_ts": ts, "created_at": ts})

    ins(GROUP, "+400", "Old", 1)
    ins(GROUP, "+400", "New", 2)
    ins(GROUP, "+400", None, 3)  # NULL name does not shadow the newest non-null
    assert chat_log_sender_name(GROUP, "+400") == "New"
    ins(OTHER_GROUP, "+400", "Elsewhere", 4)
    assert chat_log_sender_name(OTHER_GROUP, "+400") == "Elsewhere"
    assert chat_log_sender_name(GROUP, "+400") == "New"  # per-channel
    assert chat_log_sender_name(GROUP, "+999") is None


async def test_mirror_works_without_a_contact_book():
    """contacts absent (DI default) -> the pill rung is skipped, name/nolabel
    rungs still work; the mirror still goes out."""
    e = make_env()
    agent = build_mock_agent_loop()  # contacts=None
    agent._auth_manager = e.auth_manager
    agent.attach_web(e.bus, e.web_auth)
    agent._dispatch_or_queue = MagicMock()
    _seed_channel_name(e, GROUP, SENDER, "Neighbor")
    e.bus._names.clear()
    agent.submit_web_message(SENDER, GROUP, "hello", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    call = agent._signal_client.send_message.call_args
    assert call.args[1] == "🌐 Neighbor: hello"
    agent.submit_web_message(SENDER + "8", GROUP, "yo", [], mentions=None)
    await asyncio.gather(*agent._web_tasks)
    assert agent._signal_client.send_message.call_args.args[1] == "🌐 yo"


async def test_voice_mirror_and_static_prompts_never_carry_a_number():
    """Audit confirmations (plan §Audit): voice mirror body number-free; the
    harness templates for mirror/voice/cron surfaces address nobody by number."""
    from src.nixclaw_agent import prompts as prompts_mod
    agent, e = _agent_with_web()
    agent.submit_voice_message(SENDER, "hello")
    await asyncio.gather(*agent._web_tasks)
    body = agent._signal_client.send_message.call_args.args[1]
    assert body == "🎙️ hello" and SENDER not in body
    specs = {s.name: s for s in prompts_mod.REGISTRY}
    assert specs["voice-mirror"].placeholders == frozenset({"text"})
    assert specs["web-mirror-direct"].placeholders == frozenset({"text"})
    assert specs["web-mirror-group-nolabel"].placeholders == frozenset({"text"})
    assert specs["consent-rejected-nolabel"].placeholders == frozenset({"question"})
    for s in prompts_mod.REGISTRY:
        assert not (set(s.placeholders) & {"number", "phone", "phone_number", "uuid", "caller", "voter_number"})


# --- consent render chain (same-family fix) ------------------------------------------

CONSENT_VOTER = PRIV  # "+000000": PRIVILEGED under the harness AuthManager


async def _consent_vote(agent, *, approved: bool, voter_name, channel_id: str = GROUP,
                        result: "ToolResult | None" = None):
    """Drive one chat-lane consent poll end to end and return (result, approved).

    `PollVote.voter_name` is set BEFORE on_vote consumes it (plan Tests 10:
    not ConsentDecision after)."""
    from src.nixclaw_agent.types import ConsentRequest, ToolCall, ToolResult, UserTier
    agent._signal_client.create_poll = AsyncMock(return_value=4242)
    agent._signal_client.delete_message = AsyncMock(return_value=True)
    agent._signal_client.send_message = AsyncMock(return_value=1)
    agent._tool_registry.execute_tool = AsyncMock(
        return_value=result or ToolResult(name="bash", success=True, output="done", error=None))
    tc = ToolCall(id="c1", name="bash", arguments={"cmd": "print.sh"})
    request = ConsentRequest(question="Print?", user_text="out", command="print.sh",
                             arguments={"cmd": "print.sh"})
    task = asyncio.create_task(agent._pause_for_consent(
        context=SimpleNamespace(turn_output=None), channel_id=channel_id,
        reply_destination=channel_id, tier=UserTier.PRIVILEGED, tc=tc, request=request,
        held_messages=[]))
    for _ in range(400):
        if agent._consent._pending:
            break
        await asyncio.sleep(0.005)
    assert agent._consent._pending, "poll was never posted"
    v = vote(channel_id, CONSENT_VOTER, 4242)
    v.voter_name = voter_name
    v.option_indexes = [0 if approved else 1]  # 0 = Yes, 1 = No (consent APPROVE/REJECT indexes)
    await agent._consent.on_vote(v)
    return await asyncio.wait_for(task, 5)


async def test_consent_approved_uses_chat_log_name_and_keys_on_voter_number():
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, CONSENT_VOTER, "Pat")
    result, approved = await _consent_vote(agent, approved=True, voter_name="Bob")
    assert approved
    assert result.output == "done\n(approved by Pat)"  # ladder keys on voter_number first
    assert CONSENT_VOTER not in (result.output or "")


async def test_consent_approved_falls_back_to_webhook_name():
    agent, e = _agent_with_web()
    result, approved = await _consent_vote(agent, approved=True, voter_name="Bob")
    assert result.output == "done\n(approved by Bob)"
    assert CONSENT_VOTER not in (result.output or "")


@pytest.mark.parametrize("unsafe", ["   ", "\t\n", "+491761234567", "￼"])
async def test_consent_approved_skips_the_suffix_for_unsafe_names(unsafe):
    """No dangling \n, no "(approved by )" (round-2 Critical 2b)."""
    agent, e = _agent_with_web()
    result, approved = await _consent_vote(agent, approved=True, voter_name=unsafe)
    assert approved
    assert result.output == "done"
    assert CONSENT_VOTER not in (result.output or "")


async def test_consent_approved_empty_error_fold_appends_suffix_cleanly():
    """Approved re-run that FAILED with error=None (review round-2 suggestion):
    the suffix must land verbatim, no leading blank line."""
    from src.nixclaw_agent.types import ToolResult
    agent, e = _agent_with_web()
    _seed_channel_name(e, GROUP, CONSENT_VOTER, "Pat")
    failed = ToolResult(name="bash", success=False, output=None, error=None)
    result, approved = await _consent_vote(agent, approved=True, voter_name="Bob",
                                           result=failed)
    assert approved
    assert result.error == "(approved by Pat)"  # ladder (DB rung) wins over webhook name


async def test_consent_rejected_with_a_name_is_unchanged():
    agent, e = _agent_with_web()
    result, approved = await _consent_vote(agent, approved=False, voter_name="Bob")
    assert not approved and not result.success
    assert result.error == 'NOT RUN: Bob rejected "Print?" in the approval poll.'


async def test_consent_rejected_without_a_name_uses_the_nolabel_key():
    """New key, subjectless text, non-empty error, never the voter number."""
    agent, e = _agent_with_web()
    result, _ = await _consent_vote(agent, approved=False, voter_name=None)
    assert not result.success and result.error
    assert CONSENT_VOTER not in result.error
    assert "Print?" in result.error
    assert not result.error.startswith("NOT RUN:  ")  # no orphaned subject slot


@pytest.mark.parametrize("unsafe", ["   ", "+491761234567"])
async def test_consent_rejected_sanitizes_the_webhook_name_leg(unsafe):
    agent, e = _agent_with_web()
    result, _ = await _consent_vote(agent, approved=False, voter_name=unsafe)
    assert result.error
    assert CONSENT_VOTER not in result.error and unsafe not in result.error
