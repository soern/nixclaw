import asyncio
import base64
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
import ollama

from . import prompts
from .config import OllamaConfig, VllmConfig
from .debug_log import safe_get_debug_logger
from .skills import load_skills_manifest, load_user_skills_manifest
from .tools.mute import is_channel_muted
from .types import Event, Message, Tool, ToolCall, ToolResult

LOG = logging.getLogger(__name__)


class ContextLengthExceededError(Exception):
    """Raised when LLM context window is exceeded."""
    pass


class StreamStallError(Exception):
    """Raised when a streaming vLLM response goes idle (no SSE chunk) past the
    configured watchdog timeout. Retried by VLLMClient.chat; not raised to callers
    unless retries are exhausted."""
    pass


class VLLMConcurrencyLimitError(Exception):
    """Raised when a request waits too long for a vLLM concurrency slot."""
    pass


class ModelNotFoundError(Exception):
    """Raised when the vLLM backend rejects the configured/cached model id
    (e.g. the backend was restarted serving a different model). Retried by
    VLLMClient.chat via re-probing /v1/models; not raised to callers unless
    the model is pinned or re-probing also fails."""
    pass


class ImageProcessingError(Exception):
    """Raised when the vLLM backend's multimodal processor rejects an image
    in the request (e.g. a resolution the currently-served model's vision
    processor can't handle). Retried by VLLMClient.chat by downscaling the
    offending image(s) and resending; not raised to callers unless the
    shrink ladder is exhausted or the request had no images to shrink."""
    pass


# Module-level (not a VLLMClient instance attribute) so the concurrency cap
# is shared across every VLLMClient in the process. There are two separate
# instantiation sites - the long-lived client built once in main.py and a
# fresh one created per call inside tools/files.py's analyze_image - and a
# per-instance semaphore would give the second site its own full-capacity
# semaphore every call, bypassing the cap entirely.
_shared_vllm_semaphore: Optional[asyncio.Semaphore] = None
_shared_vllm_semaphore_size: Optional[int] = None


def _get_shared_vllm_semaphore(max_concurrent: int) -> asyncio.Semaphore:
    """Return the process-wide vLLM concurrency semaphore, creating it on
    first use. Later VLLMClient instantiations with a different
    `max_concurrent_requests` do not resize the semaphore - only warn -
    since resizing an in-use asyncio.Semaphore isn't safe to do at runtime."""
    global _shared_vllm_semaphore, _shared_vllm_semaphore_size
    if _shared_vllm_semaphore is None:
        _shared_vllm_semaphore = asyncio.Semaphore(max_concurrent)
        _shared_vllm_semaphore_size = max_concurrent
    elif _shared_vllm_semaphore_size != max_concurrent:
        LOG.warning(
            f"vLLM concurrency semaphore already created with size "
            f"{_shared_vllm_semaphore_size}; ignoring different size "
            f"{max_concurrent} from a later VLLMClient instantiation"
        )
    return _shared_vllm_semaphore


# Module-level, keyed by served model id - not a VLLMClient instance
# attribute - for the same reason as the semaphore above: tools/files.py's
# analyze_image builds a fresh VLLMClient per call, so an instance-level
# cache would never be reused across calls. Keying by model id means a
# model swap (new id -> cache miss -> None) naturally forgets a stale
# limit instead of needing explicit invalidation.
_vllm_image_max_dimension_cache: dict[str, int] = {}

# Descending ladder of "longest edge in pixels" values tried, in order,
# when the vLLM backend rejects an image outright (ImageProcessingError).
# Not user/config-facing: the backend never reports its actual limit (vLLM's
# /v1/models only exposes id/max_model_len, nothing about the vision
# processor), so this is discovered empirically per served model and cached
# above rather than requiring per-model tuning in agent.yaml.
IMAGE_SHRINK_LADDER_PIXELS = [2048, 1536, 1024, 768, 512]

# Matches the vLLM backend's error body when its multimodal processor
# rejects an image outright (as opposed to a context-length or
# unknown-model error, which are matched separately). Observed verbatim
# from a real failure: "Failed to apply Qwen3VLProcessor on data={'text':
# ..., 'images': [<PIL.Image.Image ...>]} ...". Deliberately broad (not
# tied to "Qwen3VLProcessor" specifically) so it also catches the same
# class of failure from other vision-capable models served behind vLLM.
_IMAGE_PROCESSOR_ERROR_RE = re.compile(
    r"failed to apply\b.*processor|image_processor|PIL\.Image\.Image",
    re.IGNORECASE,
)


