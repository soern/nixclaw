"""Text-to-speech tools backed by the TTS sidecar(s) (see tts.py).

text_to_speech synthesizes speech from text for a supported language. The
main agent composes the text itself; this tool does not rewrite it.
list_tts_voices / set_tts_voice query the servers' voice catalogs and store
a per-channel, per-language voice choice in data/<channel>/tts_voice.json
(missing file/language -> the engine's configured `voice`). Voice
resolution in text_to_speech: explicit `voice` argument > channel setting >
engine config.

On success the audio file is written to the channel's attachments/
directory and only the bare filename is returned, so the LLM can pass it
straight to send_signal_attachment - same division of labour as
generate_image (tools/imagegen_ops.py).
"""

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import config, TTSEngineConfig
from ..path_safety import is_within_directory
from ..tts import KokoroTTSClient, TTSConcurrencyLimitError, TTSError
from ..types import ToolResult
from .paths import get_attachments_dir

LOG = logging.getLogger(__name__)

_TOOL_NAME = "text_to_speech"


TTS_VOICE_CONFIG_PATH = Path("tts_voice.json")


def _error(msg: str, name: str = _TOOL_NAME) -> ToolResult:
    return ToolResult(name=name, success=False, output=None, error=msg)


def _voice_file(channel_id: str) -> Path:
    context_dir = (config.paths.data_dir / channel_id).resolve()
    if not channel_id or not is_within_directory(context_dir, config.paths.data_dir):
        raise ValueError("Path traversal detected")
    voice_path = (context_dir / TTS_VOICE_CONFIG_PATH).resolve()
    if not is_within_directory(voice_path, context_dir):
        raise ValueError("Path traversal detected")
    return voice_path


