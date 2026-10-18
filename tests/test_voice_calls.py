"""Voice calls: utterance segmentation, JSON-RPC client, call policy.

Offline: no PulseAudio, no signal-cli, no webrtcvad (sessions are stubbed).
"""

import asyncio
import json

import pytest

from src.nixclaw_agent.config import VoiceCallsConfig
from src.nixclaw_agent.types import UserTier
from src.nixclaw_agent.voice import manager as manager_mod
from src.nixclaw_agent.voice.audio import pcm_to_wav, wav_to_pcm
from src.nixclaw_agent.voice.rpc import RpcError, SignalCallRpc
from src.nixclaw_agent.voice.vad import FRAME_BYTES, FRAME_MS, SpeechStarted, Utterance, UtteranceSegmenter

FRAME = b"\x01\x00" * (FRAME_BYTES // 2)


def feed(seg, pattern):
    """pattern: list of (is_speech, n_frames)."""
    events = []
    for is_speech, n in pattern:
        for _ in range(n):
            events += seg.push(FRAME, is_speech)
    return events


# --- segmenter -----------------------------------------------------------------

class TestSegmenter:
    def test_utterance_after_end_silence(self):
        seg = UtteranceSegmenter(start_speech_ms=60, end_silence_ms=700, min_speech_ms=250)
        events = feed(seg, [(False, 10), (True, 25), (False, 35)])
        assert isinstance(events[0], SpeechStarted)
        assert isinstance(events[1], Utterance)
        assert events[1].speech_ms == 25 * 20
        # pre-roll (200 ms = 10 frames, incl. the 3 start frames) + speech + 200 ms tail
        assert len(events[1].pcm) == (10 + 22 + 10) * FRAME_BYTES
        assert not seg.speaking

    def test_no_utterance_before_end_silence(self):
        seg = UtteranceSegmenter(end_silence_ms=700)
        events = feed(seg, [(True, 25), (False, 30)])
        assert [type(e) for e in events] == [SpeechStarted]
        assert seg.speaking

    def test_short_blip_dropped(self):
        seg = UtteranceSegmenter(start_speech_ms=60, min_speech_ms=250)
        events = feed(seg, [(True, 5), (False, 40)])
        assert [type(e) for e in events] == [SpeechStarted]

    def test_isolated_speech_frames_do_not_start(self):
        seg = UtteranceSegmenter(start_speech_ms=60)
        events = feed(seg, [(True, 2), (False, 1)] * 10)
        assert events == []

    def test_pause_inside_utterance_kept(self):
        seg = UtteranceSegmenter(end_silence_ms=700, min_speech_ms=250)
        events = feed(seg, [(True, 15), (False, 20), (True, 15), (False, 35)])
        utts = [e for e in events if isinstance(e, Utterance)]
        assert len(utts) == 1 and utts[0].speech_ms == 30 * 20

    def test_max_utterance_forces_cut(self):
        seg = UtteranceSegmenter(max_utterance_ms=1000, min_speech_ms=100)
        events = feed(seg, [(True, 60)])
        assert sum(isinstance(e, Utterance) for e in events) >= 1


def test_wav_roundtrip():
    pcm = b"\x10\x00\xf0\xff" * 100
    out, rate, channels = wav_to_pcm(pcm_to_wav(pcm, 24000))
    assert (out, rate, channels) == (pcm, 24000, 1)
    with pytest.raises(ValueError):
        wav_to_pcm(b"not a wav")


# --- JSON-RPC client -------------------------------------------------------------

class FakeDaemon:
    """Minimal signal-cli JSON-RPC daemon: answers calls, can push events."""

    def __init__(self):
        self.requests = []
        self.writers = []
        self.server = None
        self.errors = {}

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        while line := await reader.readline():
            req = json.loads(line)
            self.requests.append(req)
            if req["method"] in self.errors:
                resp = {"jsonrpc": "2.0", "id": req["id"], "error": {"message": self.errors[req["method"]]}}
            else:
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"ok": req["method"]}}
            # a "receive" notification in between must be ignored
            writer.write(b'{"jsonrpc":"2.0","method":"receive","params":{"x":1}}\n')
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()

    async def push_event(self, result):
        msg = {"jsonrpc": "2.0", "method": "callEvent", "params": {"subscription": 0, "result": result}}
        for w in self.writers:
            w.write(json.dumps(msg).encode() + b"\n")
            await w.drain()


@pytest.mark.asyncio
async def test_rpc_subscribes_calls_and_dispatches_events():
    daemon = FakeDaemon()
    port = await daemon.start()
    events = []

    async def on_event(ev):
        events.append(ev)

    rpc = SignalCallRpc("127.0.0.1", port, "+000000", on_event)
    task = asyncio.create_task(rpc.run())
    await asyncio.wait_for(rpc.connected.wait(), 5)
    assert daemon.requests[0]["method"] == "subscribeCallEvents"
    assert daemon.requests[0]["params"]["account"] == "+000000"

    assert await rpc.accept(42) == {"ok": "acceptCall"}
    assert daemon.requests[-1]["params"] == {"account": "+000000", "callId": 42}

    daemon.errors["hangupCall"] = "no such call"
    with pytest.raises(RpcError, match="no such call"):
        await rpc.hangup(7)

    await daemon.push_event({"callId": 1, "state": "RINGING_INCOMING"})
    await daemon.push_event({"callId": 1, "state": "CONNECTED"})
    for _ in range(50):
        if len(events) == 2:
            break
        await asyncio.sleep(0.02)
    assert [e["state"] for e in events] == ["RINGING_INCOMING", "CONNECTED"]

    rpc.stop()
    task.cancel()
    daemon.server.close()


# --- call policy -----------------------------------------------------------------

class FakeAuth:
    def __init__(self, tiers):
        self.tiers = tiers

    def get_user_tier(self, number):
        return self.tiers.get(number, UserTier.UNKNOWN)


class FakeRpc:
    def __init__(self):
        self.calls = []

    async def accept(self, call_id):
        self.calls.append(("accept", call_id))

    async def reject(self, call_id):
        self.calls.append(("reject", call_id))

    async def hangup(self, call_id):
        self.calls.append(("hangup", call_id))


class StubSession:
    instances = []

    def __init__(self, info, cfg, handler):
        self.info = info
        self.channel_id = info.number
        self.started = asyncio.Event()
        StubSession.instances.append(self)

    async def run(self):
        self.started.set()
        await asyncio.Event().wait()


class RecordingHandler:
    def __init__(self):
        self.ended = []

    async def on_call_started(self, session):
        pass

    async def on_utterance(self, session, text):
        pass

    async def on_call_ended(self, session):
        self.ended.append(session.info.call_id)


