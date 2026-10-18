"""Unit tests for src/nixclaw_agent/stt.py.

Covers:
- is_audio_attachment MIME-type detection
- probe_duration parsing (and zero-failure on bad paths)
- convert_to_wav_chunks argv shape (via subprocess mocking)
- FasterWhisperClient httpx interaction (success, 500, network error,
  missing text field, language passthrough)
- transcribe_attachment: cache hit, too-long cap, multi-chunk join,
  failure placeholder, tempdir cleanup
- Sidecar cache write/read round-trip
"""

import asyncio
import os
import time
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.nixclaw_agent.config import SttConfig
from src.nixclaw_agent.stt import (
    AudioTooLongError,
    FasterWhisperClient,
    STTError,
    convert_to_wav_chunks,
    get_cached_transcript,
    is_audio_attachment,
    join_segments,
    probe_duration,
    sweep_expired_audio,
    transcribe_attachment,
    write_transcript_cache,
)
from src.nixclaw_agent.types import Attachment


def _make_wav(path: Path, duration_s: float = 1.0, rate: int = 16000) -> Path:
    """Write a tiny valid WAV file at `path`."""
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00\x00" * int(rate * duration_s))
    return path


def _make_cfg(**overrides) -> SttConfig:
    cfg = SttConfig(
        enabled=True,
        base_url="http://localhost:8001",
        model="large-v3",
        language="de",
        timeout=10.0,
        max_duration_seconds=300.0,
        chunk_seconds=28.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class _FakeAttachment:
    def __init__(self, local_path=None, content_type="audio/aac", filename="x.aac", att_id="abc"):
        self.local_path = local_path
        self.content_type = content_type
        self.filename = filename
        self.id = att_id


class TestIsAudioAttachment:
    @pytest.mark.parametrize("ct", [
        "audio/aac", "audio/mp4", "audio/mpeg", "audio/wav",
        "audio/x-wav", "audio/flac", "audio/ogg", "audio/webm", "audio/m4a",
        "AUDIO/AAC",  # case-insensitive
    ])
    def test_audio_types(self, ct):
        assert is_audio_attachment(_FakeAttachment(content_type=ct)) is True

    @pytest.mark.parametrize("ct", ["image/jpeg", "image/png", "application/pdf",
                                     "text/plain", "application/octet-stream", ""])
    def test_non_audio_types(self, ct):
        assert is_audio_attachment(_FakeAttachment(content_type=ct)) is False


class TestJoinSegments:
    def test_basic_join(self):
        assert join_segments(["Hallo Welt", "wie geht es dir"]) == "Hallo Welt wie geht es dir"

    def test_collapses_whitespace(self):
        assert join_segments(["a  b", "c\nd", "  e  "]) == "a b c d e"

    def test_filters_empty(self):
        assert join_segments(["", "  ", "ok"]) == "ok"

    def test_empty_input(self):
        assert join_segments([]) == ""


class TestProbeDuration:
    @pytest.mark.asyncio
    async def test_parses_float(self, tmp_path):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"12.345\n", b""))
        proc.returncode = 0
        audio = tmp_path / "fake.wav"
        audio.write_bytes(b"\x00")
        with patch("asyncio.create_subprocess_exec", return_value=proc) as exe:
            d = await probe_duration(audio)
        assert d == 12.345
        # argv shape: ffprobe ... -i fd:, the file passed as stdin
        args = exe.call_args.args
        assert "ffprobe" in args
        assert args[-2:] == ("-i", "fd:")
        assert exe.call_args.kwargs["stdin"].name == str(audio)

    @pytest.mark.asyncio
    async def test_returns_zero_on_nonzero_exit(self, tmp_path):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"error"))
        proc.returncode = 1
        audio = tmp_path / "x"
        audio.write_bytes(b"\x00")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await probe_duration(audio) == 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_on_missing_file(self, tmp_path):
        assert await probe_duration(tmp_path / "missing") == 0.0

    @pytest.mark.asyncio
    async def test_returns_zero_on_missing_ffprobe(self):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            assert await probe_duration(Path("/tmp/x")) == 0.0


