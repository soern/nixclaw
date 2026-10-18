"""Core agent loop for nixclaw-agent."""

import asyncio
import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import re
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from .auth import AuthManager
from .debug_log import safe_get_debug_logger
from . import prompts
from .config import config, plan_config_reload, apply_config_reload
from .consent import ConsentDecision, ConsentManager, PendingConsent
from .context import ContextManager
from .cron import CronScheduler
from .db.operations import DatabaseOperations
from .llm import LLMClient, VLLMClient, _tool_result_content, with_attachment_note
from .memory import MemoryManager
from .signal import SignalClient
from .soul import soul_manager
from .stt import FasterWhisperClient, is_audio_attachment, sweep_expired_audio, transcribe_attachment
from .tools.browse import close_browser_scope
from .tools.mute import is_channel_muted, mute_channel as mute_channel_tool, unmute_channel as unmute_channel_tool
from .tools.channel_mode import is_channel_mode_always_process
from .tools.message_mode import resolve_message_mode
from .tools.registry import ToolRegistry
from .tool_markup import chat_with_markup_guard
from .turn_output import TurnOutput
from .types import Attachment, ConsentRequest, Context, Event, Message, PollVote, PreprocessedMessage, ToolCall, ToolResult, UserTier
from .web.auth import parse_web_command
from .web.channels import GROUP_PREFIX

if TYPE_CHECKING:
    from .web.auth import WebAuth
    from .web.bus import ChatBus
    from .web.channels import MembershipCache

LOG = logging.getLogger(__name__)

ISSUE_MARKER = "__ISSUE_DETECTED__"
DISCARD_MARKER = "__DISCARD_OUTPUT__"
CONTINUE_KEYWORD = "__AGENT_CONTINUE__"
MAX_CONTINUATION_TURNS = 10
CONTINUATION_TRIGGER = "__CONTINUE__"


def _extract_mute_keyword(response: str) -> str | None:
    """Extract __MUTE__ or __UNMUTE__ from response (case-insensitive)."""
    if re.search(r'__MUTE__', response, re.IGNORECASE):
        return "MUTE"
    if re.search(r'__UNMUTE__', response, re.IGNORECASE):
        return "UNMUTE"
    return None


def _strip_mute_keyword(response: str) -> str:
    """Remove __MUTE__ or __UNMUTE__ from response."""
    result = re.sub(r'__MUTE__\s*', '', response, flags=re.IGNORECASE)
    result = re.sub(r'__UNMUTE__\s*', '', result, flags=re.IGNORECASE)
    return result.strip()


def _extract_discard_keyword(response: str) -> bool:
    """Check if __DISCARD_OUTPUT__ is present in response (case-insensitive)."""
    return bool(re.search(r'__DISCARD_OUTPUT__', response, re.IGNORECASE))


_mention_char_pattern = re.compile(r'\ufffc')


@lru_cache(maxsize=8)
def _compile_mention_pattern(bot_name: str) -> "re.Pattern[str]":
    return re.compile(r'@?' + re.escape(bot_name) + r'\b', re.IGNORECASE)


def _mention_name_pattern() -> "re.Pattern[str]":
    """Name pattern for the current config.agent.name (cached per name, so a reload takes effect)."""
    return _compile_mention_pattern(config.agent.name.lower())


def is_mentioned(event: Event) -> bool:
    """Check if bot was mentioned in group message."""
    if event.group_id is None:
        return True
    if config.signal.phone_number in (event.mentions or []):
        return True
    if _mention_name_pattern().search(event.message):
        return True
    return False


def strip_mention(message: str) -> str:
    """Remove mention artifacts from message."""
    cleaned = _mention_char_pattern.sub('', message)
    cleaned = _mention_name_pattern().sub('', cleaned)
    return ' '.join(cleaned.split())


def _channel_id_for_event(event: Event) -> tuple[str, str]:
    """Derive (channel_id, reply_destination) for an event.

    Group messages use the shared group destination as the channel; 1:1
    messages use the sender. The two are always equal today - kept as a
    pair (rather than collapsed to one value) because call sites reason
    about them as conceptually distinct ("which channel is this turn for"
    vs "where does the reply go"), matching the split already used
    throughout process_message.
    """
    if event.group_id is not None:
        return event.destination, event.destination
    return event.source, event.source


def _strip_discard_keyword(response: str) -> str:
    """Remove __DISCARD_OUTPUT__ from response."""
    return re.sub(r'__DISCARD_OUTPUT__\s*', '', response, flags=re.IGNORECASE).strip()


def _extract_continue_keyword(response: str) -> bool:
    """Check if __AGENT_CONTINUE__ is present in response (case-insensitive)."""
    return bool(re.search(rf'{CONTINUE_KEYWORD}', response, re.IGNORECASE))


def _strip_continue_keyword(response: str) -> str:
    """Remove __AGENT_CONTINUE__ from response."""
    return re.sub(rf'{CONTINUE_KEYWORD}\s*', '', response, flags=re.IGNORECASE).strip()


@dataclass
class ReloadResult:
    """Outcome of AgentLoop.reload()."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)

    def format_reply(self) -> str:
        if not self.ok:
            lines = ["Reload failed; previous config and prompts are still active:"]
            lines += [f"- {e}" for e in self.errors]
            return "\n".join(lines)
        lines = ["Reload OK. Applied: " + ", ".join(self.applied) + "."]
        if self.restart_required:
            lines.append("Changed but restart required: " + ", ".join(self.restart_required) + ".")
        return "\n".join(lines)


MAX_CONCURRENT_TASKS = 10
# Approval polls (consent.py): tool error when the poll could not be posted.
CONSENT_POLL_FAILED_ERROR = "Could not ask the users for approval; the command was not run."
# Approved background command: output posted verbatim on success (cap), and
# the output put into the follow-up cronjob's prompt on failure (cap, tail).
CONSENT_OUTPUT_POST_MAX_CHARS = 1500
CONSENT_INSPECTION_OUTPUT_MAX_CHARS = 3000
TOOL_TIMEOUT_SECONDS = 30
MAX_MESSAGES_PER_SESSION = 50
MAX_QUEUED_INBOUND_PER_CHANNEL = 5
READ_RECEIPT_TIMEOUT_SECONDS = 5
MAX_TOOL_CACHE_SIZE = 20
MAX_REPEATED_TOOL_CALLS = 3
MAX_REPEATED_BROWSER_CALLS = 20
MAX_EMPTY_RESPONSE_NUDGES = 3
NUDGE_ERROR_MAX_CHARS = 300
SUBAGENT_COOLDOWN_SECONDS = 60
MAX_SUBAGENT_PER_SESSION = 3
# Wall-clock budget for handing task creation over to the event-loop thread
# via call_soon_threadsafe (see spawn_subagent_task) and getting the
# resulting asyncio.Task back. The scheduled callback itself is trivial
# (create_task + dict writes, no awaiting the subagent's actual work), so
# this only needs to cover how long the loop might be busy with something
# else before it gets around to running the callback.
SPAWN_SUBAGENT_REGISTER_TIMEOUT_SECONDS = 10
LOOP_DETECTION_THRESHOLD = 3
LOOP_DETECTION_WINDOW_SECONDS = 60

RATE_LIMITS_PER_TIER: dict[UserTier, int] = {
    UserTier.PRIVILEGED: 10,
    UserTier.KNOWN: 5,
    UserTier.UNKNOWN: 2,
}

MAX_TOOL_ITERATIONS_PER_TIER: dict[UserTier, int] = {
    UserTier.PRIVILEGED: 100,
    UserTier.KNOWN: 30,
    UserTier.UNKNOWN: 20,
}


def _get_rate_limit_for_tier(tier: UserTier) -> int:
    return RATE_LIMITS_PER_TIER.get(tier, RATE_LIMITS_PER_TIER[UserTier.UNKNOWN])


def _get_max_tool_iterations_for_tier(tier: UserTier) -> int:
    return MAX_TOOL_ITERATIONS_PER_TIER.get(tier, MAX_TOOL_ITERATIONS_PER_TIER[UserTier.UNKNOWN])


VOICE_CALL_HIDDEN_TOOLS = frozenset({"text_to_speech"})
# Only offered to interactive turns while a voice call is active.
VOICE_CALL_ONLY_TOOLS = frozenset({"end_call"})


def _append_to_last_user_message(messages: list[dict], note: str) -> None:
    """Append `note` to the newest user message (in-flight only, not stored)."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = f"{content}\n\n{note}"
        elif isinstance(content, list):
            content.append({"type": "text", "text": note})
        return


def _check_and_filter_issue_marker(response_text: str, channel_id: str) -> str:
    """Check for __ISSUE_DETECTED__ marker, flush buffer if found, filter from output."""
    if ISSUE_MARKER not in response_text:
        return response_text
    
    debug_logger = safe_get_debug_logger()
    if debug_logger:
        debug_logger.flush_buffer(trigger="llm_reported_issue")
    
    return response_text.replace(ISSUE_MARKER, "").strip()


class _ToolCallCache:
    """Per-run tool result cache + repeat guard for cronjob and subagent
    runs, with the same rules as the chat turn's loop in process_message:
    only successes are cached (a retry after a transient error must
    re-execute, not replay the error), at most MAX_TOOL_CACHE_SIZE entries
    (oldest evicted), and past MAX_REPEATED_*_CALLS identical calls the
    model gets a hint as the tool result instead of the call."""

    def __init__(self) -> None:
        self._results: OrderedDict[str, ToolResult] = OrderedDict()
        self._counts: dict[str, int] = {}

    @staticmethod
    def _key(tc: ToolCall) -> str:
        return f"{tc.name}:{str(tc.arguments)}"

    def lookup(self, tc: ToolCall) -> Optional[ToolResult]:
        """The result to use instead of executing tc, or None to execute it."""
        key = self._key(tc)
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        max_allowed = MAX_REPEATED_BROWSER_CALLS if tc.name.startswith("browser_") else MAX_REPEATED_TOOL_CALLS
        if count > max_allowed:
            LOG.warning(f"Repeated tool call '{tc.name}' with same arguments detected ({count} times)")
            hint = prompts.current().render("repeated-tool-call-hint", tool_name=tc.name, count=count)
            return ToolResult(name=tc.name, success=False, output=None, error=hint)
        cached = self._results.get(key)
        if cached is not None:
            self._results.move_to_end(key)
        return cached

    def store(self, tc: ToolCall, result: ToolResult) -> None:
        if not result.success:
            return
        if len(self._results) >= MAX_TOOL_CACHE_SIZE:
            self._results.popitem(last=False)
        self._results[self._key(tc)] = result


