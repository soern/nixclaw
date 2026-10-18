"""CallManager: call policy and lifecycle; CallSession: one connected call.

Flow per call: RINGING_INCOMING -> tier check -> acceptCall -> CONNECTED ->
CallSession (capture -> VAD -> utterance -> STT -> handler -> speak) ->
ENDED -> cleanup (incl. the PulseAudio modules the tunnel leaves behind).
One call at a time; further callers are rejected.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .. import prompts
from ..auth import AuthManager
from ..config import VoiceCallsConfig, config
from ..stt import FasterWhisperClient, STTError
from ..tts import KokoroTTSClient, TTSError
from ..tools.tts_ops import channel_voice
from ..types import UserTier
from .audio import PulseCapture, PulsePlayer, pcm_to_wav, unload_call_modules, wav_to_pcm
from .rpc import RpcError, SignalCallRpc
from .speech import Clip, SpeechPipeline
from .stt_stream import StreamingTranscriber, stream_url
from .text import is_backchannel, is_echo, is_filler, is_goodbye, utterance_end
from .vad import FRAME_MS, SpeechStarted, Utterance, UtteranceSegmenter, WebRtcVad

LOG = logging.getLogger(__name__)

# After end_call: hang up once nothing was spoken or processed for this long.
END_CALL_GRACE_SECONDS = 1.0
# Pause-separated parts of one sentence merged into one turn (at most).
MAX_MERGED_UTTERANCES = 4
CONTINUATION_POLL_SECONDS = 0.05


@dataclass
class CallInfo:
    call_id: int
    number: str
    tier: UserTier
    input_device: str = ""
    output_device: str = ""


class UtteranceHandler(Protocol):
    async def on_call_started(self, session: "CallSession") -> None: ...
    async def on_utterance(self, session: "CallSession", text: str) -> None: ...
    async def on_call_ended(self, session: "CallSession") -> None: ...


class ParrotHandler:
    """Pipeline test: greet, then speak every transcript straight back."""

    async def on_call_started(self, session: "CallSession") -> None:
        session.enqueue_speech(prompts.current().render("voice-call-greeting", agent_name=config.agent.name))

    async def on_utterance(self, session: "CallSession", text: str) -> None:
        session.enqueue_speech(text)

    async def on_call_ended(self, session: "CallSession") -> None:
        pass


class AgentHandler:
    """Voice turns through the AgentLoop: each utterance becomes an inbound
    message (origin "voice") in the caller's 1:1 channel; while the call is
    active the agent's turn texts are also queued for speech."""

    def __init__(self, agent):
        self.agent = agent  # AgentLoop (not imported: avoids a core <-> voice cycle)

    async def on_call_started(self, session: "CallSession") -> None:
        self.agent.register_voice_speaker(session.channel_id, session.enqueue_speech)
        session.turn_active = lambda: self.agent.is_turn_active(session.channel_id)
        session.enqueue_speech(
            prompts.current().render("voice-call-greeting", agent_name=config.agent.name))

    async def on_utterance(self, session: "CallSession", text: str) -> None:
        self.agent.submit_voice_message(session.info.number, text)

    async def on_call_ended(self, session: "CallSession") -> None:
        self.agent.unregister_voice_speaker(session.channel_id)