def _resize_image_b64(b64_data: str, max_dimension: int) -> str:
    """Downscale a base64-encoded image so its longest edge is at most
    max_dimension, preserving aspect ratio. Returns the input unchanged
    (no re-encode) if it's already within bounds. Always re-encodes as PNG
    when it does resize, which also fixes callers that mislabel the data
    URL as image/png regardless of the source format."""
    from PIL import Image
    import io

    raw = base64.b64decode(b64_data)
    with Image.open(io.BytesIO(raw)) as opened:
        width, height = opened.size
        if max(width, height) <= max_dimension:
            return b64_data
        scale = max_dimension / max(width, height)
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        converted = opened.convert("RGB") if opened.mode not in ("RGB", "L") else opened
        resized = converted.resize(new_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")


def _apply_image_cap(messages: list[dict], max_dimension: Optional[int]) -> list[dict]:
    """Return messages with any attached images downscaled to max_dimension.
    Operates on the original base64 data in each message's "images" list
    (never re-shrinks an already-shrunk copy), so repeated calls with
    different caps don't compound quality loss. A no-op (returns messages
    unchanged) when max_dimension is None."""
    if max_dimension is None:
        return messages
    result = []
    for msg in messages:
        images = msg.get("images")
        if images:
            new_msg = dict(msg)
            new_msg["images"] = [_resize_image_b64(b64, max_dimension) for b64 in images]
            result.append(new_msg)
        else:
            result.append(msg)
    return result


# Token budget constants for dynamic max_tokens computation
SAFETY_MARGIN_TOKENS = 8192
MIN_OUTPUT_TOKENS = 4096
TOKEN_ESTIMATE_INFLATION = 1.25
IMAGE_TOKEN_ESTIMATE = 1600  # conservative flat per-image token cost; revisit against real vLLM vision tile counts if still inaccurate
SAFETY_MARGIN_RATIO = 0.1  # scale the safety margin with input size so estimation error is bounded proportionally, not by a fixed token count

# Streaming stall-retry backoff: delay before retry N = base * N seconds.
RETRY_BACKOFF_BASE_SECONDS = 3.0


def get_skill_routing_for_channel(channel_id: Optional[str] = None) -> str:
    """Build skill routing section from both system and user skills manifests.
    
    Merges keywords from system skills and user skills (if exists) to provide
    a unified skill routing table for the LLM.
    """
    all_entries: dict[str, list[str]] = {}
    
    system_manifest = load_skills_manifest()
    for entry in system_manifest:
        all_entries[entry.name] = entry.keywords
    
    if channel_id:
        user_manifest = load_user_skills_manifest(channel_id)
        for entry in user_manifest:
            all_entries[entry.name] = entry.keywords
    
    if not all_entries:
        return prompts.current().text("skill-routing-empty")

    lines = []
    for skill_name, keywords in all_entries.items():
        if keywords:
            kw_str = ", ".join(keywords)
            lines.append(f"- {kw_str} → get_skill(name=\"{skill_name}\")")
        else:
            lines.append(f"- {skill_name} → get_skill(name=\"{skill_name}\")")

    return prompts.current().render("skill-routing", skill_list="\n".join(lines))


def _truncate_tool_call_arguments(args, max_chars: int) -> str:
    """Return a JSON-encoded string suitable for OpenAI/vLLM `arguments`.

    - If the JSON form of `args` fits within `max_chars` UTF-8 bytes,
      return it unchanged. No hint is added.
    - If it does not fit, replace it with a single-field object whose
      key is `_truncated` and whose value carries a visible marker:

          {"_truncated": "past tool call (auto-truncated): <first N bytes>..."}

    The marker is JSON-encoded so the result remains a valid
    `arguments` string for the chat-completions API.

    `args` may be:
      - a dict (in-memory `ToolCall.arguments` after SQLite round-trip)
      - a pre-encoded string (legacy / direct call)
      - None (rendered as JSON `null`)
      - empty / missing (rendered as `{}`)

    The caller is responsible for ensuring `max_chars` is sensible
    (clamping happens in `ChatConfig.__post_init__` at app start).
    """
    import json as _json_for_args

    _MARKER_PREFIX = "past tool call (auto-truncated): "
    _MARKER_SUFFIX = "..."

    if args is None:
        args_str = "null"
    elif isinstance(args, str):
        try:
            _json_for_args.loads(args)
            args_str = args  # keep caller's formatting
        except _json_for_args.JSONDecodeError:
            args_str = _json_for_args.dumps({"raw": args}, separators=(",", ":"))
    else:
        args_str = _json_for_args.dumps(args, separators=(",", ":"))

    if len(args_str.encode("utf-8")) <= max_chars:
        return args_str

    truncated_body = args_str.encode("utf-8")[:max_chars].decode("utf-8", errors="replace")
    truncated_marker = _MARKER_PREFIX + truncated_body + _MARKER_SUFFIX
    return _json_for_args.dumps({"_truncated": truncated_marker}, separators=(",", ":"))


def truncate_tool_result(text: str, max_chars: int) -> str:
    """Cap a tool result and append a marker if truncated."""
    if len(text) <= max_chars:
        return text
    removed = len(text) - max_chars
    return f"{text[:max_chars]}\n...[truncated, {removed} of {len(text)} chars omitted]"


def _tool_result_content(tool_result: ToolResult, max_chars: int) -> str:
    """Render a tool result for the message list, capped to avoid unbounded context growth."""
    content = (tool_result.output if tool_result.success else f"Error: {tool_result.error}") or ""
    return truncate_tool_result(content, max_chars)


REPLACED_TEXT_PREFIX = "[Replaced; not shown to user] "


def _build_history_messages(
    context_messages: list[Message],
    summary_text: Optional[str],
    keywords: Optional[list[str]],
    min_messages_to_keep: int,
    max_keywords: int,
) -> list[dict]:
    """Build the history portion of the messages list (system
    prepends + per-message loop + tool_calls/tool_results handling).

    Does NOT add the final user-role message for the current event —
    see _build_current_user_message.

    Returns the messages list up to (but not including) the current
    user turn.
    """
    from .config import config as config_obj  # lazy import (avoids circular import)

    messages: list[dict[str, Any]] = []
    n_truncated = 0
    result_max_chars = config_obj.chat.history_tool_result_max_chars

    if keywords and max_keywords > 0:
        keyword_str = ", ".join(keywords[:max_keywords])
        messages.append({
            "role": "system",
            "content": prompts.current().render("history-keywords", keywords=keyword_str)
        })

    if summary_text:
        messages.append({
            "role": "system",
            "content": prompts.current().render("conversation-summary", summary_text=summary_text)
        })

    max_chars = config_obj.chat.effective_tool_call_args_max_chars

    for msg in context_messages[-min_messages_to_keep:]:
        role = "assistant" if msg.is_outgoing else "user"
        content = msg.event.message
        if msg.source_type in ("cronjob", "subagent") and msg.source_id:
            prefix = f"[{msg.source_type.capitalize()} {msg.source_id[:8]}...] "
            content = prefix + content
        if msg.is_outgoing and not msg.user_visible and content and content.strip():
            # Single-message "replace" mode: a later text of the same turn
            # replaced this one on the user's screen (structural tag).
            content = REPLACED_TEXT_PREFIX + content
        if msg.tool_calls:
            # A stored agent row's text comes from the LLM call made AFTER its
            # tools ran, so replay the live order: assistant(calls) -> tool
            # results -> assistant(text). Real pairs (rather than a
            # "[Used tool(s): ...]" digest) leave nothing in history that
            # looks like text the model could echo back as its reply.
            call_ids = [tc.id or f"call_{i}" for i, tc in enumerate(msg.tool_calls)]
            truncated_args = []
            for call_id, tc in zip(call_ids, msg.tool_calls):
                truncated = _truncate_tool_call_arguments(tc.arguments, max_chars)
                truncated_args.append({"id": call_id, "function": {"name": tc.name, "arguments": truncated}})
                if truncated.startswith('{"_truncated"'):
                    n_truncated += 1
            messages.append({"role": "assistant", "content": "", "tool_calls": truncated_args})
            for call_id, tool_result in zip(call_ids, msg.tool_results or []):
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_result_content(tool_result, result_max_chars),
                })
            if content and content.strip():
                messages.append({"role": role, "content": content})
        else:
            messages.append({"role": role, "content": content})

    if n_truncated > 0:
        LOG.debug(f"truncated_tool_calls={n_truncated}")

    return messages