class AgentLoop:
    def __init__(
        self,
        signal_client: SignalClient,
        llm_client: LLMClient | VLLMClient,
        auth_manager: AuthManager,
        context_manager: ContextManager,
        tool_registry: ToolRegistry,
        cron_scheduler: CronScheduler,
        membership: Optional["MembershipCache"] = None,
    ):
        self._signal_client = signal_client
        # Signal group membership for the unknown-sender gate
        # (_unknown_sender_allowed); None = groups fail closed for unknown senders.
        self._membership = membership
        self._llm_client = llm_client
        self._auth_manager = auth_manager
        self._context_manager = context_manager
        self._tool_registry = tool_registry
        self._cron_scheduler = cron_scheduler
        self._db_ops = DatabaseOperations()
        self._shutdown = False
        self._last_stt_sweep_at: float = 0.0
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._tasks: set[asyncio.Task] = set()
        # Per-channel turn-serialization gate (fix/channel-turn-serialization,
        # split into two independent lanes by fix/decouple-cron-chat-lanes):
        # at most one INTERACTIVE (chat) turn is considered "active" per
        # channel_id at a time, tracked here. See _dispatch_or_queue/
        # _register_channel_task/_on_channel_task_done below and
        # doc/ARCHITECTURE.md's Concurrency section.
        self._channel_active_task: dict[str, asyncio.Task] = {}
        # Independent BACKGROUND lane: at most one of {cronjob, subagent} runs
        # per channel_id at a time, but a background task no longer blocks
        # (or is blocked by) live chat on the same channel - see
        # _register_channel_task/_on_channel_task_done. _channel_background_meta
        # records {"source_type": "cronjob"|"subagent", "source_id": <id>} for
        # whatever currently occupies the lane, set right before the task
        # starts and cleared in its done-callback; it backs both
        # stop_running_activity (id validation) and the live status note
        # injected into the chat system prompt (see process_message).
        self._channel_background_task: dict[str, asyncio.Task] = {}
        self._channel_background_meta: dict[str, dict] = {}
        # Inbound Signal events that arrived while their channel's turn was
        # already active; drained by the active turn's tool loop at each
        # iteration checkpoint (_drain_inbound_checkpoint) or, failing that,
        # by _on_channel_task_done once the turn finishes. NOT to be confused
        # with self._channel_message_queue below, which queues OUTGOING
        # replies that hit the per-channel send rate limit. Only ever
        # populated/drained for the INTERACTIVE lane now - background tasks
        # run to completion or explicit cancellation undisturbed by chat.
        self._channel_inbound_queue: dict[str, list[Event]] = {}
        # (sender, signal_timestamp_ms) of inbound messages handled by the
        # channel's current interactive turn; read receipts are sent for them
        # only once that turn ends (see _flush_read_receipts).
        self._pending_read_receipts: dict[str, list[tuple[str, int]]] = {}
        self._messages_sent: dict[str, int] = {}
        self._repeat_counter: dict[str, int] = {}
        self._session_start: datetime = datetime.now()
        self._memory_managers: dict[str, MemoryManager] = {}
        self._stt_client = FasterWhisperClient(config.stt)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subagent_cooldown: dict[str, datetime] = {}
        self._subagent_session_count: dict[str, int] = {}
        self._muted_channels: set[str] = set()
        self._always_process_channels: set[str] = set()
        self._channel_message_timestamps: dict[str, list[datetime]] = {}
        self._channel_recent_requests: dict[str, list[tuple[str, datetime]]] = {}
        self._channel_message_queue: dict[str, list[str]] = {}
        self._summarization_due: dict[str, bool] = {}
        self._continuation_turns_remaining: dict[str, int] = {}
        # Approval polls (consent.py). A chat turn paused on a poll waits on
        # its channel's wake event, which _enqueue_inbound sets.
        self._consent = ConsentManager(signal_client, auth_manager, self._db_ops)
        self._consent.on_background_decision = self._on_background_consent_decision
        self._inbound_wake: dict[str, asyncio.Event] = {}
        # Web interface (web/), set by attach_web when webui.enabled.
        self._chat_bus: Optional["ChatBus"] = None
        self._web_auth: Optional["WebAuth"] = None
        self._web_mirror_locks: dict[str, asyncio.Lock] = {}
        self._web_tasks: set[asyncio.Task] = set()
        # Active voice calls: channel_id -> speak(text) (non-blocking queue).
        self._voice_speakers: dict[str, Callable[[str], None]] = {}
        self._voice_seq = 0

    async def _handle_mute_unmute(self, response_text: str, channel_id: str) -> str:
        """Check for __MUTE__/__UNMUTE__ keywords and toggle mute state. Returns stripped response."""
        keyword = _extract_mute_keyword(response_text)
        if keyword == "MUTE":
            stripped = _strip_mute_keyword(response_text)
            mute_channel_tool(channel_id)
            self._muted_channels.add(channel_id)
            LOG.info(f"__MUTE__ detected in {channel_id}, muting channel")
            return stripped
        elif keyword == "UNMUTE":
            stripped = _strip_mute_keyword(response_text)
            unmute_channel_tool(channel_id)
            self._muted_channels.discard(channel_id)
            LOG.info(f"__UNMUTE__ detected in {channel_id}, unmuting channel")
            return stripped
        return response_text

    def _should_discard_output(self, response_text: str, channel_id: str, source: str = "user") -> bool:
        """Check if output should be discarded based on __DISCARD_OUTPUT__ marker.

        Only applies when source is 'cronjob' or 'subagent'. User messages are never discarded.
        This allows cronjob and subagent background tasks to沉默 (stay silent) when there's nothing to report.
        """
        if source not in ("cronjob", "subagent"):
            return False
        if _extract_discard_keyword(response_text):
            LOG.info(f"__DISCARD_OUTPUT__ detected in {channel_id} (source={source}), discarding output")
            return True
        return False

    def _strip_leaked_discard_marker(self, response_text: str, channel_id: str) -> str:
        """Strip a __DISCARD_OUTPUT__ marker that leaked into a live chat reply.

        The marker is only meaningful for cronjob/subagent output (see
        _should_discard_output - chat is never silently discarded). If a
        tool result handed the model text that looked like cron-output-
        control instructions (e.g. list_cronjobs returning a job's raw
        prompt, which itself said "include __DISCARD_OUTPUT__ if nothing
        changed"), a small model can echo the marker into an interactive
        reply, and without this it gets forwarded to the user verbatim
        instead of being interpreted as a keyword.
        """
        if _extract_discard_keyword(response_text):
            LOG.warning(f"__DISCARD_OUTPUT__ leaked into a chat reply for {channel_id}; stripping")
            return _strip_discard_keyword(response_text)
        return response_text

    def _store_background_output(self, channel_id: str, message: str, source_type: str, source_id: str, timestamp: Optional[int] = None, is_discarded: bool = False) -> None:
        """Store background task (cronjob/subagent) output to database."""
        from .types import Event, Message as TypesMessage
        if timestamp is None:
            import time
            timestamp = int(time.time())
        stripped_content = _strip_discard_keyword(message) if is_discarded else message
        msg = TypesMessage(
            event=Event(
                source=f"{source_type}:{source_id}",
                destination=channel_id,
                message=stripped_content,
                timestamp=timestamp,
                group_id=None,
                attachments=[],
            ),
            is_outgoing=True,
            processed=True,
            source_type=source_type,
            source_id=source_id,
            is_discarded=is_discarded,
        )
        self._db_ops.store_message(msg, channel_id, source_type=source_type, source_id=source_id, is_discarded=is_discarded)

    async def _flush_channel_queue(self, channel_id: str, tier: UserTier) -> None:
        """Flush any queued messages for a channel.

        This should be called after background operations (cron, subagent) complete,
        since these paths don't trigger _on_signal_message which would otherwise flush.
        """
        if channel_id not in self._channel_message_queue or not self._channel_message_queue[channel_id]:
            return
        LOG.info(f"Flushing {len(self._channel_message_queue[channel_id])} queued messages for {channel_id}")
        queued = self._channel_message_queue[channel_id]
        self._channel_message_queue[channel_id] = []
        for queued_msg in queued:
            await self._send_with_retry(channel_id, queued_msg, tier)

    def _get_memory_manager(self, channel_id: str) -> MemoryManager:
        """Get or create MemoryManager for channel."""
        if channel_id not in self._memory_managers:
            from .config import config
            memory_dir = config.paths.data_dir / channel_id / "memories"
            self._memory_managers[channel_id] = MemoryManager(channel_id, memory_dir)
        return self._memory_managers[channel_id]

    def _format_memories_for_prompt(self, memories: list[dict]) -> str:
        """Format memories for inclusion in system prompt."""
        if not memories:
            return ""
        lines = []
        for m in memories:
            lines.append(f"- [{m['type']}] {m['content']}")
        return "\n".join(lines)

    def _check_rate_limit(self, channel_id: str, tier: UserTier) -> bool:
        """Check if channel has exceeded rate limit. Returns True if OK to send."""
        now = datetime.now()
        rate_limit = _get_rate_limit_for_tier(tier)
        if channel_id not in self._channel_message_timestamps:
            self._channel_message_timestamps[channel_id] = []
        timestamps = self._channel_message_timestamps[channel_id]
        cutoff = now.timestamp() - 60
        self._channel_message_timestamps[channel_id] = [
            ts for ts in timestamps if ts.timestamp() > cutoff
        ]
        if len(self._channel_message_timestamps[channel_id]) >= rate_limit:
            LOG.warning(f"Rate limit exceeded for {channel_id}: {rate_limit} msg/min (tier: {tier.value})")
            return False
        self._channel_message_timestamps[channel_id].append(now)
        return True

    def _check_loop_detection(self, channel_id: str, message: str) -> bool:
        """Check for repeated similar requests. Returns True if OK to respond."""
        now = datetime.now()
        if channel_id not in self._channel_recent_requests:
            self._channel_recent_requests[channel_id] = []
        requests = self._channel_recent_requests[channel_id]
        cutoff = now.timestamp() - LOOP_DETECTION_WINDOW_SECONDS
        self._channel_recent_requests[channel_id] = [
            (msg, ts) for msg, ts in requests if ts.timestamp() > cutoff
        ]
        normalized = message.lower().strip()
        same_request_count = sum(
            1 for msg, ts in self._channel_recent_requests[channel_id]
            if ts.timestamp() > cutoff and msg == normalized
        )
        if same_request_count >= LOOP_DETECTION_THRESHOLD:
            LOG.warning(f"Loop detected for {channel_id}: same request '{normalized}' repeated {same_request_count + 1} times")
            return False
        self._channel_recent_requests[channel_id].append((normalized, now))
        return True

    def _load_channel_modes(self) -> None:
        """Load all channels that are in always_process mode."""
        data_dir = config.paths.data_dir
        if data_dir.exists():
            for channel_dir in data_dir.iterdir():
                if channel_dir.is_dir():
                    try:
                        if is_channel_mode_always_process(channel_dir.name):
                            self._always_process_channels.add(channel_dir.name)
                    except Exception as e:
                        LOG.warning(f"Failed to load channel mode for {channel_dir.name}: {e}")

    async def run(self) -> None:
        """Main event loop - runs until shutdown."""
        self._loop = asyncio.get_running_loop()
        self._load_channel_modes()
        # Before the webhook starts: polls left open by the previous process
        # can no longer be answered.
        await self._consent.cleanup_at_startup()
        await self._signal_client.start_webhook(self._on_signal_message, on_poll_vote=self._consent.on_vote)

        LOG.info("AgentLoop started, listening for messages...")

        while not self._shutdown:
            try:
                due_jobs = self._cron_scheduler.check_due()
                for job in due_jobs:
                    if self._shutdown:
                        break
                    active_task = self._channel_background_task.get(job.channel_id)
                    if active_task is not None and not active_task.done():
                        # Skip this tick rather than queue: check_due() no
                        # longer marks the row 'running' up front (see its
                        # docstring), so a skipped job is simply left
                        # pending/failed and the scheduler's next poll (60s)
                        # naturally retries it once the background lane
                        # frees up. Only blocked by another background job
                        # now - never by live chat on the same channel.
                        LOG.info(f"Skipping cronjob {job.id} for {job.channel_id}: a background task is already active, will retry next poll")
                        continue
                    self._cron_scheduler.mark_running(job.id)
                    task = asyncio.create_task(self._process_cronjob(job))
                    self._register_channel_task(
                        job.channel_id, task, lane="background",
                        meta={"source_type": "cronjob", "source_id": job.id},
                    )

                # Throttled to every 5 minutes (not every 60s tick) - a full
                # data_dir walk is cheap but pointless at 1x/min against a
                # 1h retention window (config.stt.retention_hours).
                if time.monotonic() - self._last_stt_sweep_at >= 300:
                    self._last_stt_sweep_at = time.monotonic()
                    try:
                        # sweep_expired_audio() is a synchronous directory
                        # walk (iterdir/stat/unlink across every channel's
                        # attachments dir) - run it off the event loop thread
                        # so it can't stall webhook handling/chat turns/cron
                        # dispatch while it works.
                        removed = await asyncio.to_thread(
                            sweep_expired_audio, config.paths.data_dir, config.stt.retention_hours
                        )
                        if removed:
                            LOG.info(f"STT cleanup: removed {removed} expired audio file(s)")
                    except Exception as e:
                        LOG.warning(f"STT cleanup sweep failed: {e}")

                await asyncio.sleep(60)
            except Exception as e:
                LOG.error(f"Error in main loop: {e}")

        await asyncio.gather(*self._tasks, return_exceptions=True)
        LOG.info("AgentLoop shut down")

    async def _on_signal_message(self, event: Event) -> None:
        """Callback registered with SignalClient webhook.

        If no turn is currently active for this event's channel, starts one
        immediately (as before, via a semaphore-gated task). Otherwise,
        queues the event: the active turn's tool loop drains the queue at
        each iteration checkpoint (see _drain_inbound_checkpoint), so e.g. a
        "stop" sent mid-task reaches the ORIGINAL loop instead of just
        spawning an independent task that races it.
        """
        channel_id, _ = _channel_id_for_event(event)
        # Unknown-sender gate: before anything is stored, published or run.
        if not await self._unknown_sender_allowed(event, channel_id):
            return
        # Web interface history: published once at entry (also stop/reset/
        # duplicates). Our own account's messages (linked-device echoes of
        # what we sent) are skipped.
        if self._chat_bus is not None and event.source != config.signal.phone_number:
            try:
                event.chat_log_id = self._chat_bus.publish_inbound(event, channel_id, origin="signal")
            except Exception as e:
                LOG.warning(f"Publishing inbound message to the web interface failed: {e}")
        self._dispatch_or_queue(channel_id, event)

    async def _unknown_sender_allowed(self, event: Event, channel_id: str) -> bool:
        """Whether a Signal message may be handled at all (auth.unknown_users).

        KNOWN/PRIVILEGED senders and the bot's own number (linked-device /
        Note-to-Self sync messages) always pass. An UNKNOWN sender passes in
        a 1:1 chat only with allow_direct; in a group only with
        allow_in_groups AND a KNOWN/PRIVILEGED member (other than the bot)
        in the group's cached member list. Everything else is dropped
        silently (one log line, no message content). Settings are read per
        call so a reload applies immediately.
        """
        bot_number = config.signal.phone_number
        if event.source == bot_number:
            return True
        if self._auth_manager.get_user_tier(event.source) != UserTier.UNKNOWN:
            return True
        settings = config.auth.unknown_users
        if event.group_id is None:
            if settings.allow_direct:
                return True
            reason = "unknown_users.allow_direct is false"
        elif not settings.allow_in_groups:
            reason = "unknown_users.allow_in_groups is false"
        else:
            members: Optional[set[str]] = None
            if self._membership is not None:
                try:
                    members = await self._membership.group_members(channel_id)
                except Exception as e:
                    LOG.warning(f"Group membership lookup for {channel_id} failed: {e}")
            if members is None:
                reason = "group membership unknown"
            elif any(m != bot_number and self._auth_manager.get_user_tier(m) != UserTier.UNKNOWN
                     for m in members):
                return True
            else:
                reason = "no KNOWN/PRIVILEGED member in the group"
        LOG.info(f"Dropped message from unknown sender {event.source} in {channel_id}: {reason}")
        return False

    # --- Web interface (web/api.py) -------------------------------------------

    def attach_web(self, bus: "ChatBus", web_auth: "WebAuth") -> None:
        """Enable web-interface publishing and the /status and /deauth commands."""
        self._chat_bus = bus
        self._web_auth = web_auth

    def submit_web_message(self, number: str, channel_id: str, text: str,
                           attachments: list[Attachment]) -> int:
        """A message typed in the web interface: publish it, mirror it to
        Signal ("🌐 ...") and run it through the normal inbound path
        (_dispatch_or_queue -> _preprocess_incoming), exactly like a Signal
        message from `number`. `attachments` are already saved in the
        channel's attachments dir (local_path set). Returns the chat_log id.
        """
        assert self._chat_bus is not None
        is_group = channel_id != number
        now = time.time()
        event = Event(
            source=number,
            destination=channel_id if is_group else config.signal.phone_number,
            message=text if text.strip() else "[Attachment]",
            timestamp=int(now),
            group_id=channel_id[len(GROUP_PREFIX):] if is_group else None,
            attachments=list(attachments),
            source_name=self._chat_bus.display_name(number),
            origin="web",
        )
        event.chat_log_id = self._chat_bus.publish_inbound(event, channel_id, origin="web")
        self._dispatch_or_queue(channel_id, event)
        task = asyncio.create_task(self._send_web_mirror(channel_id, event, text))
        self._web_tasks.add(task)
        task.add_done_callback(self._web_tasks.discard)
        return event.chat_log_id

    def register_voice_speaker(self, channel_id: str, speak: Callable[[str], None]) -> None:
        """While a call is active, interactive turn texts in `channel_id` are
        also passed to `speak` (they are still sent as Signal text)."""
        self._voice_speakers[channel_id] = speak

    def unregister_voice_speaker(self, channel_id: str) -> None:
        self._voice_speakers.pop(channel_id, None)

    def is_turn_active(self, channel_id: str) -> bool:
        """A chat turn (interactive lane) is running or queued for `channel_id`."""
        task = self._channel_active_task.get(channel_id)
        return task is not None and not task.done()

    def submit_voice_message(self, number: str, text: str) -> None:
        """An utterance transcribed on a 1:1 voice call: publish it (web),
        mirror it to Signal ("🎙️ ...") and run it through the normal inbound
        path, like a Signal message from `number`."""
        self._voice_seq += 1
        event = Event(
            source=number,
            destination=config.signal.phone_number,
            message=text,
            timestamp=int(time.time()),
            group_id=None,
            source_name=self._chat_bus.display_name(number) if self._chat_bus else None,
            origin="voice",
        )
        event.voice_id = f"{time.time_ns()}-{self._voice_seq}"
        if self._chat_bus is not None:
            event.chat_log_id = self._chat_bus.publish_inbound(event, number, origin="voice")
        self._dispatch_or_queue(number, event)
        task = asyncio.create_task(self._send_voice_mirror(number, text))
        self._web_tasks.add(task)
        task.add_done_callback(self._web_tasks.discard)

    async def _send_voice_mirror(self, channel_id: str, text: str) -> None:
        """Signal copy of the caller's words (publish=False: the web already
        shows the inbound row). Shares the web mirror's per-channel lock."""
        lock = self._web_mirror_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            try:
                caption = prompts.current().render("voice-mirror", text=text).strip()
                await self._signal_client.send_message(channel_id, caption, publish=False)
            except Exception as e:
                LOG.warning(f"Mirroring voice transcript to Signal ({channel_id}) failed: {e}")

    def _web_mirror_text(self, event: Event, text: str) -> str:
        p = prompts.current()
        if event.group_id is None:
            return p.render("web-mirror-direct", text=text).strip()
        sender = event.source_name or event.source
        return p.render("web-mirror-group", sender=sender, text=text).strip()

    async def _send_web_mirror(self, channel_id: str, event: Event, text: str) -> None:
        """Post the 🌐 copy of a web message to Signal (publish=False: not
        shown on the web again). Serialized per channel so the copies keep
        the order the messages were sent in. Errors are only logged."""
        lock = self._web_mirror_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            try:
                caption = self._web_mirror_text(event, text)
                if not event.attachments:
                    await self._signal_client.send_message(channel_id, caption, publish=False)
                    return
                prefix_only = self._web_mirror_text(event, "")
            except Exception as e:
                LOG.warning(f"Mirroring web message to Signal ({channel_id}) failed: {e}")
                return
            for i, att in enumerate(event.attachments):
                if not att.local_path:
                    continue
                try:
                    await self._signal_client.send_attachment(
                        channel_id, att.local_path, att.content_type,
                        caption=caption if i == 0 else prefix_only,
                        publish=False,
                    )
                except Exception as e:
                    LOG.warning(f"Mirroring attachment {att.filename or att.id} to Signal ({channel_id}) failed: {e}")

    async def on_web_poll_vote(self, vote: PollVote) -> None:
        """A poll vote cast in the web interface (same rules as Signal votes)."""
        await self._consent.on_vote(vote)

    def _dispatch_or_queue(self, channel_id: str, event: Event) -> None:
        """Start a new turn for `event` if channel_id is free, else queue it.

        IMPORTANT: no `await` may be introduced between the busy-check and
        the action taken (queue vs. register) - asyncio's single-threaded
        cooperative scheduling is what makes this atomic and race-free.
        Splitting this across an await point would reopen the exact race
        this fix closes.

        Shared by _on_signal_message (fresh inbound webhook events) and
        _on_channel_task_done (re-checking once a turn frees up, in case a
        message queued after the turn's last checkpoint drain but before it
        fully finished).
        """
        active_task = self._channel_active_task.get(channel_id)
        if active_task is not None and not active_task.done():
            self._enqueue_inbound(channel_id, event)
            return
        self._start_channel_task(channel_id, event)

    def _enqueue_inbound(self, channel_id: str, event: Event) -> None:
        """Queue an inbound event for a channel whose turn is already running.

        Capped at MAX_QUEUED_INBOUND_PER_CHANNEL (oldest dropped once full);
        a consecutive identical message from the same sender is deduped
        in-place rather than queued again. The raw event is persisted
        immediately (same store_message pattern used elsewhere in this
        module) so it isn't lost even though full processing - dedup,
        attachment download, STT, mention gating - only happens later, when
        the active turn drains the queue (_preprocess_incoming gives it
        identical treatment at that point).
        """
        queue = self._channel_inbound_queue.setdefault(channel_id, [])
        if queue and queue[-1].source == event.source and queue[-1].message == event.message:
            LOG.debug(f"Deduping consecutive identical queued message for {channel_id} from {event.source}")
        else:
            if len(queue) >= MAX_QUEUED_INBOUND_PER_CHANNEL:
                dropped = queue.pop(0)
                LOG.warning(f"Inbound queue full for {channel_id} ({MAX_QUEUED_INBOUND_PER_CHANNEL}), dropping oldest queued message from {dropped.source}")
            queue.append(event)
            LOG.info(f"Channel {channel_id} busy, queued inbound message from {event.source} (queue depth {len(queue)})")

        self._db_ops.store_message(Message(event=event, is_outgoing=False, processed=False), channel_id)
        wake = self._inbound_wake.get(channel_id)
        if wake is not None:
            wake.set()

    def _start_channel_task(self, channel_id: str, event: Event) -> None:
        """Start a new turn-processing task for `event` and register it as
        channel_id's active turn (see _register_channel_task)."""
        async def process_with_semaphore() -> None:
            async with self._semaphore:
                try:
                    await self.process_message(event)
                except asyncio.CancelledError:
                    # Shutdown: no read receipts for an unfinished turn
                    self._pending_read_receipts.pop(channel_id, None)
                    raise
                except Exception as e:
                    LOG.error(f"Error processing event from {event.source}: {e}")
                await self._flush_read_receipts(channel_id)

        task = asyncio.create_task(process_with_semaphore())
        self._register_channel_task(channel_id, task)

    def _queue_read_receipt(self, channel_id: str, event: Event) -> None:
        """Remember `event` for a read receipt at the end of the current turn."""
        if not config.chat.read_receipts or event.signal_timestamp_ms is None:
            return
        if event.source == config.signal.phone_number:
            return
        self._pending_read_receipts.setdefault(channel_id, []).append(
            (event.source, event.signal_timestamp_ms)
        )

    async def _flush_read_receipts(self, channel_id: str) -> None:
        """Send read receipts for everything the finished turn handled.

        Called after the turn's last reply, so "read" marks the agent as done.
        Short timeout so a hung API doesn't hold up the next queued turn.
        """
        pending = self._pending_read_receipts.pop(channel_id, None)
        if not pending:
            return
        try:
            # asyncio.timeout, not wait_for: on Python 3.11 wait_for drops a
            # cancel that arrives as the gather completes (a turn cancelled
            # while pausing on a consent poll would then wait forever).
            async with asyncio.timeout(READ_RECEIPT_TIMEOUT_SECONDS):
                await asyncio.gather(
                    *(self._signal_client.send_read_receipt(sender, ts)
                      for sender, ts in dict.fromkeys(pending)),
                    return_exceptions=True,
                )
        except Exception as e:
            LOG.warning(f"Sending read receipts for {channel_id} failed: {e}")

    def _register_channel_task(
        self,
        channel_id: str,
        task: asyncio.Task,
        lane: Literal["interactive", "background"] = "interactive",
        meta: Optional[dict] = None,
    ) -> asyncio.Task:
        """Track `task` in self._tasks and record it as channel_id's active
        task in the given lane (self._channel_active_task for "interactive",
        self._channel_background_task/_channel_background_meta for
        "background"), wiring up _on_channel_task_done for lane-scoped
        cleanup.

        `meta` is required for the "background" lane (see
        self._channel_background_meta's docstring in __init__) and ignored
        for "interactive".

        Callers create the task themselves (plain asyncio.create_task vs.
        self._loop.create_task - spawn_subagent_task runs off the event-loop
        thread, inside a tool call executed via asyncio.to_thread, so it
        needs the stored loop reference) and pass it in here.
        """
        self._tasks.add(task)
        if lane == "interactive":
            self._channel_active_task[channel_id] = task
        else:
            self._channel_background_task[channel_id] = task
            self._channel_background_meta[channel_id] = meta or {}

        def _on_done(t: asyncio.Task, cid: str = channel_id, ln: Literal["interactive", "background"] = lane) -> None:
            self._on_channel_task_done(t, cid, ln)

        task.add_done_callback(_on_done)
        return task

    def _on_channel_task_done(
        self,
        task: asyncio.Task,
        channel_id: str,
        lane: Literal["interactive", "background"] = "interactive",
    ) -> None:
        """Deregister a finished task from its lane and, for the interactive
        lane only, drain anything stranded in the inbound queue.

        Only clears the lane's dict entry for channel_id if `task` is still
        the registered owner (defensive identity check - two lanes are now
        fully independent, so the interactive/background "takeover" case
        this used to guard against no longer exists, but a stale callback
        firing after a newer task already claimed the slot is still worth
        guarding against).

        The inbound-queue drain only applies to the interactive lane: a
        non-owner finishing must NOT touch the queue, since it could pop the
        oldest queued event and re-enqueue it via
        _dispatch_or_queue/_enqueue_inbound while the channel is still busy
        (owned by someone else) - that append-at-end path would put the
        oldest message BEHIND newer ones still in the queue, a FIFO-ordering
        violation. Whoever does own the slot will hit its own checkpoint (or
        its own done-callback) and drain the queue in order. The background
        lane never touches the inbound queue at all (see
        self._channel_inbound_queue's docstring in __init__).
        """
        self._tasks.discard(task)

        if lane == "background":
            if self._channel_background_task.get(channel_id) is task:
                del self._channel_background_task[channel_id]
                self._channel_background_meta.pop(channel_id, None)
            return

        is_owner = self._channel_active_task.get(channel_id) is task
        if is_owner:
            del self._channel_active_task[channel_id]
        else:
            return

        queued = self._channel_inbound_queue.get(channel_id)
        if queued:
            # A message may have arrived after the turn's last checkpoint
            # drain but before it fully finished (e.g. no further tool-loop
            # iterations ran). Re-run the gate so it isn't stranded forever.
            next_event = queued.pop(0)
            if not queued:
                del self._channel_inbound_queue[channel_id]
            self._dispatch_or_queue(channel_id, next_event)

    def reload(self, source: str) -> ReloadResult:
        """Reload config/agent.yaml (whitelisted sections) and the prompt set.

        Fully synchronous (no awaits) so it is atomic on the event loop.
        All-or-nothing: everything is validated first; on any error the
        previous config and prompts stay active.
        """
        try:
            plan = plan_config_reload(config)
            errors = list(plan.errors)
            new_prompts = None
            try:
                new_prompts = prompts.load_prompts()
            except prompts.PromptError as e:
                errors += e.errors
        except Exception as e:
            errors = [f"unexpected error: {e}"]
            new_prompts = None
        if errors or new_prompts is None:
            LOG.error(f"Reload ({source}) failed, keeping previous config and prompts: {errors}")
            return ReloadResult(ok=False, errors=errors)

        apply_config_reload(config, plan)
        self._auth_manager.update(config.auth)
        logging.getLogger().setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
        prompts.set_current(new_prompts)
        if self._chat_bus is not None:
            # Numbers that lost access: end their open web streams.
            self._chat_bus.close_numbers(
                lambda n: self._auth_manager.get_user_tier(n) == UserTier.UNKNOWN
            )
        result = ReloadResult(
            ok=True,
            applied=plan.applied + ["prompts"],
            restart_required=plan.restart_required,
        )
        LOG.info(f"Reload ({source}) OK: applied={result.applied} restart_required={result.restart_required}")
        return result

    async def _preprocess_incoming(self, event: Event, channel_id: str, reply_destination: str) -> PreprocessedMessage:
        """Shared pre-loop handling for one inbound Event.

        Covers: dedup check, loop detection, the canned stop/reset-keyword
        shortcuts, mention gating + stripping, attachment download + STT
        transcription, and message persistence (store_message /
        mark_message_processed).

        Used both by process_message's "channel free, process immediately"
        path AND by _drain_inbound_checkpoint (messages picked up from
        self._channel_inbound_queue mid-turn), so a queued message gets
        IDENTICAL treatment to one processed right away - including the fast
        "stop" canned reply, which signals via the returned
        `abort_loop=True` that any tool loop currently running for this
        channel should terminate immediately rather than have this content
        injected as a new turn.
        """
        # Timestamps are second-resolution (signal.py truncates ms), so two
        # distinct messages from one user within the same second would collide
        # without the content hash - they'd be silently dropped as duplicates.
        content_hash = hashlib.sha1((event.message or "").encode()).hexdigest()[:8]
        message_id = f"{event.source}:{event.timestamp}:{content_hash}"
        if event.origin == "web" and event.chat_log_id is not None:
            # Web messages have no Signal timestamp; their row id is unique.
            message_id = f"web:{event.chat_log_id}"
        elif event.origin == "voice":
            message_id = f"voice:{event.voice_id}"

        debug_logger = safe_get_debug_logger()
        if debug_logger:
            debug_logger.log("incoming_event", channel_id, {
                "source": event.source,
                "destination": event.destination,
                "message": event.message[:500] if event.message else "",
                "group_id": event.group_id,
                "attachments_count": len(event.attachments) if event.attachments else 0,
            })

        if self._db_ops.is_message_processed(message_id, channel_id):
            LOG.debug(f"Duplicate message {message_id} in {channel_id}, skipping")
            return PreprocessedMessage(event=None, reply_destination=reply_destination)

        unmentioned_group = (
            event.group_id is not None
            and not is_mentioned(event)
            and not is_channel_mode_always_process(channel_id)
        )
        # Handled by this turn (unless it's an unmentioned group message,
        # skipped below): mark it read once the turn ends.
        if not unmentioned_group:
            self._queue_read_receipt(channel_id, event)

        # Pre-existing no-op (muted channels aren't actually short-circuited
        # here); left exactly as-is, unrelated to this fix.
        if channel_id in self._muted_channels or is_channel_muted(channel_id):
            pass

        if not self._check_loop_detection(channel_id, event.message):
            LOG.debug(f"Loop detected for {channel_id}, suppressing response")
            await self._send_typing(reply_destination, start=False)
            return PreprocessedMessage(event=None, reply_destination=reply_destination)

        if (
            event.group_id is None
            and strip_mention(event.message).strip() == "/reload"
            and self._auth_manager.get_user_tier(event.source) == UserTier.PRIVILEGED
        ):
            LOG.info(f"/reload requested by {event.source}")
            result = self.reload(f"chat:{event.source}")
            await self._send_typing(reply_destination, start=False)
            await self._send_with_retry(reply_destination, result.format_reply(), UserTier.PRIVILEGED)
            return PreprocessedMessage(event=None, reply_destination=reply_destination)

        if self._web_auth is not None:
            # Mentions are stripped in groups only: in 1:1 a /deauth
            # argument (endpoint name) must stay exactly as typed.
            web_command = parse_web_command(
                strip_mention(event.message) if event.group_id is not None else event.message
            )
            if web_command is not None:
                tier = self._auth_manager.get_user_tier(event.source)
                reply: Optional[str] = None
                if event.group_id is None and tier in (UserTier.PRIVILEGED, UserTier.KNOWN):
                    LOG.info(f"{web_command[0]} requested by {event.source}")
                    reply = self._web_auth.handle_command(event.source, *web_command)
                elif event.group_id is not None and is_mentioned(event):
                    reply = prompts.current().text("web-command-group-hint")
                if reply is not None:
                    await self._send_typing(reply_destination, start=False)
                    await self._send_with_retry(reply_destination, reply, tier)
                    return PreprocessedMessage(event=None, reply_destination=reply_destination)

        stop_keywords = ["stop", "halt", "cancel", "abort", "enough", "cease"]
        message_lower = event.message.lower().strip()
        if any(re.match(rf"^{re.escape(keyword)}.{{0,3}}$", message_lower) for keyword in stop_keywords):
            LOG.info(f"STOP/HALT request detected in {channel_id}: '{event.message}'")
            await self._send_typing(reply_destination, start=False)
            tier = self._auth_manager.get_user_tier(event.source)
            await self._send_with_retry(reply_destination, prompts.current().text("operation-halted"), tier)
            return PreprocessedMessage(event=None, reply_destination=reply_destination, abort_loop=True)

        reset_keywords = ["reset context", "reset your context", "forget everything", "start fresh", "clear context", "clear my context", "new conversation", "forget prior", "forget previous", "don't remember", "stop remembering"]
        if any(keyword in message_lower for keyword in reset_keywords):
            LOG.info(f"CONTEXT RESET request detected in {channel_id}: '{event.message}'")
            memory_mgr = self._get_memory_manager(channel_id)
            await asyncio.to_thread(memory_mgr.reset)
            self._db_ops.clear_messages(channel_id)
            soul_manager.invalidate_cache(channel_id)
            await self._send_typing(reply_destination, start=False)
            tier = self._auth_manager.get_user_tier(event.source)
            await self._send_with_retry(reply_destination, prompts.current().text("context-reset"), tier)
            return PreprocessedMessage(event=None, reply_destination=reply_destination, abort_loop=True)

        msg = Message(event=event, is_outgoing=False, processed=False)
        self._db_ops.store_message(msg, channel_id)

        self._db_ops.mark_message_processed(message_id, channel_id)

        if unmentioned_group:
            LOG.info(f"Skipping LLM for unmentioned group message from {event.source}")
            # Web interface: keep the attachment viewable (no STT, no LLM).
            if self._chat_bus is not None and event.attachments and event.chat_log_id is not None:
                try:
                    await self._download_attachments(event, channel_id)
                except Exception as e:
                    LOG.warning(f"Downloading unmentioned group attachments failed: {e}")
            return PreprocessedMessage(event=None, reply_destination=reply_destination)

        message_text = strip_mention(event.message)
        event = dataclasses.replace(event, message=message_text)

        if event.attachments:
            downloaded = await self._download_attachments(event, channel_id)
            event.attachments = downloaded
            if downloaded:
                image_count = sum(1 for a in downloaded if a.content_type.startswith("image/"))
                LOG.info(f"Sending message to LLM with {len(downloaded)} attachments ({image_count} images)")
                self._tool_registry.invalidate_file_list_cache(channel_id)

                # Announce attachments by on-disk filename and re-store, so
                # the name survives into history and message_search (it was
                # previously only added to the in-memory prompt). Audio is
                # left out when STT is on: the transcript below is tagged
                # "[Voice message: <filename>]" already.
                names = [
                    Path(a.local_path).name for a in downloaded
                    if a.local_path and not (config.stt.enabled and is_audio_attachment(a))
                ]
                if names:
                    event.message = with_attachment_note(event.message, names)
                    try:
                        self._db_ops.store_message(
                            Message(event=event, is_outgoing=False, processed=True),
                            channel_id,
                        )
                    except Exception as exc:
                        LOG.warning(f"Re-store after attachment note failed: {exc}")

            # Voice-message transcription. Runs only when STT is enabled
            # AND at least one attachment is audio. Per-attachment try/except
            # so a single failure doesn't kill the whole turn — the
            # placeholder string is informative enough for the agent to
            # tell the user something went wrong.
            if config.stt.enabled and any(is_audio_attachment(a) for a in event.attachments):
                audio_count = sum(1 for a in event.attachments if is_audio_attachment(a))
                LOG.info(f"STT: transcribing {audio_count} audio attachment(s)")
                transcripts: list[str] = []
                for att in event.attachments:
                    if not is_audio_attachment(att):
                        continue
                    filename = Path(att.local_path).name if att.local_path else (att.filename or att.id)
                    try:
                        text = await transcribe_attachment(att, self._stt_client, config.stt)
                    except Exception as exc:
                        LOG.warning(f"STT failed for {att.filename or att.id}: {exc}")
                        text = prompts.current().text("voice-transcription-failed")
                    # Filename is embedded per-attachment (not just a generic
                    # "[Voice message]" prefix) so the agent can pass it to
                    # the retranscribe_audio tool if this transcript looks
                    # garbled - see tools/stt_ops.py.
                    if text:
                        transcripts.append(f"[Voice message: {filename}] {text}")
                        if self._chat_bus is not None and event.chat_log_id is not None:
                            try:
                                self._chat_bus.set_transcript(event.chat_log_id, att.id, text)
                            except Exception as exc:
                                LOG.warning(f"Publishing transcript to the web interface failed: {exc}")
                combined = "\n".join(transcripts)
                if combined:
                    if not event.message or event.message == "[Attachment]":
                        event.message = combined
                    else:
                        event.message = f"{combined}\n{event.message}"
                    # Re-store so the placeholder row stored above gets
                    # overwritten with the transcript-bearing message.
                    try:
                        self._db_ops.store_message(
                            Message(event=event, is_outgoing=False, processed=True),
                            channel_id,
                        )
                    except Exception as exc:
                        LOG.warning(f"STT: re-store after transcript failed: {exc}")
                    LOG.info(f"STT: message rewritten, {len(combined)} chars of transcript")

        return PreprocessedMessage(event=event, reply_destination=reply_destination)

    async def _download_attachments(self, event: Event, channel_id: str) -> list[Attachment]:
        """Save the event's Signal attachments into the channel's attachments
        dir. Attachments that already have a local file (web uploads) are
        kept as they are. Updates the web interface row, if any."""
        downloaded: list[Attachment] = []
        for att in event.attachments:
            if att.local_path:
                downloaded.append(att)
                continue
            saved = await self._signal_client.save_attachment(
                att.id,
                att.content_type,
                channel_id,
                original_filename=att.filename
            )
            if saved:
                downloaded.append(saved)
                LOG.info(f"Downloaded attachment {att.id} ({att.content_type}, {saved.size} bytes) for channel {channel_id}")
            else:
                LOG.warning(f"Failed to download attachment {att.id} for channel {channel_id}")
        if self._chat_bus is not None and event.chat_log_id is not None and downloaded:
            try:
                self._chat_bus.update_attachments(event.chat_log_id, downloaded)
            except Exception as e:
                LOG.warning(f"Publishing attachments to the web interface failed: {e}")
        return downloaded

    async def _drain_inbound_checkpoint(self, channel_id: str, reply_destination: str, messages: list[dict], turn_output: Optional[TurnOutput] = None) -> bool:
        """Drain self._channel_inbound_queue[channel_id] at a tool-loop
        checkpoint (called at the top of process_message's inline
        tool-calling while loop - the only remaining call site now that
        cron/subagent runs have their own independent background lane and
        are no longer interrupted by queued chat messages, see
        _process_cronjob/_process_subagent), running every queued event
        through _preprocess_incoming for identical treatment to a
        normally-processed message, and injecting the result as a synthetic
        user turn so the model sees it on its next completion.

        Returns True if the loop should abort immediately (a queued message
        resolved to the "stop"/reset canned shortcut, which already sent its
        own reply via _preprocess_incoming) - the caller is responsible for
        releasing whatever turn-scoped resources it holds on that path, same
        as any other early return from the loop.

        An injected user message, or a canned reply _preprocess_incoming
        sent itself (e.g. a queued /reload), also ends the current
        single-message target (turn_output): the agent's next text is a new
        Signal message below those, not an edit of the one above them.
        """
        queued_events = self._channel_inbound_queue.pop(channel_id, None)
        if not queued_events:
            return False
        for queued_event in queued_events:
            sent_before = self._messages_sent.get(channel_id, 0)
            preprocessed = await self._preprocess_incoming(queued_event, channel_id, reply_destination)
            if turn_output is not None and self._messages_sent.get(channel_id, 0) != sent_before:
                turn_output.reset_target()
            if preprocessed.abort_loop:
                LOG.info(f"Aborting in-flight tool loop for {channel_id}: queued message resolved to a stop/reset shortcut")
                return True
            if preprocessed.event is not None:
                LOG.info(f"Injecting queued message from {queued_event.source} into running tool loop for {channel_id}")
                messages.append({
                    "role": "user",
                    "content": prompts.current().render("inbound-drain", message=preprocessed.event.message),
                })
                if turn_output is not None:
                    turn_output.reset_target()
        return False

    async def _pause_for_consent(
        self,
        context: Context,
        channel_id: str,
        reply_destination: str,
        tier: UserTier,
        tc: ToolCall,
        request: ConsentRequest,
        held_messages: list[str],
    ) -> Optional[tuple[ToolResult, bool]]:
        """A chat-turn tool call asked for the users' approval (consent.py):
        post the poll and wait for a vote, a new message, or the timeout.

        Returns (result for the paused call, approved), or None if the turn
        must end (stop/reset shortcut, shutdown). Results of a command that
        did not run are failures, so the turn's tool cache never replays
        them. A message that arrives instead of a vote goes into the paused
        call's result (not a user turn of its own, so the assistant
        tool_calls -> tool results order stays intact). A message consumed
        while a vote arrived loses to the vote and is appended to
        `held_messages`, which the caller injects after this step's tool
        results. The task semaphore slot stays held while paused (at most
        one paused turn per channel).
        """
        await self._send_typing(reply_destination, start=False)
        pending = await self._consent.request(channel_id, request, tc.name, lane="interactive")
        if pending is None:
            if self._shutdown:  # no new polls while shutting down
                return None
            await self._send_typing(reply_destination, start=True)
            return ToolResult(
                name=tc.name, success=False, output=None, error=CONSENT_POLL_FAILED_ERROR,
            ), False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._consent.timeout_seconds("interactive")
        wake = self._inbound_wake.setdefault(channel_id, asyncio.Event())
        user_message: Optional[str] = None
        turn_output = context.turn_output
        try:
            # The users see the poll now: what this turn handled counts as
            # read, and the next text must not edit a message above the poll.
            if turn_output is not None:
                turn_output.reset_target()
            await self._flush_read_receipts(channel_id)
            if self._shutdown:
                return None
            while not pending.future.done():
                if self._channel_inbound_queue.get(channel_id):
                    wake.clear()
                    outcome, text = await self._drain_while_paused(
                        channel_id, reply_destination, pending, turn_output, held_messages
                    )
                    if outcome == "abort":
                        return None
                    if outcome == "message":
                        user_message = text
                        break
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                # No await since the queue check above, so nothing can have
                # been queued in between.
                wake.clear()
                wake_waiter: asyncio.Future = asyncio.ensure_future(wake.wait())
                vote: asyncio.Future = pending.future
                try:
                    await asyncio.wait(
                        [vote, wake_waiter], timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    wake_waiter.cancel()
        finally:
            self._inbound_wake.pop(channel_id, None)
            # Deletes the poll on every outcome except a vote (the vote
            # already did): timeout, message, abort, cancel, error.
            try:
                await self._consent.discard(pending)
            except Exception as e:
                LOG.warning(f"Could not discard consent poll {pending.poll_ts}: {e}")

        question = pending.question
        if user_message is not None:
            LOG.info(f"Consent poll {pending.poll_ts} in {channel_id}: answered with a message")
            result_error = prompts.current().render("consent-not-run-message", question=question, message=user_message)
        elif pending.future.done() and not pending.future.cancelled():
            decision = pending.future.result()
            if decision is None:
                LOG.info(f"Consent poll {pending.poll_ts} in {channel_id}: ended by shutdown")
                return None
            if decision.approved:
                await self._send_typing(reply_destination, start=True)
                rerun = await self._tool_registry.execute_tool(
                    tc.name, tc.arguments, tier, consent_question=question
                )
                suffix = prompts.current().render("consent-approved-suffix", voter=decision.voter)
                if rerun.success:
                    rerun = dataclasses.replace(rerun, output=f"{rerun.output or ''}\n{suffix}")
                else:
                    rerun = dataclasses.replace(rerun, error=f"{rerun.error or ''}\n{suffix}")
                return rerun, True
            result_error = prompts.current().render("consent-rejected", voter=decision.voter, question=question)
        else:
            LOG.info(f"Consent poll {pending.poll_ts} in {channel_id}: expired")
            result_error = prompts.current().render("consent-expired", question=question)
        await self._send_typing(reply_destination, start=True)
        return ToolResult(name=tc.name, success=False, output=None, error=result_error), False

    async def _drain_while_paused(
        self,
        channel_id: str,
        reply_destination: str,
        pending: "PendingConsent",
        turn_output: Optional[TurnOutput],
        held_messages: list[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Run queued inbound events through _preprocess_incoming one by one
        while a turn waits on an approval poll.

        Returns ("abort", None) for a stop/reset shortcut, ("message", text)
        for a message that would start a turn, (None, None) if only chatter
        (unmentioned group messages, duplicates) was queued. Stops as soon as
        a vote resolved the poll: the vote wins and the rest stays queued for
        the turn's next normal checkpoint. A message already consumed when
        the vote arrived (it was being preprocessed) goes to `held_messages`.
        On abort the rest is dropped, like _drain_inbound_checkpoint does.
        """
        queued = self._channel_inbound_queue.pop(channel_id, [])
        while queued and not pending.future.done():
            queued_event = queued.pop(0)
            sent_before = self._messages_sent.get(channel_id, 0)
            preprocessed = await self._preprocess_incoming(queued_event, channel_id, reply_destination)
            if turn_output is not None and self._messages_sent.get(channel_id, 0) != sent_before:
                turn_output.reset_target()
            if preprocessed.abort_loop:
                LOG.info(f"Ending turn paused on a consent poll in {channel_id}: stop/reset shortcut")
                return "abort", None
            if preprocessed.event is not None:
                if pending.future.done() and not pending.future.cancelled():
                    # A vote arrived while this was being preprocessed.
                    held_messages.append(preprocessed.event.message)
                    break
                self._requeue_front(channel_id, queued)
                return "message", preprocessed.event.message
        self._requeue_front(channel_id, queued)
        return None, None

    def _requeue_front(self, channel_id: str, events: list[Event]) -> None:
        """Put unprocessed events back at the front of the inbound queue."""
        if events:
            self._channel_inbound_queue[channel_id] = events + self._channel_inbound_queue.get(channel_id, [])

    async def process_message(self, event: Event) -> None:
        """Process a Signal message event."""
        channel_id, reply_destination = _channel_id_for_event(event)

        preprocessed = await self._preprocess_incoming(event, channel_id, reply_destination)
        if preprocessed.event is None:
            return
        event = preprocessed.event

        # New turn for this channel: reset its per-channel session counters.
        # self._action_counter/_session_start remain process-wide scalars
        # (write-only elsewhere in the codebase - not converted per-channel,
        # out of scope for this fix).
        self._messages_sent[channel_id] = 0
        self._action_counter: Counter[str] = Counter()
        self._session_start = datetime.now()
        self._subagent_session_count[channel_id] = 0

        await self._send_typing(reply_destination, start=True)

        if channel_id in self._channel_message_queue and self._channel_message_queue[channel_id]:
            LOG.info(f"Flushing {len(self._channel_message_queue[channel_id])} queued messages for {channel_id}")
            queued = self._channel_message_queue[channel_id]
            self._channel_message_queue[channel_id] = []
            tier = self._auth_manager.get_user_tier(event.source)
            for queued_msg in queued:
                await self._send_with_retry(channel_id, queued_msg, tier)
            if self._channel_message_queue[channel_id]:
                LOG.info(f"Some queued messages re-queued due to rate limit for {channel_id}")

        context = self._context_manager.get_context(channel_id)
        self._tool_registry.set_context(context)
        tier = self._auth_manager.get_user_tier(event.source)
        LOG.info(f"TIER_LOOKUP: event.source={event.source}, tier={tier}")
        # Single-message mode is snapshotted once per turn.
        message_mode = resolve_message_mode(channel_id)
        context.turn_output = self._new_turn_output(reply_destination, tier, message_mode)
        tools = self._tool_registry.get_tools_for_tier(tier)
        if channel_id in self._voice_speakers:
            # The reply is spoken anyway; a text_to_speech job would also
            # block the (serial) TTS server for the call's own speech.
            tools = [t for t in tools if t.name not in VOICE_CALL_HIDDEN_TOOLS]
        else:
            tools = [t for t in tools if t.name not in VOICE_CALL_ONLY_TOOLS]

        # Exclude the just-stored current event to avoid it appearing
        # twice (once in history, once as the trailing user turn in
        # build_messages).  before_timestamp is inclusive (<=), so we
        # subtract 1 s to get a strict <.  Guard against a bogus
        # timestamp (0/missing): with -1 we would silently truncate
        # the whole history to nothing, so fall back to the old
        # unfiltered load in that case.
        history = self._context_manager.get_messages_for_llm(
            channel_id,
            before_timestamp=(event.timestamp - 1 if event.timestamp > 0 else None),
        )
        # Use total uncapped count for summarization gate (not just loaded history)
        total_msgs = self._context_manager.get_message_count(channel_id)

        from .config import config as config_obj
        min_keep = config_obj.chat.min_messages_to_keep
        context_threshold = config_obj.chat.context_size_threshold

        message_dicts = [
            {"content": msg.event.message} for msg in history
        ]
        current_tokens = LLMClient.estimate_token_count(
            message_dicts + [{"content": event.message}],
            system_prompt=""
        )

        can_summarize = (
            total_msgs > min_keep + config_obj.chat.min_messages_to_summarize
            and current_tokens >= context_threshold
            and not self._summarization_due.get(channel_id)
        )

        if can_summarize:
            self._summarization_due[channel_id] = True
            LOG.info(f"SUMMARIZATION_DUE: channel={channel_id}, messages={total_msgs}, tokens={current_tokens}")

        context_implying_patterns = [
            "try again", "as i mentioned", "continuing", "as before",
            "remember when", "you were helping", "we were discussing",
            "keep going", "carry on", "don't forget", "as we talked about",
            "continue where we left off"
        ]
        message_lower = event.message.lower()
        context_recovery_flag = any(pattern in message_lower for pattern in context_implying_patterns)
        LOG.debug(f"CONTEXT_RECOVERY: event.source={event.source}, message='{event.message}', context_recovery_flag={context_recovery_flag}")

        summary_text, keywords = self._context_manager.get_summary(channel_id) or (None, None)
        user_tier_str = tier.value.capitalize()

        memory_context = ""
        try:
            memory_mgr = self._get_memory_manager(channel_id)
            memories = await asyncio.to_thread(
                memory_mgr.search, event.message, limit=5
            )
            memory_context = self._format_memories_for_prompt(memories)
        except Exception as e:
            LOG.warning(f"Memory retrieval failed: {e}")
            memory_context = ""

        soul_content = self._context_manager.get_soul_content(channel_id)
        # Live status note (chat call site ONLY - cron/subagent system
        # prompts don't get this, since those are the ones potentially
        # running in the background lane, not the ones that need to know
        # about it): id + type only, never the raw prompt (may contain
        # credentials) - see _build_background_activity_section in llm.py.
        background_activity = self._channel_background_meta.get(channel_id)
        system_prompt = self._llm_client.build_system_prompt(tools, user_tier_str, soul_content, memory_context if 'memory_context' in dir() else None, event.source, channel_id, background_activity=background_activity, message_mode=message_mode)
        history_messages = self._llm_client.build_messages(history, event, summary_text, keywords)
        # Active call on this channel: no reasoning (latency) and the call
        # instruction appended to the current user message - noticed far
        # better there than inside the long system prompt, and the system
        # prompt stays byte-identical (backend prefix cache).
        voice_call = channel_id in self._voice_speakers
        thinking = False if voice_call else None
        if voice_call:
            _append_to_last_user_message(history_messages, prompts.current().text("voice-call"))

        messages = [
            {"role": "system", "content": system_prompt},
        ] + history_messages

        try:
            await self._send_typing(reply_destination, start=True)
            if context_recovery_flag and tier != UserTier.UNKNOWN:
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        original_content = msg["content"]
                        msg["content"] = prompts.current().render("context-recovery", message=msg["content"])
                        LOG.debug(f"RECOVERY_PROMPT_INJECTED: original='{original_content[:50]}...', tier={tier}")
                        break
            response_text, tool_calls = await chat_with_markup_guard(self._llm_client, messages, tools, channel_id, source="chat", thinking=thinking)
            response_text = _check_and_filter_issue_marker(response_text, channel_id)
            response_text = await self._handle_mute_unmute(response_text, channel_id)
            response_text = self._strip_leaked_discard_marker(response_text, channel_id)
            should_discard = self._should_discard_output(response_text, channel_id)

            continue_keyword_found = _extract_continue_keyword(response_text)
            if continue_keyword_found:
                remaining = self._continuation_turns_remaining.get(channel_id, MAX_CONTINUATION_TURNS)
                if remaining > 0:
                    self._continuation_turns_remaining[channel_id] = remaining - 1
                    response_text = _strip_continue_keyword(response_text)
                    LOG.info(f"__AGENT_CONTINUE__ detected in {channel_id}, continuing (remaining: {remaining - 1})")
                else:
                    LOG.info(f"__AGENT_CONTINUE__ detected but max turns reached for {channel_id}")
                    response_text = _strip_continue_keyword(response_text)
                    self._continuation_turns_remaining[channel_id] = MAX_CONTINUATION_TURNS
                    continue_keyword_found = False
            else:
                self._continuation_turns_remaining[channel_id] = MAX_CONTINUATION_TURNS
        except Exception as e:
            LOG.error(f"LLM error: {e}")
            await self._send_typing(reply_destination, start=False)
            await self._send_turn_text(context, reply_destination, prompts.current().text("llm-unavailable"), tier, notice=True)
            # Store the failure reply so the user's message doesn't sit
            # unanswered in history - without this, a later turn's
            # context window shows the user's message with no visible
            # response, and the model (much later, once the backend is
            # back) tries to "catch up" and answer it out of order.
            self._store_outgoing_message(event, prompts.current().text("llm-unavailable"), reply_destination)
            return

        if not tool_calls:
            if response_text and response_text.strip() and not should_discard and not continue_keyword_found:
                await self._send_turn_text(context, reply_destination, response_text, tier)
                self._store_outgoing_message(event, response_text, reply_destination)
                if event.source != "agent":
                    asyncio.create_task(self._check_memory_importance(event, response_text, channel_id))
            elif not should_discard and not continue_keyword_found:
                # A completion with neither text nor a tool call isn't a
                # deliberate silence (that's __DISCARD_OUTPUT__'s job) - it's
                # the backend returning nothing. Left as a silent no-op, the
                # user's message went completely unanswered with no stored
                # reply, so a later "did you do X?" had no prior turn to
                # check itself against.
                fallback = prompts.current().text("empty-response")
                await self._send_turn_text(context, reply_destination, fallback, tier)
                self._store_outgoing_message(event, fallback, reply_destination)
            await self._send_typing(reply_destination, start=False)
            return

        if response_text and response_text.strip() and not should_discard and not continue_keyword_found:
            await self._send_turn_text(context, reply_destination, response_text, tier)

        tool_results_list: list[ToolResult] = []
        # id of the stored row holding the latest response_text (see the
        # tool-loop store below); lets replace mode flag replaced texts.
        last_row_id: Optional[str] = None
        iteration = 0
        executed_tools: OrderedDict[str, tuple[str, ToolResult]] = OrderedDict()
        repeat_counter: dict[str, int] = {}
        empty_nudges_used = 0
        max_tool_iterations = _get_max_tool_iterations_for_tier(tier)
        continuation_turns_remaining = self._continuation_turns_remaining.get(channel_id, MAX_CONTINUATION_TURNS)
        while (tool_calls and iteration < max_tool_iterations) or (iteration == 0 and continuation_turns_remaining > 0):
            iteration += 1

            if await self._drain_inbound_checkpoint(channel_id, reply_destination, messages, context.turn_output):
                await self._send_typing(reply_destination, start=False)
                return

            if self._messages_sent.get(channel_id, 0) >= MAX_MESSAGES_PER_SESSION:
                LOG.warning(f"Max messages per session ({MAX_MESSAGES_PER_SESSION}) reached for {event.source}")
                await self._send_typing(reply_destination, start=False)
                await self._send_turn_text(context, reply_destination, prompts.current().text("too-many-messages"), tier, notice=True)
                return

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": response_text}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc.id, "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)
            tool_results_list = []
            # Set once a command of this step was not approved: the remaining
            # calls get a "skipped" result (every call needs one result).
            consent_refused = False
            # Messages that lost against a vote (see _pause_for_consent).
            held_messages: list[str] = []
            for tc in tool_calls:
                cache_key = f"{tc.name}:{str(tc.arguments)}"

                if consent_refused:
                    skipped = ToolResult(name=tc.name, success=False, output=None, error=prompts.current().text("consent-skipped"))
                    tool_results_list.append(skipped)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": _tool_result_content(skipped, config.chat.effective_tool_result_max_chars)})
                    continue

                repeat_count = repeat_counter.get(cache_key, 0) + 1
                repeat_counter[cache_key] = repeat_count
                max_allowed = MAX_REPEATED_BROWSER_CALLS if tc.name.startswith("browser_") else MAX_REPEATED_TOOL_CALLS
                if repeat_count > max_allowed:
                    # Feed this back to the model as the tool result instead
                    # of ending the turn: it decides how to change course.
                    # max_tool_iterations still bounds a model that ignores it.
                    LOG.warning(f"Repeated tool call '{tc.name}' with same arguments detected ({repeat_count} times) for {event.source}")
                    hint = prompts.current().render("repeated-tool-call-hint", tool_name=tc.name, count=repeat_count)
                    tool_results_list.append(ToolResult(name=tc.name, success=False, output=None, error=hint))
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": hint})
                    continue

                if cache_key in executed_tools:
                    cached_args, cached_result = executed_tools[cache_key]
                    executed_tools.move_to_end(cache_key)
                    result_content = _tool_result_content(cached_result, config.chat.effective_tool_result_max_chars)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_content,
                    })
                    tool_results_list.append(cached_result)
                    continue

                LOG.debug(f"TOOL_EXECUTE: source={event.source}, tier={tier}, tool={tc.name}")
                if tc.name in VOICE_CALL_HIDDEN_TOOLS and channel_id in self._voice_speakers:
                    # Hidden from the tool list, but the model may still copy
                    # the call from history.
                    tool_result = ToolResult(
                        name=tc.name, success=False, output=None,
                        error="Not available during a voice call: your reply is already spoken aloud.",
                    )
                else:
                    tool_result = await self._tool_registry.execute_tool(
                        tc.name, tc.arguments, tier
                    )

                approved_run = False
                if isinstance(tool_result.consent_request, ConsentRequest):
                    paused = await self._pause_for_consent(
                        context, channel_id, reply_destination, tier, tc, tool_result.consent_request, held_messages
                    )
                    if paused is None:
                        await self._send_typing(reply_destination, start=False)
                        return
                    tool_result, approved_run = paused
                    consent_refused = not approved_run

                if tool_result.success and len(executed_tools) >= MAX_TOOL_CACHE_SIZE:
                    evicted_key = executed_tools.popitem(last=False)
                    LOG.debug(f"Evicted oldest tool from cache: {evicted_key[0]}")

                # Failures are never cached: a retry after a transient
                # error must actually re-execute instead of replaying it.
                # An approved run is not cached either: an identical call
                # later in the turn must ask the users again.
                if tool_result.success and not approved_run:
                    executed_tools[cache_key] = (str(tc.arguments), tool_result)
                tool_results_list.append(tool_result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _tool_result_content(tool_result, config.chat.effective_tool_result_max_chars),
                })

            # After all of this step's tool results (S1 message order), like
            # a message drained at a checkpoint.
            for held in held_messages:
                messages.append({"role": "user", "content": prompts.current().render("inbound-drain", message=held)})
                if context.turn_output is not None:
                    context.turn_output.reset_target()

            original_tool_calls = tool_calls
            try:
                await self._send_typing(reply_destination, start=False)
                response_text, tool_calls = await chat_with_markup_guard(self._llm_client, messages, tools, channel_id, source="chat", thinking=thinking)
                response_text = _check_and_filter_issue_marker(response_text, channel_id)
                response_text = await self._handle_mute_unmute(response_text, channel_id)
                response_text = self._strip_leaked_discard_marker(response_text, channel_id)
                if tool_calls:
                    await self._send_typing(reply_destination, start=True)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                LOG.error(f"LLM error during tool execution: {type(e).__name__}: {e}\nFull traceback:\n{tb}")
                await self._send_typing(reply_destination, start=False)
                await self._send_turn_text(context, reply_destination, prompts.current().text("llm-unavailable"), tier, notice=True)
                self._store_outgoing_message(event, prompts.current().text("llm-unavailable"), reply_destination)
                return

            continue_keyword_found = _extract_continue_keyword(response_text)
            if continue_keyword_found and continuation_turns_remaining > 0:
                continuation_turns_remaining -= 1
                response_text = _strip_continue_keyword(response_text)
                LOG.info(f"__AGENT_CONTINUE__ detected, continuing (remaining: {continuation_turns_remaining})")
                if original_tool_calls:
                    continue_msg: dict[str, Any] = {"role": "assistant", "content": response_text}
                    continue_msg["tool_calls"] = [
                        {"id": tc.id, "function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in original_tool_calls
                    ]
                    messages.append(continue_msg)
                    for tc, result in zip(original_tool_calls, tool_results_list):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": _tool_result_content(result, config.chat.effective_tool_result_max_chars),
                        })
                else:
                    messages.append({"role": "assistant", "content": response_text})
                continue
            elif continue_keyword_found:
                response_text = _strip_continue_keyword(response_text)
                self._continuation_turns_remaining[channel_id] = MAX_CONTINUATION_TURNS
                continuation_turns_remaining = 0
            else:
                self._continuation_turns_remaining[channel_id] = MAX_CONTINUATION_TURNS
                continuation_turns_remaining = 0

            if original_tool_calls:
                assistant_event = Event(
                    source="agent",
                    destination=reply_destination,
                    message=response_text or "",
                    timestamp=int(datetime.now().timestamp()),
                    group_id=event.group_id,
                    attachments=[],
                )
                assistant_record = Message(
                    event=assistant_event,
                    is_outgoing=True,
                    processed=True,
                    tool_calls=original_tool_calls,
                    tool_results=tool_results_list,
                )
                last_row_id = self._db_ops.store_message(assistant_record, reply_destination)

            # Sent after storing its row so replace mode knows the row id.
            if tool_calls and response_text and response_text.strip():
                await self._send_typing(reply_destination, start=False)
                await self._send_turn_text(context, reply_destination, response_text, tier, row_id=last_row_id)

            while (
                not tool_calls
                and not (response_text and response_text.strip())
                and empty_nudges_used < MAX_EMPTY_RESPONSE_NUDGES
            ):
                # Known vLLM behavior: after a tool result the model sometimes
                # returns an empty completion with finish_reason=stop. Nudge it
                # (inside the loop, so a nudged tool call is executed normally).
                # After a failed tool call the model keeps its tools and is told
                # what failed, so it can retry or re-plan; the user hears about
                # it only if the model decides to say so.
                empty_nudges_used += 1
                failed = [r for r in tool_results_list if not r.success]
                LOG.warning(
                    f"Empty response after {iteration} tool iterations for {channel_id}; "
                    f"nudging LLM ({empty_nudges_used}/{MAX_EMPTY_RESPONSE_NUDGES}, tool_failed={bool(failed)})"
                )
                if failed:
                    nudge_text = prompts.current().render(
                        "tool-failure-nudge",
                        tool_names=", ".join(dict.fromkeys(r.name for r in failed)),
                        error=str(failed[-1].error or "unknown error")[:NUDGE_ERROR_MAX_CHARS],
                    )
                else:
                    nudge_text = prompts.current().text("empty-response-nudge")
                messages.append({"role": "user", "content": nudge_text})
                try:
                    response_text, tool_calls = await chat_with_markup_guard(
                        self._llm_client, messages, tools if failed else [], channel_id, source="chat", thinking=thinking
                    )
                    response_text = _check_and_filter_issue_marker(response_text, channel_id)
                    response_text = await self._handle_mute_unmute(response_text, channel_id)
                    response_text = self._strip_leaked_discard_marker(response_text, channel_id)
                    if tool_calls:
                        await self._send_typing(reply_destination, start=True)
                except Exception as e:
                    LOG.error(f"LLM error during empty-response nudge: {e}")
                    response_text, tool_calls = "", []

        if iteration >= max_tool_iterations:
            LOG.warning(f"Max tool iterations ({max_tool_iterations}) reached for {event.source}")
            await self._send_typing(reply_destination, start=False)
            await self._send_turn_text(context, reply_destination, prompts.current().text("internal-limit"), tier, notice=True)
            return

        is_fallback = False
        if iteration and not (response_text and response_text.strip()):
            # Last resort: the model stayed empty through every nudge.
            response_text = prompts.current().text("no-summary-fallback")
            is_fallback = True

        if response_text:
            await self._send_turn_text(
                context, reply_destination, response_text, tier,
                row_id=last_row_id if iteration else None, notice=is_fallback,
            )
            if not iteration:
                self._store_outgoing_message(event, response_text, reply_destination)
            if event.source != "agent":
                asyncio.create_task(self._check_memory_importance(event, response_text, channel_id))
        await self._send_typing(reply_destination, start=False)

        self._maybe_schedule_summarization(channel_id)

    def _maybe_schedule_summarization(self, channel_id: str) -> None:
        """Schedule summarization if it's due (all three paths call this).

        Deferred while a voice call is active on the channel: the extra LLM
        call competes for the GPU with TTS; it runs after the call instead.
        """
        if self._summarization_due.get(channel_id) and channel_id not in self._voice_speakers:
            asyncio.create_task(self._run_summarization(channel_id))

    async def _run_summarization(self, channel_id: str) -> None:
        """Background task to summarize conversation history."""
        from .config import config as config_obj
        
        LOG.info(f"SUMMARIZATION_START: channel={channel_id}")
        
        try:
            prior_summary, prior_keywords = self._context_manager.get_summary(channel_id) or (None, [])
            
            keywords = []
            if prior_summary:
                keywords = await self._llm_client.distill_keywords(
                    prior_summary, prior_keywords, config_obj.chat.max_keywords
                )
                LOG.info(f"SUMMARIZATION_KEYWORDS_DISTILLED: channel={channel_id}, count={len(keywords)}")
            
            all_msgs = self._context_manager.get_messages_for_llm(channel_id)
            min_keep = config_obj.chat.min_messages_to_keep
            msgs_to_summarize = all_msgs[:-min_keep] if len(all_msgs) > min_keep else []
            
            if not msgs_to_summarize:
                self._summarization_due[channel_id] = False
                LOG.info(f"SUMMARIZATION_SKIPPED: no messages to summarize for {channel_id}")
                return
            
            message_dicts = [{"content": msg.event.message} for msg in msgs_to_summarize]
            before_tokens = LLMClient.estimate_token_count(message_dicts, "")
            
            new_summary = await self._llm_client.summarize_conversation(
                msgs_to_summarize,
                prior_summary=prior_summary,
                keywords=keywords,
                max_keywords=config_obj.chat.max_keywords,
            )
            
            last_ts = msgs_to_summarize[-1].event.timestamp
            self._context_manager.mark_summarized(channel_id, last_ts)
            
            version = (len(prior_keywords) // config_obj.chat.max_keywords) + 1 if prior_summary else 1
            self._context_manager.store_summary(
                channel_id=channel_id,
                content=new_summary,
                keywords=keywords,
                version=version,
                token_count=before_tokens,
                messages_before_count=len(msgs_to_summarize),
            )
            
            self._summarization_due[channel_id] = False
            LOG.info(f"SUMMARIZATION_COMPLETE: channel={channel_id}, "
                     f"msgs_summarized={len(msgs_to_summarize)}, "
                     f"tokens_before={before_tokens}, version={version}")
        except Exception as e:
            LOG.error(f"SUMMARIZATION_FAILED: channel={channel_id}, error={e}")

    async def _check_memory_importance(self, event: Event, response_text: str, channel_id: str) -> None:
        """Check if exchange contains anything worth remembering (fire-and-forget).

        Skipped during a voice call: the extra LLM call slows down TTS on a
        shared GPU right when the reply is being spoken.
        """
        if channel_id in self._voice_speakers:
            return
        try:
            memory_mgr = self._get_memory_manager(channel_id)
            check_messages = [
                {"role": "system", "content": prompts.current().text("memory-classifier")},
                {"role": "user", "content": f"User: {event.message}\nAgent: {response_text}"}
            ]
            
            response_text, _ = await self._llm_client.chat(check_messages, [], channel_id)
            
            try:
                result = json.loads(response_text)
                if result.get("should_remember") and result.get("content"):
                    await asyncio.to_thread(
                        memory_mgr.add_memory,
                        content=result["content"],
                        memory_type=result.get("memory_type", "fact"),
                        source_msg_id=f"{event.source}:{event.timestamp}"
                    )
                    LOG.info(f"Auto-memorized: [{result.get('memory_type')}] {result['content']}")
            except json.JSONDecodeError:
                pass
        except Exception as e:
            LOG.debug(f"Memory importance check failed: {e}")

    async def _send_with_retry(self, recipient: str, message: str, tier: UserTier) -> Optional[int]:
        """Send message with one retry after 5s on failure. Queues if rate limited.

        Returns the sent message's Signal timestamp (None if queued, failed,
        or not reported) - single-message mode edits that message later.

        Note: Messages queued due to rate limiting will NOT be delivered unless
        the queue is flushed. Queue flushing happens automatically when a user
        message triggers _on_signal_message(), but background operations (cron,
        subagent) must explicitly call _flush_channel_queue() after this method
        to ensure queued messages are delivered.
        """
        debug_logger = safe_get_debug_logger()
        if debug_logger:
            debug_logger.log("outgoing_message", recipient, {
                "recipient": recipient,
                "message": message[:500] if message else "",
            })
        if not self._check_rate_limit(recipient, tier):
            LOG.warning(f"Rate limit exceeded for {recipient}, queuing message")
            if recipient not in self._channel_message_queue:
                self._channel_message_queue[recipient] = []
            self._channel_message_queue[recipient].append(message)
            return None
        try:
            ts = await self._signal_client.send_message(recipient, message)
            self._messages_sent[recipient] = self._messages_sent.get(recipient, 0) + 1
            return ts
        except Exception as e:
            LOG.warning(f"Signal send failed, retrying in 5s: {e}")
            await asyncio.sleep(5)
            try:
                ts = await self._signal_client.send_message(recipient, message)
                self._messages_sent[recipient] = self._messages_sent.get(recipient, 0) + 1
                return ts
            except Exception as e2:
                LOG.error(f"Signal send retry failed: {e2}")
                if debug_logger:
                    debug_logger.record_error(f"signal_send_failed:{recipient}")
                return None

    async def _send_edit(self, recipient: str, target_ts: int, message: str) -> bool:
        """Edit an earlier Signal message (single-message mode).

        Never retried or rate-limited: on failure TurnOutput falls back to a
        new message (which gets the normal retry). Edits still count toward
        MAX_MESSAGES_PER_SESSION (runaway guard).
        """
        debug_logger = safe_get_debug_logger()
        if debug_logger:
            debug_logger.log("outgoing_message", recipient, {
                "recipient": recipient,
                "message": message[:500] if message else "",
                "edit_timestamp": target_ts,
            })
        try:
            await self._signal_client.send_message(recipient, message, edit_timestamp=target_ts)
        except Exception as e:
            LOG.warning(f"Signal edit of {target_ts} failed: {e}")
            return False
        self._messages_sent[recipient] = self._messages_sent.get(recipient, 0) + 1
        return True

    def _new_turn_output(self, recipient: str, tier: UserTier, mode: str) -> TurnOutput:
        """Per-turn single-message state for one chat turn / background run."""
        sm = config.chat.single_message

        async def send_new(text: str) -> Optional[int]:
            return await self._send_with_retry(recipient, text, tier)

        async def send_edit(target_ts: int, text: str) -> bool:
            return await self._send_edit(recipient, target_ts, text)

        def mark_hidden(row_id: str) -> None:
            try:
                self._db_ops.set_message_user_visible(row_id, recipient, False)
            except Exception as e:
                LOG.warning(f"Could not flag message {row_id} as replaced: {e}")

        return TurnOutput(
            mode, send_new, send_edit, mark_hidden,
            separator=sm.separator,
            max_edits=sm.max_edits_per_message,
            max_chars=sm.max_message_chars,
        )

    async def _send_turn_text(
        self,
        context: Context,
        recipient: str,
        message: str,
        tier: UserTier,
        row_id: Optional[str] = None,
        notice: bool = False,
    ) -> None:
        """Send one in-turn text, through the turn's TurnOutput unless the
        message mode is off. notice=True for canned harness notices: always
        appended, never replacing the partial answer."""
        speak = self._voice_speakers.get(recipient)
        if speak is not None and getattr(context, "lane", None) != "background":
            speak(message)
        turn_output = getattr(context, "turn_output", None)
        if not isinstance(turn_output, TurnOutput) or turn_output.mode == "off":
            await self._send_with_retry(recipient, message, tier)
            return
        await turn_output.send_text(message, row_id=row_id, notice=notice)

    def _store_outgoing_message(self, event: Event, message: str, reply_destination: str, tool_calls: Optional[list[ToolCall]] = None, tool_results: Optional[list[ToolResult]] = None) -> None:
        """Store outgoing message in database."""
        outgoing_event = Event(
            source="agent",
            destination=reply_destination,
            message=message,
            timestamp=event.timestamp + 1,
            group_id=event.group_id,
            attachments=[],
        )
        outgoing_msg = Message(event=outgoing_event, is_outgoing=True, processed=True, tool_calls=tool_calls, tool_results=tool_results)
        self._db_ops.store_message(outgoing_msg, reply_destination)

    async def _maybe_compact_session(self, messages: list[dict], anchor_len: int, channel_id: str, source: str) -> None:
        """Compact the in-session working history in place when it exceeds the token
        threshold. The anchor (messages[:anchor_len]) and a recent tail are kept
        verbatim; the middle is replaced by one PROGRESS SUMMARY system message.
        Ephemeral — nothing is persisted. `source` is "cron" or "subagent" for logs."""
        keep_recent = config.chat.min_messages_to_keep
        threshold = config.chat.turn_compaction_threshold_tokens

        working_len = len(messages) - anchor_len
        if working_len <= keep_recent:
            return

        current_tokens = LLMClient.estimate_token_count(messages, "")
        if current_tokens < threshold:
            return

        # Snap the tail boundary forward to the next assistant message so an
        # assistant-with-tool_calls never gets separated from its tool results.
        tail_start = len(messages) - keep_recent
        while tail_start < len(messages) and messages[tail_start]["role"] != "assistant":
            tail_start += 1

        # Need at least one message in the middle and one in the tail.
        if not (anchor_len < tail_start < len(messages)):
            return

        middle = messages[anchor_len:tail_start]
        tail = messages[tail_start:]

        try:
            summary_text = await self._llm_client.summarize_working_history(middle, max_keywords=config.chat.max_keywords)
        except Exception as e:
            LOG.warning(f"SESSION_COMPACTION_FAILED: source={source}, channel={channel_id}, error={e}")
            return

        if not summary_text:
            return

        summary_msg = {"role": "system", "content": prompts.current().render("progress-summary", summary_text=summary_text)}
        messages[:] = messages[:anchor_len] + [summary_msg] + tail
        LOG.info(f"SESSION_COMPACTION: source={source}, channel={channel_id}, tokens_before={current_tokens}, summarized_msgs={len(middle)}, tail_msgs={len(tail)}")

    async def _process_cronjob(self, job) -> None:
        """Process a due cronjob."""
        LOG.info(f"Processing cronjob {job.id} for channel {job.channel_id}")
        context = None
        try:
            context = self._context_manager.get_context(job.channel_id, lane="background")
            self._tool_registry.set_context(context)

            tier = self._auth_manager.get_user_tier(job.creator_number)
            tools = [t for t in self._tool_registry.get_tools_for_tier(tier)
                     if t.name not in VOICE_CALL_ONLY_TOOLS]
            message_mode = resolve_message_mode(job.channel_id)
            context.turn_output = self._new_turn_output(job.channel_id, tier, message_mode)

            soul_content = self._context_manager.get_soul_content(job.channel_id)
            system_prompt = self._llm_client.build_system_prompt(tools, tier.value.capitalize(), soul_content, None, None, job.channel_id, message_mode=message_mode)
            messages = [
                {"role": "system", "content": system_prompt},
            ]

            # Cronjobs start fresh with NO channel summary (unlike subagents).
            messages.append({"role": "user", "content": prompts.current().text("cron-run-preamble") + "\n" + job.prompt})
            anchor_len = len(messages)

            # LLM errors are deliberately NOT caught here (unlike the chat
            # path) - they propagate to this method's own `except Exception`
            # below, which calls mark_complete(success=False) and lets the
            # existing failure-count/threshold machinery handle retries.
            # Catching locally and substituting "LLM temporarily unavailable"
            # used to make every LLM outage look like a successful run: the
            # placeholder text was sent AND stored to the user on every
            # cron tick (e.g. every 20 minutes for a recurring job) instead
            # of being retried quietly.
            response_text, tool_calls = await chat_with_markup_guard(self._llm_client, messages, tools, job.channel_id, source="cron")

            tool_cache = _ToolCallCache()
            max_tool_iterations = _get_max_tool_iterations_for_tier(tier)
            iteration = 0

            while tool_calls and iteration < max_tool_iterations:
                iteration += 1

                assistant_msg: dict = {"role": "assistant", "content": response_text}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {"id": tc.id, "function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in tool_calls
                    ]
                messages.append(assistant_msg)

                for tc in tool_calls:
                    cached_result = tool_cache.lookup(tc)
                    if cached_result is not None:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": _tool_result_content(cached_result, config.chat.effective_tool_result_max_chars),
                        })
                        continue

                    LOG.debug(f"CRON TOOL_EXECUTE: cronjob={job.id}, tool={tc.name}")
                    tool_result = await self._background_consent_result(job.channel_id, await self._tool_registry.execute_tool(
                        tc.name, tc.arguments, tier
                    ))
                    tool_cache.store(tc, tool_result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _tool_result_content(tool_result, config.chat.effective_tool_result_max_chars),
                    })

                await self._maybe_compact_session(messages, anchor_len, job.channel_id, "cron")

                # See the comment on the first chat_with_markup_guard call
                # above: let LLM errors propagate to this method's own
                # except Exception handler instead of masking them as a
                # successful "LLM temporarily unavailable" run.
                response_text, tool_calls = await chat_with_markup_guard(self._llm_client, messages, tools, job.channel_id, source="cron")

            if not response_text or not response_text.strip():
                LOG.info(f"LLM gave empty response text: {response_text}")

            should_discard = self._should_discard_output(response_text, job.channel_id, source="cronjob")
            if should_discard:
                LOG.info(f"__DISCARD_OUTPUT__ detected in cronjob {job.id}, discarding output")
                self._store_background_output(job.channel_id, response_text, "cronjob", job.id, is_discarded=True)
                result = self._cron_scheduler.mark_complete(job.id, success=True, is_recurring=job.is_recurring, interval_minutes=job.interval_minutes, is_cancelled=job.cancelled, scheduled_at=job.scheduled_at)
                return

            self._store_background_output(job.channel_id, response_text, "cronjob", job.id, is_discarded=False)
            if response_text:
                await self._send_turn_text(context, job.channel_id, response_text, tier)
            await self._flush_channel_queue(job.channel_id, tier)
            self._maybe_schedule_summarization(job.channel_id)
            result = self._cron_scheduler.mark_complete(job.id, success=True, is_recurring=job.is_recurring, interval_minutes=job.interval_minutes, is_cancelled=job.cancelled, scheduled_at=job.scheduled_at)
        except asyncio.CancelledError:
            # Deliberate stop via stop_running_activity: treat like a normal
            # successful completion for scheduling purposes (reuses the
            # existing anchor-based reschedule math in mark_complete, resets
            # failure_count, doesn't trip failure-threshold/backoff logic).
            # No reply is sent here - the interactive turn that called
            # stop_running_activity already acknowledged the stop. Swallowed
            # (not re-raised) so this task finishes in a normal, not
            # cancelled, state; the finally block below still runs.
            LOG.info(f"Cronjob {job.id} for {job.channel_id} cancelled via stop_running_activity")
            self._cron_scheduler.mark_complete(job.id, success=True, is_recurring=job.is_recurring, interval_minutes=job.interval_minutes, is_cancelled=job.cancelled, scheduled_at=job.scheduled_at)
        except Exception as e:
            LOG.error(f"Cronjob {job.id} failed: {e}")
            result = self._cron_scheduler.mark_complete(job.id, success=False, is_recurring=job.is_recurring, interval_minutes=job.interval_minutes, is_cancelled=job.cancelled, scheduled_at=job.scheduled_at)
            if result.terminal_action == "one_time_threshold_tripped":
                await self._handle_one_time_threshold_tripped(job)
        finally:
            if context is not None:
                await self._close_run_browser_scope(context)

    async def _background_consent_result(self, channel_id: str, result: ToolResult) -> ToolResult:
        """Background runs (cronjob/subagent): a tool call that asked for the
        users' approval posts the poll and the run goes on without waiting
        (the command was killed before acting). An approving vote re-runs it
        later (_on_background_consent_decision). If an identical command already
        waits on a poll in this channel, no second poll is posted (S7).
        """
        request = result.consent_request
        if not isinstance(request, ConsentRequest):
            return result
        if self._consent.has_pending_background(channel_id, result.name, request.arguments):
            LOG.info(f"Background run in {channel_id}: identical {result.name} command already waits on a poll")
        else:
            pending = await self._consent.request(channel_id, request, result.name, lane="background")
            if pending is None:
                return ToolResult(name=result.name, success=False, output=None, error=CONSENT_POLL_FAILED_ERROR)
        pending_text = prompts.current().text("consent-background-pending")
        return ToolResult(name=result.name, success=False, output=None, error=pending_text)

    async def _on_background_consent_decision(self, pending: PendingConsent, decision: ConsentDecision) -> None:
        """ConsentManager hook for a vote on a background poll (the poll is
        already deleted). "Yes" re-runs the command in a task of its own, so
        the vote handler never waits on it (S4)."""
        if not decision.approved:
            return
        task = asyncio.create_task(self._run_approved_background_command(pending, decision))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_approved_background_command(self, pending: PendingConsent, decision: ConsentDecision) -> None:
        """Run an approved background command again (NIXCLAW_CONSENT=1), no LLM.

        Success with the OK marker: the output goes to the channel verbatim.
        Anything else: one one-time cronjob, due now, created by the voter,
        lets the agent look at the output and tell the users (N5). Runs with
        the voter's tier and a background Context (channel cwd, own browser
        scope), like a cronjob.
        """
        channel_id = pending.channel_id
        context: Optional[Context] = None
        try:
            context = self._context_manager.get_context(channel_id, lane="background")
            self._tool_registry.set_context(context)
            voter_tier = self._auth_manager.get_user_tier(decision.voter_number)
            result = await self._tool_registry.execute_tool(
                pending.tool_name, dict(pending.request.arguments), voter_tier,
                consent_question=pending.question,
            )
            if result.success and result.consent_ok:
                text = (result.output or "").strip()
                if len(text) > CONSENT_OUTPUT_POST_MAX_CHARS:
                    text = text[:CONSENT_OUTPUT_POST_MAX_CHARS] + "…"
                LOG.info(f"Approved background command (poll {pending.poll_ts}) in {channel_id} succeeded")
                if text:
                    await self._signal_client.send_message(channel_id, text)
                    self._store_background_output(channel_id, text, "consent", str(pending.poll_ts))
                return
            output = (result.output if result.success else (result.error or result.output)) or ""
            LOG.info(f"Approved background command (poll {pending.poll_ts}) in {channel_id} failed")
            self._schedule_consent_inspection(pending, decision, output)
        except Exception as e:
            LOG.error(f"Approved background command (poll {pending.poll_ts}) in {channel_id} crashed: {e}")
            self._schedule_consent_inspection(pending, decision, f"Internal error: {e}")
        finally:
            if context is not None:
                await self._close_run_browser_scope(context)

    def _schedule_consent_inspection(self, pending: PendingConsent, decision: ConsentDecision, output: str) -> None:
        """One one-time cronjob, due now, created by the voter, so the agent
        tells the users why an approved command failed (N5). Never raises."""
        try:
            if len(output) > CONSENT_INSPECTION_OUTPUT_MAX_CHARS:
                # The end of the output usually says what went wrong.
                output = "…" + output[-CONSENT_INSPECTION_OUTPUT_MAX_CHARS:]
            prompt = prompts.current().render(
                "consent-inspection", command=pending.request.command, output=output
            )
            job_id = self._cron_scheduler.schedule(
                when=datetime.now(timezone.utc),
                prompt=prompt,
                channel_id=pending.channel_id,
                creator_number=decision.voter_number,
            )
            LOG.info(f"Cronjob {job_id} will report the failed approved command (poll {pending.poll_ts}) in {pending.channel_id}")
        except Exception as e:
            LOG.error(f"Could not schedule the consent inspection cronjob for poll {pending.poll_ts}: {e}")

    async def _close_run_browser_scope(self, context: Context) -> None:
        """Close a background run's ephemeral browser scope (never raises)."""
        try:
            await close_browser_scope(context.channel_id, context.browser_scope)
        except Exception as e:
            LOG.warning(f"Could not close browser scope {context.browser_scope}: {e}")

    async def _handle_one_time_threshold_tripped(self, job) -> None:
        """Handle terminal action for a one-time cronjob that hit its failure threshold.

        Separated into a dedicated method so it can be tested in isolation without
        constructing the full AgentLoop dependency graph.
        """
        try:
            await self._send_one_time_threshold_notification(job)
        except Exception as ne:
            LOG.error(f"Threshold notification path failed for {job.id}: {ne}")
        try:
            self._db_ops.delete_cronjob(job.id)
        except Exception as de:
            LOG.error(f"Failed to delete threshold-tripped cronjob {job.id}: {de}")

    async def _send_one_time_threshold_notification(self, job) -> None:
        """Send a CronJobNotification to the channel when a one-time cronjob hits its failure threshold.

        The HH:MM field is rendered in UTC because scheduled_at is stored UTC.
        TODO: switch to per-channel local wall-clock once timezone tracking is implemented.
        """
        try:
            sched_dt = datetime.fromisoformat(job.scheduled_at)
            if sched_dt.tzinfo is None:
                sched_dt = sched_dt.replace(tzinfo=timezone.utc)
            scheduled_hhmm = sched_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            scheduled_hhmm = str(job.scheduled_at)
        threshold = config.cron.max_failure_count
        msg = prompts.current().render("cron-failed", time=scheduled_hhmm, count=threshold)
        tier = self._auth_manager.get_user_tier(job.creator_number)
        await self._send_with_retry(job.channel_id, msg, tier)
        await self._flush_channel_queue(job.channel_id, tier)

    async def shutdown(self) -> None:
        """Initiate graceful shutdown."""
        LOG.info("Shutdown requested")
        self._shutdown = True
        # Turns paused on an approval poll end now (they delete their polls
        # and post consent-expired-restart; background polls are left to the
        # startup cleanup).
        self._consent.shutdown()

    def spawn_subagent_task(self, prompt: str, channel_id: str, user_tier: str = "KNOWN") -> asyncio.Task:
        """Spawn a subagent task to process a prompt in the background.

        Args:
            prompt: The prompt/task for the subagent
            channel_id: The channel ID for context
            user_tier: The tier of the user spawning the subagent

        Returns:
            asyncio.Task that is processing the subagent

        Raises:
            RuntimeError: If cooldown is active, session limit reached, or
                the channel's background lane is already occupied (by a
                cronjob or another subagent)
        """
        tier = UserTier(user_tier) if isinstance(user_tier, str) else user_tier

        now = datetime.now()
        if channel_id in self._subagent_cooldown:
            last_spawn = self._subagent_cooldown[channel_id]
            elapsed = (now - last_spawn).total_seconds()
            if elapsed < SUBAGENT_COOLDOWN_SECONDS:
                raise RuntimeError(
                    f"Subagent spawning is in cooldown for {channel_id}. "
                    f"Last spawn {elapsed:.0f}s ago, cooldown is {SUBAGENT_COOLDOWN_SECONDS}s."
                )

        session_count = self._subagent_session_count.get(channel_id, 0)
        if session_count >= MAX_SUBAGENT_PER_SESSION:
            raise RuntimeError(
                f"Subagent session limit ({MAX_SUBAGENT_PER_SESSION}) reached for {channel_id}. "
                f"Please wait for existing subagents to complete."
            )

        self._subagent_cooldown[channel_id] = now
        self._subagent_session_count[channel_id] = session_count + 1

        subagent_id = str(uuid.uuid4())

        # spawn_subagent_task is invoked synchronously from the spawn_subagent
        # tool handler, which registry.py's execute_tool runs via
        # asyncio.to_thread - i.e. on a worker thread, off the event loop.
        # asyncio.Task creation and Task.add_done_callback are documented as
        # not thread-safe, so create_task()+_register_channel_task() (which
        # itself calls add_done_callback) must actually execute ON the event
        # loop thread. We hand a small synchronous callback to
        # call_soon_threadsafe to do that, and use a concurrent.futures.Future
        # (thread-safe, unlike asyncio.Future) to synchronously hand the
        # resulting Task back to this worker thread - self._loop is set once
        # in run() before the webhook server (and therefore any tool call)
        # starts, so it is always available here.
        loop = self._loop
        if loop is None:
            raise RuntimeError("spawn_subagent_task called before AgentLoop.run() started the event loop")

        registered: "concurrent.futures.Future[asyncio.Task]" = concurrent.futures.Future()

        def _create_and_register_on_loop() -> None:
            try:
                # The background-lane busy-check must happen HERE, on the
                # event-loop thread, not in the worker-thread body of
                # spawn_subagent_task above: reading
                # self._channel_background_task/_channel_background_meta from
                # the worker thread with no synchronization, with an
                # unbounded gap before this function actually runs, would let
                # two concurrent spawns (e.g. a subagent spawn racing a
                # cron-tick for the same channel) both observe the lane as
                # free and both register, silently overwriting each other's
                # dict entries - defeating the lane's mutual exclusion.
                # Checking immediately before create_task(), on the loop
                # thread, closes that race.
                active_background = self._channel_background_task.get(channel_id)
                if active_background is not None and not active_background.done():
                    occupied_by = self._channel_background_meta.get(channel_id, {}).get("source_type", "another background task")
                    raise RuntimeError(
                        f"A background task ({occupied_by}) is already running for {channel_id}. "
                        f"Wait for it to finish, or stop it with stop_running_activity."
                    )
                task = loop.create_task(self._process_subagent(prompt, channel_id, tier))
                self._register_channel_task(
                    channel_id, task, lane="background",
                    meta={"source_type": "subagent", "source_id": subagent_id},
                )
            except Exception as e:
                registered.set_exception(e)
            else:
                registered.set_result(task)

        loop.call_soon_threadsafe(_create_and_register_on_loop)
        return registered.result(timeout=SPAWN_SUBAGENT_REGISTER_TIMEOUT_SECONDS)

    def cancel_background_activity(self, channel_id: str, activity_id: str) -> str:
        """Cancel whatever background task (cronjob or subagent) currently
        occupies channel_id's background lane, after validating `activity_id`
        against what's actually recorded in self._channel_background_meta.

        Distinct from cancel_cronjob (tools/cron.py), which only flips a DB
        flag to stop FUTURE scheduling - this stops whatever's executing
        RIGHT NOW.

        Returns a human-readable confirmation string.

        Raises:
            RuntimeError: no background task is running for this channel, or
                activity_id doesn't match what's actually running (same
                style as spawn_subagent_task's cooldown/session-limit
                checks above).
        """
        task = self._channel_background_task.get(channel_id)
        meta = self._channel_background_meta.get(channel_id)
        if task is None or task.done() or not meta:
            raise RuntimeError(f"No background task is currently running for {channel_id}.")

        recorded_id = meta.get("source_id")
        if recorded_id != activity_id:
            raise RuntimeError(
                f"activity_id '{activity_id}' does not match the currently running "
                f"{meta.get('source_type', 'background')} task"
                + (f" ({recorded_id})" if recorded_id else "")
                + ". Use list_cronjobs, or the background-activity status note, to get the current id."
            )

        source_type = meta.get("source_type", "background")

        # This method is invoked synchronously from the stop_running_activity
        # tool handler, which registry.py's execute_tool runs via
        # asyncio.to_thread - i.e. on a worker thread, off the event loop.
        # asyncio.Task.cancel() is not documented as thread-safe, so schedule
        # it onto the event loop thread via call_soon_threadsafe, same
        # rationale as spawn_subagent_task's use of it above. We don't need
        # Task.cancel()'s return value back on this thread - task.done() was
        # already checked above under the caller's own read of the dicts.
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(task.cancel)
        else:
            task.cancel()

        return f"Cancellation requested for {source_type} task {activity_id} on {channel_id}."

    async def _process_subagent(self, prompt: str, channel_id: str, tier: UserTier) -> None:
        """Process a subagent prompt in the background."""
        LOG.info(f"Processing subagent for channel {channel_id} with tier {tier}")
        context = None
        try:
            # Fresh background Context: the subagent gets its own browser
            # scope, never the spawning chat turn's (contextvars are copied
            # into this task, so set_context must override them here).
            context = self._context_manager.get_context(channel_id, lane="background")
            self._tool_registry.set_context(context)
            tools = [t for t in self._tool_registry.get_tools_for_tier(tier)
                     if t.name not in VOICE_CALL_ONLY_TOOLS]
            message_mode = resolve_message_mode(channel_id)
            context.turn_output = self._new_turn_output(channel_id, tier, message_mode)

            soul_content = self._context_manager.get_soul_content(channel_id)
            system_prompt = self._llm_client.build_system_prompt(tools, tier.value.capitalize(), soul_content, None, None, channel_id, message_mode=message_mode)
            messages = [
                {"role": "system", "content": system_prompt},
            ]

            # D6: Prepend channel summary if available (subagent starts with zero history context)
            summary_data = self._context_manager.get_summary(channel_id)
            if summary_data:
                summary_text, keywords = summary_data
                summary_msg = prompts.current().render("conversation-summary", summary_text=summary_text)
                messages.append({"role": "system", "content": summary_msg})

            messages.append({"role": "user", "content": prompt})
            anchor_len = len(messages)

            response_text = prompts.current().text("task-completed")
            tool_calls: list[ToolCall] = []
            llm_failed = False
            try:
                response_text, tool_calls = await chat_with_markup_guard(self._llm_client, messages, tools, channel_id, source="subagent")
            except Exception as e:
                LOG.error(f"LLM error in subagent: {e}")
                response_text = prompts.current().text("llm-unavailable")
                llm_failed = True

            tool_cache = _ToolCallCache()
            max_tool_iterations = _get_max_tool_iterations_for_tier(tier)
            iteration = 0

            while tool_calls and iteration < max_tool_iterations:
                iteration += 1

                assistant_msg: dict = {"role": "assistant", "content": response_text}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {"id": tc.id, "function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in tool_calls
                    ]
                messages.append(assistant_msg)

                for tc in tool_calls:
                    cached_result = tool_cache.lookup(tc)
                    if cached_result is not None:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": _tool_result_content(cached_result, config.chat.effective_tool_result_max_chars),
                        })
                        continue

                    LOG.debug(f"SUBAGENT TOOL_EXECUTE: channel={channel_id}, tool={tc.name}")
                    tool_result = await self._background_consent_result(channel_id, await self._tool_registry.execute_tool(tc.name, tc.arguments, tier))
                    tool_cache.store(tc, tool_result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _tool_result_content(tool_result, config.chat.effective_tool_result_max_chars),
                    })

                await self._maybe_compact_session(messages, anchor_len, channel_id, "subagent")

                try:
                    response_text, tool_calls = await chat_with_markup_guard(self._llm_client, messages, tools, channel_id, source="subagent")
                except Exception as e:
                    LOG.error(f"LLM error during subagent tool execution: {e}")
                    response_text = prompts.current().text("llm-unavailable")
                    llm_failed = True
                    break

            should_discard = self._should_discard_output(response_text, channel_id, source="subagent")
            if not should_discard and response_text:
                self._store_background_output(channel_id, response_text, "subagent", f"subagent-{id(self)}", is_discarded=False)
                await self._send_turn_text(context, channel_id, response_text, tier, notice=llm_failed)
            elif should_discard:
                self._store_background_output(channel_id, response_text, "subagent", f"subagent-{id(self)}", is_discarded=True)
            await self._flush_channel_queue(channel_id, tier)
            self._maybe_schedule_summarization(channel_id)
        except asyncio.CancelledError:
            # Deliberate stop via stop_running_activity: no scheduler
            # bookkeeping needed, just skip sending/storing output. No reply
            # is sent - the interactive turn that called stop_running_activity
            # already acknowledged the stop. Swallowed (not re-raised) so
            # this task finishes in a normal, not cancelled, state; the
            # finally block below still runs.
            LOG.info(f"Subagent for {channel_id} cancelled via stop_running_activity")
        except Exception as e:
            LOG.error(f"Subagent failed for {channel_id}: {e}")
        finally:
            self._subagent_session_count[channel_id] = max(0, self._subagent_session_count.get(channel_id, 1) - 1)
            if context is not None:
                await self._close_run_browser_scope(context)

    async def _send_typing(self, recipient: str, start: bool) -> None:
        """Send typing indicator, logging but not raising on failure."""
        try:
            await self._signal_client.send_typing_indicator(recipient, start)
        except Exception as e:
            LOG.warning(f"Typing indicator failed: {e}")