class TestConvertToWavChunks:
    @pytest.mark.asyncio
    async def test_creates_chunks(self, tmp_path):
        # Real round-trip: write a wav, ffmpeg-segment it, check chunks.
        src = _make_wav(tmp_path / "in.wav", duration_s=2.0)
        out_dir = tmp_path / "chunks"
        chunks = await convert_to_wav_chunks(src, out_dir, chunk_seconds=1.0)
        assert chunks, "no chunks produced"
        for c in chunks:
            assert c.exists()
            assert c.suffix == ".wav"

    @pytest.mark.asyncio
    async def test_raises_on_nonzero_exit(self, tmp_path):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"some ffmpeg error"))
        proc.returncode = 1
        src = tmp_path / "x.aac"
        src.write_bytes(b"\x00")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(STTError, match="ffmpeg failed"):
                await convert_to_wav_chunks(src, tmp_path / "out", 28.0)

    @pytest.mark.asyncio
    async def test_raises_on_missing_input(self, tmp_path):
        with pytest.raises(STTError, match="cannot open"):
            await convert_to_wav_chunks(tmp_path / "missing.aac", tmp_path / "out", 28.0)


class TestTranscriptCache:
    def test_write_and_read_roundtrip(self, tmp_path):
        audio = tmp_path / "msg.aac"
        audio.write_bytes(b"\x00")
        write_transcript_cache(audio, "Hallo Welt")
        assert get_cached_transcript(audio) == "Hallo Welt"

    def test_returns_none_when_missing(self, tmp_path):
        assert get_cached_transcript(tmp_path / "nope.aac") is None

    def test_sidecar_path(self, tmp_path):
        audio = tmp_path / "msg.aac"
        cache = audio.with_suffix(audio.suffix + ".transcript.md")
        write_transcript_cache(audio, "x")
        assert cache.exists()
        assert "x" in cache.read_text()


class _MockResponse:
    def __init__(self, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self._json = json_payload
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "err", request=MagicMock(), response=MagicMock(status_code=self.status_code)
            )


