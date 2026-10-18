"""Integration test for STT injection into core.AgentLoop.process_message.

Verifies that when a message arrives with an audio attachment:
- The transcript replaces the placeholder text ([Attachment] or empty)
- The DB row gets re-stored with the transcript-bearing message
- A transcription failure still produces a placeholder (no crash)
- The re-stored message has the same source:timestamp key (INSERT OR REPLACE)

Uses the shared reset_db fixture in conftest.py (also used by
test_core_tool_result_wiring.py, test_channel_turn_serialization.py, and
test_whitespace_only_response_not_sent.py). _make_agent() here is its own
parameterized variant (stt_config/transcript/fail + STT-client stubbing), not
the identical build_mock_agent_loop() those three files share.
"""

import asyncio
import sqlite3
import tempfile
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.nixclaw_agent.auth import AuthManager, AuthConfig
from src.nixclaw_agent.config import SttConfig
from src.nixclaw_agent.context import ContextManager
from src.nixclaw_agent.core import AgentLoop
from src.nixclaw_agent.cron import CronScheduler
from src.nixclaw_agent.tools.registry import ToolRegistry
from src.nixclaw_agent.types import Attachment, Event

pytestmark = pytest.mark.usefixtures("reset_db")


def _make_wav(path: Path, duration_s: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * int(16000 * duration_s))
    return path


def _make_agent(stt_config=None, transcript="hallo welt", fail=False) -> AgentLoop:
    """Build an AgentLoop with signal_client + llm_client stubbed out."""
    cfg = stt_config or SttConfig(enabled=True, base_url="http://x", language="de",
                                  timeout=5.0, max_duration_seconds=60.0, chunk_seconds=10.0)
    # The core.py module reads `config.stt` at runtime (not at AgentLoop
    # construction time), so monkeypatching the singleton here is enough
    # to flip the kill switch for that test.
    from src.nixclaw_agent.config import config as live_cfg
    import dataclasses
    live_cfg.stt = cfg

    signal_client = MagicMock()
    signal_client.save_attachment = AsyncMock()
    signal_client.send_message = AsyncMock()
    signal_client.send_typing_indicator = AsyncMock()
    signal_client.send_read_receipt = AsyncMock()

    llm_client = MagicMock()
    llm_client.build_system_prompt = MagicMock(return_value="sys")
    llm_client.build_messages = MagicMock(return_value=[{"role": "user", "content": "x"}])
    llm_client.chat = AsyncMock(return_value=("ok", []))

    auth = AuthManager(AuthConfig(privileged_numbers=["+000000"]))
    tool_registry = MagicMock(spec=ToolRegistry)
    tool_registry.set_context = MagicMock()
    tool_registry.get_tools_for_tier = MagicMock(return_value=[])
    tool_registry.invalidate_file_list_cache = MagicMock()
    context_manager = MagicMock(spec=ContextManager)
    context_manager.get_context = MagicMock()
    context_manager.get_messages_for_llm = MagicMock(return_value=[])
    context_manager.get_summary = MagicMock(return_value=(None, None))
    context_manager.get_message_count = MagicMock(return_value=0)
    cron = MagicMock(spec=CronScheduler)

    agent = AgentLoop(
        signal_client=signal_client,
        llm_client=llm_client,
        auth_manager=auth,
        context_manager=context_manager,
        tool_registry=tool_registry,
        cron_scheduler=cron,
    )

    # Stub STT client so we don't hit a real network.
    if fail:
        from src.nixclaw_agent.stt import STTError
        agent._stt_client.transcribe_file = AsyncMock(side_effect=STTError("boom"))
    else:
        agent._stt_client.transcribe_file = AsyncMock(return_value=transcript)

    return agent