def with_attachment_note(message: str, filenames: list[str]) -> str:
    """Return `message` with an "[Attachment received: ...]" note appended.

    Attachments are never inlined (no base64 "images" on the chat turn):
    a large inline image crashed the shared SGLang backend, and the model
    needs the on-disk filename in attachments/ anyway to pass to
    analyze_image / generate_image. A bare "[Attachment]" placeholder is
    replaced by the note. Applied once in AgentLoop._preprocess_incoming
    and stored with the message, so history and message_search keep the
    filename on later turns.
    """
    if not filenames:
        return message
    note = f"[Attachment received: {', '.join(filenames)}]"
    if message and message != "[Attachment]":
        return f"{message}\n\n{note}"
    return note


def _build_current_user_message(event: Event) -> dict:
    """Build the user-role message for the current event.

    Attachment filenames are already in event.message (see
    with_attachment_note); attachments are never inlined.
    """
    return {"role": "user", "content": event.message}


def _build_background_activity_section(background_activity: Optional[dict]) -> str:
    """Render the ★ BACKGROUND ACTIVITY status block for build_system_prompt.

    Only id + type are ever rendered - never the underlying cronjob/subagent
    prompt text (which can contain credentials, e.g. a plaintext login
    embedded in a monitoring job's prompt). list_cronjobs already exposes
    full prompt text to PRIVILEGED callers on demand; this doesn't change
    that trust boundary, it just avoids pushing prompt content into every
    turn's context automatically.

    Returns "" when no background activity is running for this channel.
    """
    if not background_activity:
        return ""
    return prompts.current().render(
        "background-activity",
        source_type=background_activity.get("source_type", "unknown"),
        source_id=background_activity.get("source_id", "unknown"),
    )


def _build_system_prompt(
    tools: list[Tool],
    user_tier: str,
    soul_content: Optional[str],
    memory_context: Optional[str],
    event_source: Optional[str],
    channel_id: Optional[str],
    background_activity: Optional[dict],
    message_mode: Optional[str] = None,
) -> str:
    """Assemble the system prompt: main template, then optional sections
    (debug, single-message replace mode, background activity, personal-info/limitations, mute/unmute,
    multi-turn) joined with blank lines.

    Self-free module-level helper (both LLMClient and VLLMClient delegate here).
    """
    from datetime import datetime
    from .config import config as config_obj

    p = prompts.current()
    local_now = datetime.now().astimezone()

    tools_description = []
    for tool in tools:
        params = tool.parameters.get("properties", {})
        param_str = ", ".join(params.keys()) if params else "no parameters"
        tools_description.append(f"- {tool.name}({param_str}): {tool.description}")
    tools_text = "\n".join(tools_description) if tools_description else "No tools available"

    soul_section = p.render("soul-section", soul_content=soul_content) if soul_content else ""
    memory_section = p.render("memory-section", memory_context=memory_context) if memory_context else ""
    agent_name = config_obj.agent.name or "Agent"

    sections = [p.render(
        "system-prompt",
        agent_name=agent_name,
        tools_description=tools_text,
        user_tier=user_tier,
        soul_section=soul_section,
        memory_section=memory_section,
        skill_routing=get_skill_routing_for_channel(channel_id),
        user_phone_number=event_source if event_source else "the channel/phone number of the conversation",
        current_datetime=local_now.strftime("%Y-%m-%d %H:%M:%S"),
        timezone=local_now.tzname(),
    )]

    if config_obj.debug_logging.mode == "auto":
        sections.append(p.text("debug-mode"))

    if message_mode == "replace":
        sections.append(p.render("single-message-replace", replaced_tag=REPLACED_TEXT_PREFIX.strip()))

    if background_activity:
        sections.append(_build_background_activity_section(background_activity))

    if any(tool.name == "message_search" for tool in tools):
        sections.append(p.text("personal-info"))
    else:
        sections.append(p.text("information-limitations"))

    if channel_id and is_channel_muted(channel_id):
        sections.append(p.render("unmute", agent_name=agent_name))
    elif channel_id:
        sections.append(p.text("mute"))

    if user_tier in ("Privileged", "Known"):
        sections.append(p.text("continue"))

    return "\n\n".join(sections)


