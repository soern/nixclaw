"""Call audio over PulseAudio (signal-api container, reached via TCP).

Per call, signal-call-tunnel creates (ringrtc bin/virtual_audio.sh):
  - `<outputDeviceName>` null sink: remote audio is played into it, so we
    record its `.monitor`
  - `sink_for_<inputDeviceName>` null sink: what we play into it is the
    call's microphone
The tunnel does not always unload these modules when a call ends, so
`unload_call_modules` sweeps them.
"""

import asyncio
import io
import logging
import os
import re
import wave
from typing import AsyncIterator, Optional

from .vad import FRAME_BYTES, SAMPLE_RATE

LOG = logging.getLogger(__name__)

LEAD_SILENCE_MS = 150
TAIL_SILENCE_MS = 400
PLAYBACK_LATENCY_MS = 150


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data: bytes) -> tuple[bytes, int, int]:
    """(pcm, rate, channels) of a 16-bit PCM WAV; ValueError otherwise."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if w.getsampwidth() != 2:
                raise ValueError(f"unsupported sample width {w.getsampwidth()}")
            return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    except wave.Error as e:
        raise ValueError(f"not a PCM WAV: {e}") from e


def _env(pulse_server: str) -> dict[str, str]:
    return {**os.environ, "PULSE_SERVER": pulse_server}


def _raw_args(rate: int, channels: int) -> list[str]:
    return ["--raw", f"--rate={rate}", f"--channels={channels}", "--format=s16le"]


async def _kill(proc: Optional[asyncio.subprocess.Process]) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except asyncio.TimeoutError:
        LOG.warning(f"audio subprocess {proc.pid} did not exit after kill")


class PulseCapture:
    """Records the remote party: 16 kHz mono s16le in 20 ms frames."""

    def __init__(self, output_device: str, pulse_server: str):
        self.source = f"{output_device}.monitor"
        self.pulse_server = pulse_server
        self._proc: Optional[asyncio.subprocess.Process] = None

    async def frames(self) -> AsyncIterator[bytes]:
        self._proc = await asyncio.create_subprocess_exec(
            "parec", "-d", self.source, *_raw_args(SAMPLE_RATE, 1), "--latency-msec=20",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=_env(self.pulse_server),
        )
        assert self._proc.stdout is not None
        try:
            while True:
                try:
                    yield await self._proc.stdout.readexactly(FRAME_BYTES)
                except asyncio.IncompleteReadError:
                    return
        finally:
            await _kill(self._proc)

    async def stop(self) -> None:
        await _kill(self._proc)


class PulsePlayer:
    """Plays PCM into the call; `interrupt()` stops the current playback."""

    def __init__(self, input_device: str, pulse_server: str):
        self.sink = f"sink_for_{input_device}"
        self.pulse_server = pulse_server
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._interrupted = False

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def play(self, pcm: bytes, rate: int, channels: int = 1) -> bool:
        """Play and wait until drained. False if interrupted or pacat failed.

        Silence is added around the clip: without it the start and especially
        the end of a clip got clipped on the way through PulseAudio and the
        tunnel's encoder.
        """
        self._interrupted = False
        bytes_per_ms = rate * channels * 2 // 1000
        pcm = b"\0" * (bytes_per_ms * LEAD_SILENCE_MS) + pcm + b"\0" * (bytes_per_ms * TAIL_SILENCE_MS)
        proc = await asyncio.create_subprocess_exec(
            "pacat", "-p", "-d", self.sink, *_raw_args(rate, channels),
            f"--latency-msec={PLAYBACK_LATENCY_MS}",
            stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_env(self.pulse_server),
        )
        self._proc = proc
        assert proc.stdin is not None
        try:
            proc.stdin.write(pcm)
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.wait()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._proc = None
        if self._interrupted:
            return False
        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
            LOG.warning(f"pacat into {self.sink} exited {proc.returncode}: {err[:200]}")
            return False
        return True

    async def interrupt(self) -> None:
        self._interrupted = True
        await _kill(self._proc)


async def unload_call_modules(device_names: list[str], pulse_server: str) -> int:
    """Unload the PulseAudio modules a call's tunnel left behind."""
    names = [n for n in device_names if n]
    if not names:
        return 0
    proc = await asyncio.create_subprocess_exec(
        "pactl", "list", "short", "modules",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=_env(pulse_server),
    )
    out, _ = await proc.communicate()
    # whole-name match: signal_input_12 must not match signal_input_123
    pattern = re.compile("(?:%s)(?!\\w)" % "|".join(re.escape(n) for n in names))
    ids = []
    for line in out.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and pattern.search(parts[2]):
            ids.append(parts[0])
    # remap-source modules reference the sinks: unload newest first
    for mod_id in sorted(ids, key=int, reverse=True):
        p = await asyncio.create_subprocess_exec(
            "pactl", "unload-module", mod_id,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=_env(pulse_server),
        )
        await p.wait()
    return len(ids)
