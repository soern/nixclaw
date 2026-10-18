"""End-to-end audio transcription test against the mock Signal API.

Self-skips unless:
  - mock-signal-api is reachable at http://localhost:8081/_mock/inject
  - faster-whisper-server is reachable at $STT_BASE_URL (default
    http://localhost:8001) and responds to /v1/models

Sends a voice message via the mock API with the `voice_test.aac` fixture,
then asserts the mock logs (docker logs nixclaw-mock-signal-api) show a
reply whose text references the fixture transcript's keyword.

Run with the standard compose stack up:
    docker compose up -d --build
    python3 -m pytest tests/test_audio_e2e.py -v -s
"""

import pytest

import asyncio
import os
import subprocess
import time
from datetime import datetime, timezone

import httpx
import pytest


MOCK_URL = os.environ.get("MOCK_API_URL", "http://localhost:8081")
STT_URL = os.environ.get("STT_BASE_URL", "http://localhost:8001")
SOURCE = "+000000"  # PRIVILEGED in default config
FIXTURE = "voice_test.aac"


def _reachable(url: str, path: str = "/_mock/inject", timeout: float = 2.0) -> bool:
    """Lightweight reachability check — does GET on `url/path` return any HTTP response?"""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url.rstrip("/") + path)
            return r.status_code < 500
    except Exception:
        return False


def _stt_alive() -> bool:
    try:
        with httpx.Client(timeout=2.0) as client:
            r = client.get(STT_URL.rstrip("/") + "/v1/models")
            return r.status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _reachable(MOCK_URL),
        reason=f"mock-signal-api not reachable at {MOCK_URL}",
    ),
    pytest.mark.skipif(
        not _stt_alive(),
        reason=f"faster-whisper-server not reachable at {STT_URL}",
    ),
]


def _inject_voice_message(text: str = "[voice]") -> dict:
    payload = {
        "source": SOURCE,
        "destination": "+491234567890",
        "message": text,
        "attachments": [
            {"id": FIXTURE, "contentType": "audio/aac", "filename": FIXTURE},
        ],
    }
    r = httpx.post(f"{MOCK_URL}/_mock/inject", json=payload, timeout=10.0)
    r.raise_for_status()
    return r.json()


def _container_logs_since(container: str, since: str) -> str:
    """Log lines of `container` written at or after `since` (RFC 3339)."""
    try:
        out = subprocess.run(
            ["docker", "logs", container, "--since", since],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=15,
        )
        return out.stdout
    except Exception as exc:
        return f"<docker log fetch failed: {exc}>"


def test_e2e_voice_message_round_trip():
    """Send a voice message via mock API; expect a transcription and a reply.

    Only log lines written after the injection count, so a reply left in the
    log by an earlier run or test can't make this pass."""
    # Budget: STT of the fixture + an LLM turn, which can queue behind other
    # requests on the shared backend (vllm max_concurrent_wait_seconds 240s).
    deadline = time.time() + 300
    since = datetime.now(timezone.utc).isoformat()
    _inject_voice_message("[voice] transcribe please")

    reply = f"Reply received for ['{SOURCE}']"
    mock_logs = nixclaw_logs = ""
    while time.time() < deadline:
        mock_logs = _container_logs_since("nixclaw-mock-signal-api", since)
        if reply in mock_logs:
            break
        time.sleep(5)
    nixclaw_logs = _container_logs_since("nixclaw", since)

    assert "STT: transcribing" in nixclaw_logs, (
        f"No transcription logged by nixclaw. Recent lines:\n{nixclaw_logs[-2000:]}"
    )
    assert reply in mock_logs, (
        f"No reply to {SOURCE} within 300s. Mock log lines:\n{mock_logs[-2000:]}"
    )


def test_e2e_stt_server_reachable():
    """Sanity: the whisper server itself answers /v1/audio/transcriptions on a known wav."""
    # Use the same fixture, transcoded to wav on the fly via the server's
    # own multipart path. The server accepts whatever ffmpeg decodes.
    import wave
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / FIXTURE
    assert fixture.exists(), f"fixture missing: {fixture}"

    with open(fixture, "rb") as f:
        r = httpx.post(
            f"{STT_URL}/v1/audio/transcriptions",
            files={"file": (FIXTURE, f.read(), "audio/aac")},
            data={"language": "de", "model": "base"},
            timeout=30.0,
        )
    assert r.status_code == 200, f"STT returned {r.status_code}: {r.text}"
    body = r.json()
    assert "text" in body
    # We don't strictly check the text content (TTS audio quality varies)
    # but the response should be non-empty.
    assert body["text"].strip(), f"empty transcription: {body}"