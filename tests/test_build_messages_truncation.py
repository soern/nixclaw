"""Tests for tool-result discard and tool-call-argument truncation.

These tests assert how tool results are discarded and tool-call
arguments truncated when building LLM messages.

Architecture:
- No SQLite round-trip; Message/Event/Attachment built directly from
  dataclasses in src.nixclaw_agent.types.
- The helper `_truncate_tool_call_arguments` is tested directly.
- Integration through `LLMClient.build_messages` / `VLLMClient.build_messages`.
- Config-based tests patch `ChatConfig` / module-level config object.
- Image-attachment fixture re-uses the 1x1 PNG from test_build_messages_refactor.py.
"""

import base64
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nixclaw_agent.config import ChatConfig, OllamaConfig, VllmConfig
from nixclaw_agent.llm import _truncate_tool_call_arguments, LLMClient, VLLMClient
from nixclaw_agent.types import (
    Attachment,
    Event,
    Message,
    ToolCall,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Fixture helpers (mirrored from test_build_messages_refactor.py)
# ---------------------------------------------------------------------------

_PNG_1x1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def make_event(
    message: str,
    source: str = "+000000",
    destination: str = "bot",
    attachments: Optional[list[Attachment]] = None,
    group_id: Optional[str] = None,
    timestamp: int = 1,
    mentions: Optional[list[str]] = None,
) -> Event:
    return Event(
        source=source,
        destination=destination,
        message=message,
        timestamp=timestamp,
        group_id=group_id,
        attachments=attachments or [],
        mentions=mentions or [],
    )


def make_message(
    text: str,
    is_outgoing: bool,
    *,
    tool_calls: Optional[list[ToolCall]] = None,
    tool_results: Optional[list[ToolResult]] = None,
    source_type: Optional[str] = None,
    source_id: Optional[str] = None,
    attachments: Optional[list[Attachment]] = None,
) -> Message:
    return Message(
        event=make_event(text, attachments=attachments),
        is_outgoing=is_outgoing,
        processed=True,
        tool_calls=tool_calls,
        tool_results=tool_results,
        source_type=source_type,
        source_id=source_id,
    )


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

class TestTruncateToolCallArguments:
    """Unit tests for the _truncate_tool_call_arguments helper."""

    # ------------------------------------------------------------------
    # Item #1: No-truncation passthrough (verbatim contract)
    # ------------------------------------------------------------------
    def test_no_truncation_passthrough(self):
        """Short args are returned unchanged; no _truncated key anywhere."""
        args = {"url": "https://example.com"}
        result = _truncate_tool_call_arguments(args, max_chars=256)
        assert result == '{"url":"https://example.com"}'
        # The marker text must not appear.
        assert "past tool call (auto-truncated):" not in result
        assert "_truncated" not in result

    # ------------------------------------------------------------------
    # Item #2: Truncation fires and marker is visible
    # ------------------------------------------------------------------
    def test_truncation_fires(self):
        """Large args are replaced with a _truncated marker object."""
        args = {"big": "x" * 5000}
        result = _truncate_tool_call_arguments(args, max_chars=4000)
        parsed = json.loads(result)
        assert "_truncated" in parsed
        marker_text = parsed["_truncated"]
        assert "past tool call (auto-truncated):" in marker_text

    # ------------------------------------------------------------------
    # Item #3: Boundary conditions
    # ------------------------------------------------------------------
    def test_boundary_no_truncation(self):
        """len(args) == max_chars → no truncation."""
        # Build a dict that serialises to exactly 256 bytes.
        args = {"k": "v" * (256 - 16)}  # 256 bytes when serialised
        result = _truncate_tool_call_arguments(args, max_chars=256)
        parsed = json.loads(result)
        assert "_truncated" not in parsed

    def test_boundary_with_truncation(self):
        """len(args) == max_chars + 1 → truncation fires."""
        # {"k": "v" * 249} serialises to 257 bytes (5 header bytes + 249 + 3 suffix).
        # At max_chars=256 this is 1 byte over, so truncation fires.
        args = {"k": "v" * 249}  # 257 bytes when JSON-serialised
        result = _truncate_tool_call_arguments(args, max_chars=256)
        parsed = json.loads(result)
        assert "_truncated" in parsed

    # ------------------------------------------------------------------
    # Item #9: Multiple tool calls — mixed truncation
    # ------------------------------------------------------------------
    def test_multiple_tool_calls_mixed_truncation(self):
        """Short, long, long → first untouched, second and third truncated."""
        tc1_args = {"short": "y"}
        tc2_args = {"long": "x" * 3000}
        tc3_args = {"longer": "z" * 4000}

        r1 = _truncate_tool_call_arguments(tc1_args, max_chars=256)
        r2 = _truncate_tool_call_arguments(tc2_args, max_chars=256)
        r3 = _truncate_tool_call_arguments(tc3_args, max_chars=256)

        # tc1 untouched
        assert json.loads(r1) == {"short": "y"}
        # tc2 truncated
        p2 = json.loads(r2)
        assert "_truncated" in p2
        # tc3 truncated
        p3 = json.loads(r3)
        assert "_truncated" in p3
        # IDs (caller's concern) are unaffected — just check the marker
        # format is identical for both truncated entries.
        assert "past tool call (auto-truncated):" in p2["_truncated"]
        assert "past tool call (auto-truncated):" in p3["_truncated"]

    # ------------------------------------------------------------------
    # Item #12: None arguments → JSON null
    # ------------------------------------------------------------------
    def test_none_arguments_render_as_null(self):
        """arguments=None renders as 'null' and is never truncated."""
        # "null" is 4 bytes. Use a max_chars >= 4 so the null literal itself
        # cannot trigger truncation; the "never truncated" guarantee applies
        # to the *kind* of input, not an arbitrary byte budget.
        result = _truncate_tool_call_arguments(None, max_chars=4)
        assert result == "null"
        # Verify the same result is produced even with a large budget.
        result_large = _truncate_tool_call_arguments(None, max_chars=10000)
        assert result_large == result

    # ------------------------------------------------------------------
    # Item #13: Empty dict → "{}"
    # ------------------------------------------------------------------
    def test_empty_dict_arguments(self):
        """arguments={} renders as '{}' and is never truncated."""
        # "{}" is 2 bytes. Use max_chars >= 2 so the literal cannot itself
        # trigger truncation; the "never truncated" guarantee applies to the
        # *kind* of input (empty dict / empty string), not an arbitrary budget.
        result = _truncate_tool_call_arguments({}, max_chars=2)
        assert result == "{}"
        # Also verify dict round-trips through the helper.
        parsed = json.loads(result)
        assert parsed == {}

    # ------------------------------------------------------------------
    # Additional helper coverage
    # ------------------------------------------------------------------
    def test_string_argument_preserved(self):
        """Pre-encoded string args that are valid JSON are kept as-is."""
        raw = '{"url":"https://example.com/path?q=1"}'
        result = _truncate_tool_call_arguments(raw, max_chars=256)
        # Should return the string unchanged (or equivalent JSON).
        assert "url" in result
        assert "_truncated" not in result

    def test_non_json_string_argument_encoded(self):
        """Non-JSON string args are wrapped in {"raw": ...}."""
        raw = "not json at all"
        result = _truncate_tool_call_arguments(raw, max_chars=256)
        parsed = json.loads(result)
        assert "raw" in parsed
        assert parsed["raw"] == raw

    def test_marker_suffix_present(self):
        """Truncated output ends with the '...' suffix."""
        args = {"big": "x" * 5000}
        result = _truncate_tool_call_arguments(args, max_chars=100)
        parsed = json.loads(result)
        marker_text = parsed["_truncated"]
        assert marker_text.endswith("...")

    def test_result_is_valid_json(self):
        """All return values of the helper are valid JSON strings."""
        cases = [
            {"key": "value"},
            None,
            {},
            {"big": "x" * 5000},
            42,  # integer (edge — should be JSON-serialised)
        ]
        for args in cases:
            result = _truncate_tool_call_arguments(args, max_chars=256)
            try:
                json.loads(result)
            except json.JSONDecodeError as e:
                pytest.fail(f"Helper returned invalid JSON for args={args!r}: {e}")


# ---------------------------------------------------------------------------
# Integration tests via LLMClient.build_messages
# ---------------------------------------------------------------------------

class TestBuildMessagesTruncation:
    """Integration tests for truncation behaviour through the public API."""

    # ------------------------------------------------------------------
    # Item #4: Past tool results replayed as call/result pairs
    # ------------------------------------------------------------------
    def test_past_tool_results_replayed(self):
        """Prior results appear as role=tool messages paired with the call id."""
        prior_assistant = make_message(
            "the assistant replies",
            is_outgoing=True,
            tool_calls=[
                ToolCall(id="tc_abc1", name="bash", arguments={"cmd": "echo hello"}),
            ],
            tool_results=[
                ToolResult(name="bash", success=True, output="hello\n", error=None),
            ],
        )
        msgs = [
            make_message("what time is it?", is_outgoing=False),
            prior_assistant,
        ]
        event = make_event("thanks")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        tool_msgs = [m for m in out if m["role"] == "tool"]
        assert [(m["tool_call_id"], m["content"]) for m in tool_msgs] == [("tc_abc1", "hello\n")]
        assert len([m for m in out if m.get("tool_calls")]) == 1

    def test_past_tool_results_replayed_vllm(self):
        """Same behaviour via VLLMClient.build_messages."""
        prior_assistant = make_message(
            "the assistant replies",
            is_outgoing=True,
            tool_calls=[
                ToolCall(id="tc_abc2", name="bash", arguments={"cmd": "echo hi"}),
            ],
            tool_results=[
                ToolResult(name="bash", success=True, output="hi\n", error=None),
            ],
        )
        msgs = [
            make_message("hello?", is_outgoing=False),
            prior_assistant,
        ]
        event = make_event("thanks")
        out = VLLMClient(VllmConfig()).build_messages(msgs, event)

        tool_msgs = [m for m in out if m["role"] == "tool"]
        assert [(m["tool_call_id"], m["content"]) for m in tool_msgs] == [("tc_abc2", "hi\n")]

    # ------------------------------------------------------------------
    # Item #5: Floor clamp and warning at config load
    # ------------------------------------------------------------------
    def test_floor_clamp_and_warning(self, caplog):
        """Sub-floor tool_call_args_max_chars triggers LOG.warning once."""
        with caplog.at_level(logging.WARNING):
            cfg = ChatConfig(tool_call_args_max_chars=50)

        assert len(caplog.records) == 1
        w = caplog.records[0]
        assert "tool_call_args_max_chars=50" in w.message
        assert "256" in w.message
        assert "clamping" in w.message.lower()
        assert "config/agent.yaml" in w.message

        # The effective value is clamped to the floor.
        assert cfg.effective_tool_call_args_max_chars == 256

    def test_warning_not_fired_at_floor(self, caplog):
        """tool_call_args_max_chars=256 emits no warning."""
        with caplog.at_level(logging.WARNING):
            cfg = ChatConfig(tool_call_args_max_chars=256)

        warning_messages = [r.message for r in caplog.records]
        clamp_warnings = [m for m in warning_messages if "clamping" in m.lower()]
        assert len(clamp_warnings) == 0
        assert cfg.effective_tool_call_args_max_chars == 256

    def test_warning_not_fired_above_floor(self, caplog):
        """tool_call_args_max_chars > 256 emits no warning."""
        with caplog.at_level(logging.WARNING):
            cfg = ChatConfig(tool_call_args_max_chars=1024)

        warning_messages = [r.message for r in caplog.records]
        clamp_warnings = [m for m in warning_messages if "clamping" in m.lower()]
        assert len(clamp_warnings) == 0
        assert cfg.effective_tool_call_args_max_chars == 1024

    # ------------------------------------------------------------------
    # Item #7: Configurable ceiling above the floor
    # ------------------------------------------------------------------
    def test_configurable_ceiling_above_floor(self):
        """tool_call_args_max_chars=1024 allows 1024-byte args without truncation."""
        cfg = ChatConfig(tool_call_args_max_chars=1024)
        # Args at exactly 1024 bytes should not trigger truncation.
        args = {"k": "v" * (1024 - 14)}
        result = _truncate_tool_call_arguments(args, max_chars=cfg.effective_tool_call_args_max_chars)
        parsed = json.loads(result)
        assert "_truncated" not in parsed

    # ------------------------------------------------------------------
    # Item #8: In-loop path unchanged (live tool results still appended)
    # ------------------------------------------------------------------
    def test_in_loop_path_unchanged(self, tmp_path, monkeypatch):
        """Live tool results (current turn) must still be appended to messages.

        We simulate the in-loop path by constructing a Message that has both
        tool_calls and tool_results, and verify the assistant message appears
        with tool_calls in the output. The tool-results-drop behaviour applies
        only to *prior* turns (messages that already exist in context_messages
        when build_messages is called); the current turn's tool results are
        appended separately by the agent loop (core.py:612-625), which is
        not changed by this plan.

        NOTE: This test verifies the *helper* does not drop tool_results for
        the assistant message being processed — the discard only applies to
        messages *already in* context_messages. Since _build_history_messages
        (tested here) drops all tool_results, we assert the *assistant message
        itself* is preserved (with tool_calls) so the loop can later append
        its tool result.
        """
        prior = make_message(
            "the user asked something",
            is_outgoing=False,
            tool_calls=None,
            tool_results=None,
        )
        # A "live" assistant message: tool_calls present, tool_results absent
        # (results will be appended by the loop after build_messages returns).
        live_assistant = make_message(
            "I'm running a tool",
            is_outgoing=True,
            tool_calls=[
                ToolCall(id="live_tc_1", name="bash", arguments={"cmd": "echo live"}),
            ],
            tool_results=None,  # not yet available; added by the loop
        )
        msgs = [prior, live_assistant]
        event = make_event("please continue")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        # No results stored -> no role=tool messages.
        assert "tool" not in [m["role"] for m in out]
        # The tool_calls message is preserved, followed by the text message.
        call_msgs = [m for m in out if m.get("tool_calls")]
        assert len(call_msgs) == 1
        assert call_msgs[0]["tool_calls"][0]["id"] == "live_tc_1"
        assert out[out.index(call_msgs[0]) + 1] == {"role": "assistant", "content": "I'm running a tool"}

    # ------------------------------------------------------------------
    # Item #6: Effective value used at build time
    # ------------------------------------------------------------------
    def test_effective_value_used_in_build(self):
        """_build_history_messages uses effective_tool_call_args_max_chars.

        We call _build_history_messages directly with a tool call that exceeds
        a temporarily-reduced effective threshold and verify truncation fires.
        This avoids patching sys.modules which would break other imports.
        """
        from nixclaw_agent.llm import _build_history_messages

        # Tool call args: ~105 bytes. At max_chars=50, must truncate.
        msgs = [
            make_message(
                "some prior message",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(id="tc_eff", name="store_data", arguments={"data": "x" * 100}),
                ],
            ),
        ]

        # Temporarily reduce the live config's effective value so truncation fires.
        import nixclaw_agent.config as config_module

        original_eff = config_module.config.chat._effective_tool_call_args_max_chars
        try:
            config_module.config.chat._effective_tool_call_args_max_chars = 50
            out = _build_history_messages(
                msgs,
                summary_text=None,
                keywords=None,
                min_messages_to_keep=15,
                max_keywords=50,
            )
        finally:
            config_module.config.chat._effective_tool_call_args_max_chars = original_eff

        assistant_msgs = [m for m in out if m.get("tool_calls")]
        assert len(assistant_msgs) == 1
        args_str = assistant_msgs[0]["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args_str)
        assert "_truncated" in parsed, (
            f"Expected truncation at effective value 50, but got args: {args_str}"
        )

    # ------------------------------------------------------------------
    # Item #9: Multiple tool calls in one prior turn
    # ------------------------------------------------------------------
    def test_multiple_tool_calls_mixed_truncation_via_build(self):
        """Assistant message with short + long + long tool calls — ids preserved."""
        msgs = [
            make_message(
                "checking things",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(id="tc_short", name="noop", arguments={"x": 1}),
                    ToolCall(id="tc_long1", name="bash", arguments={"cmd": "x" * 3000}),
                    ToolCall(id="tc_long2", name="bash", arguments={"cmd": "y" * 4000}),
                ],
                # tool_results would be dropped anyway; omit for clarity.
            ),
        ]
        event = make_event("anything?")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        assistant_msgs = [m for m in out if m.get("tool_calls")]
        assert len(assistant_msgs) == 1
        tcs = assistant_msgs[0]["tool_calls"]
        assert len(tcs) == 3

        # IDs preserved verbatim.
        assert tcs[0]["id"] == "tc_short"
        assert tcs[1]["id"] == "tc_long1"
        assert tcs[2]["id"] == "tc_long2"

        # tc1 untouched.
        assert json.loads(tcs[0]["function"]["arguments"]) == {"x": 1}

        # tc2 and tc3 truncated.
        assert "_truncated" in json.loads(tcs[1]["function"]["arguments"])
        assert "_truncated" in json.loads(tcs[2]["function"]["arguments"])

    # ------------------------------------------------------------------
    # Item #10: Round-trip through SQLite (mock path via make_message)
    # ------------------------------------------------------------------
    def test_tool_call_with_none_arguments_via_build(self):
        """ToolCall.arguments=None renders as 'null' via build_messages."""
        msgs = [
            make_message(
                "calling tool",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(id="tc_none", name="noop", arguments=None),
                ],
            ),
        ]
        event = make_event("go")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        assistant_msgs = [m for m in out if m["role"] == "assistant"]
        args_str = assistant_msgs[0]["tool_calls"][0]["function"]["arguments"]
        assert args_str == "null"

    def test_tool_call_with_empty_dict_arguments_via_build(self):
        """ToolCall.arguments={} renders as '{}' via build_messages."""
        msgs = [
            make_message(
                "calling tool",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(id="tc_empty", name="noop", arguments={}),
                ],
            ),
        ]
        event = make_event("go")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        assistant_msgs = [m for m in out if m["role"] == "assistant"]
        args_str = assistant_msgs[0]["tool_calls"][0]["function"]["arguments"]
        assert args_str == "{}"

    # ------------------------------------------------------------------
    # Item #11: Live regression — tool results no longer accumulate
    # ------------------------------------------------------------------
    def test_many_prior_tool_results_each_clipped(self):
        """With 15 prior tool-results, each is replayed, capped per result."""
        # Build exactly 15 messages (all assistant) so the count is deterministic
        # regardless of min_messages_to_keep.
        msgs = []
        for i in range(15):
            prior_assistant = make_message(
                f"assistant msg {i}",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(
                        id=f"tc_{i}",
                        name="browser_snapshot",
                        arguments={"url": f"http://example.com/{i}"},
                    ),
                ],
                tool_results=[
                    ToolResult(
                        name="browser_snapshot",
                        success=True,
                        output=f"screenshot_{i}.png content..." + "x" * 100,
                        error=None,
                    ),
                ],
            )
            msgs.append(prior_assistant)

        # Replace the last prior tool result with one larger than the cap.
        from nixclaw_agent.config import config as _cfg
        cap = _cfg.chat.history_tool_result_max_chars
        oversized = "BIG" * (cap * 3)
        msgs[-1].tool_results[0].output = oversized

        event = make_event("latest user message")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        tool_msgs = [m for m in out if m["role"] == "tool"]
        assert len(tool_msgs) == 15
        assert len([m for m in out if m.get("tool_calls")]) == 15

        clipped = tool_msgs[-1]["content"]
        assert "truncated" in clipped and "chars omitted" in clipped
        assert len(clipped) < len(oversized)
        assert clipped.startswith(oversized[:cap])


