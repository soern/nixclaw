"""Tests for the build_messages refactor.

These tests assert parity between the pre-refactor and post-refactor
output of LLMClient.build_messages and VLLMClient.build_messages.

Architecture:
- No SQLite round-trip; Message/Event/Attachment built directly from
  dataclasses in src.nixclaw_agent.types.
- No mocks; the helper accepts min_messages_to_keep and max_keywords
  as parameters, so tests pass small literals.
- Fixture set was captured by running the pre-refactor code and
  embedded below as Python literals, except the attachment fixtures
  (7-9): attachments are no longer inlined as base64 images but
  announced by filename, identically on both backends.
"""

import base64
import inspect
import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nixclaw_agent.config import OllamaConfig, VllmConfig
from nixclaw_agent.llm import LLMClient, VLLMClient, _build_history_messages, with_attachment_note
from nixclaw_agent.types import (
    Attachment,
    Event,
    Message,
    ToolCall,
    ToolResult,
)


# 1x1 transparent PNG, hardcoded so the test is self-contained
_PNG_1x1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


# --- fixture helpers --------------------------------------------------------


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


# Recorded pre-refactor fixtures (captured by running the pre-refactor
# code with local attachments where applicable). Item 4 (image prompt)
# uses the post-typo-fix string for vLLM.
LLM_FIXTURES: dict[str, list[dict]] = {
    "1_no_summary_no_keywords": [
        {"role": "user", "content": "hi there"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what's up?"},
    ],
    "2_summary_only": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "anything else?"},
    ],
    "3_keywords_only": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "a question"},
    ],
    "4_both": [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "question"},
    ],
    "5_cronjob": [
        {"role": "user", "content": "[Cronjob abcdefgh...] cronjob content"},
        {"role": "user", "content": "now"},
    ],
    # Arguments now JSON-serialized strings.
    "6_assistant_tool_calls_results": [
        {"role": "user", "content": "what is the weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc_1",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city":"Berlin"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc_1", "content": "20C"},
        {"role": "assistant", "content": "checking weather"},
        {"role": "user", "content": "20C in Berlin"},
        {"role": "user", "content": "thanks"},
    ],
    # Images are never inlined; announced by filename like any attachment.
    "7_image_attachment_placeholder": [
        {"role": "user", "content": "[Attachment received: photo.png]"}
    ],
    "8_image_attachment_user_text": [
        {"role": "user", "content": "describe this\n\n[Attachment received: photo.png]"}
    ],
    "9_non_image_attachment": [
        {"role": "user", "content": "see attached\n\n[Attachment received: doc.pdf]"}
    ],
    "10_empty_context": [
        {"role": "user", "content": "hello"},
    ],
    "11_truncation_over_cap": [
        {"role": "user", "content": f"msg{n}"}
        if n % 2 == 1
        else {"role": "assistant", "content": f"msg{n}"}
        for n in range(15, 30)
    ] + [{"role": "user", "content": "now"}],
    "12_subagent": [
        {"role": "user", "content": "[Subagent subagent...] subagent content"},
        {"role": "user", "content": "hi"},
    ],
    "2_with_summary": [
        {"role": "system", "content": "CONVERSATION SUMMARY:\nprior summary"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "anything else?"},
    ],
    "3_with_keywords": [
        {
            "role": "system",
            "content": "in earlier conversations we talked about: alpha, beta",
        },
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "a question"},
    ],
    "4_with_both": [
        {
            "role": "system",
            "content": "in earlier conversations we talked about: alpha, beta",
        },
        {"role": "system", "content": "CONVERSATION SUMMARY:\nprior summary"},
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "question"},
    ],
}


VLLM_FIXTURES: dict[str, list[dict]] = {
    k: v for k, v in LLM_FIXTURES.items()
}