async def _summarize_from_text(
    chat_fn: Callable[..., Awaitable[tuple[str, list]]],
    conversation_text: str,
    prior_summary: Optional[str] = None,
    keywords: Optional[list[str]] = None,
    max_keywords: int = 50,
) -> str:
    """Build the summary prompt from pre-formatted conversation text and call the LLM.

    Shared by summarize_conversation (Message objects) and summarize_working_history
    (raw OpenAI-format working dicts) so the summary prompt lives in one place.

    Self-free module-level helper (both LLMClient and VLLMClient delegate here,
    passing their own `self.chat` as `chat_fn`).
    """
    prior_section = ""
    if prior_summary:
        keywords_str = ", ".join(keywords) if keywords else ""
        prior_section = prompts.current().render(
            "summarize-prior-block", prior_summary=prior_summary, prior_keywords=keywords_str
        ) + "\n"

    prompt = prompts.current().render(
        "summarize",
        max_keywords=max_keywords,
        prior_section=prior_section,
        conversation_text=conversation_text,
    )

    try:
        response, _ = await chat_fn([{"role": "user", "content": prompt}], [])
        return response
    except Exception as e:
        LOG.error(f"Summarization failed: {e}")
        return prior_summary if prior_summary else ""


async def _summarize_conversation(
    chat_fn: Callable[..., Awaitable[tuple[str, list]]],
    messages: list[Message],
    prior_summary: Optional[str] = None,
    keywords: Optional[list[str]] = None,
    max_keywords: int = 50,
) -> str:
    """Summarize conversation messages, optionally incorporating prior summary and keywords.

    Self-free module-level helper (see _summarize_from_text)."""
    from .config import config as config_obj

    if not messages:
        return ""

    msg_lines = []
    for msg in messages:
        role = "Assistant" if msg.is_outgoing else "User"
        content = msg.event.message
        if msg.tool_calls:
            tc_names = ", ".join(tc.name for tc in msg.tool_calls)
            content = f"{content}\n[Tools called: {tc_names}]"
        # Include tool results to improve summary quality (D6 improvement)
        if msg.tool_results:
            result_summaries = []
            for tr in msg.tool_results:
                if tr.success:
                    # Truncate result to first 200 chars for summary context
                    output = tr.output or ""
                    truncated = (output[:200] + "...") if len(output) > 200 else output
                    result_summaries.append(f"{tr.name}: {truncated}")
                else:
                    result_summaries.append(f"{tr.name}: Error - {tr.error}")
            if result_summaries:
                content = f"{content}\n[Results: {'; '.join(result_summaries)}]"
        msg_lines.append(f"{role}: {content}")

    conversation_text = "\n".join(msg_lines)

    return await _summarize_from_text(chat_fn, conversation_text, prior_summary, keywords, max_keywords)


async def _summarize_working_history(
    chat_fn: Callable[..., Awaitable[tuple[str, list]]],
    working_msgs: list[dict],
    max_keywords: int = 50,
) -> str:
    """Summarize an in-session working-history slice (raw OpenAI-format dicts:
    assistant messages with optional tool_calls, and tool-result messages).
    Used for ephemeral session-local compaction in cron/subagent loops.

    Self-free module-level helper (see _summarize_from_text)."""
    if not working_msgs:
        return ""

    lines: list[str] = []
    for msg in working_msgs:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            line = f"Assistant: {content}"
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                names = ", ".join(tc["function"]["name"] for tc in tool_calls)
                line = f"{line}\n[Tools called: {names}]"
            lines.append(line)
        elif role == "tool":
            truncated = (content[:200] + "...") if len(content) > 200 else content
            lines.append(f"Tool result: {truncated}")
        elif role == "system":
            lines.append(f"System: {content}")

    conversation_text = "\n".join(lines)
    return await _summarize_from_text(chat_fn, conversation_text, max_keywords=max_keywords)


async def _distill_keywords(
    chat_fn: Callable[..., Awaitable[tuple[str, list]]],
    existing_summary: str,
    existing_keywords: Optional[list[str]] = None,
    max_keywords: int = 50,
) -> list[str]:
    """Extract keywords from existing summary for retrieval optimization.

    Self-free module-level helper (see _summarize_from_text)."""
    prior_kw_str = ", ".join(existing_keywords) if existing_keywords else "none"

    prompt = prompts.current().render(
        "distill-keywords",
        max_keywords=max_keywords,
        prior_keywords=prior_kw_str,
        summary=existing_summary,
    )

    try:
        response, _ = await chat_fn([{"role": "user", "content": prompt}], [])
        keywords = [kw.strip() for kw in response.split(",") if kw.strip()]
        return keywords[:max_keywords]
    except Exception as e:
        LOG.error(f"Keyword distillation failed: {e}")
        return existing_keywords[:max_keywords] if existing_keywords else []