@pytest.fixture
def mgr(monkeypatch):
    StubSession.instances = []
    monkeypatch.setattr(manager_mod, "CallSession", StubSession)
    unloaded = []

    async def fake_unload(devices, pulse_server):
        unloaded.append(devices)
        return 0

    monkeypatch.setattr(manager_mod, "unload_call_modules", fake_unload)
    auth = FakeAuth({"+000000": UserTier.PRIVILEGED, "+000001": UserTier.KNOWN})
    m = manager_mod.CallManager(VoiceCallsConfig(enabled=True), auth, RecordingHandler())
    m.rpc = FakeRpc()
    m.unloaded = unloaded
    return m


def ring(call_id, number):
    return {"callId": call_id, "state": "RINGING_INCOMING", "number": number,
            "inputDeviceName": f"signal_input_{call_id}", "outputDeviceName": f"signal_output_{call_id}"}


@pytest.mark.asyncio
async def test_known_caller_accepted_once(mgr):
    await mgr._on_call_event(ring(1, "+000001"))
    await mgr._on_call_event(ring(1, "+000001"))  # repeated by signal-cli
    assert mgr.rpc.calls == [("accept", 1)]


@pytest.mark.asyncio
async def test_unknown_caller_rejected(mgr):
    await mgr._on_call_event(ring(2, "+000002"))
    await mgr._on_call_event({**ring(3, ""), "number": None})
    assert mgr.rpc.calls == [("reject", 2), ("reject", 3)]


@pytest.mark.asyncio
async def test_privileged_only_config(mgr):
    mgr.cfg = VoiceCallsConfig(enabled=True, allowed_tiers=["PRIVILEGED"])
    await mgr._on_call_event(ring(4, "+000001"))
    await mgr._on_call_event(ring(5, "+000000"))
    assert mgr.rpc.calls == [("reject", 4), ("accept", 5)]


@pytest.mark.asyncio
async def test_second_call_rejected_while_busy(mgr):
    await mgr._on_call_event(ring(1, "+000000"))
    await mgr._on_call_event(ring(2, "+000001"))
    assert mgr.rpc.calls == [("accept", 1), ("reject", 2)]


@pytest.mark.asyncio
async def test_connected_starts_session_and_ended_cleans_up(mgr):
    await mgr._on_call_event(ring(1, "+000000"))
    await mgr._on_call_event({**ring(1, "+000000"), "state": "CONNECTED"})
    assert len(StubSession.instances) == 1
    await asyncio.wait_for(StubSession.instances[0].started.wait(), 2)

    await mgr._on_call_event({**ring(1, "+000000"), "state": "ENDED", "reason": "remote_hangup"})
    assert mgr._sessions == {} and mgr._calls == {}
    assert mgr.handler.ended == [1]
    assert mgr.unloaded == [["signal_input_1", "signal_output_1"]]
    # a new call is accepted again afterwards
    await mgr._on_call_event(ring(9, "+000001"))
    assert mgr.rpc.calls[-1] == ("accept", 9)


@pytest.mark.asyncio
async def test_connected_for_unaccepted_call_ignored(mgr):
    await mgr._on_call_event({**ring(5, "+000000"), "state": "CONNECTED"})
    assert StubSession.instances == []


def test_config_validation():
    with pytest.raises(ValueError):
        VoiceCallsConfig(allowed_tiers=["UNKNOWN"])
    with pytest.raises(ValueError):
        VoiceCallsConfig(mode="robot")
    with pytest.raises(ValueError):
        VoiceCallsConfig(end_silence_ms=0)
    cfg = VoiceCallsConfig.from_dict({"enabled": True, "allowed_tiers": ["PRIVILEGED"]})
    assert cfg.enabled and cfg.allowed_tiers == ["PRIVILEGED"] and cfg.jsonrpc_port == 6002
    assert cfg.tts_sampling == {}
    assert VoiceCallsConfig(tts_sampling={"temperature": 0.6, "top_k": 20}).tts_sampling["top_k"] == 20
    for bad in ({"tts_sampling": {"temp": 0.6}}, {"tts_sampling": {"top_k": "20"}},
                {"tts_sampling": [0.6]}):
        with pytest.raises(ValueError):
            VoiceCallsConfig(**bad)


def _tone(amplitude, seconds=0.5, rate=24000):
    import array, math as m
    return array.array("h", (int(amplitude * m.sin(2 * m.pi * 220 * i / rate))
                             for i in range(int(seconds * rate)))).tobytes()


@pytest.mark.asyncio
async def test_call_speech_sends_sampling(monkeypatch):
    sent = {}

    async def fake_synth(self, text, *, voice=None, speed=None, response_format=None, extra=None):
        sent.update(extra or {})
        return pcm_to_wav(_tone(2000), 24000), "wav"

    monkeypatch.setattr(manager_mod.KokoroTTSClient, "synthesize", fake_synth)
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    cfg = VoiceCallsConfig(enabled=True, tts_sampling={"temperature": 0.6, "top_k": 20})
    session = manager_mod.CallSession(info, cfg, RecordingHandler())
    if manager_mod.config.tts.engine_for(session.language) is None:
        pytest.skip("no TTS engine configured for the default language")
    monkeypatch.setattr(manager_mod.config.tts, "enabled", True)
    assert await session._synthesize("Hallo.") is not None
    assert sent == {"temperature": 0.6, "top_k": 20}


# --- text helpers ----------------------------------------------------------------

from src.nixclaw_agent.voice.text import is_backchannel, is_echo, is_filler, is_goodbye, split_sentences  # noqa: E402


def test_is_filler():
    for t in ("hm", "Hm.", "ähm, äh", "Mhm!", ".", "", " ... ", "Die.", "Ich.", "Punkt.", "in", "In.", "mich.", "auf"):
        assert is_filler(t), t
    for t in ("ja", "nein", "hm, ja", "Was sagtest du gerade", "42", "Hamburg.", "Stopp!", "Ich auch.", "Hör auf.", "In Paderborn"):
        assert not is_filler(t), t


def test_split_sentences():
    text = ("Okay. Das Wetter morgen wird bewölkt mit leichtem Regen. "
            "Die Temperaturen liegen zwischen neun und dreizehn Grad! Gut?")
    assert split_sentences(text) == [
        "Okay. Das Wetter morgen wird bewölkt mit leichtem Regen.",
        "Die Temperaturen liegen zwischen neun und dreizehn Grad! Gut?",
    ]
    assert split_sentences("Hallo.") == ["Hallo."]
    assert split_sentences("  ") == []
    assert split_sentences("Ein Satz ohne Punkt am Ende") == ["Ein Satz ohne Punkt am Ende"]