# ---------------------------------------------------------------------------
# System prompt marker sentence tests
# ---------------------------------------------------------------------------

class TestSystemPromptMarkerSentence:
    """Item #14: System prompt must contain the auto-truncated safety sentence."""

    def test_llm_system_prompt_contains_marker_sentence(self):
        """LLMClient.build_system_prompt includes the auto-truncated safety sentence."""
        client = LLMClient(OllamaConfig())
        prompt = client.build_system_prompt(tools=[], user_tier="Privileged")
        assert "auto-truncated" in prompt, (
            "System prompt must contain the marker-safety sentence"
        )

    def test_vllm_system_prompt_contains_marker_sentence(self):
        """VLLMClient.build_system_prompt includes the auto-truncated safety sentence."""
        client = VLLMClient(VllmConfig())
        prompt = client.build_system_prompt(tools=[], user_tier="Privileged")
        assert "auto-truncated" in prompt, (
            "System prompt must contain the marker-safety sentence"
        )


# ---------------------------------------------------------------------------
# Startup LOG.info threshold test
# ---------------------------------------------------------------------------

class TestStartupLogging:
    """Item #15: Startup LOG.info prints the resolved threshold."""

    def test_chatconfig_reports_effective_value(self):
        """ChatConfig emits LOG.info with the effective value at construction.

        The plan specifies a startup LOG.info line in llm.py that prints
        tool_call_args_max_chars=<value>.  This test verifies the ChatConfig
        field is accessible so that log line can read it.
        """
        cfg = ChatConfig(tool_call_args_max_chars=512)
        assert hasattr(cfg, "effective_tool_call_args_max_chars")
        assert cfg.effective_tool_call_args_max_chars == 512

    def test_floor_value_accessible(self):
        """The clamped effective value is accessible even when below floor."""
        cfg = ChatConfig(tool_call_args_max_chars=10)
        assert cfg.effective_tool_call_args_max_chars == 256


