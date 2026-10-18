"""Speech-to-text transcription via faster-whisper-server.

This module turns inbound Signal voice messages (audio/* attachments)
into text before they reach the LLM turn. The transcription service is
a separate HTTP server (see deploy/dgx-spark/faster-whisper/) that exposes
an OpenAI-shaped `POST /v1/audio/transcriptions` endpoint.

Pipeline:
    inbound audio attachment
        -> is_audio_attachment() check
        -> probe_duration() (ffprobe) -> AudioTooLongError if > cap
        -> convert_to_wav_chunks() (ffmpeg segment muxer, 16 kHz mono)
           (both run as `nobody` with resource limits, see sandbox.py)
        -> sequential HTTP calls to /v1/audio/transcriptions
        -> join segments with spaces
        -> sidecar .transcript.md cache next to the audio file

The source audio file is *not* deleted after transcription - it's kept
for `SttConfig.retention_hours` (default 1h) so the `retranscribe_audio`
tool (tools/stt_ops.py) can re-run it through an alternate STT backend if
the agent thinks the primary transcript looks wrong. `sweep_expired_audio()`
below is called periodically (see core.py's main loop) to delete audio
older than the retention window, regardless of whether it was ever
successfully transcribed.

Failure mode is graceful: any transcode / HTTP error -> the message
becomes a placeholder string; the agent turn continues so the user can
be told something went wrong.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

import httpx

from . import sandbox
from .config import SttConfig
from .path_safety import is_within_directory

LOG = logging.getLogger(__name__)


class STTError(Exception):
    """Generic STT failure (transcode, HTTP, parse)."""


class AudioTooLongError(STTError):
    """Audio clip exceeds the configured max_duration_seconds cap."""


_AUDIO_TYPES = ("audio/aac", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav",
                "audio/flac", "audio/ogg", "audio/webm", "audio/m4a")


def is_audio_attachment(att) -> bool:
    """True if `att.content_type` looks like an audio MIME type."""
    ct = (att.content_type or "").lower()
    return ct.startswith("audio/")


async def probe_duration(path: Path) -> float:
    """Return clip duration in seconds (0.0 on failure)."""
    try:
        # Unprivileged, input as open fd (see sandbox.py)
        with open(path, "rb") as src:
            proc = await asyncio.create_subprocess_exec(
                *sandbox.wrap([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", "-i", "fd:",
                ]),
                stdin=src,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=sandbox.env(),
            )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()
            return 0.0
        if proc.returncode != 0:
            return 0.0
        text = stdout.decode("utf-8", errors="ignore").strip()
        return float(text) if text else 0.0
    except (FileNotFoundError, ValueError, OSError) as exc:
        LOG.warning(f"probe_duration failed for {path}: {exc}")
        return 0.0


async def convert_to_wav_chunks(
    src_path: Path,
    out_dir: Path,
    chunk_seconds: float,
) -> list[Path]:
    """Transcode `src_path` to 16 kHz mono WAV chunks in `out_dir`.

    Uses the ffmpeg segment muxer with `-segment_time <chunk_seconds>`.
    Returns the list of produced chunk paths (sorted by filename).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if sandbox.enabled():
        os.chown(out_dir, sandbox.SANDBOX_UID, sandbox.SANDBOX_GID)
    pattern = out_dir / "chunk_%03d.wav"

    # Unprivileged, input as open fd (see sandbox.py)
    try:
        src = open(src_path, "rb")
    except OSError as exc:
        raise STTError(f"cannot open {src_path}: {exc}")
    with src:
        proc = await asyncio.create_subprocess_exec(
            *sandbox.wrap([
                "ffmpeg", "-y", "-i", "fd:",
                "-ac", "1", "-ar", "16000",
                "-f", "segment",
                "-segment_time", str(chunk_seconds),
                "-reset_timestamps", "1",
                str(pattern),
            ]),
            stdin=src,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=sandbox.env(),
        )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        raise STTError(f"ffmpeg timed out converting {src_path}")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore")[-500:]
        raise STTError(f"ffmpeg failed for {src_path}: {err}")
    return sorted(out_dir.glob("chunk_*.wav"))


def _transcript_cache_path(audio_path: Path) -> Path:
    """Sidecar cache: <audio>.transcript.md next to the audio file."""
    return audio_path.with_suffix(audio_path.suffix + ".transcript.md")


def get_cached_transcript(audio_path: Path) -> Optional[str]:
    """Return the cached transcript text, or None on miss / read error."""
    cache = _transcript_cache_path(audio_path)
    if not cache.exists():
        return None
    try:
        text = cache.read_text(encoding="utf-8").strip()
        return text if text else None
    except OSError as exc:
        LOG.warning(f"Could not read transcript cache {cache}: {exc}")
        return None


