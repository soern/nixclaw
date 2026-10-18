"""Tests for session-local (ephemeral) working-history compaction.

Covers:
- LLMClient.summarize_working_history (raw OpenAI-format dicts)
- LLMClient.summarize_conversation unchanged behavior (Message objects)
- AgentLoop._maybe_compact_session boundary / snap logic
"""
import types
from unittest.mock import AsyncMock

import pytest

from src.nixclaw_agent.config import OllamaConfig, VllmConfig, config as config_module
from src.nixclaw_agent.core import AgentLoop
from src.nixclaw_agent.llm import LLMClient, VLLMClient
from src.nixclaw_agent.types import Event, Message, ToolCall, ToolResult


def _make_client() -> LLMClient:
    return LLMClient(OllamaConfig())


def _event(msg: str) -> Event:
    return Event(source="+000000", destination="+491234567890", message=msg, timestamp=0, group_id=None)


# --------------------------------------------------------------------------
# summarize_working_history
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_working_history_empty_returns_blank():
    client = _make_client()
    client.chat = AsyncMock(return_value=("should not be used", []))
    result = await client.summarize_working_history([])
    assert result == ""
    client.chat.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_working_history_formats_and_calls_llm():
    client = _make_client()
    client.chat = AsyncMock(return_value=("SUMMARY: compacted\nKEYWORDS: a, b", []))

    working = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": "calling a tool",
            "tool_calls": [{"id": "1", "function": {"name": "search_files", "arguments": {}}}],
        },
        {"role": "tool", "tool_call_id": "1", "content": "x" * 500},
        {"role": "assistant", "content": "done reasoning"},
    ]

    result = await client.summarize_working_history(working, max_keywords=7)

    assert result == "SUMMARY: compacted\nKEYWORDS: a, b"
    client.chat.assert_awaited_once()

    # The prompt passed to chat should embed the formatted conversation text.
    sent_messages = client.chat.call_args.args[0]
    prompt = sent_messages[0]["content"]
    assert "User: do the thing" in prompt
    assert "Assistant: calling a tool" in prompt
    assert "[Tools called: search_files]" in prompt
    # Tool result truncated to 200 chars + "..."
    assert "Tool result: " + ("x" * 200) + "..." in prompt
    assert "max 7" in prompt


# --------------------------------------------------------------------------
# summarize_conversation (unchanged behavior smoke test)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_conversation_empty_returns_blank():
    client = _make_client()
    client.chat = AsyncMock(return_value=("nope", []))
    assert await client.summarize_conversation([]) == ""
    client.chat.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_conversation_summarizes_message_objects():
    client = _make_client()
    client.chat = AsyncMock(return_value=("SUMMARY: ok\nKEYWORDS: k", []))

    messages = [
        Message(event=_event("hello there"), is_outgoing=False, processed=True),
        Message(
            event=_event("here is my reply"),
            is_outgoing=True,
            processed=True,
            tool_calls=[ToolCall(id="1", name="web_search", arguments={})],
            tool_results=[ToolResult(name="web_search", success=True, output="found data", error=None)],
        ),
    ]

    result = await client.summarize_conversation(messages)
    assert result == "SUMMARY: ok\nKEYWORDS: k"
    client.chat.assert_awaited_once()
    prompt = client.chat.call_args.args[0][0]["content"]
    assert "User: hello there" in prompt
    assert "Assistant: here is my reply" in prompt
    assert "[Tools called: web_search]" in prompt


# --------------------------------------------------------------------------
# _maybe_compact_session
# --------------------------------------------------------------------------

def _fake_loop(summary_return: str = "SUMMARY: compacted work"):
    """Minimal fake `self` for AgentLoop._maybe_compact_session."""
    llm = types.SimpleNamespace()
    llm.summarize_working_history = AsyncMock(return_value=summary_return)
    return types.SimpleNamespace(_llm_client=llm)


def _build_messages():
    """anchor (system + user) + 3 assistant/tool groups (working)."""
    big = "y" * 400  # large content so estimate_token_count is high
    messages = [
        {"role": "system", "content": big},
        {"role": "user", "content": big},
    ]
    for i in range(3):
        messages.append({
            "role": "assistant",
            "content": big,
            "tool_calls": [{"id": str(i), "function": {"name": "grep", "arguments": {}}}],
        })
        messages.append({"role": "tool", "tool_call_id": str(i), "content": big})
    return messages  # anchor_len = 2, total 8


@pytest.fixture
def small_compaction_config(monkeypatch):
    monkeypatch.setattr(config_module.chat, "min_messages_to_keep", 2)
    monkeypatch.setattr(config_module.chat, "turn_compaction_threshold_tokens", 10)
    monkeypatch.setattr(config_module.chat, "max_keywords", 20)