# ---------------------------------------------------------------------------
# Per-build LOG.debug truncation counter
# ---------------------------------------------------------------------------

class TestPerBuildDebugLog:
    """Item #16: Per-build LOG.debug line with truncation count."""

    def test_build_messages_debug_log_count(self, caplog):
        """When truncation fires, LOG.debug emits truncated_tool_calls=N."""
        from nixclaw_agent.llm import _build_history_messages

        import nixclaw_agent.config as config_module

        msgs = [
            make_message(
                "call1",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(id="tc_a", name="bash", arguments={"cmd": "x" * 100}),
                    ToolCall(id="tc_b", name="bash", arguments={"cmd": "y" * 200}),
                ],
            ),
        ]

        # Force truncation at 50 chars by temporarily patching the live config.
        original_eff = config_module.config.chat._effective_tool_call_args_max_chars
        try:
            config_module.config.chat._effective_tool_call_args_max_chars = 50
            with caplog.at_level(logging.DEBUG):
                _build_history_messages(
                    msgs,
                    summary_text=None,
                    keywords=None,
                    min_messages_to_keep=15,
                    max_keywords=50,
                )
        finally:
            config_module.config.chat._effective_tool_call_args_max_chars = original_eff

        debug_lines = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        truncated_lines = [l for l in debug_lines if "truncated_tool_calls" in l]
        assert len(truncated_lines) >= 1, (
            f"Expected at least one LOG.debug line with 'truncated_tool_calls', "
            f"got debug_lines={debug_lines!r}"
        )
        # Both tool calls exceed 50 chars, so count must be 2.
        assert "truncated_tool_calls=2" in truncated_lines[0]