def write_transcript_cache(audio_path: Path, transcript: str) -> None:
    """Persist the transcript to the sidecar cache (best-effort)."""
    cache = _transcript_cache_path(audio_path)
    try:
        cache.write_text(transcript.strip() + "\n", encoding="utf-8")
    except OSError as exc:
        LOG.warning(f"Could not write transcript cache {cache}: {exc}")


def join_segments(segments: Iterable[str]) -> str:
    """Join per-chunk transcripts into a single string, collapsing whitespace."""
    text = " ".join(s.strip() for s in segments if s and s.strip())
    return re.sub(r"\s+", " ", text).strip()


async def _transcribe_chunks(chunks: list[Path], client: "FasterWhisperClient") -> str:
    """Transcribe `chunks` sequentially and join. Shared by transcribe_attachment
    and tools/stt_ops.py's retranscribe_audio so the two don't drift.

    Re-raises STTError from the first failing chunk (after logging) -
    callers decide how to surface that to their own caller.
    """
    segments: list[str] = []
    for chunk in chunks:
        try:
            segments.append(await client.transcribe_file(chunk))
        except STTError as exc:
            LOG.warning(f"STT failed on chunk {chunk.name}: {exc}")
            raise
    return join_segments(segments)


class FasterWhisperClient:
    """Thin httpx wrapper around the OpenAI-shaped /v1/audio/transcriptions endpoint."""

    def __init__(self, cfg: SttConfig):
        self.cfg = cfg
        self._base = cfg.base_url.rstrip("/")

    async def transcribe_file(self, wav_path: Path, *, language: Optional[str] = None) -> str:
        """Send a single WAV chunk and return the transcribed text."""
        try:
            wav_bytes = wav_path.read_bytes()
        except OSError as exc:
            raise STTError(f"Could not read {wav_path}: {exc}") from exc
        return await self.transcribe_wav_bytes(wav_bytes, wav_path.name, language=language)

    async def transcribe_wav_bytes(self, wav_bytes: bytes, filename: str = "audio.wav", *,
                                   language: Optional[str] = None) -> str:
        """Send one in-memory WAV and return the transcribed text.

        `language` None = `stt.language`, "" = the server's auto-detect.
        """
        payload = await self.transcribe_wav_payload(wav_bytes, filename, language=language)
        return payload["text"].strip()

    async def transcribe_wav_payload(self, wav_bytes: bytes, filename: str = "audio.wav", *,
                                     language: Optional[str] = None,
                                     extra: Optional[dict] = None) -> dict:
        """Like transcribe_wav_bytes, but returns the whole JSON response
        (`text` validated); `extra` adds form fields (e.g. detect_speech)."""
        data: dict = {"model": self.cfg.model, **(extra or {})}
        eff_lang = language if language is not None else self.cfg.language
        if eff_lang:
            data["language"] = eff_lang

        files = {"file": (filename, wav_bytes, "audio/wav")}
        url = f"{self._base}/v1/audio/transcriptions"
        timeout = httpx.Timeout(self.cfg.timeout, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, files=files, data=data)
        except httpx.HTTPError as exc:
            raise STTError(f"HTTP error contacting {url}: {exc}") from exc

        if resp.status_code != 200:
            raise STTError(
                f"STT server returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise STTError(f"Non-JSON response from STT server: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise STTError(f"STT response missing 'text' field: {payload}")
        return payload


async def transcribe_attachment(
    att,
    client: FasterWhisperClient,
    cfg: SttConfig,
) -> str:
    """Transcribe an audio attachment end-to-end.

    Returns a transcript string suitable for the LLM turn. On any failure,
    returns a placeholder so the agent can still respond. Raises only on
    programming errors; HTTP / transcode errors are logged + swallowed.
    """
    if not att.local_path:
        return "[Voice message — file not saved]"

    audio_path = Path(att.local_path)
    if not audio_path.exists():
        return "[Voice message — file not found]"

    cached = get_cached_transcript(audio_path)
    if cached:
        LOG.info(f"STT cache hit: {audio_path.name}")
        return cached

    duration = await probe_duration(audio_path)
    if duration > cfg.max_duration_seconds:
        return f"[Voice message — too long ({duration:.0f}s > {cfg.max_duration_seconds:.0f}s)]"

    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="stt-"))
    try:
        chunks = await convert_to_wav_chunks(audio_path, tmpdir, cfg.chunk_seconds)
        if not chunks:
            return "[Voice message — transcode produced no chunks]"

        try:
            transcript = await _transcribe_chunks(chunks, client)
        except STTError:
            return "[Voice message — transcription failed]"
        if not transcript:
            return "[Voice message — empty transcription]"
        write_transcript_cache(audio_path, transcript)
        return transcript
    finally:
        # Best-effort cleanup of the wav chunks.
        try:
            for chunk in tmpdir.glob("chunk_*.wav"):
                chunk.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