def _make_input_for(key: str, tmp_path: Path):
    """Build the (context_messages, event) pair for a given fixture key."""
    img_path = tmp_path / "photo.png"
    img_path.write_bytes(base64.b64decode(_PNG_1x1_BASE64))
    img_att = Attachment(
        id="att_1",
        content_type="image/png",
        filename="photo.png",
        local_path=str(img_path),
        size=100,
    )
    pdf_att = Attachment(
        id="att_2",
        content_type="application/pdf",
        filename="doc.pdf",
        local_path=str(tmp_path / "doc.pdf"),
        size=200,
    )

    if key in ("1_no_summary_no_keywords",):
        msgs = [
            make_message("hi there", is_outgoing=False),
            make_message("hello", is_outgoing=True),
        ]
        event = make_event("what's up?")
        return msgs, event

    if key in ("2_summary_only", "3_keywords_only", "2_with_summary", "3_with_keywords"):
        msgs = [
            make_message("hi", is_outgoing=False),
            make_message("hello", is_outgoing=True),
        ]
        if key.startswith("2"):
            event = make_event("anything else?")
        elif key.startswith("3"):
            event = make_event("a question")
        else:
            raise ValueError(key)
        return msgs, event

    if key == "4_both":
        msgs = [make_message("hi", is_outgoing=False)]
        event = make_event("question")
        return msgs, event

    if key == "4_with_both":
        msgs = [make_message("hi", is_outgoing=False)]
        event = make_event("question")
        return msgs, event

    if key == "5_cronjob":
        msgs = [
            make_message(
                "cronjob content",
                is_outgoing=False,
                source_type="cronjob",
                source_id="abcdefgh-1234-5678-9012-abcdefghijkl",
            )
        ]
        event = make_event("now")
        return msgs, event

    if key == "6_assistant_tool_calls_results":
        msgs = [
            make_message("what is the weather?", is_outgoing=False),
            make_message(
                "checking weather",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(
                        id="tc_1",
                        name="get_weather",
                        arguments={"city": "Berlin"},
                    )
                ],
                tool_results=[
                    ToolResult(name="get_weather", success=True, output="20C", error=None)
                ],
            ),
            make_message("20C in Berlin", is_outgoing=False),
        ]
        event = make_event("thanks")
        return msgs, event

    # The note is added in AgentLoop._preprocess_incoming; build_messages
    # passes event.message through unchanged.
    if key == "7_image_attachment_placeholder":
        return [], make_event(with_attachment_note("[Attachment]", ["photo.png"]), attachments=[img_att])

    if key == "8_image_attachment_user_text":
        return [], make_event(with_attachment_note("describe this", ["photo.png"]), attachments=[img_att])

    if key == "9_non_image_attachment":
        return [], make_event(with_attachment_note("see attached", ["doc.pdf"]), attachments=[pdf_att])

    if key == "10_empty_context":
        return [], make_event("hello")

    if key == "11_truncation_over_cap":
        msgs = [make_message(f"msg{i}", is_outgoing=(i % 2 == 0)) for i in range(30)]
        event = make_event("now")
        return msgs, event

    if key == "12_subagent":
        msgs = [
            make_message(
                "subagent content",
                is_outgoing=False,
                source_type="subagent",
                source_id="subagent-deadbeef-1234-5678-9012-abcdef012345",
            )
        ]
        event = make_event("hi")
        return msgs, event

    raise ValueError(f"Unknown fixture key: {key}")


def _summary_keywords_for(key: str) -> tuple[Optional[str], Optional[list[str]]]:
    if key == "2_with_summary":
        return "prior summary", None
    if key == "3_with_keywords":
        return None, ["alpha", "beta"]
    if key == "4_with_both":
        return "prior summary", ["alpha", "beta"]
    return None, None


# --- tests ------------------------------------------------------------------


def _llm():
    return LLMClient(OllamaConfig())


def _vllm():
    return VLLMClient(VllmConfig())


class TestParityLLMClient:
    """Item #1: LLMClient.build_messages produces pre-refactor output."""

    @pytest.mark.parametrize("key", sorted(LLM_FIXTURES.keys()))
    def test_matches_recorded_fixture(self, key, tmp_path):
        msgs, event = _make_input_for(key, tmp_path)
        summary_text, keywords = _summary_keywords_for(key)
        out = _llm().build_messages(msgs, event, summary_text, keywords)
        assert out == LLM_FIXTURES[key], (
            f"LLMClient output drifted for key={key!r}"
        )