# --- agent integration (core.py, llm.py, tool_markup.py) -------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from conftest import build_mock_agent_loop  # noqa: E402
from src.nixclaw_agent import core  # noqa: E402
from src.nixclaw_agent.tool_markup import chat_with_markup_guard  # noqa: E402
from src.nixclaw_agent.types import UserTier as _Tier  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_voice_message_dispatched_mirrored_and_deduped_per_utterance():
    agent = build_mock_agent_loop()
    agent._dispatch_or_queue = MagicMock()
    agent.submit_voice_message("+000000", "Wie wird das Wetter?")
    agent.submit_voice_message("+000000", "Wie wird das Wetter?")
    events = [c.args[1] for c in agent._dispatch_or_queue.call_args_list]
    assert [c.args[0] for c in agent._dispatch_or_queue.call_args_list] == ["+000000", "+000000"]
    assert all(e.origin == "voice" and e.group_id is None for e in events)
    assert events[0].voice_id != events[1].voice_id
    await asyncio.gather(*agent._web_tasks)
    agent._signal_client.send_message.assert_awaited_with("+000000", "🎙️ Wie wird das Wetter?", publish=False)

    seen = []
    agent._db_ops.is_message_processed = lambda mid, cid: seen.append(mid) or False
    await agent._preprocess_incoming(events[0], "+000000", "+000000")
    assert seen == [f"voice:{events[0].voice_id}"]


@pytest.mark.asyncio
async def test_turn_text_spoken_during_call_only_in_interactive_lane():
    agent = build_mock_agent_loop()
    spoken = []
    agent.register_voice_speaker("+000000", spoken.append)
    agent._send_with_retry = AsyncMock(return_value=None)
    chat = SimpleNamespace(lane="interactive", turn_output=None)
    cron = SimpleNamespace(lane="background", turn_output=None)
    await agent._send_turn_text(chat, "+000000", "Sonnig.", _Tier.PRIVILEGED)
    await agent._send_turn_text(cron, "+000000", "Cron output", _Tier.PRIVILEGED)
    await agent._send_turn_text(chat, "+000001", "Andere Person", _Tier.KNOWN)
    assert spoken == ["Sonnig."]
    assert agent._send_with_retry.await_count == 3  # text is always sent too
    agent.unregister_voice_speaker("+000000")
    await agent._send_turn_text(chat, "+000000", "Nach dem Anruf", _Tier.PRIVILEGED)
    assert spoken == ["Sonnig."]


def test_voice_note_appended_to_last_user_message_only():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "alt"},
            {"role": "assistant", "content": "a"}, {"role": "user", "content": "Wetter?"}]
    core._append_to_last_user_message(msgs, "[Phone call]")
    assert msgs[3]["content"] == "Wetter?\n\n[Phone call]"
    assert msgs[1]["content"] == "alt" and msgs[0]["content"] == "sys"
    multimodal = [{"role": "user", "content": [{"type": "text", "text": "Bild"}]}]
    core._append_to_last_user_message(multimodal, "[Phone call]")
    assert multimodal[0]["content"][-1] == {"type": "text", "text": "[Phone call]"}


def test_to_speakable_drops_bracketed_tags():
    from src.nixclaw_agent.voice.text import to_speakable
    # from a call: the model copied tags from its history; TTS read the uuid out
    assert to_speakable("[Voice message: 00b7d2ae-0c2b-4ebc-9a99-f8520625a971.ogg] Also, das klingt gut.") \
        == "Also, das klingt gut."
    assert to_speakable("[End of Signal voice call. The call has ended.]") == ""
    assert to_speakable("Siehe [Tagesschau](https://tagesschau.de).") == "Siehe Tagesschau."


def test_to_speakable_strips_markdown_emoji_urls():
    from src.nixclaw_agent.voice.text import to_speakable
    text = ("Kurzer Recap:\n\n**1. Sprachtest** 🎙️\n- Englisch → geantwortet\n"
            "- Details: https://example.com/x\nMehr unter [Doku](https://x.y).")
    assert to_speakable(text) == ("Kurzer Recap:\nSprachtest\nEnglisch, geantwortet\n"
                                  "Details:\nMehr unter Doku.")


class _StrictLLM:
    """chat() without a thinking parameter, like the test fakes in the suite."""

    def __init__(self):
        self.calls = []

    async def chat(self, messages, tools, channel_id=None, **kwargs):
        self.calls.append(kwargs)
        return "ok", []


@pytest.mark.asyncio
async def test_markup_guard_passes_thinking_only_when_set():
    llm = _StrictLLM()
    await chat_with_markup_guard(llm, [], [], "+000000", source="chat")
    await chat_with_markup_guard(llm, [], [], "+000000", source="chat", thinking=False)
    assert llm.calls == [{}, {"thinking": False}]


@pytest.mark.asyncio
async def test_agent_handler_registers_speaker_and_submits():
    agent = MagicMock()
    handler = manager_mod.AgentHandler(agent)
    session = SimpleNamespace(channel_id="+000000", enqueue_speech=MagicMock(),
                              info=SimpleNamespace(number="+000000"))
    await handler.on_call_started(session)
    agent.register_voice_speaker.assert_called_once_with("+000000", session.enqueue_speech)
    session.enqueue_speech.assert_called_once()  # greeting
    await handler.on_utterance(session, "Hallo")
    agent.submit_voice_message.assert_called_once_with("+000000", "Hallo")
    await handler.on_call_ended(session)
    agent.unregister_voice_speaker.assert_called_once_with("+000000")


@pytest.mark.asyncio
async def test_memory_classifier_and_summarization_paused_during_call(monkeypatch):
    agent = build_mock_agent_loop()
    agent._get_memory_manager = MagicMock()
    agent.register_voice_speaker("+000000", lambda text: None)
    ev = SimpleNamespace(message="hi", source="+000000")
    await agent._check_memory_importance(ev, "hallo", "+000000")
    agent._get_memory_manager.assert_not_called()

    scheduled = []
    monkeypatch.setattr(agent, "_run_summarization", lambda cid: scheduled.append(cid) or asyncio.sleep(0))
    agent._summarization_due["+000000"] = True
    agent._maybe_schedule_summarization("+000000")
    assert scheduled == []
    agent.unregister_voice_speaker("+000000")
    agent._maybe_schedule_summarization("+000000")
    await asyncio.sleep(0)
    assert scheduled == ["+000000"]


