"""Configuration loading for nixclaw-agent."""

import dataclasses
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

import yaml

LOG = logging.getLogger(__name__)


MIN_TOOL_CALL_ARGS_CHARS = 256
MIN_TOOL_RESULT_CHARS = 1024


PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "agent.yaml"


def _load_yaml_config() -> dict:
    """Load configuration from YAML file."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


@dataclass
class OllamaOptionsConfig:
    temperature: float = 1.0
    top_k: int = 20
    top_p: float = 0.95
    num_predict: int = 2048

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "OllamaOptionsConfig":
        if not data:
            return cls()
        return cls(
            temperature=data.get("temperature", 1.0),
            top_k=data.get("top_k", 40),
            top_p=data.get("top_p", 0.95),
            num_predict=data.get("num_predict", 2048),
        )

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class OllamaConfig:
    host: str = "http://localhost:11434"
    model: str = "llama3"
    options: OllamaOptionsConfig = field(default_factory=OllamaOptionsConfig)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "OllamaConfig":
        if not data:
            return cls()
        opts = data.get("options")
        return cls(
            host=data.get("host", "http://localhost:11434"),
            model=data.get("model", "llama3"),
            options=OllamaOptionsConfig.from_dict(opts) if opts else OllamaOptionsConfig(),
        )


@dataclass
class VllmOptionsConfig:
    temperature: float = 1.0
    top_p: float = 0.95
    top_k: int = 64
    max_tokens: int = 220000
    timeout: float = 120.0
    context_window: int = 131072
    # Streaming stall watchdog: max seconds between SSE chunks before a request
    # is treated as stalled and retried. NOT a flat total-request timeout.
    # 30s (not 15s): a benchmark against production hardware found a solo,
    # uncontended ~22K-token "heavy" conversation already has ~19.7s p50
    # time-to-first-token, so 15s was too tight even without concurrency.
    stream_idle_timeout: float = 30.0
    # Retries after a stream stall, with backoff = 3s * attempt_number.
    max_retries: int = 10
    # Client-side cap on concurrent in-flight requests to the vLLM backend,
    # shared across every VLLMClient instance in the process (see the
    # module-level semaphore in llm.py). Benchmark-derived for the current
    # model/hardware (qwen3.6, small KV-cache pool - num_gpu_blocks=1250 on
    # the deployed backend); re-benchmark if either changes significantly.
    max_concurrent_requests: int = 2
    # How long a request waits queued for a concurrency slot before giving
    # up with VLLMConcurrencyLimitError. Bounds
    # how long one hung/slow request can block unrelated requests behind it.
    max_concurrent_wait_seconds: float = 240.0

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "VllmOptionsConfig":
        if not data:
            return cls()
        return cls(
            temperature=data.get("temperature", 1.0),
            top_p=data.get("top_p", 0.95),
            top_k=data.get("top_k", 64),
            max_tokens=data.get("max_tokens", 220000),
            timeout=data.get("timeout", 120.0),
            context_window=data.get("context_window", 131072),
            stream_idle_timeout=data.get("stream_idle_timeout", 30.0),
            max_retries=data.get("max_retries", 10),
            max_concurrent_requests=data.get("max_concurrent_requests", 2),
            max_concurrent_wait_seconds=data.get("max_concurrent_wait_seconds", 240.0),
        )

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class VllmConfig:
    host: str = "http://localhost:8000"
    model: Optional[str] = None
    # Optional dedicated vision model for image analysis (analyze_image);
    # falls back to `model` when unset.
    vision_model: Optional[str] = None
    options: VllmOptionsConfig = field(default_factory=VllmOptionsConfig)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "VllmConfig":
        if not data:
            return cls()
        opts = data.get("options")
        return cls(
            host=data.get("host", "http://localhost:8000"),
            model=data.get("model"),
            vision_model=data.get("vision_model"),
            options=VllmOptionsConfig.from_dict(opts) if opts else VllmOptionsConfig(),
        )


@dataclass
class SignalConfig:
    api_url: str = "http://localhost:8080"
    phone_number: str = "+491234567890"
    webhook_port: int = 3002
    webhook_path: str = "/webhook/signal"
    # Shared secret the signal-api must send (?token=...). Unset = webhook on 127.0.0.1 only.
    webhook_secret: Optional[str] = None
    mock_api_url: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SignalConfig":
        if not data:
            return cls()
        return cls(
            api_url=data.get("api_url", "http://localhost:8080"),
            phone_number=data.get("phone_number", "+491234567890"),
            webhook_port=data.get("webhook_port", 3002),
            webhook_path=data.get("webhook_path", "/webhook/signal"),
            webhook_secret=data.get("webhook_secret") or None,
            mock_api_url=data.get("mock_api_url"),
        )


@dataclass
class SttAlternateConfig:
    """Secondary STT backend, reached only via the `retranscribe_audio` tool.

    Not part of the automatic voice-message pipeline - the primary `SttConfig`
    above stays the only backend used there. This one exists so the agent can
    ask for a second opinion mid-conversation when the primary transcript
    looks garbled or nonsensical. Currently points at `deploy/dgx-spark/canary-1b-v2/`
    (NVIDIA Canary-1b-v2), which has no language auto-detection - if
    `language` is None here, the tool caller must supply one.
    """
    enabled: bool = False
    base_url: str = "http://localhost:8006"
    model: str = "canary-1b-v2"
    language: Optional[str] = None
    timeout: float = 90.0
    max_concurrent_requests: int = 1
    max_concurrent_wait_seconds: float = 120.0

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SttAlternateConfig":
        if not data:
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            base_url=data.get("base_url", "http://localhost:8006"),
            model=data.get("model", "canary-1b-v2"),
            language=data.get("language"),
            timeout=data.get("timeout", 90.0),
            max_concurrent_requests=data.get("max_concurrent_requests", 1),
            max_concurrent_wait_seconds=data.get("max_concurrent_wait_seconds", 120.0),
        )


@dataclass
class SttConfig:
    """Faster-Whisper STT service configuration.

    The server lives at `base_url` (default `http://localhost:8001`; e.g. a
    GPU host running `deploy/dgx-spark/faster-whisper/`). It exposes an
    OpenAI-shaped `POST /v1/audio/transcriptions` endpoint. The client
    pre-chunks audio to <=`chunk_seconds` (default 28 s, below Whisper's
    30 s hard limit) and refuses clips longer than `max_duration_seconds`
    (default 300 s = 5 min). `language` is a forced ISO-639-1 code
    (default "en") or None for auto-detect.

    Transcribed audio (and its `.transcript.md` sidecar cache) is kept on
    disk for `retention_hours` after transcription - not deleted immediately
    - so the `retranscribe_audio` tool can re-run it through `alternate`.
    A periodic sweep (`stt.sweep_expired_audio`) deletes anything older.
    `retention_hours <= 0` disables the sweep (kept forever).
    """
    enabled: bool = False
    base_url: str = "http://localhost:8001"
    model: str = "large-v3"
    language: Optional[str] = "en"   # forced ISO-639-1 code; null = auto-detect
    timeout: float = 60.0
    max_duration_seconds: float = 300.0
    chunk_seconds: float = 28.0
    retention_hours: float = 1.0
    alternate: SttAlternateConfig = field(default_factory=SttAlternateConfig)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SttConfig":
        if not data:
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            base_url=data.get("base_url", "http://localhost:8001"),
            model=data.get("model", "large-v3"),
            language=data.get("language", "en"),
            timeout=data.get("timeout", 60.0),
            max_duration_seconds=data.get("max_duration_seconds", 300.0),
            chunk_seconds=data.get("chunk_seconds", 28.0),
            retention_hours=data.get("retention_hours", 1.0),
            alternate=SttAlternateConfig.from_dict(data.get("alternate")),
        )


@dataclass
class ImageGenConfig:
    """FLUX.2 [klein] image generation service configuration.

    The server lives at `base_url` (the GPU host that runs
    `deploy/dgx-spark/flux2-klein/`, by default the same DGX Spark that
    hosts vLLM). It exposes an OpenAI-shaped `POST /v1/images/generations`
    and `POST /v1/images/edits` endpoint. The GPU is shared with vLLM, so
    the client caps in-flight generations at `max_concurrent_requests`
    (module-level semaphore in imagegen.py) and gives up after
    `max_concurrent_wait_seconds` of queueing.
    """
    enabled: bool = False
    base_url: str = "http://localhost:8002"
    model: str = "black-forest-labs/FLUX.2-klein-9B"
    timeout: float = 180.0
    default_size: str = "1024x1024"
    default_steps: int = 4
    max_steps: int = 40
    max_concurrent_requests: int = 1
    max_concurrent_wait_seconds: float = 240.0
    max_input_images: int = 8

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ImageGenConfig":
        if not data:
            return cls()
        return cls(
            enabled=data.get("enabled", False),
            base_url=data.get("base_url", "http://localhost:8002"),
            model=data.get("model", "black-forest-labs/FLUX.2-klein-9B"),
            timeout=data.get("timeout", 180.0),
            default_size=data.get("default_size", "1024x1024"),
            default_steps=int(data.get("default_steps", data.get("steps", 4))),
            max_steps=int(data.get("max_steps", 40)),
            max_concurrent_requests=data.get("max_concurrent_requests", 1),
            max_concurrent_wait_seconds=data.get("max_concurrent_wait_seconds", 240.0),
            max_input_images=int(data.get("max_input_images", 8)),
        )


def normalize_image_steps(value, max_steps: int) -> tuple[int, bool]:
    """Coerce a raw tool-argument steps value to a valid int.

    Returns (steps, clamped). Raises ValueError on anything < 1 or unparseable.
    Values above max_steps are clamped to max_steps (clamped=True).
    """
    if isinstance(value, bool):
        raise ValueError(f"Invalid steps value: {value!r} - must be a positive integer")
    if isinstance(value, int):
        steps = value
    elif isinstance(value, str):
        try:
            steps = int(value.strip())
        except ValueError:
            raise ValueError(f"Invalid steps value: {value!r} - must be a positive integer")
    else:
        raise ValueError(f"Invalid steps value: {value!r} - must be a positive integer")
    if steps < 1:
        raise ValueError(f"Invalid steps value: {steps!r} - must be a positive integer")
    if steps > max_steps:
        return max_steps, True
    return steps, False


@dataclass
class TTSEngineConfig:
    """One text-to-speech engine (one language, one sidecar server).

    Mirrors `ImageGenConfig`'s shape/idiom. `language` is filled in from the
    `tts.engines` mapping key by `TTSConfig.from_dict`, not read from the
    per-engine dict itself. `max_input_chars` (20000) matches the sidecar
    server's own default rejection threshold (see
    `deploy/cpu/kokoro-tts-english/`) so the client-side check and the
    server-side rejection agree.
    """
    language: str = "en"
    base_url: str = "http://localhost:8005"
    model: str = "hexgrad/Kokoro-82M"
    voice: str = "af_heart"
    speed: float = 1.0
    response_format: str = "aac"
    timeout: float = 120.0
    max_input_chars: int = 20000
    max_concurrent_requests: int = 2
    max_concurrent_wait_seconds: float = 180.0

    @classmethod
    def from_dict(cls, language: str, data: Optional[dict]) -> "TTSEngineConfig":
        if not data:
            return cls(language=language)
        return cls(
            language=language,
            base_url=data.get("base_url", "http://localhost:8005"),
            model=data.get("model", "hexgrad/Kokoro-82M"),
            voice=data.get("voice", "af_heart"),
            speed=data.get("speed", 1.0),
            response_format=data.get("response_format", "aac"),
            timeout=data.get("timeout", 120.0),
            max_input_chars=data.get("max_input_chars", 20000),
            max_concurrent_requests=data.get("max_concurrent_requests", 2),
            max_concurrent_wait_seconds=data.get("max_concurrent_wait_seconds", 180.0),
        )


@dataclass
class TTSConfig:
    """Text-to-speech configuration (`text_to_speech` tool).

    `engines` is a mapping keyed by lowercased language code, one entry per
    deployed sidecar (e.g. `deploy/cpu/kokoro-tts-english/`). Repeated top-level
    `tts:` keys can't happen (YAML mappings require unique keys), so a
    per-language mapping under `engines:` is the readable way to support
    more than one language/server without list-of-dicts noise.

    No format validation on language-code keys, matching every other config
    class's permissive style (no sibling config ever raises out of
    `from_dict`): `from_dict` just lowercases every key as it builds the
    dict, so `"DE"` and `"de"` collapse to the same entry (silently keeping
    the last, same as PyYAML does for duplicate mapping keys).
    """
    enabled: bool = False
    default_language: str = "en"
    engines: dict[str, "TTSEngineConfig"] = field(default_factory=lambda: {"en": TTSEngineConfig()})

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "TTSConfig":
        if not data:
            return cls()
        engines_data = data.get("engines")
        if engines_data:
            engines = {
                str(lang).lower(): TTSEngineConfig.from_dict(str(lang).lower(), engine_data)
                for lang, engine_data in engines_data.items()
            }
        else:
            engines = {"en": TTSEngineConfig()}
        return cls(
            enabled=data.get("enabled", False),
            default_language=data.get("default_language", "en"),
            engines=engines,
        )

    def supported_languages(self) -> list[str]:
        return sorted(self.engines.keys())

    def engine_for(self, language: str) -> Optional[TTSEngineConfig]:
        return self.engines.get(language.lower())


@dataclass
class BrowserConfig:
    ws_url: str = "ws://localhost:8931"
    # Deprecated, unused: the global browser lease it configured was replaced
    # by per-scope BrowserContexts (tools/browser_client.py). The YAML key is
    # still accepted so existing agent.yaml files keep loading; the former
    # BROWSER_LEASE_WAIT_TIMEOUT_SECONDS env var is ignored.
    lease_wait_timeout_seconds: int = 240

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "BrowserConfig":
        if not data:
            return cls()
        return cls(
            ws_url=data.get("ws_url", "ws://localhost:8931"),
            lease_wait_timeout_seconds=data.get("lease_wait_timeout_seconds", 240),
        )


@dataclass
class UnknownUsersConfig:
    """auth.unknown_users: whether the agent answers UNKNOWN-tier senders at all.

    allow_direct: 1:1 chats with unknown numbers (default off).
    allow_in_groups: unknown senders in a group, but only while the group has
    at least one KNOWN or PRIVILEGED member (default on).
    Read per message (core.py _unknown_sender_allowed), so a reload applies it.
    """
    allow_direct: bool = False
    allow_in_groups: bool = True

    def __post_init__(self) -> None:
        # Checked at load time, so both startup and /reload reject bad values
        # (a quoted "false" must not silently count as true).
        for name in ("allow_direct", "allow_in_groups"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"auth.unknown_users.{name} must be true or false")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "UnknownUsersConfig":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("auth.unknown_users must be a mapping")
        return cls(
            allow_direct=data.get("allow_direct", False),
            allow_in_groups=data.get("allow_in_groups", True),
        )


@dataclass
class AuthConfig:
    privileged_numbers: list[str] = field(default_factory=list)
    known_numbers: list[str] = field(default_factory=list)
    unknown_users: UnknownUsersConfig = field(default_factory=UnknownUsersConfig)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AuthConfig":
        if not data:
            return cls()
        return cls(
            privileged_numbers=data.get("privileged_numbers", []),
            known_numbers=data.get("known_numbers", []),
            unknown_users=UnknownUsersConfig.from_dict(data.get("unknown_users")),
        )


MESSAGE_MODES = ("off", "append", "replace")
DEFAULT_MESSAGE_MODE = "replace"


@dataclass
class SingleMessageConfig:
    """chat.single_message: how a turn's texts are presented on Signal.

    off = every text is its own Signal message; append/replace = later texts
    of a turn edit the turn's last message (see turn_output.py).
    """
    mode: str = DEFAULT_MESSAGE_MODE
    separator: str = "\n\n"
    max_edits_per_message: int = 9  # Signal drops edits beyond 10
    max_message_chars: int = 2000   # Signal's long-text cutoff
    _effective_mode: str = field(init=False, repr=False)

    def __post_init__(self):
        # An unknown mode falls back to the default at startup (a /reload
        # refuses it instead, see _validate_reloaded).
        if self.mode not in MESSAGE_MODES:
            LOG.warning(
                "chat.single_message.mode=%r is not one of %s; using %r.",
                self.mode, "/".join(MESSAGE_MODES), DEFAULT_MESSAGE_MODE,
            )
            self._effective_mode = DEFAULT_MESSAGE_MODE
        else:
            self._effective_mode = self.mode

    @property
    def effective_mode(self) -> str:
        return self._effective_mode

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SingleMessageConfig":
        if not data:
            return cls()
        mode = data.get("mode", DEFAULT_MESSAGE_MODE)
        if mode is False:
            # YAML 1.1 parses an unquoted `mode: off` as False.
            mode = "off"
        return cls(
            mode=mode,
            separator=data.get("separator", "\n\n"),
            max_edits_per_message=data.get("max_edits_per_message", 9),
            max_message_chars=data.get("max_message_chars", 2000),
        )


@dataclass
class ChatConfig:
    sliding_window_size: int = 10
    min_messages_to_keep: int = 15
    context_size_threshold: int = 50000
    max_keywords: int = 50
    min_messages_to_summarize: int = 10
    tool_call_args_max_chars: int = MIN_TOOL_CALL_ARGS_CHARS
    tool_result_max_chars: int = 8000
    history_tool_result_max_chars: int = 500
    turn_compaction_threshold_tokens: int = 60000
    single_message: SingleMessageConfig = field(default_factory=SingleMessageConfig)
    # Mark the user's message as read when the agent is done with it (end of turn)
    read_receipts: bool = True
    _effective_tool_call_args_max_chars: int = field(init=False, repr=False)
    _effective_tool_result_max_chars: int = field(init=False, repr=False)

    def __post_init__(self):
        # Clamp tool_call_args_max_chars once at config load (= app start).
        if self.tool_call_args_max_chars < MIN_TOOL_CALL_ARGS_CHARS:
            LOG.warning(
                "chat.tool_call_args_max_chars=%d is below the floor of %d; "
                "clamping to %d. Update config/agent.yaml to silence this warning.",
                self.tool_call_args_max_chars,
                MIN_TOOL_CALL_ARGS_CHARS,
                MIN_TOOL_CALL_ARGS_CHARS,
            )
            self._effective_tool_call_args_max_chars = MIN_TOOL_CALL_ARGS_CHARS
        else:
            self._effective_tool_call_args_max_chars = self.tool_call_args_max_chars

        # Clamp tool_result_max_chars
        if self.tool_result_max_chars < MIN_TOOL_RESULT_CHARS:
            LOG.warning(
                "chat.tool_result_max_chars=%d is below the floor of %d; "
                "clamping to %d. Update config/agent.yaml to silence this warning.",
                self.tool_result_max_chars,
                MIN_TOOL_RESULT_CHARS,
                MIN_TOOL_RESULT_CHARS,
            )
            self._effective_tool_result_max_chars = MIN_TOOL_RESULT_CHARS
        else:
            self._effective_tool_result_max_chars = self.tool_result_max_chars

    @property
    def effective_tool_call_args_max_chars(self) -> int:
        return self._effective_tool_call_args_max_chars

    @property
    def effective_tool_result_max_chars(self) -> int:
        return self._effective_tool_result_max_chars

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ChatConfig":
        if not data:
            return cls()
        return cls(
            sliding_window_size=data.get("sliding_window_size", 10),
            min_messages_to_keep=data.get("min_messages_to_keep", 15),
            context_size_threshold=data.get("context_size_threshold", 50000),
            max_keywords=data.get("max_keywords", 50),
            min_messages_to_summarize=data.get("min_messages_to_summarize", 10),
            tool_call_args_max_chars=data.get("tool_call_args_max_chars", MIN_TOOL_CALL_ARGS_CHARS),
            tool_result_max_chars=data.get("tool_result_max_chars", 8000),
            history_tool_result_max_chars=data.get("history_tool_result_max_chars", 500),
            turn_compaction_threshold_tokens=data.get("turn_compaction_threshold_tokens", 60000),
            single_message=SingleMessageConfig.from_dict(data.get("single_message")),
            read_receipts=data.get("read_receipts", True),
        )


@dataclass
class CronConfig:
    poll_interval_seconds: int = 60
    max_failure_count: int = 3

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CronConfig":
        if not data:
            return cls()
        return cls(
            poll_interval_seconds=data.get("poll_interval_seconds", 60),
            max_failure_count=data.get("max_failure_count", 3),
        )


@dataclass
class ConsentConfig:
    """Approval polls (consent.py): how long a poll waits for a vote.

    Read at request time, so a /reload applies to the next poll.
    """
    timeout_minutes: int = 120              # chat turn paused on a poll
    background_timeout_minutes: int = 720   # poll posted by a cronjob/subagent

    def __post_init__(self):
        # Checked at load time, so both startup and /reload reject bad values.
        for name in ("timeout_minutes", "background_timeout_minutes"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"consent.{name} must be an integer > 0")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ConsentConfig":
        if not data:
            return cls()
        return cls(
            timeout_minutes=data.get("timeout_minutes", 120),
            background_timeout_minutes=data.get("background_timeout_minutes", 720),
        )


VOICE_CALL_MODES = ("agent", "parrot")
VOICE_CALL_TIERS = ("PRIVILEGED", "KNOWN")


VOICE_CALL_TTS_SAMPLING = ("temperature", "top_k", "top_p", "repetition_penalty",
                           "subtalker_temperature", "seed")


@dataclass
class VoiceCallsConfig:
    """1:1 Signal voice calls (voice/): signal-cli call API + PulseAudio audio.

    mode "parrot" speaks each transcript straight back (pipeline test without
    the agent). Not hot-reloadable (restart required).
    """
    enabled: bool = False
    jsonrpc_host: str = "127.0.0.1"
    jsonrpc_port: int = 6002
    pulse_server: str = "tcp:127.0.0.1:4713"
    allowed_tiers: list[str] = field(default_factory=lambda: list(VOICE_CALL_TIERS))
    mode: str = "agent"
    vad_aggressiveness: int = 3          # webrtcvad 0 (lenient) .. 3 (strict, fewer noise hits)
    start_speech_ms: int = 60            # speech needed to open an utterance
    end_silence_ms: int = 700            # silence that ends an utterance
    min_speech_ms: int = 250             # shorter utterances are dropped
    max_utterance_seconds: int = 30
    max_call_minutes: int = 30
    barge_in_ms: int = 500               # caller speech that interrupts our speech; 0 = off
    idle_hangup_seconds: int = 180       # hang up after this long without speech either way
    noise_gate: bool = True              # extra auto-language STT pass; empty = noise, dropped
    stt_streaming: bool = False          # stream caller audio to the STT WebSocket (HTTP fallback)
    stt_flush_ms: int = 200              # streaming: silence after which the server decodes; 0 = at the end
    # After a pause, wait this much longer for the caller to go on when the
    # transcript sounds unfinished ("…, und") / is unclear (no end punctuation);
    # a continuation is merged into the same turn. 0 = don't wait.
    unfinished_wait_ms: int = 2000
    unclear_wait_ms: int = 600
    # Sampling settings sent with call speech (deploy/dgx-spark/qwen3-tts):
    # each sentence is generated on its own, lower values keep the delivery
    # steadier from one to the next. {} = the server's defaults.
    tts_sampling: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in VOICE_CALL_MODES:
            raise ValueError(f"voice_calls.mode must be one of {VOICE_CALL_MODES}")
        bad = [t for t in self.allowed_tiers if t not in VOICE_CALL_TIERS]
        if bad:
            raise ValueError(f"voice_calls.allowed_tiers: unknown tier(s) {bad}, allowed {VOICE_CALL_TIERS}")
        if self.vad_aggressiveness not in (0, 1, 2, 3):
            raise ValueError("voice_calls.vad_aggressiveness must be 0..3")
        for name in ("jsonrpc_port", "start_speech_ms", "end_silence_ms", "min_speech_ms",
                     "max_utterance_seconds", "max_call_minutes", "idle_hangup_seconds"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"voice_calls.{name} must be an integer > 0")
        for name in ("barge_in_ms", "stt_flush_ms", "unfinished_wait_ms", "unclear_wait_ms"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"voice_calls.{name} must be an integer >= 0")
        for name in ("noise_gate", "stt_streaming"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"voice_calls.{name} must be true or false")
        if not isinstance(self.tts_sampling, dict):
            raise ValueError("voice_calls.tts_sampling must be a mapping")
        bad = sorted(set(self.tts_sampling) - set(VOICE_CALL_TTS_SAMPLING))
        if bad:
            raise ValueError(f"voice_calls.tts_sampling: unknown key(s) {bad}, allowed {VOICE_CALL_TTS_SAMPLING}")
        for k, v in self.tts_sampling.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"voice_calls.tts_sampling.{k} must be a number")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "VoiceCallsConfig":
        if not data:
            return cls()
        defaults = cls()
        return cls(**{f.name: data.get(f.name, getattr(defaults, f.name)) for f in fields(cls)})


DEFAULT_WEBUI_TEXT_EXTENSIONS = [
    ".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".log", ".js", ".css", ".html", ".xml", ".conf", ".env",
]


def _validate_webui(w: "WebUIConfig") -> list[str]:
    errors: list[str] = []
    if not isinstance(w.enabled, bool):
        errors.append("webui.enabled must be true or false")
    for name in ("api_port", "code_ttl_seconds", "max_upload_mb", "editor_max_kb",
                 "code_requests_per_number_per_hour", "code_requests_per_ip_per_hour"):
        v = getattr(w, name)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(f"webui.{name} must be an integer > 0")
    v = w.token_expiry_days
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        errors.append("webui.token_expiry_days must be an integer >= 0")
    for name in ("hidden_paths", "text_extensions"):
        value = getattr(w, name)
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            errors.append(f"webui.{name} must be a list of non-empty strings")
    return errors


@dataclass
class WebUIConfig:
    """Web interface backend (src/nixclaw_agent/web/).

    Everything except enabled/api_host/api_port is read per request, so a
    reload applies it immediately.
    """
    enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 3003
    code_ttl_seconds: int = 300
    token_expiry_days: int = 90             # 0 = tokens never expire
    max_upload_mb: int = 50
    editor_max_kb: int = 1024
    hidden_paths: list[str] = field(default_factory=lambda: ["browser", "memories"])
    text_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_WEBUI_TEXT_EXTENSIONS))
    code_requests_per_number_per_hour: int = 5
    code_requests_per_ip_per_hour: int = 20

    def __post_init__(self) -> None:
        # Checked at load time, so both startup and /reload reject bad values.
        errors = _validate_webui(self)
        if errors:
            raise ValueError("; ".join(errors))

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "WebUIConfig":
        if not data:
            return cls()
        d = cls()
        return cls(
            enabled=data.get("enabled", d.enabled),
            api_host=data.get("api_host", d.api_host),
            api_port=data.get("api_port", d.api_port),
            code_ttl_seconds=data.get("code_ttl_seconds", d.code_ttl_seconds),
            token_expiry_days=data.get("token_expiry_days", d.token_expiry_days),
            max_upload_mb=data.get("max_upload_mb", d.max_upload_mb),
            editor_max_kb=data.get("editor_max_kb", d.editor_max_kb),
            hidden_paths=data.get("hidden_paths", d.hidden_paths),
            text_extensions=data.get("text_extensions", d.text_extensions),
            code_requests_per_number_per_hour=data.get(
                "code_requests_per_number_per_hour", d.code_requests_per_number_per_hour),
            code_requests_per_ip_per_hour=data.get(
                "code_requests_per_ip_per_hour", d.code_requests_per_ip_per_hour),
        )


@dataclass
class PathsConfig:
    data_dir: Path = Path("/data")
    tools_dir: Path = Path("/tools")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PathsConfig":
        if not data:
            return cls()
        data_dir = data.get("data_dir", "/data")
        tools_dir = data.get("tools_dir", "/tools")
        return cls(
            data_dir=Path(data_dir),
            tools_dir=Path(tools_dir),
        )


@dataclass
class DatabaseConfig:
    db_path: Path = Path("/data/messages.db")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "DatabaseConfig":
        if not data:
            return cls()
        db_path = data.get("db_path", "/data/messages.db")
        return cls(
            db_path=Path(db_path),
        )


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Path = Path("/logs/nixclaw-agent.log")

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "LoggingConfig":
        if not data:
            return cls()
        log_file = data.get("file", "/logs/nixclaw-agent.log")
        return cls(
            level=data.get("level", "INFO"),
            file=Path(log_file),
        )


@dataclass
class DebugLoggingConfig:
    mode: str = "auto"
    file: Path = Path("/logs/debug-agent.json")
    buffer_size: int = 400

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "DebugLoggingConfig":
        if not data:
            return cls()
        debug_file = data.get("file", "/logs/debug-agent.json")
        return cls(
            mode=data.get("mode", "auto"),
            file=Path(debug_file),
            buffer_size=data.get("buffer_size", 400),
        )


@dataclass
class AgentConfig:
    name: str = "Nixclaw"

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AgentConfig":
        if not data:
            return cls()
        return cls(
            name=data.get("name", "Nixclaw"),
        )


@dataclass
class Config:
    backend: str = "ollama"
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    vllm: VllmConfig = field(default_factory=VllmConfig)
    signal: SignalConfig = field(default_factory=SignalConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    image_gen: ImageGenConfig = field(default_factory=ImageGenConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    cron: CronConfig = field(default_factory=CronConfig)
    consent: ConsentConfig = field(default_factory=ConsentConfig)
    voice_calls: VoiceCallsConfig = field(default_factory=VoiceCallsConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    debug_logging: DebugLoggingConfig = field(default_factory=DebugLoggingConfig)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Config":
        if not data:
            return cls()
        return cls(
            backend=data.get("backend", "ollama"),
            ollama=OllamaConfig.from_dict(data.get("ollama")),
            vllm=VllmConfig.from_dict(data.get("vllm")),
            signal=SignalConfig.from_dict(data.get("signal")),
            browser=BrowserConfig.from_dict(data.get("browser")),
            stt=SttConfig.from_dict(data.get("stt")),
            image_gen=ImageGenConfig.from_dict(data.get("image_gen")),
            tts=TTSConfig.from_dict(data.get("tts")),
            auth=AuthConfig.from_dict(data.get("auth")),
            chat=ChatConfig.from_dict(data.get("chat")),
            cron=CronConfig.from_dict(data.get("cron")),
            consent=ConsentConfig.from_dict(data.get("consent")),
            voice_calls=VoiceCallsConfig.from_dict(data.get("voice_calls")),
            webui=WebUIConfig.from_dict(data.get("webui")),
            paths=PathsConfig.from_dict(data.get("paths")),
            database=DatabaseConfig.from_dict(data.get("database")),
            logging=LoggingConfig.from_dict(data.get("logging")),
            agent=AgentConfig.from_dict(data.get("agent")),
            debug_logging=DebugLoggingConfig.from_dict(data.get("debug_logging")),
        )


def _apply_env_overrides(cfg: Config) -> Config:
    """Apply environment variable overrides to configuration."""
    backend = os.environ.get("LLM_BACKEND")
    if backend:
        cfg.backend = backend

    ollama_host = os.environ.get("OLLAMA_BASE_URL")
    if ollama_host:
        cfg.ollama.host = ollama_host

    ollama_model = os.environ.get("OLLAMA_MODEL")
    if ollama_model:
        cfg.ollama.model = ollama_model

    vllm_host = os.environ.get("VLLM_BASE_URL")
    if vllm_host:
        cfg.vllm.host = vllm_host

    vllm_model = os.environ.get("VLLM_MODEL")
    if vllm_model:
        cfg.vllm.model = vllm_model

    vllm_vision_model = os.environ.get("VLLM_VISION_MODEL")
    if vllm_vision_model:
        cfg.vllm.vision_model = vllm_vision_model

    signal_api_url = os.environ.get("SIGNAL_API_URL")
    if signal_api_url:
        cfg.signal.api_url = signal_api_url

    signal_phone = os.environ.get("SIGNAL_PHONE_NUMBER")
    if signal_phone:
        cfg.signal.phone_number = signal_phone

    signal_webhook_port = os.environ.get("SIGNAL_WEBHOOK_PORT")
    if signal_webhook_port:
        cfg.signal.webhook_port = int(signal_webhook_port)

    signal_webhook_secret = os.environ.get("SIGNAL_WEBHOOK_SECRET")
    if signal_webhook_secret:
        cfg.signal.webhook_secret = signal_webhook_secret

    signal_mock_api_url = os.environ.get("SIGNAL_MOCK_API_URL")
    if signal_mock_api_url:
        cfg.signal.mock_api_url = signal_mock_api_url

    browser_ws_url = os.environ.get("BROWSER_WS_URL")
    if browser_ws_url:
        cfg.browser.ws_url = browser_ws_url

    stt_enabled = os.environ.get("STT_ENABLED")
    if stt_enabled:  # empty (docker-compose default) = keep agent.yaml's value
        cfg.stt.enabled = stt_enabled.lower() in ("1", "true", "yes", "on")

    stt_base_url = os.environ.get("STT_BASE_URL")
    if stt_base_url:
        cfg.stt.base_url = stt_base_url

    stt_model = os.environ.get("STT_MODEL")
    if stt_model:
        cfg.stt.model = stt_model

    stt_language = os.environ.get("STT_LANGUAGE")
    if stt_language:  # empty (docker-compose default) = keep agent.yaml's value
        cfg.stt.language = None if stt_language.lower() == "auto" else stt_language

    imagegen_enabled = os.environ.get("IMAGEGEN_ENABLED")
    if imagegen_enabled is not None:
        cfg.image_gen.enabled = imagegen_enabled.lower() in ("1", "true", "yes", "on")

    imagegen_base_url = os.environ.get("IMAGEGEN_BASE_URL")
    if imagegen_base_url:
        cfg.image_gen.base_url = imagegen_base_url

    imagegen_model = os.environ.get("IMAGEGEN_MODEL")
    if imagegen_model:
        cfg.image_gen.model = imagegen_model

    imagegen_timeout = os.environ.get("IMAGEGEN_TIMEOUT")
    if imagegen_timeout:
        cfg.image_gen.timeout = float(imagegen_timeout)

    imagegen_steps = os.environ.get("IMAGEGEN_STEPS")
    if imagegen_steps:
        cfg.image_gen.default_steps = int(imagegen_steps)

    imagegen_max_steps = os.environ.get("IMAGEGEN_MAX_STEPS")
    if imagegen_max_steps:
        cfg.image_gen.max_steps = int(imagegen_max_steps)

    imagegen_max_input_images = os.environ.get("IMAGEGEN_MAX_INPUT_IMAGES")
    if imagegen_max_input_images:
        cfg.image_gen.max_input_images = int(imagegen_max_input_images)

    # TTS env overrides apply only to the default-language engine (mirrors
    # STT/imagegen's single-engine override style). Full multi-engine setups
    # (multiple languages/base_urls under `tts.engines`) stay YAML-only -
    # there's no per-language env var namespace to disambiguate them.
    tts_default_engine = cfg.tts.engines.get(cfg.tts.default_language.lower())

    tts_enabled = os.environ.get("TTS_ENABLED")
    if tts_enabled is not None:
        cfg.tts.enabled = tts_enabled.lower() in ("1", "true", "yes", "on")

    tts_base_url = os.environ.get("TTS_BASE_URL")
    if tts_base_url and tts_default_engine is not None:
        tts_default_engine.base_url = tts_base_url

    tts_timeout = os.environ.get("TTS_TIMEOUT")
    if tts_timeout and tts_default_engine is not None:
        tts_default_engine.timeout = float(tts_timeout)

    debug_logging_mode = os.environ.get("DEBUG_LOGGING_MODE")
    if debug_logging_mode:
        cfg.debug_logging.mode = debug_logging_mode

    LOG.info(f"CONFIG: backend={cfg.backend}")
    LOG.info(f"CONFIG: chat.tool_call_args_max_chars={cfg.chat.effective_tool_call_args_max_chars}")
    LOG.info(f"CONFIG: chat.tool_result_max_chars={cfg.chat.effective_tool_result_max_chars}")
    LOG.info(f"CONFIG: chat.history_tool_result_max_chars={cfg.chat.history_tool_result_max_chars}")
    LOG.info(f"CONFIG: chat.turn_compaction_threshold_tokens={cfg.chat.turn_compaction_threshold_tokens}")
    if cfg.backend == "vllm":
        model_str = cfg.vllm.model or "(auto)"
        LOG.info(f"CONFIG: vllm.host={cfg.vllm.host}, vllm.model={model_str}")
        LOG.info(f"CONFIG: vllm.context_window={cfg.vllm.options.context_window}")
    else:
        LOG.info(f"CONFIG: ollama.host={cfg.ollama.host}, ollama.model={cfg.ollama.model}")

    return cfg


def _resolve_paths(cfg: Config) -> Config:
    """Resolve paths for Docker container environment."""
    cfg.paths.data_dir = Path(cfg.paths.data_dir)
    cfg.paths.tools_dir = Path(cfg.paths.tools_dir)
    cfg.database.db_path = Path(cfg.database.db_path)
    cfg.logging.file = Path(cfg.logging.file)
    cfg.debug_logging.file = Path(cfg.debug_logging.file)
    return cfg


def load_config(raw: Optional[dict] = None) -> Config:
    """Build a Config from agent.yaml (or `raw`) with paths and env overrides applied."""
    if raw is None:
        raw = _load_yaml_config()
    return _apply_env_overrides(_resolve_paths(Config.from_dict(raw)))


config = load_config()


# --- Live reload ------------------------------------------------------------

# Config sections whose new values are hot-applied by a reload (logging.level is
# handled separately). Every other changed section is reported as "restart
# required" and left untouched on the live object.
HOT_SECTIONS = ("auth", "chat", "agent", "consent")
# webui: hot-applied except these fields (the API server binds once at startup).
WEBUI_RESTART_FIELDS = ("enabled", "api_host", "api_port")


@dataclass
class ConfigReloadPlan:
    """Validated result of re-reading agent.yaml; nothing is applied yet."""

    new: Optional[Config] = None
    errors: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)


def _validate_reloaded(new: Config) -> list[str]:
    errors: list[str] = []
    for name in ("privileged_numbers", "known_numbers"):
        value = getattr(new.auth, name)
        if not isinstance(value, list) or not all(isinstance(n, str) and n for n in value):
            errors.append(f"auth.{name} must be a list of non-empty strings")
    if not errors and not new.auth.privileged_numbers:
        errors.append("auth.privileged_numbers must not be empty (would lock everyone out)")
    for f in fields(new.chat):
        if f.init and f.type in (int, "int"):
            v = getattr(new.chat, f.name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                errors.append(f"chat.{f.name} must be an integer > 0")
    sm = new.chat.single_message
    if sm.mode not in MESSAGE_MODES:
        errors.append(f"chat.single_message.mode must be one of {', '.join(MESSAGE_MODES)}")
    for name in ("max_edits_per_message", "max_message_chars"):
        v = getattr(sm, name)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errors.append(f"chat.single_message.{name} must be an integer > 0")
    if not isinstance(sm.separator, str):
        errors.append("chat.single_message.separator must be a string")
    if not isinstance(new.chat.read_receipts, bool):
        errors.append("chat.read_receipts must be true or false")
    if not isinstance(new.agent.name, str) or not new.agent.name.strip():
        errors.append("agent.name must be a non-empty string")
    if not isinstance(new.logging.level, str) or not isinstance(
        logging.getLevelName(new.logging.level.upper()), int
    ):
        errors.append(f"logging.level {new.logging.level!r} is not a valid log level")
    return errors



def plan_config_reload(current: Config) -> ConfigReloadPlan:
    """Re-read agent.yaml and validate it against `current` without applying anything."""
    plan = ConfigReloadPlan()
    try:
        raw = _load_yaml_config()
        if not raw:
            plan.errors.append(f"{CONFIG_PATH} is missing or empty")
            return plan
        new = load_config(raw)
    except Exception as e:
        plan.errors.append(f"cannot load {CONFIG_PATH}: {e}")
        return plan
    plan.errors = _validate_reloaded(new)
    if plan.errors:
        return plan
    plan.new = new
    for f in fields(Config):
        if getattr(current, f.name) == getattr(new, f.name):
            continue
        if f.name in HOT_SECTIONS:
            plan.applied.append(f.name)
        elif f.name == "webui":
            restart = [n for n in WEBUI_RESTART_FIELDS
                       if getattr(current.webui, n) != getattr(new.webui, n)]
            plan.restart_required += [f"webui.{n}" for n in restart]
            if any(getattr(current.webui, x.name) != getattr(new.webui, x.name)
                   for x in fields(WebUIConfig) if x.name not in WEBUI_RESTART_FIELDS):
                plan.applied.append("webui")
        elif f.name == "logging":
            if current.logging.level != new.logging.level:
                plan.applied.append("logging.level")
            if current.logging.file != new.logging.file:
                plan.restart_required.append("logging")
        else:
            plan.restart_required.append(f.name)
    return plan


def apply_config_reload(current: Config, plan: ConfigReloadPlan) -> None:
    """Assign the whitelisted sections of a valid plan onto the live config object."""
    assert plan.new is not None
    for name in HOT_SECTIONS:
        setattr(current, name, getattr(plan.new, name))
    current.logging.level = plan.new.logging.level
    webui = dataclasses.replace(
        plan.new.webui, **{n: getattr(current.webui, n) for n in WEBUI_RESTART_FIELDS}
    )
    current.webui = webui