@pytest.mark.asyncio
async def test_compaction_happens_over_threshold(small_compaction_config):
    fake = _fake_loop()
    messages = _build_messages()
    anchor = messages[:2]
    original = list(messages)

    await AgentLoop._maybe_compact_session(fake, messages, 2, "chan", "cron")

    # Anchor preserved exactly.
    assert messages[:2] == anchor
    # Exactly one PROGRESS SUMMARY system message at index anchor_len.
    assert messages[2]["role"] == "system"
    assert messages[2]["content"].startswith("PROGRESS SUMMARY (earlier work compacted):")
    # Only one PROGRESS SUMMARY present.
    assert sum(1 for m in messages if str(m.get("content", "")).startswith("PROGRESS SUMMARY")) == 1
    # Tail starts with an assistant message (no orphaned tool result).
    assert messages[3]["role"] == "assistant"
    # Tail preserved verbatim: last two messages match original tail.
    assert messages[-2:] == original[-2:]
    fake._llm_client.summarize_working_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_compaction_under_threshold(small_compaction_config, monkeypatch):
    monkeypatch.setattr(config_module.chat, "turn_compaction_threshold_tokens", 10_000_000)
    fake = _fake_loop()
    messages = _build_messages()
    original = list(messages)

    await AgentLoop._maybe_compact_session(fake, messages, 2, "chan", "cron")

    assert messages == original
    fake._llm_client.summarize_working_history.assert_not_called()


@pytest.mark.asyncio
async def test_no_compaction_when_working_len_small(small_compaction_config, monkeypatch):
    monkeypatch.setattr(config_module.chat, "min_messages_to_keep", 100)
    fake = _fake_loop()
    messages = _build_messages()
    original = list(messages)

    await AgentLoop._maybe_compact_session(fake, messages, 2, "chan", "cron")

    assert messages == original
    fake._llm_client.summarize_working_history.assert_not_called()


@pytest.mark.asyncio
async def test_failed_summary_leaves_messages_unchanged(small_compaction_config):
    fake = _fake_loop(summary_return="")
    messages = _build_messages()
    original = list(messages)

    await AgentLoop._maybe_compact_session(fake, messages, 2, "chan", "subagent")

    assert messages == original
    fake._llm_client.summarize_working_history.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_exception_leaves_messages_unchanged(small_compaction_config):
    fake = _fake_loop()
    fake._llm_client.summarize_working_history = AsyncMock(side_effect=RuntimeError("boom"))
    messages = _build_messages()
    original = list(messages)

    await AgentLoop._maybe_compact_session(fake, messages, 2, "chan", "cron")

    assert messages == original


# --------------------------------------------------------------------------
# VLLMClient delegation (regression guard for the production vLLM backend bug:
# summarization was a silent no-op because these methods lived only on
# LLMClient). The two classes stay independent (item #12 invariant) and share
# logic via self-free module-level helpers (item #13).
# --------------------------------------------------------------------------

class TestVLLMClientSummarizationDelegation:
    def _vllm(self) -> VLLMClient:
        # VllmConfig() defaults are enough to construct without a network call.
        return VLLMClient(VllmConfig())

    def test_vllm_exposes_summarization_methods(self):
        assert hasattr(VLLMClient, "summarize_working_history")
        assert hasattr(VLLMClient, "summarize_conversation")
        assert hasattr(VLLMClient, "distill_keywords")

    def test_class_structure_invariant_intact(self):
        # Item #12: VLLMClient must NOT inherit from LLMClient (or gain any base).
        assert VLLMClient.__bases__ == (object,)
        assert not issubclass(VLLMClient, LLMClient)

    @pytest.mark.asyncio
    async def test_vllm_summarize_working_history_delegates(self):
        vllm = self._vllm()
        vllm.chat = AsyncMock(return_value=("SUMMARY: ok\nKEYWORDS: k", []))

        working = [
            {"role": "user", "content": "do the thing"},
            {
                "role": "assistant",
                "content": "calling a tool",
                "tool_calls": [{"id": "1", "function": {"name": "grep", "arguments": {}}}],
            },
            {"role": "tool", "tool_call_id": "1", "content": "result body"},
        ]

        result = await vllm.summarize_working_history(working, max_keywords=9)

        assert result == "SUMMARY: ok\nKEYWORDS: k"
        vllm.chat.assert_awaited_once()
        prompt = vllm.chat.call_args.args[0][0]["content"]
        assert "User: do the thing" in prompt
        assert "[Tools called: grep]" in prompt
        assert "max 9" in prompt

    @pytest.mark.asyncio
    async def test_vllm_summarize_conversation_delegates(self):
        vllm = self._vllm()
        vllm.chat = AsyncMock(return_value=("SUMMARY: ok\nKEYWORDS: k", []))

        messages = [
            Message(event=_event("hello there"), is_outgoing=False, processed=True),
            Message(event=_event("here is my reply"), is_outgoing=True, processed=True),
        ]

        result = await vllm.summarize_conversation(messages)

        assert result == "SUMMARY: ok\nKEYWORDS: k"
        vllm.chat.assert_awaited_once()
        prompt = vllm.chat.call_args.args[0][0]["content"]
        assert "User: hello there" in prompt
        assert "Assistant: here is my reply" in prompt

    @pytest.mark.asyncio
    async def test_vllm_distill_keywords_delegates(self):
        vllm = self._vllm()
        vllm.chat = AsyncMock(return_value=("alpha, beta, gamma", []))

        result = await vllm.distill_keywords("some summary")

        assert result == ["alpha", "beta", "gamma"]
        vllm.chat.assert_awaited_once()
        prompt = vllm.chat.call_args.args[0][0]["content"]
        assert "some summary" in prompt