@pytest.mark.asyncio
async def test_audio_attachment_transcribed_and_message_rewritten(tmp_path, monkeypatch):
    audio = _make_wav(tmp_path / "msg.aac")
    saved = Attachment(
        id="abc",
        content_type="audio/aac",
        filename="msg.aac",
        local_path=str(audio),
        size=100,
    )
    agent = _make_agent(transcript="hallo welt, dies ist ein test")

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="[Attachment]",
        timestamp=1700000000,
        group_id=None,
        attachments=[saved],
        mentions=[],
    )
    # Patch save_attachment on the agent's signal_client to return the saved obj
    agent._signal_client.save_attachment = AsyncMock(return_value=saved)

    # Bypass LLM by having chat() raise; we only care about the message rewrite.
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop after STT"))

    await agent.process_message(event)

    # Re-read the DB and confirm the row was rewritten with the transcript
    from src.nixclaw_agent.config import config as _cfg
    conn = sqlite3.connect(str(_cfg.database.db_path))
    rows = conn.execute(
        "SELECT content FROM messages WHERE id = ?", ("+000000:1700000000",)
    ).fetchall()
    conn.close()
    assert rows, "no row in messages table"
    content = rows[0][0]
    assert "Voice message" in content
    assert "hallo welt" in content


@pytest.mark.asyncio
async def test_audio_attachment_failure_returns_placeholder(tmp_path):
    audio = _make_wav(tmp_path / "msg.aac")
    saved = Attachment(
        id="abc",
        content_type="audio/aac",
        filename="msg.aac",
        local_path=str(audio),
        size=100,
    )
    agent = _make_agent(fail=True)
    agent._signal_client.save_attachment = AsyncMock(return_value=saved)
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop after STT"))

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="[Attachment]",
        timestamp=1700000001,
        group_id=None,
        attachments=[saved],
        mentions=[],
    )

    await agent.process_message(event)

    from src.nixclaw_agent.config import config as _cfg
    conn = sqlite3.connect(str(_cfg.database.db_path))
    rows = conn.execute(
        "SELECT content FROM messages WHERE id = ?", ("+000000:1700000001",)
    ).fetchall()
    conn.close()
    assert rows
    assert "failed" in rows[0][0]


@pytest.mark.asyncio
async def test_audio_attachment_with_existing_text_appends(tmp_path):
    audio = _make_wav(tmp_path / "msg.aac")
    saved = Attachment(
        id="abc",
        content_type="audio/aac",
        filename="msg.aac",
        local_path=str(audio),
        size=100,
    )
    agent = _make_agent(transcript="hallo")
    agent._signal_client.save_attachment = AsyncMock(return_value=saved)
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop after STT"))

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="siehe sprachnachricht",
        timestamp=1700000002,
        group_id=None,
        attachments=[saved],
        mentions=[],
    )

    await agent.process_message(event)

    from src.nixclaw_agent.config import config as _cfg
    conn = sqlite3.connect(str(_cfg.database.db_path))
    rows = conn.execute(
        "SELECT content FROM messages WHERE id = ?", ("+000000:1700000002",)
    ).fetchall()
    conn.close()
    assert rows
    content = rows[0][0]
    assert "Voice message" in content
    assert "hallo" in content
    assert "siehe sprachnachricht" in content


@pytest.mark.asyncio
async def test_stt_disabled_keeps_placeholder(tmp_path):
    audio = _make_wav(tmp_path / "msg.aac")
    saved = Attachment(
        id="abc",
        content_type="audio/aac",
        filename="msg.aac",
        local_path=str(audio),
        size=100,
    )
    cfg = SttConfig(enabled=False)
    agent = _make_agent(stt_config=cfg)
    agent._signal_client.save_attachment = AsyncMock(return_value=saved)
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop"))

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="[Attachment]",
        timestamp=1700000003,
        group_id=None,
        attachments=[saved],
        mentions=[],
    )

    await agent.process_message(event)

    from src.nixclaw_agent.config import config as _cfg
    conn = sqlite3.connect(str(_cfg.database.db_path))
    rows = conn.execute(
        "SELECT content FROM messages WHERE id = ?", ("+000000:1700000003",)
    ).fetchall()
    conn.close()
    assert rows
    # When STT is disabled there is no transcript; the audio file is
    # announced like any other attachment so the agent can still name it.
    assert "Voice message" not in rows[0][0]
    assert rows[0][0] == "[Attachment received: msg.aac]"