class TestFasterWhisperClient:
    @pytest.mark.asyncio
    async def test_transcribe_success(self, tmp_path):
        wav = _make_wav(tmp_path / "c.wav")
        cfg = _make_cfg()
        client = FasterWhisperClient(cfg)

        resp = _MockResponse(200, {"text": "Hallo Welt"})
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.post = AsyncMock(return_value=resp)
        with patch("src.nixclaw_agent.stt.httpx.AsyncClient", return_value=ctx):
            text = await client.transcribe_file(wav)
        assert text == "Hallo Welt"
        # Verify multipart shape
        call = ctx.post.call_args
        assert call.args[0].endswith("/v1/audio/transcriptions")
        assert "files" in call.kwargs
        assert "data" in call.kwargs
        assert call.kwargs["data"]["model"] == "large-v3"
        assert call.kwargs["data"]["language"] == "de"
        # file tuple is (filename, bytes, content_type)
        fname, fbytes, ctype = call.kwargs["files"]["file"]
        assert fname == "c.wav"
        assert fbytes.startswith(b"RIFF") or fbytes[:4] == b"RIFF" or len(fbytes) > 0
        assert ctype == "audio/wav"

    @pytest.mark.asyncio
    async def test_transcribe_no_language(self, tmp_path):
        wav = _make_wav(tmp_path / "c.wav")
        cfg = _make_cfg(language=None)
        client = FasterWhisperClient(cfg)
        resp = _MockResponse(200, {"text": "hi"})
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.post = AsyncMock(return_value=resp)
        with patch("src.nixclaw_agent.stt.httpx.AsyncClient", return_value=ctx):
            await client.transcribe_file(wav)
        # When language is None, we omit it from the form data.
        assert "language" not in ctx.post.call_args.kwargs["data"]

    @pytest.mark.asyncio
    async def test_transcribe_per_call_language_override(self, tmp_path):
        wav = _make_wav(tmp_path / "c.wav")
        cfg = _make_cfg(language="en")
        client = FasterWhisperClient(cfg)
        resp = _MockResponse(200, {"text": "hi"})
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.post = AsyncMock(return_value=resp)
        with patch("src.nixclaw_agent.stt.httpx.AsyncClient", return_value=ctx):
            await client.transcribe_file(wav, language="fr")
        assert ctx.post.call_args.kwargs["data"]["language"] == "fr"

    @pytest.mark.asyncio
    async def test_transcribe_500_raises(self, tmp_path):
        wav = _make_wav(tmp_path / "c.wav")
        cfg = _make_cfg()
        client = FasterWhisperClient(cfg)
        resp = _MockResponse(500, text="boom")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.post = AsyncMock(return_value=resp)
        with patch("src.nixclaw_agent.stt.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(STTError, match="HTTP 500"):
                await client.transcribe_file(wav)

    @pytest.mark.asyncio
    async def test_transcribe_missing_text_field(self, tmp_path):
        wav = _make_wav(tmp_path / "c.wav")
        cfg = _make_cfg()
        client = FasterWhisperClient(cfg)
        resp = _MockResponse(200, {"foo": "bar"})
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.post = AsyncMock(return_value=resp)
        with patch("src.nixclaw_agent.stt.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(STTError, match="missing 'text'"):
                await client.transcribe_file(wav)

    @pytest.mark.asyncio
    async def test_transcribe_network_error(self, tmp_path):
        wav = _make_wav(tmp_path / "c.wav")
        cfg = _make_cfg()
        client = FasterWhisperClient(cfg)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=None)
        ctx.post = AsyncMock(side_effect=httpx.ConnectError("nope"))
        with patch("src.nixclaw_agent.stt.httpx.AsyncClient", return_value=ctx):
            with pytest.raises(STTError, match="HTTP error"):
                await client.transcribe_file(wav)


class TestTranscribeAttachment:
    @pytest.mark.asyncio
    async def test_returns_placeholder_when_no_path(self):
        att = Attachment(id="x", content_type="audio/aac", filename=None, local_path=None)
        client = MagicMock(spec=FasterWhisperClient)
        out = await transcribe_attachment(att, client, _make_cfg())
        assert "not saved" in out

    @pytest.mark.asyncio
    async def test_returns_placeholder_when_missing_file(self, tmp_path):
        att = Attachment(id="x", content_type="audio/aac", filename=None,
                         local_path=str(tmp_path / "missing.aac"))
        client = MagicMock(spec=FasterWhisperClient)
        out = await transcribe_attachment(att, client, _make_cfg())
        assert "not found" in out

    @pytest.mark.asyncio
    async def test_cache_hit_skips_http(self, tmp_path):
        audio = _make_wav(tmp_path / "msg.aac")
        write_transcript_cache(audio, "from cache")
        att = Attachment(id="x", content_type="audio/aac", filename="msg.aac",
                         local_path=str(audio))
        client = MagicMock(spec=FasterWhisperClient)
        out = await transcribe_attachment(att, client, _make_cfg())
        assert out == "from cache"
        client.transcribe_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_too_long_returns_placeholder(self, tmp_path):
        audio = _make_wav(tmp_path / "msg.aac", duration_s=400.0)
        att = Attachment(id="x", content_type="audio/aac", filename="msg.aac",
                         local_path=str(audio))
        cfg = _make_cfg(max_duration_seconds=10.0)
        out = await transcribe_attachment(att, MagicMock(), cfg)
        assert "too long" in out
        assert audio.exists()

    @pytest.mark.asyncio
    async def test_multi_chunk_join(self, tmp_path):
        # Make a 4-second clip, chunk at 1s -> expect ~4 chunks joined.
        audio = _make_wav(tmp_path / "msg.aac", duration_s=4.0)
        att = Attachment(id="x", content_type="audio/aac", filename="msg.aac",
                         local_path=str(audio))
        cfg = _make_cfg(chunk_seconds=1.0, max_duration_seconds=60.0)

        client = MagicMock(spec=FasterWhisperClient)
        # transcript depends on chunk index for assertion clarity
        async def fake_transcribe(p, *, language=None):
            return {0: "eins", 1: "zwei", 2: "drei", 3: "vier"}.get(int(p.stem.split("_")[-1]), "x")
        client.transcribe_file = AsyncMock(side_effect=fake_transcribe)

        out = await transcribe_attachment(att, client, cfg)
        assert out == "eins zwei drei vier"
        assert client.transcribe_file.await_count >= 4
        # Cache written
        assert get_cached_transcript(audio) == "eins zwei drei vier"

    @pytest.mark.asyncio
    async def test_keeps_audio_after_success(self, tmp_path):
        # Make a 4-second clip, chunk at 1s -> expect ~4 chunks joined.
        audio = _make_wav(tmp_path / "msg.aac", duration_s=4.0)
        att = Attachment(id="x", content_type="audio/aac", filename="msg.aac",
                         local_path=str(audio))
        cfg = _make_cfg(chunk_seconds=1.0, max_duration_seconds=60.0)

        client = MagicMock(spec=FasterWhisperClient)
        async def fake_transcribe(p, *, language=None):
            return {0: "eins", 1: "zwei", 2: "drei", 3: "vier"}.get(int(p.stem.split("_")[-1]), "x")
        client.transcribe_file = AsyncMock(side_effect=fake_transcribe)

        out = await transcribe_attachment(att, client, cfg)
        assert out == "eins zwei drei vier"
        # Source audio is kept after successful transcription (retention
        # window + sweep_expired_audio() handle cleanup, not this function)...
        assert audio.exists()
        # ...and the transcript sidecar cache is correct.
        assert get_cached_transcript(audio) == "eins zwei drei vier"

    @pytest.mark.asyncio
    async def test_failure_returns_placeholder(self, tmp_path):
        audio = _make_wav(tmp_path / "msg.aac", duration_s=1.0)
        att = Attachment(id="x", content_type="audio/aac", filename="msg.aac",
                         local_path=str(audio))
        cfg = _make_cfg()
        client = MagicMock(spec=FasterWhisperClient)
        client.transcribe_file = AsyncMock(side_effect=STTError("boom"))
        out = await transcribe_attachment(att, client, cfg)
        assert "failed" in out
        assert audio.exists()

    @pytest.mark.asyncio
    async def test_tempdir_cleaned_up(self, tmp_path):
        audio = _make_wav(tmp_path / "msg.aac", duration_s=1.0)
        att = Attachment(id="x", content_type="audio/aac", filename="msg.aac",
                         local_path=str(audio))
        cfg = _make_cfg()
        client = MagicMock(spec=FasterWhisperClient)
        client.transcribe_file = AsyncMock(return_value="hi")

        out = await transcribe_attachment(att, client, cfg)
        assert out == "hi"
        # No stt-* tmp dirs left behind in /tmp (best-effort check)
        leftover = list(Path("/tmp").glob("stt-*"))
        assert leftover == [], f"tempdir not cleaned: {leftover}"


class TestAudioTooLongError:
    def test_is_stt_error(self):
        assert issubclass(AudioTooLongError, STTError)


def _age_file(path: Path, hours_old: float) -> None:
    """Backdate a file's mtime by `hours_old` hours."""
    ts = time.time() - (hours_old * 3600)
    os.utime(path, (ts, ts))


class TestSweepExpiredAudio:
    def test_deletes_old_cached_audio_and_sidecar(self, tmp_path):
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        audio = _make_wav(channel_dir / "old.aac")
        write_transcript_cache(audio, "hallo")
        _age_file(audio, hours_old=2.0)
        _age_file(audio.with_suffix(audio.suffix + ".transcript.md"), hours_old=2.0)

        removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
        assert removed == 1
        assert not audio.exists()
        assert get_cached_transcript(audio) is None

    def test_keeps_fresh_audio(self, tmp_path):
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        audio = _make_wav(channel_dir / "fresh.aac")
        write_transcript_cache(audio, "hallo")

        removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
        assert removed == 0
        assert audio.exists()

    def test_deletes_old_audio_with_no_sidecar(self, tmp_path):
        # Audio from a failed transcription (too-long / ffmpeg error / empty
        # result) never gets a .transcript.md sidecar written - it must
        # still be swept by age alone, or it accumulates forever.
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        audio = _make_wav(channel_dir / "failed.aac")
        _age_file(audio, hours_old=2.0)

        removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
        assert removed == 1
        assert not audio.exists()

    def test_retention_hours_zero_disables_sweep(self, tmp_path):
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        audio = _make_wav(channel_dir / "old.aac")
        _age_file(audio, hours_old=100.0)

        removed = sweep_expired_audio(tmp_path, retention_hours=0.0)
        assert removed == 0
        assert audio.exists()

    def test_sweeps_extensions_not_in_old_hardcoded_list(self, tmp_path):
        # .mpeg (audio/mpeg) and .x-wav (audio/x-wav) are both in
        # is_audio_attachment()'s _AUDIO_TYPES and are what signal.py's
        # save_attachment() actually writes to disk for those content-types,
        # but were missing from the old hand-maintained extension list.
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        mpeg = _make_wav(channel_dir / "old.mpeg")
        xwav = _make_wav(channel_dir / "old.x-wav")
        _age_file(mpeg, hours_old=2.0)
        _age_file(xwav, hours_old=2.0)

        removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
        assert removed == 2
        assert not mpeg.exists()
        assert not xwav.exists()

    def test_sweeps_orphaned_sidecar_with_no_audio_file(self, tmp_path):
        # Simulates a prior sweep that unlinked the audio but then failed to
        # unlink the sidecar - the main pass is keyed off audio files, so an
        # audio-less sidecar must be picked up by its own mtime instead.
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        orphan_cache = channel_dir / "gone.aac.transcript.md"
        orphan_cache.write_text("stale transcript\n", encoding="utf-8")
        _age_file(orphan_cache, hours_old=2.0)

        removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
        # Not counted in `removed` (that metric is audio files deleted).
        assert removed == 0
        assert not orphan_cache.exists()

    def test_keeps_fresh_orphaned_sidecar(self, tmp_path):
        channel_dir = tmp_path / "chan1" / "attachments"
        channel_dir.mkdir(parents=True)
        orphan_cache = channel_dir / "gone.aac.transcript.md"
        orphan_cache.write_text("stale transcript\n", encoding="utf-8")

        removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
        assert removed == 0
        assert orphan_cache.exists()

def test_sweep_covers_nested_group_attachment_dirs(tmp_path):
    # Group channel ids contain "/" (signal-group:<a>/<b>), so their
    # attachments dir is nested deeper than data/<channel>/attachments.
    group_dir = tmp_path / "signal-group:abc" / "def==" / "attachments"
    group_dir.mkdir(parents=True)
    audio = _make_wav(group_dir / "old.aac")
    _age_file(audio, hours_old=2.0)
    fresh = _make_wav(group_dir / "fresh.aac")

    removed = sweep_expired_audio(tmp_path, retention_hours=1.0)
    assert removed == 1
    assert not audio.exists()
    assert fresh.exists()


def test_sweep_does_not_follow_symlinks_out_of_data_dir(tmp_path):
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside" / "attachments"
    outside.mkdir(parents=True)
    victim = _make_wav(outside / "old.aac")
    _age_file(victim, hours_old=2.0)
    (data_dir / "chan").mkdir(parents=True)
    os.symlink(outside, data_dir / "chan" / "attachments")
    os.symlink(tmp_path / "outside", data_dir / "link")

    assert sweep_expired_audio(data_dir, retention_hours=1.0) == 0
    assert victim.exists()


def test_sweep_skips_attachments_folders_inside_workspaces(tmp_path):
    user_dir = tmp_path / "+000000" / "projects" / "attachments"
    user_dir.mkdir(parents=True)
    song = _make_wav(user_dir / "x.aac")
    _age_file(song, hours_old=5.0)
    group_ws = tmp_path / "signal-group:abc" / "def==" / "attachments" / "attachments"
    group_ws.mkdir(parents=True)
    nested = _make_wav(group_ws / "y.aac")
    _age_file(nested, hours_old=5.0)

    assert sweep_expired_audio(tmp_path, retention_hours=1.0) == 0
    assert song.exists() and nested.exists()