_SIDECAR_SUFFIX = ".transcript.md"

# Derived from _AUDIO_TYPES (not hand-maintained separately) so this can't
# silently drift out of sync with what signal.py's save_attachment() actually
# writes to disk (it derives the extension straight from content_type's
# subtype, e.g. "audio/x-wav" -> ".x-wav") - a hand-maintained list here
# previously missed ".mpeg"/".x-wav", letting those files accumulate forever.
_AUDIO_FILE_EXTENSIONS = frozenset("." + t.split("/")[-1] for t in _AUDIO_TYPES)


def _audio_path_for_sidecar(cache_path: Path) -> Path:
    """Inverse of _transcript_cache_path: strip the `.transcript.md` suffix."""
    name = cache_path.name
    if name.endswith(_SIDECAR_SUFFIX):
        return cache_path.with_name(name[: -len(_SIDECAR_SUFFIX)])
    return cache_path


def _iter_attachments_dirs(data_dir: Path) -> Iterable[Path]:
    """Channel attachments dirs only: data/<channel>/attachments, plus the
    nested ones group channel ids create (they contain "/":
    data/signal-group:<a>/<b>/attachments). A group subtree is walked only
    until its first attachments dir; user workspace folders that happen to be
    named "attachments" (data/<number>/projects/attachments) are never swept.
    Symlinked dirs are not followed; anything outside data_dir is skipped."""
    def ok(path: Path) -> bool:
        return path.is_dir() and not path.is_symlink() and is_within_directory(path, data_dir)

    for channel_dir in data_dir.iterdir():
        if not channel_dir.is_dir() or channel_dir.is_symlink():
            continue
        direct = channel_dir / "attachments"
        if ok(direct):
            yield direct
        elif channel_dir.name.startswith("signal-group:"):
            stack = [channel_dir]
            while stack:
                current = stack.pop()
                for child in current.iterdir():
                    if not child.is_dir() or child.is_symlink():
                        continue
                    if ok(child / "attachments"):
                        yield child / "attachments"
                    else:
                        stack.append(child)


def sweep_expired_audio(data_dir: Path, retention_hours: float) -> int:
    """Delete audio attachments (+ their sidecar cache) past `retention_hours`.

    Swept strictly by the audio file's own mtime, not by whether a
    `.transcript.md` sidecar exists - audio whose transcription failed
    (too-long / ffmpeg error / empty result, all of which return before
    write_transcript_cache() ever runs) never gets a sidecar and would
    otherwise accumulate forever. `max_duration_seconds` caps any single
    transcription well under an hour, so age alone is a safe signal that
    a file is no longer mid-flight.

    A second pass handles sidecars whose audio file is already gone (e.g. a
    prior sweep unlinked the audio but the sidecar unlink then failed) -
    the main pass is keyed off audio files, so an audio-less sidecar would
    otherwise never be revisited. These are swept by the sidecar's own
    mtime and aren't counted in the returned total (that count is audio
    files removed, the metric callers/tests care about).

    Returns the number of audio files deleted. Best-effort: an OSError on
    any one file is logged and skipped, not raised.
    """
    if retention_hours <= 0:
        return 0
    if not data_dir.exists():
        return 0

    cutoff = time.time() - (retention_hours * 3600)
    removed = 0
    for attachments_dir in _iter_attachments_dirs(data_dir):
        for path in attachments_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in _AUDIO_FILE_EXTENSIONS:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except OSError as exc:
                LOG.warning(f"sweep_expired_audio: could not stat {path}: {exc}")
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                LOG.warning(f"sweep_expired_audio: could not remove {path}: {exc}")
                continue
            cache = _transcript_cache_path(path)
            try:
                cache.unlink(missing_ok=True)
            except OSError as exc:
                LOG.warning(f"sweep_expired_audio: could not remove sidecar {cache}: {exc}")

    for attachments_dir in _iter_attachments_dirs(data_dir):
        for cache in attachments_dir.glob(f"*{_SIDECAR_SUFFIX}"):
            if _audio_path_for_sidecar(cache).exists():
                continue  # handled by the main pass above
            try:
                if cache.stat().st_mtime > cutoff:
                    continue
            except OSError as exc:
                LOG.warning(f"sweep_expired_audio: could not stat orphaned sidecar {cache}: {exc}")
                continue
            try:
                cache.unlink()
            except OSError as exc:
                LOG.warning(f"sweep_expired_audio: could not remove orphaned sidecar {cache}: {exc}")
    return removed