class TestParityVLLMClient:
    """Item #2: VLLMClient.build_messages produces pre-refactor output.

    Image-attachment fixture records the post-typo-fix string.
    """

    @pytest.mark.parametrize("key", sorted(VLLM_FIXTURES.keys()))
    def test_matches_recorded_fixture(self, key, tmp_path):
        msgs, event = _make_input_for(key, tmp_path)
        summary_text, keywords = _summary_keywords_for(key)
        out = _vllm().build_messages(msgs, event, summary_text, keywords)
        assert out == VLLM_FIXTURES[key], (
            f"VLLMClient output drifted for key={key!r}"
        )


class TestPerMessageLoop:
    """Item #3: per-message loop cases."""

    def test_empty_context_messages_only_current_user(self):
        event = make_event("hi")
        out = _build_history_messages(
            context_messages=[],
            summary_text=None,
            keywords=None,
            min_messages_to_keep=15,
            max_keywords=50,
        )
        # The helper itself only handles history; current user turn is
        # added by build_messages. For this test we exercise the helper
        # in isolation.
        assert out == []
        # And via the full build_messages path:
        full = _llm().build_messages([], event)
        assert full == [{"role": "user", "content": "hi"}]

    def test_one_user_one_assistant(self):
        msgs = [
            make_message("user1", is_outgoing=False),
            make_message("assistant1", is_outgoing=True),
        ]
        event = make_event("user2")
        out = _llm().build_messages(msgs, event)
        assert out == [
            {"role": "user", "content": "user1"},
            {"role": "assistant", "content": "assistant1"},
            {"role": "user", "content": "user2"},
        ]

    def test_assistant_tool_calls_with_two_results_fallback_tc_id(self):
        """Results are zipped with calls; a surplus result is ignored."""
        msgs = [
            make_message(
                "calling tools",
                is_outgoing=True,
                tool_calls=[
                    ToolCall(id="tc_1", name="tool_a", arguments={}),
                ],
                tool_results=[
                    ToolResult(name="tool_a", success=True, output="result_1", error=None),
                    ToolResult(name="tool_a", success=False, output=None, error="boom"),
                ],
            ),
        ]
        event = make_event("next")
        out = _llm().build_messages(msgs, event)
        assert out == [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc_1", "function": {"name": "tool_a", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "result_1"},
            {"role": "assistant", "content": "calling tools"},
            {"role": "user", "content": "next"},
        ]

    def test_cronjob_prefix(self):
        msgs = [
            make_message(
                "cron payload",
                is_outgoing=False,
                source_type="cronjob",
                source_id="abcdefgh-1234",
            )
        ]
        event = make_event("now")
        out = _llm().build_messages(msgs, event)
        assert out == [
            {"role": "user", "content": "[Cronjob abcdefgh...] cron payload"},
            {"role": "user", "content": "now"},
        ]

    def test_subagent_prefix(self):
        msgs = [
            make_message(
                "sub payload",
                is_outgoing=False,
                source_type="subagent",
                source_id="subagent-deadbeef-1234",
            )
        ]
        event = make_event("hi")
        out = _llm().build_messages(msgs, event)
        assert out == [
            {"role": "user", "content": "[Subagent subagent...] sub payload"},
            {"role": "user", "content": "hi"},
        ]


def _image_att(tmp_path, name="photo.png"):
    img_path = tmp_path / name
    img_path.write_bytes(base64.b64decode(_PNG_1x1_BASE64))
    return Attachment(
        id="att_1", content_type="image/png",
        filename=name, local_path=str(img_path), size=100,
    )


@pytest.fixture(params=["llm", "vllm"])
def any_client(request):
    return _llm() if request.param == "llm" else _vllm()


class TestImagesNeverInlined:
    """Images on the current turn are never inlined as base64 (an inline
    image crashed the shared SGLang backend); event.message, which already
    carries the attachment note, is passed through unchanged."""

    def test_message_passed_through(self, any_client, tmp_path):
        msg = "describe this\n\n[Attachment received: photo.png]"
        out = any_client.build_messages([], make_event(msg, attachments=[_image_att(tmp_path)]))
        assert out == [{"role": "user", "content": msg}]
        assert not any("images" in m for m in out)

    def test_note_not_added_twice(self, any_client, tmp_path):
        out = any_client.build_messages([], make_event("hi", attachments=[_image_att(tmp_path)]))
        assert out == [{"role": "user", "content": "hi"}]