def test_hidden_tools_during_call():
    assert "text_to_speech" in core.VOICE_CALL_HIDDEN_TOOLS


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_voice_turn_refuses_text_to_speech_and_disables_thinking():
    from src.nixclaw_agent.types import Event, ToolCall, ToolResult
    agent = build_mock_agent_loop()
    spoken = []
    agent.register_voice_speaker("+000000", spoken.append)
    agent._tool_registry.get_tools_for_tier = MagicMock(return_value=[
        SimpleNamespace(name="text_to_speech"), SimpleNamespace(name="web_search")])
    agent._tool_registry.execute_tool = AsyncMock(
        return_value=ToolResult(name="web_search", success=True, output="sonnig", error=None))
    agent._llm_client.chat = AsyncMock(side_effect=[
        ("", [ToolCall(id="1", name="text_to_speech", arguments={"text": "x"})]),
        ("Es wird sonnig.", []),
    ])
    event = Event(source="+000000", destination="+491234567890", message="Wetter?",
                  timestamp=1700000010, group_id=None, attachments=[], mentions=[], origin="voice")
    await agent.process_message(event)

    agent._tool_registry.execute_tool.assert_not_awaited()  # refused, never executed
    first = agent._llm_client.chat.await_args_list[0]
    assert first.kwargs.get("thinking") is False
    assert [t.name for t in first.args[1]] == ["web_search"]  # hidden from the list
    second_msgs = agent._llm_client.chat.await_args_list[1].args[0]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert "voice call" in tool_msgs[0]["content"]
    assert spoken == ["Es wird sonnig."]


# --- speech pipeline / barge-in --------------------------------------------------

from src.nixclaw_agent.voice.speech import SpeechPipeline  # noqa: E402


class FakePlayer:
    def __init__(self, play_seconds=0.05):
        self.played = []
        self.play_seconds = play_seconds
        self._playing = False
        self._stop = asyncio.Event()

    @property
    def playing(self):
        return self._playing

    async def play(self, pcm, rate, channels=1):
        self._playing = True
        self._stop.clear()
        try:
            await asyncio.wait_for(self._stop.wait(), self.play_seconds)
            return False  # interrupted
        except asyncio.TimeoutError:
            self.played.append(pcm.decode())
            return True
        finally:
            self._playing = False

    async def interrupt(self):
        self._stop.set()


def make_synth(delay=0.01, log=None):
    async def synth(text):
        if log is not None:
            log.append(("start", text))
        await asyncio.sleep(delay)
        return text.encode(), 24000, 1
    return synth


@pytest.mark.asyncio
async def test_pipeline_plays_in_order_and_synthesizes_ahead():
    log = []
    player = FakePlayer(play_seconds=0.1)
    sp = SpeechPipeline(make_synth(0.01, log), player)
    task = asyncio.create_task(sp.run())
    sp.say("Erster Satz ist hier fertig. Zweiter Satz kommt gleich danach.")
    sp.say("Und eine neue Nachricht danach.")
    await asyncio.sleep(0.05)  # first clip is playing
    started = [t for kind, t in log if kind == "start"]
    assert len(started) >= 2  # second sentence synthesized while the first plays
    await asyncio.wait_for(sp.wait_idle(0.01), 2)
    assert player.played == ["Erster Satz ist hier fertig.", "Zweiter Satz kommt gleich danach.",
                             "Und eine neue Nachricht danach."]
    task.cancel()


@pytest.mark.asyncio
async def test_interrupt_drops_pending_and_in_flight_speech():
    player = FakePlayer(play_seconds=0.2)
    sp = SpeechPipeline(make_synth(0.05), player)
    task = asyncio.create_task(sp.run())
    sp.say("Das ist der erste lange Satz. Das ist der zweite lange Satz. Das ist der dritte lange Satz.")
    await asyncio.sleep(0.1)  # first plays, second in synthesis
    assert sp.busy
    await sp.interrupt()
    await asyncio.sleep(0.3)
    assert player.played == [] and not sp.busy
    sp.say("Neue Antwort nach dem Unterbrechen.")
    await asyncio.wait_for(sp.wait_idle(0.01), 2)
    assert player.played == ["Neue Antwort nach dem Unterbrechen."]
    task.cancel()


def test_short_first_cuts_a_short_head_off_the_first_chunk():
    def first(t):
        return split_sentences(t, short_first=True)
    # a sentence end within the limit beats a later comma
    assert first("Ryan ist jetzt an. Hören wir mal, wie dir diese Stimme gefällt.") == [
        "Ryan ist jetzt an.", "Hören wir mal, wie dir diese Stimme gefällt."]
    # otherwise the latest clause mark within the limit
    assert first("Jetzt ist in Buxtehude Sonne bei sieben Grad, windig wird es heute nicht.")[0] == \
        "Jetzt ist in Buxtehude Sonne bei sieben Grad,"
    # no mark: a long chunk is cut at a space, only the first chunk is affected
    t = "Ich schaue gleich mal nach den aktuellen Nachrichten von heute Vormittag. " + "Zweiter Satz " * 8
    chunks = first(t)
    assert len(chunks[0]) <= 40 and " ".join(chunks).split() == t.split()
    assert chunks[2:] == split_sentences(t)[1:]
    # short replies and a rest under MIN_SENTENCE_CHARS stay whole
    for t in ("Kein Problem, zurück zu Vivian.", "Das Rezept für Grießbrei ist ganz einfach, wirklich."):
        assert first(t) == [t]
    assert split_sentences("Ryan ist jetzt an. Hören wir mal, wie dir diese Stimme gefällt.") == [
        "Ryan ist jetzt an. Hören wir mal, wie dir diese Stimme gefällt."]


@pytest.mark.asyncio
async def test_pipeline_shortens_only_speech_that_starts_from_silence():
    player = FakePlayer(play_seconds=0.1)
    sp = SpeechPipeline(make_synth(0.01), player)
    task = asyncio.create_task(sp.run())
    reply = "Ryan ist jetzt an. Hören wir mal, wie dir diese Stimme gefällt."
    sp.say(reply)
    sp.say(reply)  # queued behind the first: no cut
    await asyncio.wait_for(sp.wait_idle(0.01), 2)
    assert player.played == ["Ryan ist jetzt an.", "Hören wir mal, wie dir diese Stimme gefällt.", reply]
    task.cancel()


def test_segmenter_silence_ms():
    seg = UtteranceSegmenter(start_speech_ms=60, end_silence_ms=200, min_speech_ms=60)
    assert seg.silence_ms == 0
    for _ in range(5):
        seg.push(FRAME, True)
    for _ in range(3):
        seg.push(FRAME, False)
    assert seg.silence_ms == 3 * FRAME_MS
    seg.push(FRAME, True)
    assert seg.silence_ms == 0


