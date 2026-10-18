"""Detect and suppress leaked LLM internal markup in plain-text output.

The vLLM backend occasionally emits internal markup as plain text in the
message content instead of keeping it out of the user-visible response —
either tool-call markup (Qwen-XML and other template families, typically
when the markup is malformed and the server-side parser falls through), or
reasoning-related leakage (raw <think>/<thinking> tags, or the model
narrating about its own hidden reasoning instead of answering — e.g.
"[Assistant's reasoning omitted]" — observed after switching to backends
that split reasoning_content from content via --reasoning-parser). This
module detects both classes and wraps chat() so a leaking completion is
discarded whole (all-or-nothing) and the model is re-prompted, rather than
forwarding the leaked internals to the user.
"""
import re
from typing import Optional

from . import prompts
from .debug_log import safe_get_debug_logger
from .types import ToolCall

MAX_MARKUP_RETRIES = 3

# Qwen-XML family (observed in incidents) plus common other template families.
TOOL_MARKUP_RE = re.compile(
    r"(</?tool_call>"
    r"|<function=[^>\n]*>?"        # tolerate truncated/unclosed variants
    r"|</function>"
    r"|<parameter=[^>\n]*>?"
    r"|</parameter>"
    r"|\[TOOL_CALLS\]"             # Mistral
    r"|<\|tool_call\|>"           # misc chat templates
    r"|<\|tool_call_start\|>"     # LFM2/LFM2.5 (llama.cpp fell through to plain text)
    r"|<\|tool_call_end\|>"
    r"|<\|python_tag\|>)"        # Llama 3
)

# Raw thinking-block tags that should have been stripped server-side by the
# reasoning parser, plus placeholder text some reasoning models emit when
# narrating about hidden reasoning instead of producing a real answer.
REASONING_LEAK_RE = re.compile(
    r"(</?think>"
    r"|</?thinking>"
    r"|\[Assistant.{0,2}s?\s+reasoning\s+omitted\]"
    r"|\[reasoning\s+omitted\])",
    re.IGNORECASE,
)

def contains_tool_markup(text: str) -> bool:
    """True if the text contains any recognizable tool-call markup fragment."""
    if not text:
        return False
    return TOOL_MARKUP_RE.search(text) is not None


def contains_reasoning_leak(text: str) -> bool:
    """True if the text contains leaked raw reasoning tags or placeholder text."""
    if not text:
        return False
    return REASONING_LEAK_RE.search(text) is not None


async def chat_with_markup_guard(
    llm_client,
    messages: list[dict],
    tools: list,
    channel_id: Optional[str],
    source: str,
    thinking: Optional[bool] = None,
) -> tuple[str, list[ToolCall]]:
    """Wrap llm_client.chat(); discard whole completions that leak internal markup.

    Normal path (no leak) is a zero-overhead passthrough. If tool-call markup or
    a reasoning leak is detected in the response text, the ENTIRE completion is
    discarded (text and any parsed tool_calls — all-or-nothing) and the model is
    re-prompted with a corrective instruction, up to MAX_MARKUP_RETRIES times.
    Every failed attempt is logged. If all retries are exhausted, returns ("", [])
    so the caller sends nothing.
    """
    # thinking only passed when set: test fakes implement chat(messages, tools, channel_id)
    extra = {"thinking": thinking} if thinking is not None else {}
    response_text, tool_calls = await llm_client.chat(messages, tools, channel_id, **extra)
    is_markup = contains_tool_markup(response_text)
    is_reasoning_leak = not is_markup and contains_reasoning_leak(response_text)
    if not is_markup and not is_reasoning_leak:
        return response_text, tool_calls

    debug_logger = safe_get_debug_logger()
    for attempt in range(1, MAX_MARKUP_RETRIES + 1):
        event_name = "leaked_tool_markup" if is_markup else "leaked_reasoning"
        corrective = prompts.current().text("markup-corrective" if is_markup else "markup-reasoning-corrective")
        # log() before record_error() so the structured event is buffered before
        # record_error triggers a buffer flush in auto mode.
        if debug_logger is not None:
            debug_logger.log(
                event_name,
                channel_id or "",
                {"attempt": attempt, "source": source, "response_text": response_text[:500]},
            )
            debug_logger.record_error(f"{event_name}:{source}:{channel_id}")
        # Build the corrective retry on a COPY of messages — never mutate the
        # caller's shared list (it is reused for the caller's next chat()).
        attempt_messages = list(messages) + [
            {"role": "assistant", "content": response_text},
            {"role": "user", "content": corrective},
        ]
        response_text, tool_calls = await llm_client.chat(attempt_messages, tools, channel_id, **extra)
        is_markup = contains_tool_markup(response_text)
        is_reasoning_leak = not is_markup and contains_reasoning_leak(response_text)
        if not is_markup and not is_reasoning_leak:
            return response_text, tool_calls

    # All retries exhausted — discard so nothing is sent.
    return "", []
