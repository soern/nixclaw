"""Tool registry for nixclaw-agent."""

import asyncio
import copy
import hashlib
import logging
import math
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from enum import Enum

from ..auth import AuthManager, TOOL_PERMISSIONS, UserTier
from .. import prompts
from ..config import config, normalize_image_steps
from ..debug_log import safe_get_debug_logger
from ..turn_output import TurnOutput
from ..types import Tool, ToolResult

if TYPE_CHECKING:
    from ..types import Context
from .bash import effective_timeout as bash_effective_timeout


def _serialize_for_debug(args: dict) -> dict:
    """Convert arguments to JSON-serializable format for debug logging."""
    result = {}
    for k, v in args.items():
        if isinstance(v, Enum):
            result[k] = v.value
        elif isinstance(v, dict):
            result[k] = _serialize_for_debug(v)
        else:
            result[k] = v
    return result


def _get_required_tier(tool_name: str) -> Optional[UserTier]:
    """Find which tier has access to a tool. Returns None if no tier has access."""
    for tier, tools in TOOL_PERMISSIONS.items():
        if tool_name in tools:
            return tier
    return None

TOOL_TIMEOUT_SECONDS = 30
TOOL_TIMEOUT_OVERRIDES = {
    "analyze_image": 120,
}
# The 5 browser tools get a fixed per-tool operation budget instead of the
# flat TOOL_TIMEOUT_SECONDS default (screenshots of heavy pages are slow).
BROWSER_TOOLS = {
    "web_search",
    "browser_navigate",
    "browser_interact",
    "browser_evaluate",
    "browser_screenshot",
}
BROWSER_OPERATION_BUDGET_SECONDS = {
    "web_search": 45,
    "browser_navigate": 45,
    "browser_interact": 45,
    "browser_evaluate": 45,
    "browser_screenshot": 120,
}
MAX_TOOL_CACHE_SIZE = 50
MAX_REPEATED_TOOL_FAILURES = 3
TOOL_FAILURE_WINDOW_SECONDS = 60

TOOLS_NEEDING_CHANNEL_ID = {
    "end_call",
    "file_read", "file_write", "file_grep", "file_list", "file_search",
    "message_search",
    "schedule_cron", "list_cronjobs", "cancel_cronjob", "spawn_subagent", "stop_running_activity", "update_soul", "read_soul",
    "remember", "recall_memories",
    "analyze_image",
    "get_skill",
    "bash",
    "send_signal_attachment",
    "generate_image",
    "text_to_speech",
    "list_tts_voices",
    "set_tts_voice",
    "retranscribe_audio",
    "browser_screenshot",
    "web_search",
    "browser_navigate",
    "browser_interact",
    "browser_evaluate",
    "get_channel_mode",
    "set_channel_mode",
    "get_message_mode",
    "set_message_mode",
    "debug_cron",
    "debug_subagent",
}
TOOLS_NEEDING_USER_TIER = {"bash", "spawn_subagent"}
# Internal handler arguments the model must never supply itself: stripped from
# its arguments before the harness injects the real values (a model-supplied
# `consent` / `consent_question` must not skip the users' approval).
RESERVED_ARGUMENTS = frozenset({"consent", "consent_question", "channel_id", "user_tier"})

_current_context: ContextVar[Optional["Context"]] = ContextVar('current_context', default=None)

LOG = logging.getLogger(__name__)


@dataclass
class RegisteredTool:
    name: str
    handler: Callable
    parameters: dict


@dataclass
class CachedToolResult:
    result: ToolResult
    timestamp: float


