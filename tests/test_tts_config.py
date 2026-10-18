"""Unit tests for the `tts` section of src/nixclaw_agent/config.py.

Covers TTSEngineConfig.from_dict (mapping key -> language), TTSConfig's
default single-`en` engine, engine_for's case-insensitive lookup, and the
TTS_ENABLED/TTS_BASE_URL/TTS_TIMEOUT env overrides (which must apply only
to the default-language engine, never other engines).

Mirrors tests/test_imagegen.py's plain-dataclass testing style; no async
machinery is needed here since config parsing is synchronous.
"""

import os
from unittest.mock import patch

from src.nixclaw_agent.config import Config, TTSConfig, TTSEngineConfig


class TestTTSEngineConfigFromDict:
    def test_from_dict_fills_language_from_key(self):
        engine = TTSEngineConfig.from_dict("de", {"base_url": "http://host:8003"})
        assert engine.language == "de"
        assert engine.base_url == "http://host:8003"

    def test_from_dict_none_data_uses_defaults_but_keeps_language(self):
        engine = TTSEngineConfig.from_dict("fr", None)
        assert engine.language == "fr"
        assert engine.base_url == "http://localhost:8005"
        assert engine.model == "hexgrad/Kokoro-82M"
        assert engine.voice == "af_heart"
        assert engine.speed == 1.0
        assert engine.response_format == "aac"
        assert engine.timeout == 120.0
        assert engine.max_input_chars == 20000
        assert engine.max_concurrent_requests == 2
        assert engine.max_concurrent_wait_seconds == 180.0

    def test_from_dict_overrides_all_fields(self):
        data = {
            "base_url": "http://other:9000",
            "model": "some/model",
            "voice": "alice",
            "speed": 1.5,
            "response_format": "wav",
            "timeout": 30.0,
            "max_input_chars": 500,
            "max_concurrent_requests": 5,
            "max_concurrent_wait_seconds": 10.0,
        }
        engine = TTSEngineConfig.from_dict("fr", data)
        assert engine.language == "fr"
        assert engine.base_url == "http://other:9000"
        assert engine.model == "some/model"
        assert engine.voice == "alice"
        assert engine.speed == 1.5
        assert engine.response_format == "wav"
        assert engine.timeout == 30.0
        assert engine.max_input_chars == 500
        assert engine.max_concurrent_requests == 5
        assert engine.max_concurrent_wait_seconds == 10.0


class TestTTSConfigDefaults:
    def test_default_construction_has_single_en_engine(self):
        cfg = TTSConfig()
        assert cfg.enabled is False
        assert cfg.default_language == "en"
        assert set(cfg.engines.keys()) == {"en"}
        assert cfg.engines["en"].language == "en"

    def test_from_dict_none_defaults_to_single_en_engine(self):
        cfg = TTSConfig.from_dict(None)
        assert cfg.enabled is False
        assert cfg.default_language == "en"
        assert set(cfg.engines.keys()) == {"en"}

    def test_from_dict_engines_omitted_defaults_to_single_en_engine(self):
        # `engines:` key entirely absent from an otherwise-populated tts dict.
        cfg = TTSConfig.from_dict({"enabled": True, "default_language": "en"})
        assert cfg.enabled is True
        assert set(cfg.engines.keys()) == {"en"}
        assert cfg.engines["en"].base_url == "http://localhost:8005"

    def test_engines_default_factory_produces_independent_instances(self):
        # default_factory=lambda: {"en": TTSEngineConfig()} must not share
        # one dict/instance across separate TTSConfig() constructions.
        cfg_a = TTSConfig()
        cfg_b = TTSConfig()
        assert cfg_a.engines is not cfg_b.engines
        cfg_a.engines["en"].base_url = "http://mutated:1234"
        assert cfg_b.engines["en"].base_url == "http://localhost:8005"


