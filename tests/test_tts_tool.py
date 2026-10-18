"""Unit tests for the text_to_speech tool (src/nixclaw_agent/tools/tts_ops.py).

Covers:
- disabled kill-switch
- unsupported language (error lists supported codes)
- empty/whitespace text rejection
- invalid channel_id
- success: audio file lands in {data_dir}/{channel_id}/attachments/, bare filename returned
- client error propagation (TTSError, TTSConcurrencyLimitError)
- per-channel voices: list_tts_voices / set_tts_voice and the voice
  resolution order in text_to_speech (argument > channel > config)
"""

import json
import re
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.nixclaw_agent import config as config_module
from src.nixclaw_agent.config import TTSEngineConfig
from src.nixclaw_agent.tts import TTSConcurrencyLimitError, TTSError
from src.nixclaw_agent.tools import tts_ops


AUDIO_BYTES = b"FAKEAUDIODATA"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.config.paths, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.config.tts, "enabled", True)
    monkeypatch.setattr(
        config_module.config.tts, "engines", {"de": TTSEngineConfig(language="de")}
    )
    monkeypatch.setattr(config_module.config.tts, "default_language", "de")
    return config_module.config


@contextmanager
def _mock_client(
    synth_result=(AUDIO_BYTES, "aac"),
    synth_side_effect=None,
    catalog=None,
    list_side_effect=None,
):
    """Patch tts_ops.KokoroTTSClient; yields the client instance mock."""
    instance = MagicMock()
    instance.synthesize = AsyncMock(
        return_value=synth_result, side_effect=synth_side_effect
    )
    instance.list_voices = AsyncMock(return_value=catalog, side_effect=list_side_effect)
    with patch.object(tts_ops, "KokoroTTSClient", return_value=instance):
        yield instance


class TestTextToSpeechTool:
    @pytest.mark.asyncio
    async def test_disabled_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_module.config.paths, "data_dir", tmp_path)
        monkeypatch.setattr(config_module.config.tts, "enabled", False)
        result = await tts_ops.text_to_speech("Hallo", channel_id="chan1")
        assert result.success is False
        assert "disabled" in result.error
        assert "tts.enabled" in result.error

    @pytest.mark.asyncio
    async def test_unsupported_language_lists_supported_codes(self, cfg):
        result = await tts_ops.text_to_speech("hello", language="fr", channel_id="chan1")
        assert result.success is False
        assert "fr" in result.error
        assert "de" in result.error

    @pytest.mark.asyncio
    async def test_empty_text_rejected(self, cfg):
        result = await tts_ops.text_to_speech("", channel_id="chan1")
        assert result.success is False
        assert "text must not be empty" in result.error

    @pytest.mark.asyncio
    async def test_whitespace_text_rejected(self, cfg):
        result = await tts_ops.text_to_speech("   \n\t  ", channel_id="chan1")
        assert result.success is False
        assert "text must not be empty" in result.error

    @pytest.mark.asyncio
    async def test_channel_id_traversal_rejected(self, cfg):
        result = await tts_ops.text_to_speech("hello", channel_id="../etc")
        assert result.success is False
        assert "Invalid channel_id" in result.error

    @pytest.mark.asyncio
    async def test_success_writes_audio_and_returns_filename(self, cfg):
        with _mock_client() as client:
            result = await tts_ops.text_to_speech("Guten Morgen", channel_id="chan1")
        assert result.success is True
        assert result.error is None
        assert re.fullmatch(
            r"speech_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[a-z0-9]{4}\.aac",
            result.output,
        )
        filepath = cfg.paths.data_dir / "chan1" / "attachments" / result.output
        assert filepath.exists()
        assert filepath.read_bytes() == AUDIO_BYTES
        client.synthesize.assert_awaited_once()
        _, kwargs = client.synthesize.call_args
        assert kwargs.get("voice") is None

    @pytest.mark.asyncio
    async def test_voice_forwarded(self, cfg):
        with _mock_client() as client:
            await tts_ops.text_to_speech("hi", voice="martin", channel_id="chan1")
        _, kwargs = client.synthesize.call_args
        assert kwargs["voice"] == "martin"

    @pytest.mark.asyncio
    async def test_extension_from_client_used(self, cfg):
        with _mock_client(synth_result=(b"OGGDATA", "ogg")):
            result = await tts_ops.text_to_speech("hi", channel_id="chan1")
        assert result.output.endswith(".ogg")

    @pytest.mark.asyncio
    async def test_client_error_propagated_as_failure(self, cfg):
        with _mock_client(synth_side_effect=TTSError("boom")):
            result = await tts_ops.text_to_speech("hi", channel_id="chan1")
        assert result.success is False
        assert "Text-to-speech failed" in result.error

    @pytest.mark.asyncio
    async def test_concurrency_limit_propagated(self, cfg):
        with _mock_client(synth_side_effect=TTSConcurrencyLimitError("timed out")):
            result = await tts_ops.text_to_speech("hi", channel_id="chan1")
        assert result.success is False
        assert "timed out" in result.error