class ToolRegistry:
    def __init__(self, auth_manager: AuthManager, _get_current_time: Optional[Callable[[], float]] = None):
        self._auth_manager = auth_manager
        self._tools: dict[str, RegisteredTool] = {}
        self._spawn_subagent_callback: Optional[Callable] = None
        self._tool_cache: OrderedDict[str, CachedToolResult] = OrderedDict()
        self._failure_counter: dict[tuple[str, str], list[float]] = {}
        self._file_list_versions: dict[str, int] = {}
        self._get_current_time = _get_current_time or time.time

    def set_context(self, context: "Context") -> None:
        _current_context.set(context)

    def set_spawn_subagent_callback(self, callback: Callable) -> None:
        self._spawn_subagent_callback = callback

    def _get_cache_key(self, tool_name: str, arguments: dict) -> str:
        args_str = str(sorted(arguments.items()))
        hash_obj = hashlib.sha256(f"{tool_name}:{args_str}".encode())
        return f"{tool_name}:{hash_obj.hexdigest()[:16]}"

    def invalidate_file_list_cache(self, channel_id: str) -> None:
        """Invalidate file_list cache for a channel when attachments change."""
        self._file_list_versions[channel_id] = self._file_list_versions.get(channel_id, 0) + 1
        keys_to_remove = [
            k for k in self._tool_cache.keys()
            if k.startswith("file_list:")
        ]
        for k in keys_to_remove:
            self._tool_cache.pop(k, None)

    def _get_param_hash(self, tool_name: str, arguments: dict) -> str:
        """Compute a hash of tool arguments for circuit breaker tracking."""
        args_str = str(sorted(arguments.items()))
        hash_obj = hashlib.sha256(f"{tool_name}:{args_str}".encode())
        return hash_obj.hexdigest()[:16]

    def _check_circuit_breaker(self, tool_name: str, arguments: dict) -> bool:
        now = self._get_current_time()
        param_hash = self._get_param_hash(tool_name, arguments)
        key = (tool_name, param_hash)
        if key not in self._failure_counter:
            self._failure_counter[key] = []
        failures = self._failure_counter[key]
        cutoff = now - TOOL_FAILURE_WINDOW_SECONDS
        self._failure_counter[key] = [ts for ts in failures if ts > cutoff]
        if len(self._failure_counter[key]) >= MAX_REPEATED_TOOL_FAILURES:
            LOG.warning(f"Circuit breaker OPEN for '{tool_name}[{param_hash}]': {len(self._failure_counter[key])} failures in {TOOL_FAILURE_WINDOW_SECONDS}s")
            return True
        return False

    def _record_failure(self, tool_name: str, arguments: dict) -> None:
        param_hash = self._get_param_hash(tool_name, arguments)
        key = (tool_name, param_hash)
        self._failure_counter.setdefault(key, []).append(self._get_current_time())

    def register(self, name: str, handler: Callable, parameters: dict) -> None:
        self._tools[name] = RegisteredTool(
            name=name,
            handler=handler,
            parameters=parameters,
        )

    def get_tools_for_tier(self, tier: UserTier) -> list[Tool]:
        # Descriptions come from the prompt set (config/prompts) on every call,
        # so a reload is visible immediately. Registered schemas are never mutated.
        docs = prompts.current().doc("tool-descriptions.yaml")
        allowed = self._auth_manager.get_allowed_tools(tier)
        result = []
        for tool_name in allowed:
            if tool_name in self._tools:
                t = self._tools[tool_name]
                entry = docs.get(t.name, {})
                parameters = copy.deepcopy(t.parameters)
                for pname, pdesc in entry.get("params", {}).items():
                    prop = parameters.get("properties", {}).get(pname)
                    if pdesc and prop is not None:
                        prop["description"] = pdesc
                result.append(Tool(
                    name=t.name,
                    description=entry.get("description", ""),
                    parameters=parameters,
                ))
        return result

    def get_tool_handler(self, tool_name: str) -> Optional[Callable]:
        tool = self._tools.get(tool_name)
        return tool.handler if tool else None

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict,
        tier: UserTier,
        *,
        consent_question: Optional[str] = None,
    ) -> ToolResult:
        """Run one tool call.

        `consent_question` is passed only by core.py, to re-run a bash command
        the users approved in a poll (see consent.py); never from the model.
        """
        LOG.info(f"EXEC_TOOL_ENTRY: tool={tool_name}, tier={tier}, can_use={self._auth_manager.can_use_tool(tier, tool_name)}")
        if not self._auth_manager.can_use_tool(tier, tool_name):
            required_tier = _get_required_tier(tool_name)
            if required_tier:
                tier_name = required_tier.value
                error_msg = f"{tool_name} requires {tier_name} access"
            else:
                error_msg = f"Tool '{tool_name}' not available"
            return ToolResult(
                name=tool_name,
                success=False,
                output=None,
                error=error_msg,
            )

        handler = self.get_tool_handler(tool_name)
        if not handler:
            return ToolResult(
                name=tool_name,
                success=False,
                output=None,
                error=f"Tool '{tool_name}' not found in registry",
            )

        if isinstance(arguments, dict) and "_truncated" in arguments:
            # History compaction (llm._truncate_tool_call_arguments) replaces
            # oversized past arguments with this placeholder; a model that
            # copies it would otherwise crash the handler with a TypeError.
            return ToolResult(
                name=tool_name,
                success=False,
                output=None,
                error=(
                    "Invalid arguments: '_truncated' is a placeholder for a past, shortened tool call in the "
                    "conversation history, not a real parameter. Call the tool again with the actual arguments."
                ),
            )

        if isinstance(arguments, dict) and RESERVED_ARGUMENTS & arguments.keys():
            LOG.warning(f"Tool '{tool_name}': dropping reserved arguments {sorted(RESERVED_ARGUMENTS & arguments.keys())} supplied by the model")
            arguments = {k: v for k, v in arguments.items() if k not in RESERVED_ARGUMENTS}

        cache_key = self._get_cache_key(tool_name, arguments)
        if tool_name in BROWSER_TOOLS:
            # A cached web_search result skips navigation, so it must only be
            # served to the browser scope whose page it describes (otherwise
            # channel B / a cron run would get channel A's results while its
            # own page never moved).
            current = _current_context.get()
            scope = get_current_browser_scope() or (current.channel_id if current else "")
            cache_key = f"{scope}|{cache_key}"
        if tool_name == "web_search" and cache_key in self._tool_cache:
            cached = self._tool_cache.pop(cache_key)
            self._tool_cache[cache_key] = cached
            LOG.info(f"Tool '{tool_name}' cache hit, returning cached result")
            return cached.result

        if self._check_circuit_breaker(tool_name, arguments):
            return ToolResult(
                name=tool_name,
                success=False,
                output=None,
                error=f"Tool '{tool_name}' circuit breaker open due to repeated failures",
            )

        context = _current_context.get()
        if context and tool_name in TOOLS_NEEDING_CHANNEL_ID:
            arguments = {**arguments, "channel_id": context.channel_id}
        if tool_name in TOOLS_NEEDING_USER_TIER:
            LOG.info(f"Tool {tool_name}: passing tier {tier} to handler")
            arguments = {**arguments, "user_tier": tier}
        if consent_question is not None:
            arguments = {**arguments, "consent_question": consent_question}

        debug_logger = safe_get_debug_logger()
        if debug_logger:
            debug_logger.log("tool_call", context.channel_id if context else "unknown", {
                "tool_name": tool_name,
                "arguments": _serialize_for_debug(arguments),
                "tier": tier.value,
            })

        try:
            timeout: float
            if tool_name in BROWSER_TOOLS:
                timeout = BROWSER_OPERATION_BUDGET_SECONDS[tool_name]
            elif tool_name == "generate_image":
                # generate_image waits on the shared GPU semaphore (imagegen.py)
                # *inside* the handler, so the outer timeout must cover
                # semaphore-wait + generation (image_gen.timeout) + margin -
                # a flat override would truncate the queue wait. Each sampling pass costs
                # roughly linear time on the server, so the generation budget
                # (and the per-extra-image term) scales with the steps ratio
                # (steps / default_steps, clamped via normalize_image_steps to
                # max_steps). Multi-image edits additionally cost VAE-encode +
                # attention time, hence the half-timeout term per extra image.
                # Missing/garbage steps arguments fall back to ratio 1.0 so the
                # timeout computation itself can never crash.
                n_images = 1
                input_images_arg = arguments.get("input_images")
                if input_images_arg:
                    if isinstance(input_images_arg, list):
                        n_images = min(len(input_images_arg), config.image_gen.max_input_images)
                    elif isinstance(input_images_arg, str):
                        n_images = 1
                steps_arg = arguments.get("steps")
                if steps_arg is None:
                    steps_ratio = 1.0
                else:
                    try:
                        normalized_steps, _ = normalize_image_steps(
                            steps_arg, config.image_gen.max_steps
                        )
                        steps_ratio = max(
                            normalized_steps / max(config.image_gen.default_steps, 1), 1.0
                        )
                    except ValueError:
                        steps_ratio = 1.0
                timeout = (
                    config.image_gen.max_concurrent_wait_seconds
                    + config.image_gen.timeout * steps_ratio
                    + 30.0
                    + config.image_gen.timeout * 0.5 * steps_ratio * (n_images - 1)
                )
            elif tool_name == "text_to_speech":
                # text_to_speech waits on a per-base_url shared semaphore
                # (tts.py's _get_shared_tts_semaphore) *inside* the handler,
                # same rationale as the generate_image branch above: the
                # outer timeout must cover semaphore-wait + synthesis
                # (engine.timeout) + margin, per engine, or a flat override
                # would truncate the queue wait. Multiple engines may be
                # configured (one per language), so take the max across all
                # of them; fall back to TOOL_TIMEOUT_SECONDS if none are
                # configured (empty dict), avoiding a ValueError from max([]).
                timeout = (
                    max(
                        [
                            e.max_concurrent_wait_seconds + e.timeout
                            for e in config.tts.engines.values()
                        ]
                        or [TOOL_TIMEOUT_SECONDS]
                    )
                    + 30.0
                )
            elif tool_name == "retranscribe_audio":
                # retranscribe_audio waits on a per-base_url shared semaphore
                # (tools/stt_ops.py's _get_alt_semaphore) *inside* the handler,
                # same rationale as generate_image/text_to_speech above, plus
                # it makes up to ceil(max_duration_seconds/chunk_seconds)
                # sequential HTTP calls to the alternate backend - the outer
                # timeout must cover semaphore-wait + every chunk call + margin,
                # or a flat override would kill a legitimately-progressing
                # multi-chunk transcription partway through.
                max_chunks = math.ceil(config.stt.max_duration_seconds / config.stt.chunk_seconds)
                timeout = (
                    max_chunks * config.stt.alternate.timeout
                    + config.stt.alternate.max_concurrent_wait_seconds
                    + 30.0
                )
            elif tool_name == "bash":
                # bash enforces its own (model-chosen) timeout, kills the
                # process group and returns the output collected so far. The
                # outer timeout must fire later, or it would discard that
                # partial output (e.g. job IDs a loop had already submitted).
                timeout = bash_effective_timeout(arguments.get("timeout")) + 5.0
            else:
                timeout = TOOL_TIMEOUT_OVERRIDES.get(tool_name, TOOL_TIMEOUT_SECONDS)
            if asyncio.iscoroutinefunction(handler):
                result = await asyncio.wait_for(
                    handler(**arguments),
                    timeout=timeout,
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(handler, **arguments),
                    timeout=timeout,
                )
            if isinstance(result, ToolResult):
                tool_result = result
            else:
                tool_result = ToolResult(
                    name=tool_name,
                    success=True,
                    output=str(result),
                    error=None,
                )

            # Only successes are cached: a failed search (timeout, backend
            # hiccup) must be retried, not replayed from the cache.
            if tool_name == "web_search" and tool_result.success:
                if len(self._tool_cache) >= MAX_TOOL_CACHE_SIZE:
                    self._tool_cache.popitem(last=False)
                self._tool_cache[cache_key] = CachedToolResult(result=tool_result, timestamp=time.time())

            if debug_logger:
                debug_logger.log("tool_result", context.channel_id if context else "unknown", {
                    "tool_name": tool_name,
                    "success": tool_result.success,
                    "output": tool_result.output[:200] if tool_result.output else None,
                    "error": tool_result.error,
                })

            return tool_result
        except asyncio.TimeoutError:
            LOG.warning(f"Tool '{tool_name}' timed out after {timeout}s")
            self._record_failure(tool_name, arguments)
            if debug_logger:
                debug_logger.record_error(f"tool_timeout:{tool_name}")
            return ToolResult(
                name=tool_name,
                success=False,
                output=None,
                error=f"Tool execution timed out after {timeout} seconds",
            )
        except Exception as e:
            LOG.error(f"Tool '{tool_name}' failed: {e}")
            self._record_failure(tool_name, arguments)
            if debug_logger:
                debug_logger.record_error(f"tool_error:{tool_name}")
            return ToolResult(
                name=tool_name,
                success=False,
                output=None,
                error=str(e),
            )


def get_current_turn_id() -> Optional[str]:
    """Return the turn_id of the currently active Context, if any.

    Returns None if no context has been set (e.g. a tool function called
    directly in a test, outside the normal
    process_message/_process_cronjob/_process_subagent turn lifecycle).
    """
    context = _current_context.get()
    return context.turn_id if context is not None else None


def get_current_browser_scope() -> Optional[str]:
    """Return the browser scope of the currently active Context, if any.

    Used by browse.py to pick the BrowserContext/page for the running turn
    (channel scope for chat, an ephemeral per-run scope for cronjobs and
    subagents - see Context.browser_scope). Returns None if no context has
    been set; callers then fall back to the channel (chat) scope.
    """
    context = _current_context.get()
    return context.browser_scope if context is not None else None


def get_current_turn_output() -> Optional[TurnOutput]:
    """Return the running turn's TurnOutput (single-message mode), if any."""
    context = _current_context.get()
    turn_output = getattr(context, "turn_output", None)
    return turn_output if isinstance(turn_output, TurnOutput) else None