class LLMClient:
    def __init__(self, config: OllamaConfig):
        self.host = config.host
        self.model = config.model
        self.options = config.options.to_dict()
        self._client = ollama.Client(host=self.host)

    def encode_image_base64(self, path: str) -> Optional[str]:
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            LOG.error(f"Failed to encode image {path}: {e}")
            return None

    async def chat(
        self, messages: list[dict], tools: list[Tool], channel_id: Optional[str] = None,
        thinking: Optional[bool] = None,
    ) -> tuple[str, list[ToolCall]]:
        """Send chat request to ollama, return response text and any tool calls."""
        ollama_messages = []
        for msg in messages:
            if isinstance(msg, dict):
                ollama_messages.append(msg)
            elif isinstance(msg, Message):
                role = "assistant" if msg.is_outgoing else "user"
                ollama_messages.append({
                    "role": role,
                    "content": msg.event.message,
                })

        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

        debug_logger = safe_get_debug_logger()
        if debug_logger:
            debug_logger.log("llm_input", channel_id or "unknown", {
                "messages_count": len(messages),
                "tools_count": len(tools),
            })

        def _do_chat():
            extra = {"think": thinking} if thinking is not None else {}
            return self._client.chat(
                model=self.model,
                messages=ollama_messages,
                tools=tool_defs,
                options=self.options,
                **extra,
            )

        try:
            response = await asyncio.to_thread(_do_chat)
        except Exception as e:
            if debug_logger:
                debug_logger.record_error(f"LLM call failed: {str(e)}")
            raise

        text_response = response["message"]["content"] or ""
        tool_calls: list[ToolCall] = []

        if response["message"].get("tool_calls"):
            for tc in response["message"]["tool_calls"]:
                func = tc.get("function", tc)
                tool_calls.append(ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=func.get("arguments", {}),
                ))

        if debug_logger:
            debug_logger.log("llm_output", channel_id or "unknown", {
                "response_text": text_response[:500] if text_response else "",
                "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in tool_calls],
            })

        return text_response, tool_calls

    def build_prompt(self, context_messages: list[Message], event: Event) -> str:
        """Build prompt from context and event (legacy format - returns single string)."""
        lines = []
        for msg in context_messages[-10:]:
            role = "Assistant" if msg.is_outgoing else "User"
            lines.append(f"{role}: {msg.event.message}")

        lines.append(f"User: {event.message}")
        return "\n".join(lines)

    def build_messages(self, context_messages: list[Message], event: Event,
                       summary_text: Optional[str] = None, keywords: Optional[list[str]] = None) -> list[dict]:
        """Build properly structured message history for LLM with optional summary."""
        from .config import config as config_obj  # lazy import (avoids circular import)

        messages = _build_history_messages(
            context_messages, summary_text, keywords,
            min_messages_to_keep=config_obj.chat.min_messages_to_keep,
            max_keywords=config_obj.chat.max_keywords,
        )

        messages.append(_build_current_user_message(event))
        return messages

    @staticmethod
    def estimate_token_count(messages: list[dict], system_prompt: str) -> int:
        """Rough token estimation: sum of (len(text) / 4)."""
        total = len(system_prompt) // 4
        for msg in messages:
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            total += len(content) // 4
        return total

    async def summarize_conversation(
        self,
        messages: list[Message],
        prior_summary: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        max_keywords: int = 50,
    ) -> str:
        """Summarize conversation messages, optionally incorporating prior summary and keywords."""
        return await _summarize_conversation(self.chat, messages, prior_summary, keywords, max_keywords)

    async def summarize_working_history(self, working_msgs: list[dict], max_keywords: int = 50) -> str:
        """Summarize an in-session working-history slice (raw OpenAI-format dicts:
        assistant messages with optional tool_calls, and tool-result messages).
        Used for ephemeral session-local compaction in cron/subagent loops."""
        return await _summarize_working_history(self.chat, working_msgs, max_keywords)

    async def distill_keywords(
        self,
        existing_summary: str,
        existing_keywords: Optional[list[str]] = None,
        max_keywords: int = 50,
    ) -> list[str]:
        """Extract keywords from existing summary for retrieval optimization."""
        return await _distill_keywords(self.chat, existing_summary, existing_keywords, max_keywords)

    def build_system_prompt(self, tools: list[Tool], user_tier: str, soul_content: Optional[str] = None, memory_context: Optional[str] = None, event_source: Optional[str] = None, channel_id: Optional[str] = None, background_activity: Optional[dict] = None, message_mode: Optional[str] = None) -> str:
        """Build system prompt with available tools and user tier context."""
        return _build_system_prompt(tools, user_tier, soul_content, memory_context, event_source, channel_id, background_activity, message_mode)