def test_segmenter_current_speech_ms():
    seg = UtteranceSegmenter(start_speech_ms=60, end_silence_ms=700)
    assert seg.current_speech_ms == 0
    feed(seg, [(True, 30)])
    assert seg.current_speech_ms == 30 * 20
    feed(seg, [(False, 40)])
    assert seg.current_speech_ms == 0


class _ScriptedVad:
    def __init__(self, flags):
        self.flags = iter(flags)

    def is_speech(self, frame):
        return next(self.flags, False)


async def _run_capture(monkeypatch, flags, busy=True, heard="Halt, warte mal kurz", spoken=()):
    monkeypatch.setattr(manager_mod, "WebRtcVad", lambda aggr: _ScriptedVad(flags))
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    session = manager_mod.CallSession(info, VoiceCallsConfig(enabled=True, barge_in_ms=500),
                                      RecordingHandler())

    async def frames():
        for _ in flags:
            yield FRAME

    async def transcribe(pcm, label="STT"):
        return heard

    session._capture.frames = frames
    session.transcribe = transcribe
    interrupts = []

    async def interrupt():
        interrupts.append(1)

    session.speech = SimpleNamespace(busy=busy, interrupt=interrupt, recently_spoken=lambda: list(spoken))
    session.player = SimpleNamespace(playing=busy)
    await session._capture_loop()
    if session._barge_task:
        await session._barge_task
    return interrupts


@pytest.mark.asyncio
async def test_barge_in_confirmed_by_transcript(monkeypatch):
    speech = [True] * 30 + [False] * 40
    # 0.6 s of real words while playing -> one interrupt
    assert await _run_capture(monkeypatch, speech) == [1]
    # 0.3 s backchannel -> no check, no interrupt
    assert await _run_capture(monkeypatch, [True] * 15 + [False] * 40) == []
    # nothing audible (reply only queued / in synthesis) -> no interrupt
    assert await _run_capture(monkeypatch, speech, busy=False) == []
    # speakerphone echo of our own sentence -> ignored
    assert await _run_capture(monkeypatch, speech, heard="das Wetter wird heute sonnig",
                              spoken=["Das Wetter wird heute sonnig und warm."]) == []
    # noise / filler -> ignored
    assert await _run_capture(monkeypatch, speech, heard="Die.") == []
    assert await _run_capture(monkeypatch, speech, heard="hm") == []


def test_is_echo():
    from src.nixclaw_agent.voice.text import is_echo
    spoken = ["Morgen wird es bewölkt mit leichtem Regen.", "Die Temperaturen liegen bei zwölf Grad."]
    assert is_echo("mit leichtem Regen die Temperaturen", spoken)
    assert not is_echo("Halt, ich wollte was anderes wissen", spoken)
    assert not is_echo("Regen", spoken)  # single word: can't tell


# --- end_call / idle hangup ------------------------------------------------------

from src.nixclaw_agent.tools import voice_call as voice_call_tool  # noqa: E402


@pytest.mark.asyncio
async def test_end_call_tool_uses_callback():
    status = {"+000000": "ok", "+000002": "no_goodbye"}
    voice_call_tool.set_end_call_callback(lambda cid: status.get(cid, "no_call"))
    try:
        ok = await voice_call_tool.end_call(channel_id="+000000")
        no = await voice_call_tool.end_call(channel_id="+000001")
        refused = await voice_call_tool.end_call(channel_id="+000002")
    finally:
        voice_call_tool.set_end_call_callback(None)
    assert ok.success and not no.success and "no active voice call" in no.error
    assert not refused.success and "not said goodbye" in refused.error


def test_is_goodbye():
    for t in ("Tschüss!", "Okay, danke, tschüs.", "Bis dann.", "Leg bitte auf.", "Du kannst auflegen.",
              "Okay, dann können wir legen. Nein.", "Das war's.", "Mach's gut", "Ciao",
              "Beende den Anruf", "Den Anruf beenden, bitte.", "Bye", "OK, see you", "Gute Nacht"):
        assert is_goodbye(t), t
    # from the 10:47 call: end_call was called after these
    for t in ("Nein, dat Lip springe.", "ja die SCT spendet ein bisschen. Der Ort heißt Bad Libsprenge.",
              "Ah.", "Okay.", "Hör auf.", "Warte mal", "Beende den Cronjob", "Was ist das?"):
        assert not is_goodbye(t), t


def test_session_refuses_end_call_without_goodbye():
    session = _watch_session()
    session.last_caller_text = "Nein, dat Lip springe."
    assert session.request_end() is False and not session._end_requested
    session.last_caller_text = "Danke, tschüss!"
    assert session.request_end() is True and session._end_requested


def _watch_session(cfg=None):
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    session = manager_mod.CallSession(info, cfg or VoiceCallsConfig(enabled=True), RecordingHandler())
    session.speech = SimpleNamespace(busy=False)
    return session


@pytest.mark.asyncio
async def test_watchdog_end_call_waits_for_turn_and_speech(monkeypatch):
    monkeypatch.setattr(manager_mod, "END_CALL_GRACE_SECONDS", 0.05)
    session = _watch_session()
    turn = {"active": True}
    session.turn_active = lambda: turn["active"]
    session.last_caller_text = "Tschüss"
    assert session.request_end()
    task = asyncio.create_task(session._watchdog(poll=0.01))
    await asyncio.sleep(0.15)
    assert not task.done()  # agent turn (goodbye) still running
    turn["active"] = False
    session.speech.busy = True
    await asyncio.sleep(0.15)
    assert not task.done()  # goodbye still being spoken
    session.speech.busy = False
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_watchdog_idle_hangup():
    session = _watch_session(VoiceCallsConfig(enabled=True, idle_hangup_seconds=1))
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(session._watchdog(poll=0.05), 3)
    assert asyncio.get_running_loop().time() - t0 >= 1


@pytest.mark.asyncio
async def test_request_end_finds_session_by_channel(mgr):
    await mgr._on_call_event(ring(1, "+000000"))
    await mgr._on_call_event({**ring(1, "+000000"), "state": "CONNECTED"})
    StubSession.request_end = lambda self: setattr(self, "ended", True) or True
    assert mgr.request_end("+000000") == "ok"
    assert StubSession.instances[0].ended
    assert mgr.request_end("+000001") == "no_call"
    StubSession.request_end = lambda self: False
    assert mgr.request_end("+000000") == "no_goodbye"
    for _, task in mgr._sessions.values():
        task.cancel()


