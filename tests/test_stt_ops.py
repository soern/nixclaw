"""Unit tests for the retranscribe_audio tool (src/nixclaw_agent/tools/stt_ops.py).

Covers:
- alternate backend disabled kill-switch
- filename path traversal rejection
- missing file
- missing language (no auto-detect on the alternate backend)
- too-long audio rejected (reuses config.stt.max_duration_seconds)
- success: joined transcript returned
- empty result: explicit placeholder string, not a bare empty string
- alternate backend busy (concurrency cap): clean error, not a hang/exception
"""

import wave
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.nixclaw_agent import config as config_module
from src.nixclaw_agent.stt import STTError
from src.nixclaw_agent.tools import stt_ops


def _make_wav(path: Path, duration_s: float = 1.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00\x00" * int(rate * duration_s))
    return path


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.config.paths, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.config.stt.alternate, "enabled", True)
    monkeypatch.setattr(config_module.config.stt.alternate, "language", None)
    monkeypatch.setattr(config_module.config.stt, "max_duration_seconds", 300.0)
    monkeypatch.setattr(config_module.config.stt, "chunk_seconds", 28.0)
    monkeypatch.setattr(config_module.config.stt, "retention_hours", 1.0)
    return config_module.config


@contextmanager
def _mock_client(segments):
    """Patch stt_ops.FasterWhisperClient; transcribe_file yields `segments` in order."""
    instance = MagicMock()
    instance.transcribe_file = AsyncMock(side_effect=list(segments))
    with patch.object(stt_ops, "FasterWhisperClient", return_value=instance):
        yield instance


class TestRetranscribeAudio:
    @pytest.mark.asyncio
    async def test_disabled_returns_error(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg.stt.alternate, "enabled", False)
        result = await stt_ops.retranscribe_audio("msg.aac", language="de", channel_id="chan1")
        assert result.success is False
        assert "disabled" in result.error

    @pytest.mark.asyncio
    async def test_dotdot_rejected(self, cfg):
        result = await stt_ops.retranscribe_audio("../evil.aac", language="de", channel_id="chan1")
        assert result.success is False
        assert "path traversal" in result.error

    @pytest.mark.asyncio
    async def test_subdir_rejected(self, cfg):
        result = await stt_ops.retranscribe_audio("sub/evil.aac", language="de", channel_id="chan1")
        assert result.success is False
        assert "path traversal" in result.error

    @pytest.mark.asyncio
    async def test_invalid_channel_id_rejected(self, cfg):
        result = await stt_ops.retranscribe_audio("msg.aac", language="de", channel_id="../etc")
        assert result.success is False
        assert "Invalid channel_id" in result.error

    @pytest.mark.asyncio
    async def test_missing_file(self, cfg):
        (cfg.paths.data_dir / "chan1" / "attachments").mkdir(parents=True)
        result = await stt_ops.retranscribe_audio("missing.aac", language="de", channel_id="chan1")
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_missing_language_errors(self, cfg):
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=1.0)
        result = await stt_ops.retranscribe_audio("msg.aac", channel_id="chan1")
        assert result.success is False
        assert "language" in result.error

    @pytest.mark.asyncio
    async def test_alternate_default_language_used_when_not_passed(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg.stt.alternate, "language", "de")
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=1.0)
        with _mock_client(["hallo"]):
            result = await stt_ops.retranscribe_audio("msg.aac", channel_id="chan1")
        assert result.success is True
        assert result.output == "hallo"

    @pytest.mark.asyncio
    async def test_too_long_rejected(self, cfg, monkeypatch):
        monkeypatch.setattr(cfg.stt, "max_duration_seconds", 1.0)
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=5.0)
        result = await stt_ops.retranscribe_audio("msg.aac", language="de", channel_id="chan1")
        assert result.success is False
        assert "too long" in result.error.lower()

    @pytest.mark.asyncio
    async def test_success_returns_joined_transcript(self, cfg):
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=1.0)
        with _mock_client(["hallo welt"]):
            result = await stt_ops.retranscribe_audio("msg.aac", language="de", channel_id="chan1")
        assert result.success is True
        assert result.output == "hallo welt"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_empty_result_returns_explicit_placeholder(self, cfg):
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=1.0)
        with _mock_client([""]):
            result = await stt_ops.retranscribe_audio("msg.aac", language="en", channel_id="chan1")
        assert result.success is True
        assert result.output == "[alternate STT produced no transcript]"

    @pytest.mark.asyncio
    async def test_client_error_propagated_as_failure(self, cfg):
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=1.0)
        with _mock_client([STTError("boom")]):
            result = await stt_ops.retranscribe_audio("msg.aac", language="en", channel_id="chan1")
        assert result.success is False
        assert "Alternate STT failed" in result.error

    @pytest.mark.asyncio
    async def test_alternate_backend_busy_returns_clean_error(self, cfg, monkeypatch):
        # Hold the sole slot for this base_url open for the duration of the
        # call, with a short wait budget so the test doesn't hang.
        monkeypatch.setattr(cfg.stt.alternate, "max_concurrent_requests", 1)
        monkeypatch.setattr(cfg.stt.alternate, "max_concurrent_wait_seconds", 0.05)
        att_dir = cfg.paths.data_dir / "chan1" / "attachments"
        att_dir.mkdir(parents=True)
        _make_wav(att_dir / "msg.aac", duration_s=1.0)

        sem = stt_ops._get_alt_semaphore(cfg.stt.alternate.base_url, 1)
        await sem.acquire()
        try:
            result = await stt_ops.retranscribe_audio("msg.aac", language="de", channel_id="chan1")
        finally:
            sem.release()
        assert result.success is False
        assert "busy" in result.error.lower()