class CallSession:
    def __init__(self, info: CallInfo, cfg: VoiceCallsConfig, handler: UtteranceHandler):
        self.info = info
        self.cfg = cfg
        self.handler = handler
        self.channel_id = info.number  # 1:1 channel = caller's number
        self.language = config.tts.default_language
        self.player = PulsePlayer(info.input_device, cfg.pulse_server)
        self._capture = PulseCapture(info.output_device, cfg.pulse_server)
        # (utterance, heard during our reply, streaming final or None)
        self._utterances: asyncio.Queue[
            tuple[Utterance, bool, Optional[asyncio.Future]]] = asyncio.Queue()
        self.speech = SpeechPipeline(self._synthesize, self.player)
        self._barge_task: Optional[asyncio.Task] = None
        # Set by the handler: is an agent turn for this caller still running?
        self.turn_active: Callable[[], bool] = lambda: False
        self._handling = False      # transcribing / handing an utterance over
        self._capturing = False     # caller is mid-utterance
        self._end_requested = False
        self.last_caller_text = ""  # last turn handed to the handler (end_call check)
        self._stt = FasterWhisperClient(config.stt)
        self._detect_speech: Optional[bool] = None  # server supports detect_speech?
        self._stream: Optional[StreamingTranscriber] = None
        # begin/feed/finish/cancel for the stream, in capture order
        self._stream_ops: asyncio.Queue[tuple] = asyncio.Queue()
        self._stream_live = False   # the utterance in progress is being streamed
        self._stream_flushed = False    # flush sent for the current pause
        self._stream_partials = False   # partials requested for the current utterance
        self._seg: Optional[UtteranceSegmenter] = None
        if cfg.stt_streaming:
            self._stream = StreamingTranscriber(
                stream_url(config.stt.base_url), config.stt.language,
                detect_speech=cfg.noise_gate, chunk_ms=cfg.barge_in_ms or 1000,
                on_partial=self._on_partial)

    def request_end(self) -> bool:
        """end_call: hang up once the current turn is done and spoken - only
        if the caller's last words were a goodbye (False: the call goes on)."""
        if not is_goodbye(self.last_caller_text):
            LOG.info(f"voice[{self.info.call_id}]: end_call refused, the caller didn't say "
                     f"goodbye: {self.last_caller_text!r}")
            return False
        self._end_requested = True
        return True

    async def run(self) -> None:
        """Runs until cancelled (call ended), the capture stream ends, end_call
        was requested and everything is spoken, or the call went silent for
        idle_hangup_seconds. Returning makes the CallManager hang up."""
        worker = asyncio.create_task(self._work())
        speaker = asyncio.create_task(self.speech.run())
        capture = asyncio.create_task(self._capture_loop())
        watchdog = asyncio.create_task(self._watchdog())
        pump = asyncio.create_task(self._stream_pump())
        try:
            await asyncio.wait({capture, watchdog}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (worker, speaker, capture, watchdog, pump):
                task.cancel()
            await self._capture.stop()
            await self.player.interrupt()
            if self._stream is not None:
                await self._stream.close()

    async def _capture_loop(self) -> None:
        vad = WebRtcVad(self.cfg.vad_aggressiveness)
        seg = UtteranceSegmenter(
            start_speech_ms=self.cfg.start_speech_ms,
            end_silence_ms=self.cfg.end_silence_ms,
            min_speech_ms=self.cfg.min_speech_ms,
            max_utterance_ms=self.cfg.max_utterance_seconds * 1000,
        )
        self._seg = seg
        next_check_ms = self.cfg.barge_in_ms
        overlapped = False
        async for frame in self._capture.frames():
            was_speaking = seg.speaking
            events = seg.push(frame, vad.is_speech(frame))
            self._capturing = seg.speaking
            if self._stream is not None:
                self._stream_frame(frame, was_speaking, seg, events)
            if not seg.speaking:
                next_check_ms = self.cfg.barge_in_ms
            else:
                overlapped = overlapped or self.player.playing
                # Barge-in candidate: caller speech of barge_in_ms while our
                # audio is actually playing (not while a reply is only queued:
                # "I can't hear anything" must not discard it). Confirmed by
                # transcribing it first, since speakerphone echo of our own
                # voice also looks like speech; re-checked every barge_in_ms.
                # With a live stream the partial transcripts do this instead.
                if (self.cfg.barge_in_ms and self.player.playing and not self._stream_live
                        and seg.current_speech_ms >= next_check_ms
                        and (self._barge_task is None or self._barge_task.done())):
                    next_check_ms = seg.current_speech_ms + self.cfg.barge_in_ms
                    self._barge_task = asyncio.create_task(self._check_barge_in(seg.current_pcm()))
            for ev in events:
                if isinstance(ev, SpeechStarted):
                    LOG.debug(f"voice[{self.info.call_id}]: speech started")
                elif isinstance(ev, Utterance):
                    LOG.info(f"voice[{self.info.call_id}]: utterance {ev.speech_ms} ms speech, "
                             f"{len(ev.pcm) // 32} ms total")
                    final: Optional[asyncio.Future] = None
                    if self._stream is not None:
                        final = asyncio.get_running_loop().create_future()
                        self._stream_ops.put_nowait(("finish", final, asyncio.get_running_loop().time()))
                    # Also while our reply is still being worked on: an "Ah." then
                    # reacts to it and must not become a turn of its own.
                    replying = overlapped or self.speech.busy or self.turn_active()
                    self._utterances.put_nowait((ev, replying, final))
                    overlapped = False
        LOG.warning(f"voice[{self.info.call_id}]: capture stream ended")

    def _stream_frame(self, frame: bytes, was_speaking: bool, seg: UtteranceSegmenter,
                      events: list) -> None:
        """Mirror the segmenter onto the STT stream (non-blocking). Flushes
        once per pause of stt_flush_ms; partials only while our audio plays
        (barge-in check), since a final decode doesn't build on them."""
        want_partials = bool(self.cfg.barge_in_ms) and self.player.playing
        if not was_speaking and seg.speaking:
            self._stream_flushed = False
            self._stream_partials = want_partials
            self._stream_ops.put_nowait(("begin", seg.current_pcm(), want_partials))   # incl. pre-roll
        elif was_speaking and seg.speaking:
            self._stream_ops.put_nowait(("feed", frame))
            if seg.silence_ms == 0:
                self._stream_flushed = False
            elif (self.cfg.stt_flush_ms and not self._stream_flushed
                    and seg.silence_ms >= self.cfg.stt_flush_ms):
                self._stream_flushed = True
                self._stream_ops.put_nowait(("flush",))
            if want_partials != self._stream_partials:
                self._stream_partials = want_partials
                self._stream_ops.put_nowait(("partials", want_partials))
        elif was_speaking and not any(isinstance(ev, Utterance) for ev in events):
            self._stream_ops.put_nowait(("cancel",))    # too short to count

    async def _stream_pump(self) -> None:
        """Runs the stream operations in order; a finish waits for its final
        (the server answers in order anyway), later audio queues meanwhile."""
        if self._stream is None:
            return
        while True:
            op = await self._stream_ops.get()
            try:
                if op[0] == "begin":
                    self._stream_live = await self._stream.begin(op[1], partials=op[2])
                elif op[0] == "feed":
                    await self._stream.feed(op[1])
                elif op[0] == "flush":
                    await self._stream.flush()
                elif op[0] == "partials":
                    await self._stream.set_partials(op[1])
                elif op[0] == "cancel":
                    self._stream_live = False
                    await self._stream.cancel()
                elif op[0] == "finish":
                    self._stream_live = False
                    # The segmenter's end-of-speech silence was streamed too,
                    # minus the frame that ended the utterance.
                    result = await self._stream.finish(
                        tail_silence_ms=max(0, self.cfg.end_silence_ms - 2 * FRAME_MS))
                    if result is not None:     # end of utterance -> final, incl. queueing
                        wait = asyncio.get_running_loop().time() - op[2]
                        result = {**result, "wait_ms": int(wait * 1000)}
                    if not op[1].done():
                        op[1].set_result(result)
            except Exception:
                LOG.exception(f"voice[{self.info.call_id}]: stream operation {op[0]} failed")
                if op[0] == "finish" and not op[1].done():
                    op[1].set_result(None)

    def _on_partial(self, text: str) -> None:
        """Partial transcript of the utterance in progress: barge-in check."""
        seg = self._seg
        if (self.cfg.barge_in_ms and self.player.playing and seg is not None
                and seg.current_speech_ms >= self.cfg.barge_in_ms
                and (self._barge_task is None or self._barge_task.done())):
            LOG.info(f"voice[{self.info.call_id}]: barge-in check (partial): {text!r}")
            self._barge_task = asyncio.create_task(self._judge_barge_in(text))

    def _active(self) -> bool:
        return (self.speech.busy or self._handling or not self._utterances.empty()
                or self._capturing or self.turn_active())

    async def _watchdog(self, poll: float = 0.5) -> None:
        last_active = time.monotonic()
        while True:
            await asyncio.sleep(poll)
            now = time.monotonic()
            if self._active():
                last_active = now
            idle = now - last_active
            if self._end_requested and idle >= END_CALL_GRACE_SECONDS:
                LOG.info(f"voice[{self.info.call_id}]: end_call requested, hanging up")
                return
            if idle >= self.cfg.idle_hangup_seconds:
                LOG.info(f"voice[{self.info.call_id}]: silent for {idle:.0f}s, hanging up")
                return

    async def _work(self) -> None:
        try:
            await self.handler.on_call_started(self)
        except Exception:
            LOG.exception(f"voice[{self.info.call_id}]: on_call_started failed")
        while True:
            item = await self._utterances.get()
            self._handling = True
            try:
                text = await self._accept(*item)
                if text:
                    text = await self._await_continuation(text)
                    self.last_caller_text = text
                    await self.handler.on_utterance(self, text)
            except Exception:
                LOG.exception(f"voice[{self.info.call_id}]: utterance handler failed")
            finally:
                self._handling = False

    async def _accept(self, utt: Utterance, replying: bool, final: Optional[asyncio.Future]) -> str:
        """Transcript of one utterance; "" for noise, filler, echo of our own
        speech and backchannels during our reply."""
        text = await self._streamed_text(final) if final is not None else None
        if text is None:
            text = await self.transcribe(utt.pcm)
        if replying and is_echo(text, self.speech.recently_spoken()):
            LOG.info(f"voice[{self.info.call_id}]: dropping echo of our own speech: {text!r}")
            return ""
        if replying and is_backchannel(text):
            LOG.info(f"voice[{self.info.call_id}]: dropping backchannel during our reply: {text!r}")
            return ""
        return "" if not text or is_filler(text) else text

    async def _await_continuation(self, text: str) -> str:
        """`text` plus what the caller says right after it: half a sentence
        cut off by a pause would confuse the agent. Waits a little after an
        unfinished/unclear ending, and always while the caller is already
        speaking again."""
        loop = asyncio.get_running_loop()
        for _ in range(MAX_MERGED_UTTERANCES - 1):
            wait_ms = {"unfinished": self.cfg.unfinished_wait_ms,
                       "unclear": self.cfg.unclear_wait_ms}.get(utterance_end(text), 0)
            deadline = loop.time() + wait_ms / 1000
            # A too-short utterance is dropped by the segmenter, so speech
            # starting again doesn't guarantee a queue entry.
            while self._utterances.empty() and (self._capturing or loop.time() < deadline):
                await asyncio.sleep(CONTINUATION_POLL_SECONDS)
            if self._utterances.empty():
                break
            more = await self._accept(*self._utterances.get_nowait())
            if more:
                LOG.info(f"voice[{self.info.call_id}]: continuation after a pause: {more!r}")
                text = f"{text} {more}"
        return text

    def enqueue_speech(self, text: str) -> None:
        """Queue `text` to be spoken after what is already queued (non-blocking)."""
        self.speech.say(text)

    async def _streamed_text(self, final: asyncio.Future) -> Optional[str]:
        """Transcript from the stream's final message; None = use HTTP."""
        result = await final
        if result is None:
            return None
        timing = f"finish {result.get('finish_ms')} ms, final after {result.get('wait_ms')} ms"
        if result.get("no_speech"):
            LOG.info(f"voice[{self.info.call_id}]: STT (stream, {timing}): "
                     f"no speech (p={result.get('no_speech_prob')})")
            return ""
        text = str(result.get("text") or "").strip()
        LOG.info(f"voice[{self.info.call_id}]: STT (stream, {timing}): {text!r}")
        return text

    async def _check_barge_in(self, pcm: bytes) -> None:
        await self._judge_barge_in(await self.transcribe(pcm, label="barge-in check"))

    async def _judge_barge_in(self, text: str) -> None:
        if is_filler(text) or is_backchannel(text) or len(text.split()) < 2:
            return
        if is_echo(text, self.speech.recently_spoken()):
            LOG.info(f"voice[{self.info.call_id}]: barge-in ignored, echo of our own speech")
            return
        if self.player.playing or self.speech.busy:
            LOG.info(f"voice[{self.info.call_id}]: barge-in, interrupting speech")
            await self.speech.interrupt()

    async def transcribe(self, pcm: bytes, label: str = "STT") -> str:
        """Transcript of `pcm`, "" for noise or on failure.

        With a forced `stt.language` Qwen3-ASR always outputs words, even for
        silence or clatter ("Ich.", "Die."); without one it reports "no
        speech" (empty text) but mishears short words ("Nein" -> "Nine").
        The noise gate asks the server for both in one request
        (`detect_speech`); a server without it gets two requests, the auto
        pass deciding speech vs noise and the forced pass supplying the text.
        """
        t0 = time.monotonic()
        wav = pcm_to_wav(pcm)
        gate = self.cfg.noise_gate and bool(config.stt.language)
        try:
            if not gate:
                speech = text = await self._stt.transcribe_wav_bytes(wav, "utterance.wav")
            elif self._detect_speech is not False:
                payload = await self._stt.transcribe_wav_payload(
                    wav, "utterance.wav", extra={"detect_speech": "true"})
                text = payload["text"].strip()
                if "no_speech" in payload:
                    self._detect_speech = True
                    speech = "" if payload["no_speech"] else "speech"
                else:
                    self._detect_speech = False
                    speech = await self._stt.transcribe_wav_bytes(wav, "utterance.wav", language="")
            else:
                speech, text = await asyncio.gather(
                    self._stt.transcribe_wav_bytes(wav, "utterance.wav", language=""),
                    self._stt.transcribe_wav_bytes(wav, "utterance.wav"))
        except STTError as e:
            LOG.warning(f"voice[{self.info.call_id}]: STT failed: {e}")
            return ""
        elapsed = time.monotonic() - t0
        if not speech.strip():
            LOG.info(f"voice[{self.info.call_id}]: {label} {elapsed:.2f}s: no speech (forced: {text!r})")
            return ""
        LOG.info(f"voice[{self.info.call_id}]: {label} {elapsed:.2f}s: {text!r}")
        return text

    async def _synthesize(self, text: str) -> Optional[Clip]:
        """(pcm, rate, channels) for one sentence, or None on failure."""
        engine = config.tts.engine_for(self.language)
        if engine is None or not config.tts.enabled:
            LOG.warning(f"voice[{self.info.call_id}]: no TTS engine for {self.language!r}")
            return None
        t0 = time.monotonic()
        try:
            audio, _ = await KokoroTTSClient(engine).synthesize(
                text, voice=channel_voice(self.channel_id, engine.language),
                response_format="wav", extra=self.cfg.tts_sampling or None)
            pcm, rate, channels = wav_to_pcm(audio)
        except (TTSError, ValueError) as e:
            LOG.warning(f"voice[{self.info.call_id}]: TTS failed: {e}")
            return None
        LOG.info(f"voice[{self.info.call_id}]: TTS {time.monotonic() - t0:.2f}s for "
                 f"{len(text)} chars -> {len(pcm) / (2 * rate * channels):.1f}s audio")
        return pcm, rate, channels


class CallManager:
    def __init__(self, cfg: VoiceCallsConfig, auth_manager: AuthManager,
                 handler: Optional[UtteranceHandler] = None):
        self.cfg = cfg
        self.auth = auth_manager
        self.handler: UtteranceHandler = handler or ParrotHandler()
        self.rpc = SignalCallRpc(cfg.jsonrpc_host, cfg.jsonrpc_port,
                                 config.signal.phone_number, self._on_call_event)
        self._calls: dict[int, CallInfo] = {}        # accepted, not yet ended
        self._sessions: dict[int, tuple[CallSession, asyncio.Task]] = {}

    async def run(self) -> None:
        LOG.info(f"voice calls enabled (mode={self.cfg.mode}, "
                 f"allowed tiers={self.cfg.allowed_tiers})")
        await self.rpc.run()

    async def stop(self) -> None:
        for call_id in list(self._calls):
            try:
                await self.rpc.hangup(call_id)
            except (RpcError, ConnectionError, asyncio.TimeoutError):
                pass
        for _, task in self._sessions.values():
            task.cancel()
        self.rpc.stop()

    def request_end(self, channel_id: str) -> str:
        """end_call tool: "ok", "no_goodbye" (refused, see CallSession.request_end)
        or "no_call" (no active call with this channel)."""
        for session, _ in self._sessions.values():
            if session.channel_id == channel_id:
                return "ok" if session.request_end() else "no_goodbye"
        return "no_call"

    async def _on_call_event(self, ev: dict) -> None:
        call_id, state = ev.get("callId"), ev.get("state")
        if not isinstance(call_id, int):
            return
        LOG.info(f"voice: call {call_id} {state} "
                 f"({'out' if ev.get('isOutgoing') else 'in'}, reason={ev.get('reason')})")
        if state == "RINGING_INCOMING":
            await self._on_ringing(call_id, ev)
        elif state == "CONNECTED":
            self._on_connected(call_id, ev)
        elif state == "ENDED":
            await self._on_ended(call_id, ev)

    async def _on_ringing(self, call_id: int, ev: dict) -> None:
        if call_id in self._calls:
            return  # signal-cli repeats RINGING_INCOMING
        number = ev.get("number") or ""
        tier = self.auth.get_user_tier(number) if number else UserTier.UNKNOWN
        if tier.name not in self.cfg.allowed_tiers:
            LOG.info(f"voice: rejecting call {call_id} from tier {tier.name}")
            await self._reject(call_id)
            return
        if self._calls:
            LOG.info(f"voice: rejecting call {call_id}, another call is active")
            await self._reject(call_id)
            return
        info = CallInfo(call_id, number, tier,
                        ev.get("inputDeviceName") or "", ev.get("outputDeviceName") or "")
        self._calls[call_id] = info
        try:
            await self.rpc.accept(call_id)
        except (RpcError, ConnectionError, asyncio.TimeoutError) as e:
            LOG.warning(f"voice: acceptCall {call_id} failed: {e}")
            self._calls.pop(call_id, None)

    def _on_connected(self, call_id: int, ev: dict) -> None:
        info = self._calls.get(call_id)
        if info is None or call_id in self._sessions:
            return
        info.input_device = ev.get("inputDeviceName") or info.input_device
        info.output_device = ev.get("outputDeviceName") or info.output_device
        if not info.input_device or not info.output_device:
            LOG.error(f"voice: call {call_id} connected without audio device names")
            return
        session = CallSession(info, self.cfg, self.handler)
        self._sessions[call_id] = (session, asyncio.create_task(self._run_session(session)))

    async def _run_session(self, session: CallSession) -> None:
        call_id = session.info.call_id
        try:
            await asyncio.wait_for(session.run(), timeout=self.cfg.max_call_minutes * 60)
        except asyncio.TimeoutError:
            LOG.info(f"voice: call {call_id} reached max_call_minutes, hanging up")
        except asyncio.CancelledError:
            return  # ENDED: cleanup happens in _on_ended
        except Exception:
            LOG.exception(f"voice: session for call {call_id} failed")
        # The session ended on its own: end the call too.
        if call_id in self._calls:
            try:
                await self.rpc.hangup(call_id)
            except (RpcError, ConnectionError, asyncio.TimeoutError) as e:
                LOG.warning(f"voice: hangupCall {call_id} failed: {e}")

    async def _on_ended(self, call_id: int, ev: dict) -> None:
        self._calls.pop(call_id, None)
        entry = self._sessions.pop(call_id, None)
        if entry is not None:
            session, task = entry
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            try:
                await self.handler.on_call_ended(session)
            except Exception:
                LOG.exception(f"voice: on_call_ended failed for call {call_id}")
        devices = [ev.get("inputDeviceName") or "", ev.get("outputDeviceName") or ""]
        try:
            n = await unload_call_modules(devices, self.cfg.pulse_server)
            if n:
                LOG.info(f"voice: unloaded {n} leftover PulseAudio module(s) of call {call_id}")
        except Exception as e:
            LOG.warning(f"voice: PulseAudio cleanup for call {call_id} failed: {e}")

    async def _reject(self, call_id: int) -> None:
        try:
            await self.rpc.reject(call_id)
        except (RpcError, ConnectionError, asyncio.TimeoutError) as e:
            LOG.warning(f"voice: rejectCall {call_id} failed: {e}")
