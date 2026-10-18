"""Chat history + live fan-out for the web interface.

Every chat event the web interface shows goes through ChatBus, which writes
it to the ``chat_log`` table (the only writer) and pushes it to the open SSE
streams of every logged-in user who may see the channel:

  * inbound messages, published once at entry (core.py ``_on_signal_message``
    for Signal, ``submit_web_message`` for the web), later updated with the
    saved attachment names and voice transcripts;
  * everything the agent sends, reported by signal.py's outbound listener
    (``on_outbound``): texts, edits, attachments, polls, remote deletes and
    typing (typing is live-only, never stored).

Each insert/update bumps the row's global ``rev``, which is the SSE event id;
a reconnecting client replays ``rev > Last-Event-ID`` from the table.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import config
from ..db import operations as db
from ..path_safety import is_within_directory
from ..tools.paths import get_attachments_dir
from ..types import Attachment, Event
from .channels import MembershipCache, encode_cid

LOG = logging.getLogger(__name__)

# A stream that falls this far behind is closed; the client reconnects and
# replays from the table.
MAX_SUBSCRIBER_BACKLOG = 1000
# Bounded number -> Signal profile name cache.
MAX_NAME_CACHE = 2000

# Queue items: (event name, rev or None, data). "close" ends the stream.
QueueItem = tuple[str, Optional[int], dict]


def now_ms() -> int:
    return int(time.time() * 1000)


def attachments_dir(channel_id: str) -> Path:
    """Sandboxed attachments dir of a channel (ValueError on traversal)."""
    return get_attachments_dir(channel_id)


def attachment_path(channel_id: str, name: str) -> Optional[Path]:
    """Path of a file in the channel's attachments dir; None if `name` is unsafe."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        return None
    try:
        base = attachments_dir(channel_id)
    except ValueError:
        return None
    path = base / name
    if not is_within_directory(path, base):
        return None
    return path


def _attachment_json(channel_id: str, entry: dict) -> dict:
    name = str(entry.get("name") or "")
    path = attachment_path(channel_id, name)
    return {
        "name": name,
        "content_type": entry.get("content_type") or "application/octet-stream",
        "size": int(entry.get("size") or 0),
        "transcript": entry.get("transcript"),
        "exists": bool(path is not None and path.is_file()),
    }


def message_json(row: Any) -> dict:
    """API Message object for a chat_log row."""
    channel_id = row["channel_id"]
    try:
        attachments = json.loads(row["attachments"] or "[]")
    except json.JSONDecodeError:
        attachments = []
    poll = None
    if row["poll"]:
        try:
            poll = json.loads(row["poll"])
        except json.JSONDecodeError:
            poll = None
    return {
        "id": row["id"],
        "rev": row["rev"],
        "cid": encode_cid(channel_id),
        "sender": row["sender"],
        "sender_name": row["sender_name"],
        "origin": row["origin"],
        "text": row["text"] or "",
        "attachments": [_attachment_json(channel_id, a) for a in attachments if isinstance(a, dict)],
        "poll": poll,
        "edited": bool(row["edited"]),
        "deleted": bool(row["deleted"]),
        "created_at": row["created_at"],
    }


@dataclass
class Subscriber:
    """One open SSE stream."""
    number: str
    endpoint_id: int
    queue: "asyncio.Queue[QueueItem]" = field(default_factory=asyncio.Queue)
    closed: bool = False

    def put(self, item: QueueItem) -> None:
        if self.closed:
            return
        if self.queue.qsize() >= MAX_SUBSCRIBER_BACKLOG:
            LOG.warning(f"SSE stream of endpoint {self.endpoint_id} fell behind, closing it")
            self.closed = True
            self.queue.put_nowait(("close", None, {}))
            return
        self.queue.put_nowait(item)