@pytest.mark.asyncio
@pytest.mark.usefixtures("reset_db")
async def test_end_call_only_offered_during_call():
    from src.nixclaw_agent.types import Event
    agent = build_mock_agent_loop()
    agent._tool_registry.get_tools_for_tier = MagicMock(return_value=[
        SimpleNamespace(name="end_call"), SimpleNamespace(name="web_search")])
    agent._llm_client.chat = AsyncMock(return_value=("ok", []))
    ev = Event(source="+000000", destination="+491234567890", message="hi",
               timestamp=1700000020, group_id=None, attachments=[], mentions=[])
    await agent.process_message(ev)
    assert [t.name for t in agent._llm_client.chat.await_args.args[1]] == ["web_search"]
    agent.register_voice_speaker("+000000", lambda text: None)
    await agent.process_message(Event(source="+000000", destination="+491234567890", message="hi2",
                                      timestamp=1700000021, group_id=None, attachments=[], mentions=[]))
    assert [t.name for t in agent._llm_client.chat.await_args.args[1]] == ["end_call", "web_search"]


def test_long_sentences_chunked_at_clauses():
    t = ("Ich meinte sicher Kokoro, das ist die TTS Engine, die wir nutzen. Ja, die kann "
         "Streaming-Mode, und das würde definitiv besser für Sprachausgabe sein, weil man "
         "sofort Audio bekommt, statt auf den ganzen Text zu warten.")
    chunks = split_sentences(t)
    assert len(chunks) == 3 and all(len(c) <= 90 for c in chunks)
    assert " ".join(chunks) == t
    no_commas = "wort " * 60
    assert all(len(c) <= 135 for c in split_sentences(no_commas))


@pytest.mark.asyncio
async def test_session_transcribe_and_utterance_handover(monkeypatch):
    calls = []

    async def fake_stt(self, wav, filename="audio.wav", *, language=None):
        calls.append(len(wav))
        return "Wie spät ist es?"

    monkeypatch.setattr(manager_mod.FasterWhisperClient, "transcribe_wav_bytes", fake_stt)
    handler = RecordingHandler()
    got = []

    async def on_utterance(session, text):
        got.append(text)

    handler.on_utterance = on_utterance
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    session = manager_mod.CallSession(info, VoiceCallsConfig(enabled=True, noise_gate=False), handler)
    session._utterances.put_nowait((Utterance(pcm=FRAME * 20, speech_ms=400), False, None))
    worker = asyncio.create_task(session._work())
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    assert got == ["Wie spät ist es?"] and calls and not session._handling


@pytest.mark.asyncio
async def test_overlapping_echo_utterance_dropped(monkeypatch):
    async def fake_stt(self, wav, filename="audio.wav", *, language=None):
        return "leichtem Regen bei zwölf Grad."

    monkeypatch.setattr(manager_mod.FasterWhisperClient, "transcribe_wav_bytes", fake_stt)
    got = []
    handler = RecordingHandler()

    async def on_utterance(session, text):
        got.append(text)

    handler.on_utterance = on_utterance
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    session = manager_mod.CallSession(info, VoiceCallsConfig(enabled=True, noise_gate=False), handler)
    session.speech = SimpleNamespace(recently_spoken=lambda: ["Morgen leichter Regen, leichtem Regen bei zwölf Grad."])
    utt = Utterance(pcm=FRAME * 20, speech_ms=400)
    session._utterances.put_nowait((utt, True, None))    # heard during our playback -> echo
    session._utterances.put_nowait((utt, False, None))   # same words, not overlapping -> real
    worker = asyncio.create_task(session._work())
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    assert got == ["leichtem Regen bei zwölf Grad."]


@pytest.mark.asyncio
async def test_noise_gate_drops_forced_language_hallucination(monkeypatch):
    # Qwen3-ASR forced to "de" turns silence into "Ich."; the auto pass says "".
    requests = []

    async def fake_payload(self, wav, filename="audio.wav", *, language=None, extra=None):
        requests.append((language, bool(extra)))
        if language == "":
            return {"text": heard["auto"]}
        payload = {"text": heard["forced"]}
        if extra and server_detects:
            payload["no_speech"] = not heard["auto"]
        return payload

    monkeypatch.setattr(manager_mod.FasterWhisperClient, "transcribe_wav_payload", fake_payload)
    monkeypatch.setattr(manager_mod.config.stt, "language", "de")
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")

    for server_detects in (False, True):
        session = manager_mod.CallSession(info, VoiceCallsConfig(enabled=True), RecordingHandler())
        heard = {"auto": "", "forced": "Ich."}
        assert await session.transcribe(FRAME * 20) == ""
        heard = {"auto": "Nine.", "forced": "Nein."}      # speech: text from the forced pass
        requests.clear()
        assert await session.transcribe(FRAME * 20) == "Nein."
        # old server: remembered, two plain requests; new server: one request
        assert len(requests) == (1 if server_detects else 2)

    session.cfg = VoiceCallsConfig(enabled=True, noise_gate=False)
    heard = {"auto": "", "forced": "Ich."}
    assert await session.transcribe(FRAME * 20) == "Ich."


# --- streaming STT ---------------------------------------------------------------

from aiohttp import WSMsgType, web  # noqa: E402

from src.nixclaw_agent.voice import stt_stream  # noqa: E402
from src.nixclaw_agent.voice.stt_stream import StreamingTranscriber, stream_url  # noqa: E402


def test_stream_url():
    assert stream_url("http://sparky.lan:8001/") == "ws://sparky.lan:8001/v1/audio/transcriptions/stream"
    assert stream_url("https://x") == "wss://x/v1/audio/transcriptions/stream"


async def _fake_stream_server(log, controls=True):
    """Fake streaming endpoint: a partial per audio frame, final = byte count;
    without `controls` flush/partials are refused like an older server does."""
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        n = 0
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                n += len(msg.data)
                await ws.send_json({"type": "partial", "text": f"p{n}"})
                continue
            data = json.loads(msg.data)
            log.append(data)
            if data["type"] == "start":
                n = 0
            elif data["type"] == "finish":
                await ws.send_json({"type": "final", "text": f"bytes={n}", "no_speech": n == 0})
            elif data["type"] in ("flush", "partials") and not controls:
                await ws.send_json({"type": "error", "detail": f"unexpected message {data['type']!r}"})
        return ws

    app = web.Application()
    app.router.add_get(stt_stream.STREAM_PATH, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}{stt_stream.STREAM_PATH}"