@pytest.mark.asyncio
async def test_non_audio_attachment_unaffected(tmp_path):
    """Image attachments must not trigger the STT path at all."""
    saved = Attachment(
        id="img",
        content_type="image/png",
        filename="x.png",
        local_path="/tmp/x.png",
        size=100,
    )
    agent = _make_agent()
    agent._signal_client.save_attachment = AsyncMock(return_value=saved)
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop"))

    event = Event(
        source="+000000",
        destination="+491234567890",
        message="schau mal",
        timestamp=1700000004,
        group_id=None,
        attachments=[saved],
        mentions=[],
    )

    await agent.process_message(event)

    from src.nixclaw_agent.config import config as _cfg
    conn = sqlite3.connect(str(_cfg.database.db_path))
    rows = conn.execute(
        "SELECT content FROM messages WHERE id = ?", ("+000000:1700000004",)
    ).fetchall()
    conn.close()
    assert rows
    assert "Voice message" not in rows[0][0]
    assert rows[0][0] == "schau mal\n\n[Attachment received: x.png]"
    # STT client never called
    agent._stt_client.transcribe_file.assert_not_called()

def _stored_row(timestamp: int):
    from src.nixclaw_agent.config import config as _cfg
    conn = sqlite3.connect(str(_cfg.database.db_path))
    row = conn.execute(
        "SELECT content, processed FROM messages WHERE id = ?", (f"+000000:{timestamp}",)
    ).fetchone()
    conn.close()
    return row


@pytest.mark.asyncio
async def test_attachment_note_stored_with_on_disk_filename(tmp_path):
    """The note is persisted (so history / message_search keep it) and
    names the file in attachments/, which differs from the sender's
    original filename on a rename."""
    saved = Attachment(id="img", content_type="image/png", filename="photo_1.png",
                       local_path=str(tmp_path / "photo_1.png"), size=100)
    agent = _make_agent()
    agent._signal_client.save_attachment = AsyncMock(return_value=saved)
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop"))

    event = Event(source="+000000", destination="+491234567890", message="dieses Bild bitte",
                  timestamp=1700000010, group_id=None,
                  attachments=[Attachment(id="img", content_type="image/png", filename="photo.png")],
                  mentions=[])
    await agent.process_message(event)

    content, _ = _stored_row(1700000010)
    assert content == "dieses Bild bitte\n\n[Attachment received: photo_1.png]"
    # The LLM saw the same text (note appears exactly once).
    sent = agent._llm_client.build_messages.call_args.args[1]
    assert sent.message == content


@pytest.mark.asyncio
async def test_failed_download_not_announced(tmp_path):
    agent = _make_agent()
    agent._signal_client.save_attachment = AsyncMock(return_value=None)
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop"))

    event = Event(source="+000000", destination="+491234567890", message="hi",
                  timestamp=1700000011, group_id=None,
                  attachments=[Attachment(id="img", content_type="image/png", filename="x.png")],
                  mentions=[])
    await agent.process_message(event)

    content, _ = _stored_row(1700000011)
    assert content == "hi"


@pytest.mark.asyncio
async def test_audio_not_announced_twice_when_stt_on(tmp_path):
    """With STT on, audio is named by the "[Voice message: ...]" tag only;
    other attachments in the same message still get the note."""
    audio = Attachment(id="a", content_type="audio/aac", filename="msg.aac",
                       local_path=str(_make_wav(tmp_path / "msg.aac")), size=100)
    image = Attachment(id="i", content_type="image/png", filename="x.png",
                       local_path=str(tmp_path / "x.png"), size=100)
    agent = _make_agent(transcript="hallo")
    agent._signal_client.save_attachment = AsyncMock(side_effect=[audio, image])
    agent._llm_client.chat = AsyncMock(side_effect=RuntimeError("stop"))

    event = Event(source="+000000", destination="+491234567890", message="[Attachment]",
                  timestamp=1700000012, group_id=None, attachments=[audio, image], mentions=[])
    await agent.process_message(event)

    content, _ = _stored_row(1700000012)
    assert content.count("msg.aac") == 1
    assert "[Voice message: msg.aac] hallo" in content
    assert "[Attachment received: x.png]" in content