class VLLMClient:
    def __init__(self, config: VllmConfig):
        self.host = config.host
        self.model = config.model
        # If the operator pinned a model explicitly, never silently swap it out
        # underneath them - a "model not found" error in that case is a real
        # misconfiguration, not a backend model change to auto-recover from.
        self._pinned = config.model is not None
        self.options = config.options
        self._semaphore = _get_shared_vllm_semaphore(config.options.max_concurrent_requests)

    async def initialize(self) -> None:
        """Auto-discover model from vLLM backend if not explicitly configured.

        Safe to call again later (e.g. backend was down at startup and this
        is retried on first chat(), or the cached model was rejected at
        request time): re-queries /v1/models and overwrites self.model.
        Raises RuntimeError if the backend can't be reached or has no models;
        callers decide how to surface that (main.py logs+continues at
        startup, chat() lets it propagate as a normal LLM error).
        """
        if self._pinned:
            return
        self.model = await self._discover_model()

    async def _discover_model(self) -> str:
        """Query /v1/models and return the first model id. Raises RuntimeError on failure."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.get(f"{self.host}/v1/models")
                response.raise_for_status()
                data = response.json()["data"]
                if not data:
                    raise RuntimeError(
                        f"vLLM backend at {self.host} reports no available models. "
                        "Specify model explicitly in config or VLLM_MODEL env var."
                    )
                model = data[0]["id"]
                LOG.info(f"Auto-selected vLLM model: {model}")
                if len(data) > 1:
                    LOG.warning(
                        f"vLLM backend has {len(data)} models, using first: {model}. "
                        "Set model explicitly in config to choose a different one."
                    )
                return model
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Failed to discover models from vLLM backend at {self.host}: "
                f"HTTP {e.response.status_code}. Specify model explicitly in config or VLLM_MODEL env var."
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to discover models from vLLM backend at {self.host}: {e}. "
                "Specify model explicitly in config or VLLM_MODEL env var."
            ) from e

    def encode_image_base64(self, path: str) -> Optional[str]:
        try:
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            LOG.error(f"Failed to encode image {path}: {e}")
            return None

    def _transform_messages_for_vllm(self, messages: list[dict]) -> list[dict]:
        """Transform messages to vLLM format, handling vision images and tool calls."""
        import json
        transformed = []
        for msg in messages:
            if "images" in msg:
                content_parts = []
                if msg.get("content") and msg["content"] != "[Attachment]":
                    content_parts.append({"type": "text", "text": msg["content"]})
                for img_b64 in msg["images"]:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"}
                    })
                transformed.append({
                    "role": msg["role"],
                    "content": content_parts if content_parts else msg.get("content", ""),
                })
            elif "tool_calls" in msg:
                tool_calls = []
                for tc in msg["tool_calls"]:
                    func = tc.get("function", tc)
                    args = func.get("arguments", {})
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    tool_calls.append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": func.get("name", ""),
                            "arguments": args,
                        }
                    })
                transformed.append({
                    "role": msg["role"],
                    "content": msg.get("content", ""),
                    "tool_calls": tool_calls,
                })
            else:
                transformed.append(msg)
        return transformed

    def _compute_max_tokens(self, messages: list[dict], tools: list[Tool]) -> int:
        """Compute dynamic max_tokens based on estimated input and context window."""
        # If context_window is 0, disable dynamic clamping (legacy behavior)
        if self.options.context_window <= 0:
            return self.options.max_tokens

        # Estimate input tokens: sum of message content (including tool defs)
        import json
        vllm_messages = self._transform_messages_for_vllm(messages)
        content_text = ""
        image_count = 0
        for msg in vllm_messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        content_text += part.get("text", "")
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        image_count += 1
            elif isinstance(content, str):
                content_text += content
            if msg.get("tool_calls"):
                content_text += json.dumps(msg["tool_calls"])

        # Add tool definitions
        tool_defs_json = json.dumps([{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
        } for t in tools])
        content_text += tool_defs_json

        # Estimate tokens: use existing heuristic with inflation factor
        raw_estimate = LLMClient.estimate_token_count([{"content": content_text}], "")
        est_input_tokens = math.ceil(raw_estimate * TOKEN_ESTIMATE_INFLATION)
        est_input_tokens += image_count * IMAGE_TOKEN_ESTIMATE

        # Compute output budget: context_window - input - safety_margin
        # Margin scales with input size so estimation error is bounded proportionally.
        effective_margin = max(SAFETY_MARGIN_TOKENS, math.ceil(est_input_tokens * SAFETY_MARGIN_RATIO))
        max_out = self.options.context_window - est_input_tokens - effective_margin
        max_out = max(MIN_OUTPUT_TOKENS, min(self.options.max_tokens, max_out))

        # Log if clamping significantly reduces the configured value
        if max_out < self.options.max_tokens * 0.5:
            LOG.info(
                f"vLLM budget clamping: input~{est_input_tokens}t, output reduced "
                f"from {self.options.max_tokens} to {max_out} (context_window={self.options.context_window})"
            )

        return max_out

    async def _stream_chat_once(
        self, payload: dict, debug_logger
    ) -> tuple[str, list[ToolCall], Optional[str]]:
        """Perform a single streaming chat request.

        Watchdogs inter-chunk idle time: raises StreamStallError if no SSE line
        arrives within self.options.stream_idle_timeout (this covers the
        time-to-first-token too, including vision prefill). Waiting longer
        than that for the response headers raises httpx.ReadTimeout, which
        _send_with_retries treats as a stall. Raises
        ContextLengthExceededError on a 400 context-length response,
        ModelNotFoundError if the backend rejects the requested model id
        (e.g. it was restarted serving a different model), or
        ImageProcessingError if the backend's multimodal processor rejects
        an attached image outright. Other HTTP/transport errors propagate
        unchanged.
        """
        import json

        idle_timeout = self.options.stream_idle_timeout
        content_parts: list[str] = []
        tool_call_acc: dict[int, dict] = {}
        finish_reason: Optional[str] = None

        # read=idle_timeout also bounds the wait for the response headers, which
        # the watchdog below doesn't cover: a backend that accepts the
        # connection and never answers (SGLang's scheduler hung, 2026-10-09)
        # otherwise blocked this call - and its concurrency slot - forever.
        # httpx.ReadTimeout is turned into StreamStallError by the caller.
        timeout = httpx.Timeout(connect=10.0, read=idle_timeout, write=10.0, pool=10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.host}/v1/chat/completions", json=payload
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    response_text = body.decode("utf-8", errors="replace")
                    if debug_logger:
                        debug_logger.record_error(f"vLLM call failed: {response_text}")
                    if response.status_code == 400 and re.search(
                        r"context length|maximum.*tokens", response_text, re.IGNORECASE
                    ):
                        raise ContextLengthExceededError(
                            f"vLLM context length exceeded: {response_text}"
                        )
                    if response.status_code in (400, 404) and re.search(
                        r"does not exist|model.*not found|unknown model|no such model",
                        response_text, re.IGNORECASE
                    ):
                        raise ModelNotFoundError(
                            f"vLLM backend rejected model '{payload.get('model')}': {response_text}"
                        )
                    if response.status_code == 400 and _IMAGE_PROCESSOR_ERROR_RE.search(response_text):
                        raise ImageProcessingError(
                            f"vLLM rejected image input: {response_text}"
                        )
                    response.raise_for_status()

                lines = response.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=idle_timeout)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise StreamStallError(
                            f"vLLM stream stalled: no data for {idle_timeout}s"
                        )

                    if not line.startswith("data:"):
                        continue
                    data_field = line[len("data:"):].strip()
                    if data_field == "[DONE]":
                        break
                    try:
                        obj = json.loads(data_field)
                    except json.JSONDecodeError:
                        continue

                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}

                    if delta.get("content"):
                        content_parts.append(delta["content"])

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tool_call_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        func = tc.get("function") or {}
                        if func.get("name"):
                            acc["name"] = func["name"]
                        if func.get("arguments"):
                            acc["arguments"] += func["arguments"]

        tool_calls: list[ToolCall] = []
        for idx in sorted(tool_call_acc):
            acc = tool_call_acc[idx]
            try:
                args = json.loads(acc["arguments"]) if acc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=acc["id"], name=acc["name"], arguments=args))

        return "".join(content_parts), tool_calls, finish_reason

    async def _send_with_retries(
        self, payload: dict, debug_logger
    ) -> tuple[str, list[ToolCall], Optional[str]]:
        """Send payload via _stream_chat_once, retrying on stream stall (idle
        watchdog or no response headers in time, linear backoff) and on a
        rejected model id (re-probes
        /v1/models, mutates payload["model"] and self.model in place).
        ImageProcessingError and other HTTP/transport errors propagate
        unchanged - image-shrink retries are handled one level up in chat(),
        since they need to rebuild payload["messages"], not just resend."""
        max_retries = max(0, self.options.max_retries)
        for attempt in range(max_retries + 1):
            try:
                try:
                    return await self._stream_chat_once(payload, debug_logger)
                except httpx.ReadTimeout as e:
                    # No byte for stream_idle_timeout, usually while waiting
                    # for the response headers: retried like a stall.
                    raise StreamStallError(
                        f"vLLM sent no data for {self.options.stream_idle_timeout}s "
                        f"(no response headers, or the stream stalled)"
                    ) from e
            except StreamStallError as e:
                if attempt >= max_retries:
                    if debug_logger:
                        debug_logger.record_error(f"vLLM streaming exhausted retries: {e}")
                    raise
                backoff = RETRY_BACKOFF_BASE_SECONDS * (attempt + 1)
                LOG.warning(
                    f"vLLM stream stall (attempt {attempt + 1}/{max_retries + 1}): {e}; "
                    f"retrying in {backoff:.0f}s"
                )
                await asyncio.sleep(backoff)
            except ModelNotFoundError as e:
                # Backend was restarted serving a different model than what we
                # cached (or explicitly pinned - in which case this is a real
                # misconfiguration, not something to auto-recover from).
                if self._pinned or attempt >= max_retries:
                    if debug_logger:
                        debug_logger.record_error(f"vLLM model unavailable: {e}")
                    raise
                LOG.warning(
                    f"vLLM backend rejected model '{self.model}' ({e}); re-probing /v1/models"
                )
                self.model = await self._discover_model()
                payload["model"] = self.model
                LOG.info(f"Retrying with re-detected vLLM model: {self.model}")
        raise AssertionError("unreachable: loop above always returns or raises")

    async def chat(
        self, messages: list[dict], tools: list[Tool], channel_id: Optional[str] = None,
        thinking: Optional[bool] = None,
    ) -> tuple[str, list[ToolCall]]:
        """Send chat request to vLLM via streaming, return response text and any
        tool calls. Retries on stream stall (idle watchdog) with linear backoff
        and on a rejected model id (re-probes /v1/models); other errors (context
        length, HTTP 4xx/5xx) propagate immediately.

        If the request carries images and the backend rejects them outright
        (ImageProcessingError - the currently-served model's vision processor
        can't handle them at their native resolution), downscales and retries
        along IMAGE_SHRINK_LADDER_PIXELS. The vLLM API has no way to query a
        model's actual image-size limit ahead of time (/v1/models exposes only
        id/max_model_len), so this is discovered empirically against the real
        backend. The working size is cached in _vllm_image_max_dimension_cache,
        keyed by model id, so later calls (including from other VLLMClient
        instances, e.g. tools/files.py's analyze_image, which builds a fresh
        client per call) skip straight to it instead of re-learning it every
        time; a model swap changes the cache key, so a new model always gets
        its own from-scratch discovery rather than inheriting a stale limit.

        If no model is known yet (auto-detect mode and the backend was down at
        startup), (re-)runs discovery first; if the backend is still down this
        raises RuntimeError, which propagates to the caller just like any other
        LLM error (surfaced to the user as "LLM temporarily unavailable")."""
        if self.model is None:
            await self.initialize()
        assert self.model is not None, "initialize() either sets self.model or raises"

        has_images = any(msg.get("images") for msg in messages)
        shrink_steps: list[Optional[int]] = [None]
        if has_images:
            cached_cap = _vllm_image_max_dimension_cache.get(self.model)
            if cached_cap is not None:
                known_steps: list[Optional[int]] = [cached_cap]
                known_steps.extend(s for s in IMAGE_SHRINK_LADDER_PIXELS if s < cached_cap)
                shrink_steps = known_steps
            else:
                shrink_steps = [None] + list(IMAGE_SHRINK_LADDER_PIXELS)

        def build_vllm_messages(cap: Optional[int]) -> list[dict]:
            return self._transform_messages_for_vllm(_apply_image_cap(messages, cap))

        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

        debug_logger = safe_get_debug_logger()
        if debug_logger:
            debug_logger.log("llm_input", channel_id or "unknown", {
                "backend": "vllm",
                "messages_count": len(messages),
                "tools_count": len(tools),
            })

        # Compute dynamic max_tokens based on input budget (image token cost
        # is a flat per-image estimate - see IMAGE_TOKEN_ESTIMATE - so it's
        # unaffected by whether images end up downscaled below).
        max_tokens_budget = self._compute_max_tokens(messages, tools)

        payload = {
            "model": self.model,
            "messages": build_vllm_messages(shrink_steps[0]),
            "max_tokens": max_tokens_budget,
            "temperature": self.options.temperature,
            "top_p": self.options.top_p,
            "top_k": self.options.top_k,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tool_defs:
            payload["tools"] = tool_defs
        if thinking is not None:
            # Qwen-style chat templates; voice turns turn reasoning off for latency.
            payload["chat_template_kwargs"] = {"enable_thinking": thinking}

        text_response = ""
        tool_calls: list[ToolCall] = []
        finish_reason: Optional[str] = None

        wait_started = time.monotonic()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), timeout=self.options.max_concurrent_wait_seconds
            )
        except asyncio.TimeoutError:
            if debug_logger:
                debug_logger.record_error(
                    f"vLLM concurrency wait exhausted: no slot free after "
                    f"{self.options.max_concurrent_wait_seconds}s"
                )
            raise VLLMConcurrencyLimitError(
                f"Timed out after {self.options.max_concurrent_wait_seconds}s waiting for a "
                f"vLLM concurrency slot (max_concurrent_requests={self.options.max_concurrent_requests})"
            )

        waited = time.monotonic() - wait_started
        if waited > 0.5:
            LOG.info(
                f"vLLM request queued for {waited:.1f}s "
                f"(max_concurrent_requests={self.options.max_concurrent_requests})"
            )

        try:
            shrink_idx = 0
            while True:
                try:
                    text_response, tool_calls, finish_reason = await self._send_with_retries(
                        payload, debug_logger
                    )
                    current_cap = shrink_steps[shrink_idx]
                    if has_images and current_cap is not None:
                        _vllm_image_max_dimension_cache[self.model] = current_cap
                    break
                except ImageProcessingError as e:
                    if not has_images or shrink_idx >= len(shrink_steps) - 1:
                        if debug_logger:
                            debug_logger.record_error(
                                f"vLLM image processing failed"
                                + (" (exhausted shrink ladder)" if has_images else "")
                                + f": {e}"
                            )
                        raise
                    shrink_idx += 1
                    next_cap = shrink_steps[shrink_idx]
                    LOG.warning(
                        f"vLLM rejected image input (likely too large for model "
                        f"'{self.model}'); retrying with images capped at "
                        f"{next_cap}px longest edge"
                    )
                    payload["messages"] = build_vllm_messages(next_cap)
        finally:
            self._semaphore.release()

        if debug_logger:
            debug_logger.log("llm_output", channel_id or "unknown", {
                "backend": "vllm",
                "response_text": text_response[:500] if text_response else "",
                "tool_calls": [{"name": tc.name, "arguments": tc.arguments} for tc in tool_calls],
                "finish_reason": finish_reason,
            })

        return text_response, tool_calls

    async def summarize_conversation(
        self,
        messages: list[Message],
        prior_summary: Optional[str] = None,
        keywords: Optional[list[str]] = None,
        max_keywords: int = 50,
    ) -> str:
        """Summarize conversation messages, optionally incorporating prior summary and keywords."""
        return await _summarize_conversation(self.chat, messages, prior_summary, keywords, max_keywords)

    async def summarize_working_history(self, working_msgs: list[dict], max_keywords: int = 50) -> str:
        """Summarize an in-session working-history slice (raw OpenAI-format dicts:
        assistant messages with optional tool_calls, and tool-result messages).
        Used for ephemeral session-local compaction in cron/subagent loops."""
        return await _summarize_working_history(self.chat, working_msgs, max_keywords)

    async def distill_keywords(
        self,
        existing_summary: str,
        existing_keywords: Optional[list[str]] = None,
        max_keywords: int = 50,
    ) -> list[str]:
        """Extract keywords from existing summary for retrieval optimization."""
        return await _distill_keywords(self.chat, existing_summary, existing_keywords, max_keywords)

    def build_prompt(self, context_messages: list[Message], event: Event) -> str:
        """Build prompt from context and event (legacy format - returns single string)."""
        lines = []
        for msg in context_messages[-10:]:
            role = "Assistant" if msg.is_outgoing else "User"
            lines.append(f"{role}: {msg.event.message}")

        lines.append(f"User: {event.message}")
        return "\n".join(lines)

    def build_messages(self, context_messages: list[Message], event: Event,
                       summary_text: Optional[str] = None, keywords: Optional[list[str]] = None) -> list[dict]:
        """Build properly structured message history for vLLM with optional summary."""
        from .config import config as config_obj  # lazy import (avoids circular import)

        messages = _build_history_messages(
            context_messages, summary_text, keywords,
            min_messages_to_keep=config_obj.chat.min_messages_to_keep,
            max_keywords=config_obj.chat.max_keywords,
        )

        messages.append(_build_current_user_message(event))
        return messages

    def build_system_prompt(self, tools: list[Tool], user_tier: str, soul_content: Optional[str] = None, memory_context: Optional[str] = None, event_source: Optional[str] = None, channel_id: Optional[str] = None, background_activity: Optional[dict] = None, message_mode: Optional[str] = None) -> str:
        """Build system prompt with available tools and user tier context."""
        return _build_system_prompt(tools, user_tier, soul_content, memory_context, event_source, channel_id, background_activity, message_mode)