def _channel_voices(channel_id: str) -> dict[str, str]:
    """The channel's stored {language: voice} choices ({} if none/unreadable)."""
    try:
        voice_path = _voice_file(channel_id)
        if voice_path.exists():
            with open(voice_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
    except Exception as e:
        LOG.warning(f"Could not read TTS voices for {channel_id}: {e}")
    return {}


def channel_voice(channel_id: str, language: str) -> Optional[str]:
    """The channel's stored voice for `language` (set_tts_voice), or None."""
    return _channel_voices(channel_id).get(language)


def _normalize_catalog(catalog: dict) -> tuple[list[str], dict[str, dict]]:
    """Split a /v1/voices body into (built-in names, {cloned name: metadata}).

    The sidecars differ: deploy/dgx-spark/qwen3-tts returns `presets` (list) +
    `cloned` (dict); Kokoro/Kyutai return `voices` as a list of names;
    deploy/dgx-spark/qwen3-tts-voice-clone returns `voices` as {name: metadata}.
    """
    builtin = [str(v) for v in catalog.get("presets") or []]
    cloned: dict[str, dict] = {}
    for key in ("cloned", "voices"):
        value = catalog.get(key)
        if isinstance(value, dict):
            cloned.update({str(k): (m if isinstance(m, dict) else {}) for k, m in value.items()})
        elif isinstance(value, list):
            builtin.extend(str(v) for v in value)
    return builtin, cloned


def _usable_voices(catalog: dict) -> set[str]:
    """Voice names a server's /v1/voices listing says can be synthesized."""
    builtin, cloned = _normalize_catalog(catalog)
    return set(builtin) | {name for name, meta in cloned.items() if not meta.get("error")}


async def text_to_speech(
    text: str,
    language: str = "",
    voice: Optional[str] = None,
    channel_id: str = "",
) -> ToolResult:
    """Synthesize speech from text and save the audio to the channel's attachments/.

    Args:
        text: The text to synthesize.
        language: Language code selecting the TTS engine (empty = tts.default_language).
        voice: Optional voice name override for the resolved engine.
        channel_id: The channel ID to scope file operations to (injected).

    Returns:
        ToolResult whose output is the bare filename on success.
    """
    try:
        if not config.tts.enabled:
            return _error("Text-to-speech is disabled (set tts.enabled: true in config/agent.yaml)")

        engine_cfg = config.tts.engine_for(language or config.tts.default_language)
        if engine_cfg is None:
            return _error(
                f"Unsupported TTS language {language!r}. "
                f"Supported: {', '.join(config.tts.supported_languages())}"
            )

        if not text or not text.strip():
            return _error("text must not be empty")

        try:
            attachments_dir = get_attachments_dir(channel_id)
        except ValueError:
            return _error(f"Invalid channel_id: {channel_id!r}")

        channel_voice = None if voice else _channel_voices(channel_id).get(engine_cfg.language)
        client = KokoroTTSClient(engine_cfg)
        try:
            audio_bytes, ext = await client.synthesize(text, voice=voice or channel_voice)
        except TTSConcurrencyLimitError as e:
            return _error(str(e))
        except TTSError as e:
            hint = ""
            if channel_voice:
                hint = (
                    f" (this chat's saved voice for {engine_cfg.language!r} is {channel_voice!r}; "
                    "check list_tts_voices and change it with set_tts_voice)"
                )
            return _error(f"Text-to-speech failed: {e}{hint}")

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        random_suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
        filename = f"speech_{timestamp}_{random_suffix}.{ext}"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        filepath = (attachments_dir / filename).resolve()
        if filepath.parent != attachments_dir:
            return _error("Invalid filename: must be a plain filename without directory parts")
        filepath.write_bytes(audio_bytes)

        return ToolResult(name=_TOOL_NAME, success=True, output=filepath.name, error=None)
    except Exception as e:
        LOG.error(f"text_to_speech failed: {e}")
        return _error(str(e))


async def list_tts_voices(language: str = "", channel_id: str = "") -> ToolResult:
    """List the voice in effect per language and the voices each server offers.

    Args:
        language: Only this language code; empty = all configured languages.
        channel_id: The channel ID whose stored voices to report (injected).
    """
    name = "list_tts_voices"
    try:
        if not config.tts.enabled:
            return _error("Text-to-speech is disabled (set tts.enabled: true in config/agent.yaml)", name)
        if language:
            engine_cfg = config.tts.engine_for(language)
            if engine_cfg is None:
                return _error(
                    f"Unsupported TTS language {language!r}. "
                    f"Supported: {', '.join(config.tts.supported_languages())}",
                    name,
                )
            engines = [engine_cfg]
        else:
            engines = [config.tts.engines[lang] for lang in config.tts.supported_languages()]

        stored = _channel_voices(channel_id)
        lines = ["Voice in effect per language:"]
        by_server: dict[str, list[TTSEngineConfig]] = {}
        for engine in engines:
            override = stored.get(engine.language)
            if override:
                lines.append(
                    f"- {engine.language}: {override} (this chat's setting; configured default: {engine.voice})"
                )
            else:
                lines.append(f"- {engine.language}: {engine.voice} (configured default)")
            by_server.setdefault(engine.base_url, []).append(engine)

        for base_url, server_engines in by_server.items():
            langs = ", ".join(e.language for e in server_engines)
            try:
                catalog = await KokoroTTSClient(server_engines[0]).list_voices()
            except TTSError as e:
                lines.append(f"\nVoices for {langs}: could not be listed ({e})")
                continue
            if catalog is None:
                lines.append(
                    f"\nVoices for {langs}: this server cannot list its voices; "
                    "only the configured voice is known to work"
                )
                continue
            lines.append(f"\nVoices on the server for {langs}:")
            presets, cloned = _normalize_catalog(catalog)
            if presets:
                lines.append(f"- built-in: {', '.join(presets)}")
            if cloned:
                described = []
                for voice_name, meta in sorted(cloned.items()):
                    if meta.get("error"):
                        described.append(f"{voice_name} (broken, unusable)")
                    elif meta.get("language"):
                        described.append(f"{voice_name} (cloned, recorded in {meta['language']})")
                    else:
                        described.append(f"{voice_name} (cloned)")
                lines.append(f"- cloned: {', '.join(described)}")
            if not presets and not cloned:
                lines.append("- (server reported no voices)")

        return ToolResult(name=name, success=True, output="\n".join(lines), error=None)
    except Exception as e:
        LOG.error(f"list_tts_voices failed: {e}")
        return _error(str(e), name)


async def set_tts_voice(language: str = "", voice: str = "", channel_id: str = "") -> ToolResult:
    """Store this channel's voice for one language; empty voice = back to the default.

    Args:
        language: Language code of the engine (default: tts.default_language).
        voice: Voice name; empty resets to the engine's configured voice.
        channel_id: The channel ID to store the setting for (injected).
    """
    name = "set_tts_voice"
    try:
        if not config.tts.enabled:
            return _error("Text-to-speech is disabled (set tts.enabled: true in config/agent.yaml)", name)
        engine_cfg = config.tts.engine_for(language or config.tts.default_language)
        if engine_cfg is None:
            return _error(
                f"Unsupported TTS language {language!r}. "
                f"Supported: {', '.join(config.tts.supported_languages())}",
                name,
            )
        try:
            voice_path = _voice_file(channel_id)
        except ValueError:
            return _error(f"Invalid channel_id: {channel_id!r}", name)

        voice = (voice or "").strip()
        note = ""
        if voice and voice != engine_cfg.voice:
            try:
                catalog = await KokoroTTSClient(engine_cfg).list_voices()
            except TTSError as e:
                return _error(f"Could not check the voice against the server: {e}", name)
            if catalog is None:
                note = " (the server cannot list its voices, so the name was not checked)"
            else:
                usable = _usable_voices(catalog)
                if voice not in usable:
                    return _error(
                        f"Unknown voice {voice!r} for {engine_cfg.language!r}. "
                        f"Available: {', '.join(sorted(usable)) or '(none)'}",
                        name,
                    )

        stored = _channel_voices(channel_id)
        if voice and voice != engine_cfg.voice:
            stored[engine_cfg.language] = voice
            message = f"Voice for {engine_cfg.language!r} set to {voice!r} for this chat{note}"
        else:
            stored.pop(engine_cfg.language, None)
            message = f"Voice for {engine_cfg.language!r} reset to the default {engine_cfg.voice!r}"
        voice_path.parent.mkdir(parents=True, exist_ok=True)
        with open(voice_path, "w") as f:
            json.dump(stored, f, indent=2)
        return ToolResult(name=name, success=True, output=message, error=None)
    except Exception as e:
        LOG.error(f"set_tts_voice failed: {e}")
        return _error(str(e), name)