@pytest.mark.asyncio
async def test_streaming_transcriber_roundtrip():
    log, partials = [], []
    runner, url = await _fake_stream_server(log)
    st = StreamingTranscriber(url, "de", detect_speech=True, chunk_ms=500, on_partial=partials.append)
    try:
        assert await st.begin(b"ab")
        await st.feed(b"cd")
        for _ in range(100):
            if partials[-1:] == ["p4"]:
                break
            await asyncio.sleep(0.01)
        assert partials == ["p2", "p4"]
        final = await st.finish(tail_silence_ms=660)
        assert final["text"] == "bytes=4"
        assert log[0] == {"type": "start", "language": "de", "chunk_ms": 500, "detect_speech": True,
                          "partials": True}
        assert log[1] == {"type": "finish", "tail_silence_ms": 660}
        # cancelled utterance, then the next one on the same connection
        assert await st.begin(b"x")
        await st.cancel()
        assert await st.begin()
        assert (await st.finish())["no_speech"] is True
        assert [m["type"] for m in log] == ["start", "finish", "start", "cancel", "start", "finish"]
    finally:
        await st.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_streaming_transcriber_flush_and_partials():
    log = []
    runner, url = await _fake_stream_server(log)
    st = StreamingTranscriber(url, "de", detect_speech=True)
    try:
        await st.flush()                        # no utterance open: nothing sent
        assert await st.begin(b"ab", partials=False)
        await st.flush()
        await st.set_partials(True)
        await st.finish()
        assert [m["type"] for m in log] == ["start", "flush", "partials", "finish"]
        assert log[0]["partials"] is False and log[2]["enabled"] is True
    finally:
        await st.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_streaming_transcriber_older_server_without_controls():
    log = []
    runner, url = await _fake_stream_server(log, controls=False)
    st = StreamingTranscriber(url, "de", detect_speech=True)
    try:
        assert await st.begin(b"ab")
        await st.flush()
        assert (await st.finish())["text"] == "bytes=2"     # the error didn't break the stream
        assert await st.begin(b"cd")
        await st.flush()                        # not sent again
        await st.set_partials(False)
        await st.finish()
        assert [m["type"] for m in log] == ["start", "flush", "finish", "start", "finish"]
    finally:
        await st.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_streaming_transcriber_unavailable_backs_off(monkeypatch):
    now = [100.0]
    st = StreamingTranscriber("ws://127.0.0.1:9/nope", "de", detect_speech=True, clock=lambda: now[0])
    assert await st.begin(b"ab") is False
    assert await st.finish() is None            # -> HTTP fallback
    connects = []
    real = st._ensure_connected

    async def counting():
        connects.append(1)
        return await real()

    monkeypatch.setattr(st, "_ensure_connected", counting)
    now[0] += 5                                  # inside the retry pause: no new connect attempt
    assert await st.begin() is False and st._session is None and connects == [1]
    await st.close()


def _stream_session(monkeypatch, cfg=None):
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    return manager_mod.CallSession(info, cfg or VoiceCallsConfig(enabled=True, stt_streaming=True),
                                   RecordingHandler())


def test_stream_frame_mirrors_segmenter(monkeypatch):
    session = _stream_session(monkeypatch)
    seg = UtteranceSegmenter(start_speech_ms=60, end_silence_ms=100, min_speech_ms=250)
    ops = []
    for is_speech, n in [(True, 5), (False, 10), (True, 20), (False, 10)]:
        for _ in range(n):
            was = seg.speaking
            events = seg.push(FRAME, is_speech)
            session._stream_frame(FRAME, was, seg, events)
    while not session._stream_ops.empty():
        ops.append(session._stream_ops.get_nowait()[0])
    # blip: begin, feeds, cancel (too short); real utterance: begin, feeds - its
    # finish is queued by the capture loop together with the Utterance
    first_cancel = ops.index("cancel")
    assert ops[0] == "begin" and set(ops[1:first_cancel]) == {"feed"}
    assert ops[first_cancel + 1] == "begin" and "cancel" not in ops[first_cancel + 1:]


def test_stream_frame_flushes_once_per_pause_and_partials_only_while_playing(monkeypatch):
    session = _stream_session(monkeypatch, VoiceCallsConfig(enabled=True, stt_streaming=True,
                                                            stt_flush_ms=100))
    session.player = SimpleNamespace(playing=False)
    seg = UtteranceSegmenter(start_speech_ms=60, end_silence_ms=300, min_speech_ms=60)
    ops = []

    def run(is_speech, n):
        for _ in range(n):
            was = seg.speaking
            events = seg.push(FRAME, is_speech)
            session._stream_frame(FRAME, was, seg, events)
            while not session._stream_ops.empty():
                op = session._stream_ops.get_nowait()
                if op[0] != "feed":
                    ops.append(op if op[0] != "begin" else ("begin", op[2]))

    run(True, 10)
    run(False, 8)                   # 160 ms pause: one flush
    session.player.playing = True   # our reply starts playing mid-utterance
    run(True, 5)
    run(False, 3)                   # 60 ms: too short for a flush
    run(True, 5)
    session.player.playing = False
    run(False, 6)                   # 120 ms: flush again
    assert ops == [("begin", False), ("flush",), ("partials", True), ("partials", False), ("flush",)]


@pytest.mark.asyncio
async def test_streamed_final_used_else_http_fallback(monkeypatch):
    async def fake_payload(self, wav, filename="audio.wav", *, language=None, extra=None):
        return {"text": "per HTTP", "no_speech": False}

    monkeypatch.setattr(manager_mod.FasterWhisperClient, "transcribe_wav_payload", fake_payload)
    monkeypatch.setattr(manager_mod.config.stt, "language", "de")
    got = []
    session = _stream_session(monkeypatch)

    async def on_utterance(s, text):
        got.append(text)

    session.handler.on_utterance = on_utterance
    loop = asyncio.get_running_loop()
    utt = Utterance(pcm=FRAME * 20, speech_ms=400)
    for result in ({"type": "final", "text": "per Stream", "no_speech": False},
                   {"type": "final", "text": "Ich.", "no_speech": True},
                   None):
        fut = loop.create_future()
        fut.set_result(result)
        session._utterances.put_nowait((utt, False, fut))
    worker = asyncio.create_task(session._work())
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    # no-speech final dropped; back-to-back utterances are one turn
    assert got == ["per Stream per HTTP"]


@pytest.mark.asyncio
async def test_partial_triggers_barge_in_only_while_playing(monkeypatch):
    session = _stream_session(monkeypatch)
    session._seg = SimpleNamespace(current_speech_ms=600)
    judged = []

    async def judge(text):
        judged.append(text)

    monkeypatch.setattr(session, "_judge_barge_in", judge)
    session.player = SimpleNamespace(playing=False)
    session._on_partial("warte mal")
    session.player = SimpleNamespace(playing=True)
    session._on_partial("warte mal")
    await asyncio.sleep(0)
    assert judged == ["warte mal"]


