import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from enum import Enum

if TYPE_CHECKING:
    from .turn_output import TurnOutput


class UserTier(Enum):
    PRIVILEGED = "privileged"
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass
class Attachment:
    id: str
    content_type: str
    filename: Optional[str]
    local_path: Optional[str] = None
    size: int = 0


@dataclass
class Event:
    source: str
    destination: str
    message: str
    timestamp: int
    group_id: Optional[str]
    attachments: list[Attachment] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    # Original Signal envelope timestamp in ms (``timestamp`` is truncated to
    # seconds); needed to send a read receipt. None if unknown.
    signal_timestamp_ms: Optional[int] = None
    # Sender's Signal profile name (envelope.sourceName), if known.
    source_name: Optional[str] = None
    # "signal" (webhook), "web" (web interface, see web/api.py) or "voice"
    # (transcribed utterance of a voice call, see voice/).
    origin: str = "signal"
    # chat_log row of this message (web interface history), when published.
    chat_log_id: Optional[int] = None
    # Unique id of a voice utterance (dedup key; no Signal timestamp).
    voice_id: Optional[str] = None


@dataclass
class PollVote:
    """A vote (or vote removal) on one of our Signal polls.

    ``voter`` is the envelope sender (not ``pollVote.author``, which is the
    poll's creator). ``option_indexes`` empty = vote removed. ``channel_id``
    is derived like chat events (``signal-group:<groupId>`` or the voter's
    number for 1:1).
    """
    channel_id: str
    voter: str
    voter_name: Optional[str]
    target_ts: int
    option_indexes: list[int]
    vote_count: int


@dataclass
class Message:
    event: Event
    is_outgoing: bool
    processed: bool
    tool_calls: Optional[list["ToolCall"]] = None
    tool_results: Optional[list["ToolResult"]] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    is_discarded: bool = False
    # False once a later text replaced this one on the user's screen
    # (single-message "replace" mode); rendered as a marker in history.
    user_visible: bool = True


@dataclass
class Context:
    channel_id: str
    messages_db: Path
    attachments_dir: Path
    soul_path: Optional[Path] = None
    memory_dir: Optional[Path] = None
    # Unique per-turn identifier (one Context = one turn of processing: an
    # inbound message, a cronjob run, or a subagent run).
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Which per-channel lane this turn runs in (see core.py Concurrency):
    # "interactive" (chat) or "background" (cronjob/subagent). Determines
    # the browser scope below.
    lane: str = "interactive"
    # Per-turn Signal output state (single-message mode, see turn_output.py);
    # set by core.py when the turn starts, None outside a turn.
    turn_output: Optional["TurnOutput"] = None

    @property
    def browser_scope(self) -> str:
        """Key of this turn's BrowserContext/page (tools/browser_client.py).

        Chat turns share the channel's persistent scope (cookies saved back
        to the channel's storage_state.json); each background run gets its
        own ephemeral scope, so a cronjob/subagent never drives the page a
        chat turn on the same channel is using (and vice versa).
        """
        if self.lane == "background":
            return f"{self.channel_id}#run:{self.turn_id}"
        return self.channel_id


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict


@dataclass
class ConsentRequest:
    """A bash script asked for the users' approval before acting (consent.py).

    ``user_text`` is the script's other output (marker line removed, capped),
    shown to the users above the poll. ``arguments`` are the tool arguments
    needed to run the command again once approved (cmd, timeout).
    """
    question: str
    user_text: str
    command: str
    arguments: dict


@dataclass
class ToolResult:
    name: str
    success: bool
    output: Optional[str]
    error: Optional[str]
    # Set by bash when the script printed the consent marker; the command was
    # killed before doing anything (see consent.py).
    consent_request: Optional[ConsentRequest] = None
    # Set by bash when an approved run printed the consent-OK marker.
    consent_ok: bool = False


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class PreprocessedMessage:
    """Result of AgentLoop._preprocess_incoming (core.py).

    `event` is None when the inbound message was fully handled and needs no
    further action (duplicate, loop-suppressed, or an unmentioned group
    message). `abort_loop` is True when a canned shortcut (stop/reset
    keyword) already sent its own reply - any tool loop currently running
    for this channel should terminate immediately rather than have this
    content injected as a new turn.
    """
    event: Optional[Event]
    reply_destination: str
    abort_loop: bool = False