class TestTTSConfigFromDictMapping:
    def test_from_dict_builds_engines_mapping_with_language_from_key(self):
        data = {
            "enabled": True,
            "default_language": "de",
            "engines": {
                "de": {"base_url": "http://de-host:8003", "voice": "martin"},
                "en": {"base_url": "http://en-host:8004", "voice": "af_heart",
                       "model": "hexgrad/Kokoro-82M"},
            },
        }
        cfg = TTSConfig.from_dict(data)
        assert cfg.enabled is True
        assert set(cfg.engines.keys()) == {"de", "en"}
        assert cfg.engines["de"].language == "de"
        assert cfg.engines["de"].base_url == "http://de-host:8003"
        assert cfg.engines["en"].language == "en"
        assert cfg.engines["en"].base_url == "http://en-host:8004"
        assert cfg.engines["en"].model == "hexgrad/Kokoro-82M"

    def test_from_dict_lowercases_keys_and_last_wins(self):
        # No hard validation/regex on keys (matches every other config
        # class's permissive style) - just lowercase and let a duplicate
        # collapse to whichever the dict iteration sees last, same as
        # PyYAML's own behavior for literal duplicate mapping keys.
        data = {
            "engines": {
                "DE": {"base_url": "http://upper:1"},
                "de": {"base_url": "http://lower:2"},
            },
        }
        cfg = TTSConfig.from_dict(data)
        assert set(cfg.engines.keys()) == {"de"}
        assert cfg.engines["de"].base_url == "http://lower:2"

    def test_default_language_fallback_when_missing(self):
        cfg = TTSConfig.from_dict({"engines": {"de": {}}})
        assert cfg.default_language == "en"


class TestEngineFor:
    def test_engine_for_exact_match(self):
        cfg = TTSConfig()
        engine = cfg.engine_for("en")
        assert engine is not None
        assert engine.language == "en"

    def test_engine_for_is_case_insensitive(self):
        cfg = TTSConfig()
        assert cfg.engine_for("EN") is cfg.engines["en"]
        assert cfg.engine_for("En") is cfg.engines["en"]

    def test_engine_for_unknown_language_returns_none(self):
        cfg = TTSConfig()
        assert cfg.engine_for("fr") is None

    def test_supported_languages_sorted(self):
        cfg = TTSConfig.from_dict({"engines": {"en": {}, "de": {}}})
        assert cfg.supported_languages() == ["de", "en"]


class TestTTSConfigWiredIntoConfig:
    def test_config_from_dict_none_has_default_tts(self):
        cfg = Config.from_dict(None)
        assert isinstance(cfg.tts, TTSConfig)
        assert cfg.tts.enabled is False
        assert set(cfg.tts.engines.keys()) == {"en"}

    def test_config_from_dict_wires_tts_section(self):
        cfg = Config.from_dict({
            "tts": {
                "enabled": True,
                "default_language": "de",
                "engines": {"de": {"base_url": "http://custom:8003"}},
            }
        })
        assert cfg.tts.enabled is True
        assert cfg.tts.engines["de"].base_url == "http://custom:8003"


class TestEnvOverrides:
    def _cfg_with_two_engines(self) -> Config:
        return Config.from_dict({
            "tts": {
                "enabled": False,
                "default_language": "de",
                "engines": {
                    "de": {"base_url": "http://de-host:8003", "timeout": 120.0},
                    "en": {"base_url": "http://en-host:8004", "timeout": 90.0},
                },
            }
        })

    def test_tts_enabled_env_override(self):
        from src.nixclaw_agent.config import _apply_env_overrides
        cfg = self._cfg_with_two_engines()
        with patch.dict(os.environ, {"TTS_ENABLED": "true"}, clear=False):
            cfg = _apply_env_overrides(cfg)
        assert cfg.tts.enabled is True

    def test_tts_base_url_env_override_hits_only_default_language_engine(self):
        from src.nixclaw_agent.config import _apply_env_overrides
        cfg = self._cfg_with_two_engines()
        with patch.dict(os.environ, {"TTS_BASE_URL": "http://overridden:9999"}, clear=False):
            cfg = _apply_env_overrides(cfg)
        assert cfg.tts.engines["de"].base_url == "http://overridden:9999"
        # The `en` engine must be untouched.
        assert cfg.tts.engines["en"].base_url == "http://en-host:8004"

    def test_tts_timeout_env_override_hits_only_default_language_engine(self):
        from src.nixclaw_agent.config import _apply_env_overrides
        cfg = self._cfg_with_two_engines()
        with patch.dict(os.environ, {"TTS_TIMEOUT": "42.0"}, clear=False):
            cfg = _apply_env_overrides(cfg)
        assert cfg.tts.engines["de"].timeout == 42.0
        assert cfg.tts.engines["en"].timeout == 90.0

    def test_no_env_vars_set_leaves_config_untouched(self):
        from src.nixclaw_agent.config import _apply_env_overrides
        cfg = self._cfg_with_two_engines()
        env_without_tts = {
            k: v for k, v in os.environ.items()
            if not k.startswith("TTS_")
        }
        with patch.dict(os.environ, env_without_tts, clear=True):
            cfg = _apply_env_overrides(cfg)
        assert cfg.tts.enabled is False
        assert cfg.tts.engines["de"].base_url == "http://de-host:8003"
        assert cfg.tts.engines["en"].base_url == "http://en-host:8004"