def test_noise_transcripts_from_call_logs_are_filler():
    # observed on calls next to running water / dishes
    for t in ("K.", "T.", "S.", "Tschttschttschttschttschttschttsch", "Lalalala"):
        assert is_filler(t), t
    for t in ("Hahaha", "Ananas", "Barbara", "Nein", "Warte mal"):
        assert not is_filler(t), t


def test_backchannel():
    assert is_backchannel("Ja.") and is_backchannel("Okay, alles klar.") and is_backchannel("mhm")
    assert not is_backchannel("ja, mach das") and not is_backchannel("") and not is_backchannel("Nein")


@pytest.mark.asyncio
async def test_backchannel_dropped_only_during_our_speech(monkeypatch):
    async def fake_stt(self, wav, filename="audio.wav", *, language=None):
        return "Okay."

    monkeypatch.setattr(manager_mod.FasterWhisperClient, "transcribe_wav_bytes", fake_stt)
    got = []
    handler = RecordingHandler()

    async def on_utterance(session, text):
        got.append(text)

    handler.on_utterance = on_utterance
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    session = manager_mod.CallSession(info, VoiceCallsConfig(enabled=True, noise_gate=False), handler)
    session.speech = SimpleNamespace(recently_spoken=lambda: [])
    utt = Utterance(pcm=FRAME * 20, speech_ms=400)
    session._utterances.put_nowait((utt, True, None))     # while we speak: listening signal
    session._utterances.put_nowait((utt, False, None))    # an answer
    worker = asyncio.create_task(session._work())
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    assert got == ["Okay."]


@pytest.mark.asyncio
async def test_utterance_during_running_turn_counts_as_during_reply(monkeypatch):
    # From a call: "Ah." said while the agent was still thinking (nothing
    # playing yet) became a turn and the model hung up. It must be flagged.
    flags = [True] * 20 + [False] * 40
    monkeypatch.setattr(manager_mod, "WebRtcVad", lambda aggr: _ScriptedVad(flags))
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    seen = []
    for turn_running in (True, False):
        session = manager_mod.CallSession(info, VoiceCallsConfig(enabled=True), RecordingHandler())

        async def frames():
            for _ in flags:
                yield FRAME

        session._capture.frames = frames
        session.speech = SimpleNamespace(busy=False, recently_spoken=lambda: [])
        session.player = SimpleNamespace(playing=False)
        session.turn_active = lambda: turn_running
        await session._capture_loop()
        seen.append(session._utterances.get_nowait()[1])
    assert seen == [True, False]


def test_echo_check_ignores_function_words():
    # from a call: Nixon had been answering in English
    spoken = ["Yes, I can absolutely have a conversation in English. Feel free to switch whenever you like.",
              "You're still in the German-English mix, but I get what you mean. Yes, you can speak "
              "English on the phone, no problem."]
    assert not is_echo("But it sounds like you have a German voice.", spoken)
    assert is_echo("Yes, you can speak English on the phone, no problem.", spoken)
    assert is_filler("den den")


def test_utterance_end():
    from src.nixclaw_agent.voice.text import utterance_end
    assert utterance_end("Wie spät ist es?") == "finished"
    assert utterance_end("Sag mir das Wetter am Abend.") == "finished"
    assert utterance_end("Okay, aber es ist denn") == "unfinished"      # from a call
    assert utterance_end("ja das ist halt äh") == "unfinished"
    assert utterance_end("Ich habe die.") == "unfinished"
    assert utterance_end("Ich wollte sagen…") == "unfinished"
    assert utterance_end("Wie wird das Wetter heute") == "unclear"      # ASR left off the "?"
    assert utterance_end("Ruf mich an") == "unclear"                    # particle, not a preposition
    assert utterance_end("Es ist sonnig, oder") == "unclear"


def _continuation_session(texts):
    """Worker session whose utterances transcribe to `texts` in order."""
    info = manager_mod.CallInfo(1, "+000000", UserTier.PRIVILEGED, "signal_input_1", "signal_output_1")
    cfg = VoiceCallsConfig(enabled=True, noise_gate=False, unfinished_wait_ms=300, unclear_wait_ms=100)
    session = manager_mod.CallSession(info, cfg, RecordingHandler())
    session.speech = SimpleNamespace(recently_spoken=lambda: [])
    it = iter(texts)

    async def transcribe(pcm, label="STT"):
        return next(it)

    session.transcribe = transcribe
    got = []

    async def on_utterance(s, text):
        got.append((text, asyncio.get_running_loop().time()))

    session.handler.on_utterance = on_utterance
    return session, got


async def _until(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred() and loop.time() < end:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_continuation_after_unfinished_ending_is_merged():
    session, got = _continuation_session(["Okay, aber es ist denn", "sonnig oder nicht?"])
    utt = Utterance(pcm=FRAME * 20, speech_ms=400)
    worker = asyncio.create_task(session._work())
    session._utterances.put_nowait((utt, False, None))
    await asyncio.sleep(0.15)                       # pause shorter than unfinished_wait_ms
    session._utterances.put_nowait((utt, False, None))
    await _until(lambda: got)
    worker.cancel()
    assert [t for t, _ in got] == ["Okay, aber es ist denn sonnig oder nicht?"]


@pytest.mark.asyncio
async def test_finished_sentence_not_held_unclear_one_briefly():
    loop = asyncio.get_running_loop()
    utt = Utterance(pcm=FRAME * 20, speech_ms=400)
    for text, min_s, max_s in (("Wie spät ist es?", 0, 0.08), ("Wie wird das Wetter heute", 0.1, 0.3)):
        session, got = _continuation_session([text])
        worker = asyncio.create_task(session._work())
        start = loop.time()
        session._utterances.put_nowait((utt, False, None))
        await _until(lambda: got)
        worker.cancel()
        assert [t for t, _ in got] == [text]
        assert min_s <= got[0][1] - start < max_s


@pytest.mark.asyncio
async def test_continuation_waits_while_caller_speaks_then_gives_up_on_dropped_blip():
    loop = asyncio.get_running_loop()
    session, got = _continuation_session(["Wie spät ist es?"])
    utt = Utterance(pcm=FRAME * 20, speech_ms=400)
    session._capturing = True                       # caller already speaking again
    worker = asyncio.create_task(session._work())
    start = loop.time()
    session._utterances.put_nowait((utt, False, None))
    await asyncio.sleep(0.2)
    assert not got                                  # held while they speak
    session._capturing = False                      # too short: segmenter dropped it
    await _until(lambda: got)
    worker.cancel()
    assert [t for t, _ in got] == ["Wie spät ist es?"] and got[0][1] - start < 0.35