class TestWithAttachmentNote:
    def test_placeholder_replaced(self):
        assert with_attachment_note("[Attachment]", ["photo.png"]) == "[Attachment received: photo.png]"

    def test_empty_message(self):
        assert with_attachment_note("", ["photo.png"]) == "[Attachment received: photo.png]"

    def test_user_text_kept(self):
        assert (with_attachment_note("describe this", ["photo.png"])
                == "describe this\n\n[Attachment received: photo.png]")

    def test_multiple_attachments(self):
        assert (with_attachment_note("both", ["photo.png", "doc.pdf"])
                == "both\n\n[Attachment received: photo.png, doc.pdf]")

    def test_no_filenames(self):
        assert with_attachment_note("hi", []) == "hi"


def _tool_row(text: str, results: list[ToolResult], calls: Optional[list[ToolCall]] = None) -> Message:
    calls = calls or [ToolCall(id="tc1", name="get_weather", arguments={"city": "Berlin"})]
    return make_message(text, is_outgoing=True, tool_calls=calls, tool_results=results)


@pytest.mark.parametrize("make_client", [_llm, _vllm], ids=["ollama", "vllm"])
class TestToolCallPairsReplayed:
    """Stored agent rows with tool calls are replayed as real
    assistant(tool_calls) -> tool result -> assistant(text) sequences, so no
    marker text exists in history for the model to echo as its reply. See
    llm.py's _build_history_messages."""

    def test_calls_results_and_text(self, make_client):
        msgs = [_tool_row("it is sunny", [ToolResult(name="get_weather", success=True, output="sunny", error=None)])]
        out = make_client().build_messages(msgs, make_event("thanks"))
        assert out[0] == {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "function": {"name": "get_weather", "arguments": '{"city":"Berlin"}'}}
            ],
        }
        assert out[1] == {"role": "tool", "tool_call_id": "tc1", "content": "sunny"}
        assert out[2] == {"role": "assistant", "content": "it is sunny"}

    def test_empty_or_whitespace_text_emits_no_text_message(self, make_client):
        for text in ("", "\n  "):
            msgs = [_tool_row(text, [ToolResult(name="get_weather", success=True, output="sunny", error=None)])]
            out = make_client().build_messages(msgs, make_event("next"))
            assert [m["role"] for m in out] == ["assistant", "tool", "user"]
            assert "Used tool" not in str(out)

    def test_multiple_calls_paired_in_order(self, make_client):
        calls = [
            ToolCall(id="a", name="t1", arguments={}),
            ToolCall(id="b", name="t2", arguments={}),
        ]
        results = [
            ToolResult(name="t1", success=True, output="r1", error=None),
            ToolResult(name="t2", success=True, output="r2", error=None),
        ]
        out = make_client().build_messages([_tool_row("done", results, calls)], make_event("x"))
        assert [(m["role"], m.get("tool_call_id")) for m in out[:4]] == [
            ("assistant", None), ("tool", "a"), ("tool", "b"), ("assistant", None),
        ]
        assert [m["content"] for m in out[1:3]] == ["r1", "r2"]

    def test_result_clipped_to_history_cap(self, make_client, monkeypatch):
        from nixclaw_agent.config import config as config_obj
        monkeypatch.setattr(config_obj.chat, "history_tool_result_max_chars", 50)
        msgs = [_tool_row("", [ToolResult(name="get_weather", success=True, output="x" * 500, error=None)])]
        out = make_client().build_messages(msgs, make_event("next"))
        assert out[1]["content"].startswith("x" * 50 + "\n...[truncated, 450 of 500 chars omitted]")

    def test_failed_and_none_output_rendering(self, make_client):
        results = [ToolResult(name="get_weather", success=False, output=None, error="boom")]
        out = make_client().build_messages([_tool_row("", results)], make_event("next"))
        assert out[1]["content"] == "Error: boom"
        results = [ToolResult(name="get_weather", success=True, output=None, error=None)]
        out = make_client().build_messages([_tool_row("", results)], make_event("next"))
        assert out[1]["content"] == ""

    def test_empty_call_id_gets_matching_synthetic_id(self, make_client):
        calls = [ToolCall(id="", name="get_weather", arguments={})]
        results = [ToolResult(name="get_weather", success=True, output="ok", error=None)]
        out = make_client().build_messages([_tool_row("", results, calls)], make_event("next"))
        assert out[0]["tool_calls"][0]["id"] == "call_0"
        assert out[1]["tool_call_id"] == "call_0"