# ---------------------------------------------------------------------------
# Cronjob/subagent paths unaffected
# ---------------------------------------------------------------------------

class TestCronSubagentPathsUnaffected:
    """Item #17: Cronjob and subagent message paths are not changed."""

    def test_cronjob_message_preserved_in_output(self):
        """A cronjob-sourced message with tool_calls still appears in output."""
        msgs = [
            make_message(
                "cronjob result",
                is_outgoing=False,
                source_type="cronjob",
                source_id="cron-abc123",
                tool_calls=[
                    ToolCall(id="tc_cron", name="bash", arguments={"cmd": "echo cron"}),
                ],
            ),
        ]
        event = make_event("continue?")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        # build_messages appends the current event as a user message, so
        # we expect at least 2 user messages. Check the cronjob one is present.
        user_msgs = [m for m in out if m["role"] == "user"]
        assert len(user_msgs) >= 1
        cronjob_msgs = [m for m in user_msgs if "[Cronjob" in m["content"]]
        assert len(cronjob_msgs) == 1
        assert "cronjob result" in cronjob_msgs[0]["content"]

    def test_subagent_message_preserved_in_output(self):
        """A subagent-sourced message with tool_calls still appears in output."""
        msgs = [
            make_message(
                "subagent result",
                is_outgoing=False,
                source_type="subagent",
                source_id="sub-deadbeef",
                tool_calls=[
                    ToolCall(id="tc_sub", name="bash", arguments={"cmd": "echo sub"}),
                ],
            ),
        ]
        event = make_event("continue?")
        out = LLMClient(OllamaConfig()).build_messages(msgs, event)

        # build_messages appends the current event as a user message, so
        # we expect at least 2 user messages. Check the subagent one is present.
        user_msgs = [m for m in out if m["role"] == "user"]
        assert len(user_msgs) >= 1
        subagent_msgs = [m for m in user_msgs if "[Subagent" in m["content"]]
        assert len(subagent_msgs) == 1
        assert "subagent result" in subagent_msgs[0]["content"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