class ChatBus:
    def __init__(self, membership: MembershipCache, bot_number: str) -> None:
        self._membership = membership
        self._bot_number = bot_number
        self._subscribers: list[Subscriber] = []
        self._names: dict[str, str] = {}
        try:
            self._loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # --- names -------------------------------------------------------------

    def note_name(self, number: str, name: Optional[str]) -> None:
        if not number or not name:
            return
        if number not in self._names and len(self._names) >= MAX_NAME_CACHE:
            self._names.pop(next(iter(self._names)))
        self._names[number] = name

    def display_name(self, number: str) -> Optional[str]:
        return self._names.get(number)

    # --- subscribers ---------------------------------------------------------

    def subscribe(self, number: str, endpoint_id: int) -> Subscriber:
        sub = Subscriber(number=number, endpoint_id=endpoint_id)
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        sub.closed = True
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    def connections(self, endpoint_id: int) -> int:
        return sum(1 for s in self._subscribers if s.endpoint_id == endpoint_id and not s.closed)

    def close_endpoint(self, endpoint_id: int) -> None:
        """Endpoint revoked: send `deauthed` and end its streams."""
        self._close_where(lambda s: s.endpoint_id == endpoint_id)

    def close_numbers(self, should_close: Callable[[str], bool]) -> None:
        """End the streams of every number for which `should_close` is true."""
        self._close_where(lambda s: should_close(s.number))

    def _close_where(self, pred: Callable[[Subscriber], bool]) -> None:
        for sub in list(self._subscribers):
            if pred(sub) and not sub.closed:
                sub.put(("deauthed", None, {}))
                sub.put(("close", None, {}))
                sub.closed = True
                self._subscribers.remove(sub)

    def close_all(self) -> None:
        """Shutdown: end every stream (clients reconnect later, no `deauthed`)."""
        for sub in list(self._subscribers):
            sub.put(("close", None, {}))
            self.unsubscribe(sub)

    def notify_channels(self, numbers: set[str]) -> None:
        """Membership changed: affected clients refetch /api/channels."""
        for sub in list(self._subscribers):
            if sub.number in numbers:
                sub.put(("channels", None, {}))

    def _deliver(self, channel_id: str, item: QueueItem) -> None:
        for sub in list(self._subscribers):
            if self._membership.is_member(sub.number, channel_id):
                sub.put(item)

    def _deliver_row(self, row: Any) -> None:
        if row is None:
            return
        self._deliver(row["channel_id"], ("message", row["rev"], message_json(row)))

    def replay(self, number: str, last_rev: int, limit: int = 500) -> list[Any]:
        """One page of rows changed since `last_rev` in the channels `number`
        may see (ascending by rev; call again with the last rev for more)."""
        return db.chat_log_since(last_rev, self._membership.channel_ids_for(number), limit)

    @staticmethod
    def current_rev() -> int:
        return db.chat_log_max_rev()

    # --- publishing ------------------------------------------------------------

    def _insert(self, values: dict) -> Any:
        values.setdefault("created_at", now_ms())
        row = db.chat_log_insert(values)
        self._deliver_row(row)
        return row

    def _update(self, row_id: int, values: dict) -> Any:
        row = db.chat_log_update(row_id, values)
        self._deliver_row(row)
        return row

    def publish_inbound(self, event: Event, channel_id: str, origin: str) -> int:
        """Store an inbound message (Signal or web) and push it; returns the row id."""
        self.note_name(event.source, event.source_name)
        attachments = [
            {
                "id": a.id,
                # Signal attachments get their on-disk name only once
                # downloaded (update_attachments); until then "" = exists:false.
                "name": Path(a.local_path).name if a.local_path else "",
                "content_type": a.content_type,
                "size": a.size,
                "transcript": None,
            }
            for a in event.attachments
        ]
        text = event.message or ""
        if attachments and text == "[Attachment]":
            text = ""
        row = self._insert({
            "channel_id": channel_id,
            "sender": event.source,
            "sender_name": event.source_name or self.display_name(event.source),
            "origin": origin,
            "text": text,
            "attachments": json.dumps(attachments),
            "signal_ts": event.signal_timestamp_ms,
        })
        return int(row["id"])

    def _update_attachment_entries(self, row_id: int, change: Callable[[list[dict]], bool]) -> None:
        row = db.chat_log_get(row_id)
        if row is None:
            return
        try:
            entries = json.loads(row["attachments"] or "[]")
        except json.JSONDecodeError:
            return
        if change(entries):
            self._update(row_id, {"attachments": json.dumps(entries)})

    def update_attachments(self, row_id: int, saved: list[Attachment]) -> None:
        """Downloaded attachments: record their on-disk names and sizes."""
        by_id = {a.id: a for a in saved}

        def change(entries: list[dict]) -> bool:
            changed = False
            for entry in entries:
                att = by_id.get(str(entry.get("id")))
                if att is None or not att.local_path:
                    continue
                new = {"name": Path(att.local_path).name, "size": att.size, "content_type": att.content_type}
                if any(entry.get(k) != v for k, v in new.items()):
                    entry.update(new)
                    changed = True
            return changed

        self._update_attachment_entries(row_id, change)

    def set_transcript(self, row_id: int, attachment_id: str, transcript: str) -> None:
        """Voice message transcript (kept after the audio file expires)."""
        def change(entries: list[dict]) -> bool:
            for entry in entries:
                if entry.get("id") == attachment_id:
                    if entry.get("transcript") == transcript:
                        return False
                    entry["transcript"] = transcript
                    return True
            return False

        self._update_attachment_entries(row_id, change)

    def on_outbound(self, kind: str, payload: dict[str, Any]) -> None:
        """signal.py outbound listener: sync, never raises, hops onto our loop."""
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is None:
            self._loop = running
        if self._loop is not None and running is not self._loop:
            try:
                self._loop.call_soon_threadsafe(self._handle_outbound, kind, payload)
            except RuntimeError as e:
                LOG.warning(f"Dropping outbound {kind} event: {e}")
            return
        self._handle_outbound(kind, payload)

    def _handle_outbound(self, kind: str, payload: dict[str, Any]) -> None:
        try:
            self._apply_outbound(kind, payload)
        except Exception as e:
            LOG.warning(f"Publishing outbound {kind} to the web interface failed: {e}")

    def _agent_row(self, channel_id: str, **values: Any) -> dict:
        return {
            "channel_id": channel_id,
            "sender": self._bot_number,
            "sender_name": config.agent.name,
            "origin": "agent",
            **values,
        }

    def _apply_outbound(self, kind: str, payload: dict[str, Any]) -> None:
        channel_id = str(payload.get("recipient") or "")
        if not channel_id:
            return
        if kind == "typing":
            self._deliver(channel_id, ("typing", None, {
                "cid": encode_cid(channel_id), "active": bool(payload.get("active")),
            }))
        elif kind == "message":
            self._insert(self._agent_row(channel_id, text=payload.get("text") or "", signal_ts=payload.get("ts")))
        elif kind == "edit":
            edit_ts = payload.get("edit_ts")
            row = db.chat_log_find_by_ts(channel_id, edit_ts) if edit_ts is not None else None
            if row is None:
                self._insert(self._agent_row(channel_id, text=payload.get("text") or "",
                                             signal_ts=edit_ts, edited=1))
            else:
                self._update(row["id"], {"text": payload.get("text") or "", "edited": 1})
        elif kind == "attachment":
            path = Path(str(payload.get("file_path") or ""))
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            entry = {"id": path.name, "name": path.name, "content_type": payload.get("content_type"),
                     "size": size, "transcript": None}
            self._insert(self._agent_row(channel_id, text=payload.get("caption") or "",
                                         attachments=json.dumps([entry]), signal_ts=payload.get("ts")))
        elif kind == "poll":
            poll = {"question": payload.get("question") or "", "answers": list(payload.get("answers") or []),
                    "ts": payload.get("ts"), "closed": False}
            self._insert(self._agent_row(channel_id, poll=json.dumps(poll), signal_ts=payload.get("ts")))
        elif kind == "delete":
            ts = payload.get("ts")
            row = db.chat_log_find_by_ts(channel_id, ts) if ts is not None else None
            if row is None:
                return
            values: dict[str, Any] = {"deleted": 1}
            if row["poll"]:
                try:
                    poll = json.loads(row["poll"])
                    poll["closed"] = True
                    values["poll"] = json.dumps(poll)
                except (json.JSONDecodeError, TypeError):
                    pass
            self._update(row["id"], values)