CATALOG = {
    "presets": ["aiden", "ryan"],
    "cloned": {
        "de-voiceclip": {"language": "de"},
        "broken-clip": {"error": "bad file"},
    },
    "default_voices": {"de": "de-voiceclip", "en": "aiden"},
}


@pytest.fixture
def two_lang_cfg(cfg, monkeypatch):
    monkeypatch.setattr(
        config_module.config.tts,
        "engines",
        {
            "de": TTSEngineConfig(language="de", base_url="http://tts:8010", voice="de-voiceclip"),
            "en": TTSEngineConfig(language="en", base_url="http://tts:8010", voice="aiden"),
        },
    )
    return cfg


class TestTtsVoices:
    @pytest.mark.asyncio
    async def test_set_then_synthesize_uses_channel_voice(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG) as client:
            result = await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            assert result.success, result.error
            await tts_ops.text_to_speech("hi", language="en", channel_id="chan1")
        assert client.synthesize.call_args.kwargs["voice"] == "ryan"
        stored = json.loads((two_lang_cfg.paths.data_dir / "chan1" / "tts_voice.json").read_text())
        assert stored == {"en": "ryan"}

    @pytest.mark.asyncio
    async def test_channel_voice_is_per_language(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG) as client:
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            await tts_ops.text_to_speech("hallo", language="de", channel_id="chan1")
        # None -> the client falls back to the engine's configured voice
        assert client.synthesize.call_args.kwargs["voice"] is None

    @pytest.mark.asyncio
    async def test_channel_voice_is_per_channel(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG) as client:
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            await tts_ops.text_to_speech("hi", language="en", channel_id="chan2")
        assert client.synthesize.call_args.kwargs["voice"] is None

    @pytest.mark.asyncio
    async def test_explicit_voice_beats_channel_voice(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG) as client:
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            await tts_ops.text_to_speech("hi", language="en", voice="aiden", channel_id="chan1")
        assert client.synthesize.call_args.kwargs["voice"] == "aiden"

    @pytest.mark.asyncio
    async def test_synthesis_error_mentions_saved_voice(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG):
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
        with _mock_client(synth_side_effect=TTSError("unknown voice 'ryan'")):
            result = await tts_ops.text_to_speech("hi", language="en", channel_id="chan1")
        assert result.success is False
        assert "saved voice" in result.error and "set_tts_voice" in result.error

    @pytest.mark.asyncio
    async def test_reset_with_empty_voice(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG) as client:
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            result = await tts_ops.set_tts_voice("en", "", channel_id="chan1")
            assert result.success and "reset" in result.output
            await tts_ops.text_to_speech("hi", language="en", channel_id="chan1")
        assert client.synthesize.call_args.kwargs["voice"] is None

    @pytest.mark.asyncio
    async def test_setting_configured_default_clears_override(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG):
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            await tts_ops.set_tts_voice("en", "aiden", channel_id="chan1")
        stored = json.loads((two_lang_cfg.paths.data_dir / "chan1" / "tts_voice.json").read_text())
        assert stored == {}

    @pytest.mark.asyncio
    async def test_unknown_voice_rejected_with_available_list(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG):
            result = await tts_ops.set_tts_voice("en", "nobody", channel_id="chan1")
        assert result.success is False
        assert "ryan" in result.error and "de-voiceclip" in result.error
        assert "broken-clip" not in result.error
        assert not (two_lang_cfg.paths.data_dir / "chan1" / "tts_voice.json").exists()

    @pytest.mark.asyncio
    async def test_broken_clone_rejected(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG):
            result = await tts_ops.set_tts_voice("en", "broken-clip", channel_id="chan1")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_set_unsupported_language(self, two_lang_cfg):
        result = await tts_ops.set_tts_voice("xx", "ryan", channel_id="chan1")
        assert result.success is False
        assert "de, en" in result.error

    @pytest.mark.asyncio
    async def test_set_without_listing_stores_with_note(self, two_lang_cfg):
        with _mock_client(catalog=None) as client:
            result = await tts_ops.set_tts_voice("en", "whoever", channel_id="chan1")
            assert result.success and "not checked" in result.output
            await tts_ops.text_to_speech("hi", language="en", channel_id="chan1")
        assert client.synthesize.call_args.kwargs["voice"] == "whoever"

    @pytest.mark.asyncio
    async def test_set_listing_error_fails(self, two_lang_cfg):
        with _mock_client(list_side_effect=TTSError("down")):
            result = await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
        assert result.success is False and "down" in result.error

    @pytest.mark.asyncio
    async def test_set_channel_traversal_rejected(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG):
            result = await tts_ops.set_tts_voice("en", "ryan", channel_id="../evil")
        assert result.success is False
        assert "Invalid channel_id" in result.error

    @pytest.mark.asyncio
    async def test_corrupt_voice_file_ignored(self, two_lang_cfg):
        path = two_lang_cfg.paths.data_dir / "chan1" / "tts_voice.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        with _mock_client() as client:
            result = await tts_ops.text_to_speech("hi", language="en", channel_id="chan1")
        assert result.success
        assert client.synthesize.call_args.kwargs["voice"] is None

    @pytest.mark.asyncio
    async def test_list_shows_effective_voices_and_catalog(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG) as client:
            await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")
            client.list_voices.reset_mock()
            result = await tts_ops.list_tts_voices(channel_id="chan1")
        assert result.success, result.error
        out = result.output
        assert "- de: de-voiceclip (configured default)" in out
        assert "- en: ryan (this chat's setting; configured default: aiden)" in out
        assert "built-in: aiden, ryan" in out
        assert "de-voiceclip (cloned, recorded in de)" in out
        assert "broken-clip (broken, unusable)" in out
        # both engines share one base_url -> one listing call
        assert client.list_voices.await_count == 1

    @pytest.mark.asyncio
    async def test_list_single_language(self, two_lang_cfg):
        with _mock_client(catalog=CATALOG):
            result = await tts_ops.list_tts_voices("de", channel_id="chan1")
        assert "- de:" in result.output and "- en:" not in result.output

    @pytest.mark.asyncio
    async def test_list_unsupported_language(self, two_lang_cfg):
        result = await tts_ops.list_tts_voices("xx", channel_id="chan1")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_list_without_server_listing(self, two_lang_cfg):
        with _mock_client(catalog=None):
            result = await tts_ops.list_tts_voices(channel_id="chan1")
        assert result.success
        assert "cannot list its voices" in result.output
        assert "- en: aiden (configured default)" in result.output

    @pytest.mark.asyncio
    async def test_list_server_error_still_succeeds(self, two_lang_cfg):
        with _mock_client(list_side_effect=TTSError("down")):
            result = await tts_ops.list_tts_voices(channel_id="chan1")
        assert result.success
        assert "could not be listed (down)" in result.output

    @pytest.mark.asyncio
    async def test_disabled(self, two_lang_cfg, monkeypatch):
        monkeypatch.setattr(config_module.config.tts, "enabled", False)
        assert (await tts_ops.list_tts_voices(channel_id="chan1")).success is False
        assert (await tts_ops.set_tts_voice("en", "ryan", channel_id="chan1")).success is False

    @pytest.mark.asyncio
    async def test_kokoro_style_voice_list(self, two_lang_cfg):
        catalog = {"voices": ["af_nicole", "am_adam"], "default_voice": "af_nicole"}
        with _mock_client(catalog=catalog):
            listed = await tts_ops.list_tts_voices("en", channel_id="chan1")
            ok = await tts_ops.set_tts_voice("en", "am_adam", channel_id="chan1")
            bad = await tts_ops.set_tts_voice("en", "nobody", channel_id="chan1")
        assert "built-in: af_nicole, am_adam" in listed.output
        assert ok.success, ok.error
        assert bad.success is False and "af_nicole" in bad.error

    @pytest.mark.asyncio
    async def test_voice_clone_server_style_dict(self, two_lang_cfg):
        catalog = {"voices": {"oma": {"language": "de"}, "kaputt": {"error": "x"}}}
        with _mock_client(catalog=catalog):
            listed = await tts_ops.list_tts_voices("de", channel_id="chan1")
            ok = await tts_ops.set_tts_voice("de", "oma", channel_id="chan1")
            bad = await tts_ops.set_tts_voice("de", "kaputt", channel_id="chan1")
        assert "oma (cloned, recorded in de)" in listed.output
        assert ok.success and bad.success is False