class TestKeywordAndSummaryEdges:
    """Item #8: keywords=None, summary_text=None → no prepending."""

    def test_neither_keyword_nor_summary(self):
        out = _build_history_messages(
            context_messages=[make_message("hi", is_outgoing=False)],
            summary_text=None,
            keywords=None,
            min_messages_to_keep=15,
            max_keywords=50,
        )
        assert out == [{"role": "user", "content": "hi"}]

    def test_keywords_with_max_keywords_zero_suppressed(self):
        """Item #9: keywords provided but max_keywords=0 → suppressed."""
        out = _build_history_messages(
            context_messages=[make_message("hi", is_outgoing=False)],
            summary_text=None,
            keywords=["alpha", "beta"],
            min_messages_to_keep=15,
            max_keywords=0,
        )
        assert out == [{"role": "user", "content": "hi"}]


class TestTruncation:
    """Items #10 and #11: min_messages_to_keep truncation."""

    def test_over_cap_truncates_to_last_n(self):
        msgs = [make_message(f"msg{i}", is_outgoing=(i % 2 == 0)) for i in range(30)]
        out = _build_history_messages(
            context_messages=msgs,
            summary_text=None,
            keywords=None,
            min_messages_to_keep=15,
            max_keywords=50,
        )
        assert len(out) == 15
        # First emitted message is msg15
        assert out[0]["content"] == "msg15"
        # Last emitted message is msg29
        assert out[-1]["content"] == "msg29"

    def test_under_cap_returns_all(self):
        msgs = [make_message(f"msg{i}", is_outgoing=(i % 2 == 0)) for i in range(3)]
        out = _build_history_messages(
            context_messages=msgs,
            summary_text=None,
            keywords=None,
            min_messages_to_keep=15,
            max_keywords=50,
        )
        assert len(out) == 3
        assert [m["content"] for m in out] == ["msg0", "msg1", "msg2"]


class TestClassStructure:
    """Item #12: VLLMClient does not inherit from LLMClient."""

    def test_vllm_does_not_inherit_from_llm(self):
        assert not issubclass(VLLMClient, LLMClient)

    def test_vllm_instance_is_not_llm_instance(self):
        v = _vllm()
        assert not isinstance(v, LLMClient)

    def test_build_messages_are_independent(self):
        """The two methods are not overrides of each other.

        Walks the full MRO (excluding the classes themselves) so the
        test fails if a future change introduces a shared parent
        that defines `build_messages`.
        """
        assert "build_messages" in LLMClient.__dict__
        assert "build_messages" in VLLMClient.__dict__
        # No ancestor of either class may define `build_messages`;
        # otherwise the local `build_messages` could silently become
        # an override rather than a fresh definition.
        for cls in LLMClient.__mro__ + VLLMClient.__mro__:
            if cls is object or cls in (LLMClient, VLLMClient):
                continue
            assert "build_messages" not in cls.__dict__, (
                f"{cls.__name__} defines build_messages; "
                f"{LLMClient.__name__} and {VLLMClient.__name__} "
                "must remain independent top-level classes."
            )

    def test_top_level_class_definitions(self):
        """Both classes are top-level (no parent class declared)."""
        # Both classes' bases must be `object` (i.e., no parent declared).
        assert LLMClient.__bases__ == (object,)
        assert VLLMClient.__bases__ == (object,)


class TestHelperIsSelfFree:
    """Item #13: _build_history_messages does not reference self."""

    def test_helper_source_does_not_reference_self(self):
        src = inspect.getsource(_build_history_messages)
        # No `self.` references; helper is module-level pure function.
        assert "self." not in src
        assert "self," not in src
        assert "self)" not in src

    def test_helper_signature_takes_no_self(self):
        sig = inspect.signature(_build_history_messages)
        assert "self" not in sig.parameters
        # Required positional/keyword params per the plan.
        expected = {
            "context_messages",
            "summary_text",
            "keywords",
            "min_messages_to_keep",
            "max_keywords",
        }
        assert expected.issubset(set(sig.parameters.keys()))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])