"""On-demand second-opinion STT tool, backed by the alternate STT backend.

The primary voice-message pipeline (core.py + stt.py) always uses
`config.stt` (faster-whisper-large-v3). This tool lets the agent re-run a
*previously received* voice message through `config.stt.alternate`
(currently NVIDIA Canary-1b-v2, deploy/dgx-spark/canary-1b-v2/) when the
primary transcript looks garbled or nonsensical - not an automatic
fallback, since benchmarking showed Canary can silently return an empty
transcript on noisy/code-switched audio.

Only works within `config.stt.retention_hours` of the message being
received - `stt.sweep_expired_audio()` deletes the source audio (and its
sidecar cache) after that window.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ..config import SttConfig, config
from ..stt import STTError, FasterWhisperClient, _transcribe_chunks, convert_to_wav_chunks, probe_duration
from ..types import ToolResult
from .paths import get_attachments_dir

LOG = logging.getLogger(__name__)

_TOOL_NAME = "retranscribe_audio"

# Module-level (not per-call) so the concurrency cap is shared across every
# retranscribe_audio invocation in the process, keyed by base_url the same
# way tts.py's _get_shared_tts_semaphore is - the alternate backend is a
# single shared GPU host and multiple channels/turns could otherwise pile
# up unthrottled requests against it.
_alt_semaphores: dict[str, asyncio.Semaphore] = {}
_alt_semaphore_sizes: dict[str, int] = {}


def _get_alt_semaphore(base_url: str, max_concurrent: int) -> asyncio.Semaphore:
    existing = _alt_semaphores.get(base_url)
    if existing is None:
        existing = asyncio.Semaphore(max_concurrent)
        _alt_semaphores[base_url] = existing
        _alt_semaphore_sizes[base_url] = max_concurrent
    elif _alt_semaphore_sizes.get(base_url) != max_concurrent:
        LOG.warning(
            f"Alternate-STT semaphore for base_url={base_url!r} already created with size "
            f"{_alt_semaphore_sizes.get(base_url)}; ignoring different size "
            f"{max_concurrent} from a later call"
        )
    return existing


def _error(msg: str) -> ToolResult:
    return ToolResult(name=_TOOL_NAME, success=False, output=None, error=msg)


async def retranscribe_audio(filename: str, language: Optional[str] = None, channel_id: str = "") -> ToolResult:
    """Re-transcribe a previously received voice message with the alternate STT backend.

    Args:
        filename: Bare filename in the attachments/ directory, as embedded
            in the "[Voice message: <filename>] ..." text the agent already
            saw for this attachment.
        language: ISO-639-1 code for the alternate backend (required if
            config.stt.alternate.language isn't set - the alternate backend
            has no language auto-detection).
        channel_id: The channel ID to scope file operations to (injected).

    Returns:
        ToolResult whose output is the alternate transcript on success.
    """
    try:
        alt_cfg = config.stt.alternate
        if not alt_cfg.enabled:
            return _error("The alternate STT backend is disabled (set stt.alternate.enabled: true in config/agent.yaml)")

        if ".." in filename or "/" in filename or "\\" in filename:
            return _error("Invalid filename - path traversal not allowed")

        try:
            attachments_dir = get_attachments_dir(channel_id)
        except ValueError:
            return _error(f"Invalid channel_id: {channel_id!r}")

        audio_path = (attachments_dir / filename).resolve()
        if audio_path.parent != attachments_dir:
            return _error("Invalid filename - path traversal not allowed")
        if not audio_path.exists():
            return _error(
                f"{filename!r} not found - it may have aged out of the "
                f"{config.stt.retention_hours}h retention window, or was never a voice message attachment"
            )

        eff_language = language or alt_cfg.language
        if not eff_language:
            return _error(
                "language is required - the alternate STT backend has no language auto-detection. "
                "Pass an ISO-639-1 code (e.g. 'de' or 'en')."
            )

        duration = await probe_duration(audio_path)
        if duration > config.stt.max_duration_seconds:
            return _error(
                f"Audio too long ({duration:.0f}s > {config.stt.max_duration_seconds:.0f}s cap) - "
                "same limit as the primary transcription pipeline"
            )

        client_cfg = SttConfig(
            enabled=True,
            base_url=alt_cfg.base_url,
            model=alt_cfg.model,
            language=eff_language,
            timeout=alt_cfg.timeout,
            max_duration_seconds=config.stt.max_duration_seconds,
            chunk_seconds=config.stt.chunk_seconds,
        )
        client = FasterWhisperClient(client_cfg)

        alt_semaphore = _get_alt_semaphore(alt_cfg.base_url, alt_cfg.max_concurrent_requests)
        try:
            await asyncio.wait_for(alt_semaphore.acquire(), timeout=alt_cfg.max_concurrent_wait_seconds)
        except asyncio.TimeoutError:
            return _error(
                f"Alternate STT backend busy - timed out after {alt_cfg.max_concurrent_wait_seconds:.0f}s "
                f"waiting for a free slot (max_concurrent_requests={alt_cfg.max_concurrent_requests})"
            )

        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="stt-alt-"))
        try:
            chunks = await convert_to_wav_chunks(audio_path, tmpdir, config.stt.chunk_seconds)
            if not chunks:
                return _error("Transcode produced no chunks")

            try:
                transcript = await _transcribe_chunks(chunks, client)
            except STTError as exc:
                return _error(f"Alternate STT failed: {exc}")

            if not transcript:
                return ToolResult(name=_TOOL_NAME, success=True, output="[alternate STT produced no transcript]", error=None)
            return ToolResult(name=_TOOL_NAME, success=True, output=transcript, error=None)
        finally:
            alt_semaphore.release()
            try:
                for chunk in tmpdir.glob("chunk_*.wav"):
                    chunk.unlink()
                tmpdir.rmdir()
            except OSError:
                pass
    except Exception as e:
        LOG.error(f"retranscribe_audio failed: {e}")
        return _error(str(e))
