"""Unit tests for tool_markup.py — leaked tool-call markup detection + guard."""

from pathlib import Path

import pytest

from src.nixclaw_agent.tool_markup import (
    MAX_MARKUP_RETRIES,
    REASONING_LEAK_RE,
    TOOL_MARKUP_RE,
    chat_with_markup_guard,
    contains_reasoning_leak,
    contains_tool_markup,
)
from src.nixclaw_agent import prompts
from src.nixclaw_agent.types import ToolCall

_DEFAULTS = prompts.load_prompts(prompts.DEFAULT_DIR, Path("/nonexistent"))
CORRECTIVE_MESSAGE = _DEFAULTS.text("markup-corrective")
REASONING_CORRECTIVE_MESSAGE = _DEFAULTS.text("markup-reasoning-corrective")


class FakeLLMClient:
    """Hand-rolled async LLM stub.

    `.chat(messages, tools, channel_id)` pops the next scripted (text,
    tool_calls) tuple and records a shallow copy of the `messages` argument so
    tests can assert on re-prompt content and count calls.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # each entry: the messages list passed to chat()

    async def chat(self, messages, tools, channel_id):
        # Record the actual object AND a snapshot of its length/content so we
        # can both inspect what was sent and detect caller-list mutation.
        self.calls.append(list(messages))
        return self._responses.pop(0)


# --------------------------------------------------------------------------
# 1. Regex recall — real leak payloads must be detected
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        # full Qwen block
        "<tool_call>\n<function=web_search>\n<parameter=query>\n"
        "Schulferien\n</parameter>\n</function>\n</tool_call>",
        # unterminated (no closing </tool_call>)
        "<tool_call>\n<function=bash>\n<parameter=command>\nls",
        # <bash>-style malformed, detection rides on <tool_call> fragment
        "<tool_call>\n<bash>ls</bash>",
        # other template families
        "[TOOL_CALLS]",
        "<|tool_call|>",
        "<|python_tag|>",
        # bare function-open tag
        "<function=foo>",
        # LFM2/LFM2.5 (observed via llama.cpp falling through to plain text)
        "<|tool_call_start|>[message_search(query='x', limit=1)]<|tool_call_end|>",
        "<|tool_call_start|>",
        "<|tool_call_end|>",
    ],
)
def test_regex_recall_true(payload):
    assert contains_tool_markup(payload) is True
    assert TOOL_MARKUP_RE.search(payload) is not None


# --------------------------------------------------------------------------
# 2. Regex precision — no false positives on ordinary content
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        "Sure, here is the answer you asked for.",
        "Here is code:\n```html\n<html><a href=\"x\">link</a></div>\n```",
        "Reach me at <a@b.com> anytime.",
        "The invariant a < b and c > d must hold.",
        '{"should_remember": false}',
    ],
)
def test_regex_precision_false(payload):
    assert contains_tool_markup(payload) is False


def test_empty_string_is_false():
    assert contains_tool_markup("") is False


# --------------------------------------------------------------------------
# 2b. Reasoning-leak regex — recall and precision
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        "[Assistant's reasoning omitted]",
        "[Assistants reasoning omitted]",
        "[assistant's REASONING omitted]",
        "[reasoning omitted]",
        "<think>\nplanning the answer\n</think>\nHere it is.",
        "Some text</think>trailing",
        "<thinking>still going</thinking>",
    ],
)
def test_reasoning_leak_recall_true(payload):
    assert contains_reasoning_leak(payload) is True
    assert REASONING_LEAK_RE.search(payload) is not None


@pytest.mark.parametrize(
    "payload",
    [
        "Sure, here is the answer you asked for.",
        "I think this approach works well.",
        "Let me reason about this for a second.",
        '{"should_remember": false}',
    ],
)
def test_reasoning_leak_precision_false(payload):
    assert contains_reasoning_leak(payload) is False


def test_reasoning_leak_empty_string_is_false():
    assert contains_reasoning_leak("") is False


# --------------------------------------------------------------------------
# 3. Guard — clean passthrough (zero overhead)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_clean_passthrough():
    fake = FakeLLMClient([("hello", [])])
    messages = [{"role": "user", "content": "hi"}]
    before_len = len(messages)

    text, tool_calls = await chat_with_markup_guard(
        fake, messages, tools=[], channel_id="c1", source="+000000"
    )

    assert text == "hello"
    assert tool_calls == []
    assert len(fake.calls) == 1
    assert len(messages) == before_len  # list untouched


# --------------------------------------------------------------------------
# 4. Guard — leak then clean
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_leak_then_clean():
    leak = "<tool_call>\n<function=web_search>\n<parameter=query>\nx\n</parameter>"
    fake = FakeLLMClient([(leak, []), ("all good", [])])
    messages = [{"role": "user", "content": "hi"}]

    text, tool_calls = await chat_with_markup_guard(
        fake, messages, tools=[], channel_id="c1", source="+000000"
    )

    assert text == "all good"
    assert tool_calls == []
    assert len(fake.calls) == 2

    # 2nd call's messages must include the corrective user message AND the prior
    # assistant leak text (so the model sees what it produced wrong).
    second_call_msgs = fake.calls[1]
    contents = [m.get("content") for m in second_call_msgs]
    assert CORRECTIVE_MESSAGE in contents
    assert leak in contents


# --------------------------------------------------------------------------
# 4b. Guard — reasoning leak then clean
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_reasoning_leak_then_clean():
    leak = "[Assistant's reasoning omitted]"
    fake = FakeLLMClient([(leak, []), ("all good", [])])
    messages = [{"role": "user", "content": "hi"}]

    text, tool_calls = await chat_with_markup_guard(
        fake, messages, tools=[], channel_id="c1", source="+000000"
    )

    assert text == "all good"
    assert tool_calls == []
    assert len(fake.calls) == 2

    second_call_msgs = fake.calls[1]
    contents = [m.get("content") for m in second_call_msgs]
    assert REASONING_CORRECTIVE_MESSAGE in contents
    assert leak in contents


# --------------------------------------------------------------------------
# 5. Guard — exhaustion returns ("", [])
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_exhaustion():
    leak = "<tool_call>\n<function=bash>\n<parameter=command>\nls"
    # 1 initial + MAX_MARKUP_RETRIES retries, all leaking
    fake = FakeLLMClient([(leak, [])] * (MAX_MARKUP_RETRIES + 1))
    messages = [{"role": "user", "content": "hi"}]

    text, tool_calls = await chat_with_markup_guard(
        fake, messages, tools=[], channel_id="c1", source="+000000"
    )

    assert text == ""
    assert tool_calls == []
    assert len(fake.calls) == MAX_MARKUP_RETRIES + 1  # == 4


# --------------------------------------------------------------------------
# 6. Guard — caller's list is never mutated
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_does_not_mutate_caller_list():
    leak = "<tool_call>\nleak"
    fake = FakeLLMClient([(leak, []), ("done", [])])
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    original_len = len(messages)

    await chat_with_markup_guard(
        fake, messages, tools=[], channel_id="c1", source="+000000"
    )

    assert len(messages) == original_len
    assert all(m.get("content") != CORRECTIVE_MESSAGE for m in messages)


# --------------------------------------------------------------------------
# 7. Guard — all-or-nothing: leaked completion's tool_calls are discarded
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guard_all_or_nothing_discards_tool_calls():
    leaked_tc = ToolCall(id="1", name="bash", arguments={"command": "ls"})
    leak = "<tool_call>leak"
    fake = FakeLLMClient([(leak, [leaked_tc]), ("done", [])])
    messages = [{"role": "user", "content": "hi"}]

    text, tool_calls = await chat_with_markup_guard(
        fake, messages, tools=[], channel_id="c1", source="+000000"
    )

    # The parsed-but-discarded tool_calls from the leaking completion must never
    # surface; the guard retries and returns the clean second completion.
    assert text == "done"
    assert tool_calls == []
    assert leaked_tc not in tool_calls
    assert len(fake.calls) == 2
