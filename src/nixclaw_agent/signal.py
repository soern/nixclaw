"""Signal client for nixclaw-agent using signal-cli-rest-api."""

import asyncio
import base64
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import aiohttp.web as web
import httpx

from .config import config
from .path_safety import is_within_directory
from .types import Attachment, Event, PollVote

LOG = logging.getLogger(__name__)

DEBOUNCE_TIMEOUT_MS = 5000

# Outbound listener (web interface, web/bus.py): called with (kind, payload)
# after every successful send of ANY SignalClient instance (main.py's and
# tools/signal_ops.py's module-level one). kinds: "message" {recipient, text,
# ts}, "edit" {recipient, text, edit_ts}, "attachment" {recipient, file_path,
# content_type, caption, ts}, "poll" {recipient, question, answers, ts},
# "delete" {recipient, ts}, "typing" {recipient, active}. Sends with
# publish=False (web mirror copies, login codes) are not reported.
OutboundListener = Callable[[str, dict[str, Any]], None]
_outbound_listener: Optional[OutboundListener] = None


def set_outbound_listener(fn: Optional[OutboundListener]) -> None:
    """Install (or with None remove) the process-wide outbound listener."""
    global _outbound_listener
    _outbound_listener = fn


def _notify_outbound(kind: str, payload: dict[str, Any]) -> None:
    """Report a successful send; never raises into the send path."""
    fn = _outbound_listener
    if fn is None:
        return
    try:
        fn(kind, payload)
    except Exception as e:
        LOG.warning(f"Outbound listener failed for {kind}: {e}")


def parse_send_timestamp(data: Any) -> Optional[int]:
    """Extract the sent message's timestamp (ms) from a /v2/send response body.

    The real signal-cli-rest-api returns ``[{"timestamp": "1791131093291"}]``
    (list, string); the mock returns ``{"status": "ok", "timestamp": <int>}``.
    Returns None when no usable timestamp is present.
    """
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    ts = data.get("timestamp")
    if isinstance(ts, bool):
        return None
    if isinstance(ts, int):
        return ts
    if isinstance(ts, str) and ts.strip().isdigit():
        return int(ts.strip())
    return None


def api_recipient(recipient: str) -> str:
    """Convert an internal recipient id to the signal-cli REST API's format.

    ``signal-group:<id>`` -> ``group.<base64(id)>``; phone numbers unchanged.
    """
    if recipient.startswith("signal-group:"):
        internal_id = recipient.replace("signal-group:", "")
        return "group." + base64.b64encode(internal_id.encode("utf-8")).decode("utf-8")
    return recipient


def _parse_poll_vote(envelope: dict, data_msg: dict) -> Optional[PollVote]:
    """Build a PollVote from an envelope whose dataMessage carries ``pollVote``.

    The voter is the envelope source; ``pollVote.author*`` is the poll's
    creator (us) and is ignored. channel_id mirrors core's
    ``_channel_id_for_event``: ``signal-group:<groupId>`` for groups, else the
    voter's number. Returns None if the vote is unusable.
    """
    raw = data_msg.get("pollVote")
    if not isinstance(raw, dict):
        return None
    LOG.debug(f"Raw pollVote: {raw!r}")
    voter = envelope.get("sourceNumber") or envelope.get("source") or ""
    if not voter:
        return None
    target = raw.get("targetSentTimestamp")
    if isinstance(target, int) and not isinstance(target, bool):
        target_ts = target
    elif isinstance(target, str) and target.strip().isdigit():
        target_ts = int(target.strip())
    else:
        return None
    indexes_raw = raw.get("optionIndexes") or []
    if not isinstance(indexes_raw, list):
        return None
    option_indexes = [i for i in indexes_raw if isinstance(i, int) and not isinstance(i, bool)]
    try:
        vote_count = int(raw.get("voteCount") or 0)
    except (TypeError, ValueError):
        vote_count = 0
    group_info = data_msg.get("groupInfo") or {}
    group_id = group_info.get("groupId") if isinstance(group_info, dict) else None
    channel_id = f"signal-group:{group_id}" if group_id else voter
    return PollVote(
        channel_id=channel_id,
        voter=voter,
        voter_name=envelope.get("sourceName") or None,
        target_ts=target_ts,
        option_indexes=option_indexes,
        vote_count=vote_count,
    )


def _response_timestamp(response: httpx.Response) -> Optional[int]:
    try:
        return parse_send_timestamp(response.json())
    except Exception:
        return None


@dataclass
class SignalClient:
    api_url: str = config.signal.api_url
    phone_number: str = config.signal.phone_number
    webhook_port: int = config.signal.webhook_port
    webhook_secret: Optional[str] = None

    _app: Optional[web.Application] = None
    _runner: Optional[web.AppRunner] = None
    _on_message: Optional[Callable[[Event], Coroutine[Any, Any, None]]] = None
    _on_poll_vote: Optional[Callable[[PollVote], Any]] = None
    _typing_state: dict[str, dict] = field(default_factory=dict)
    _typing_tasks: set[asyncio.Task] = field(default_factory=set)
    _poll_vote_tasks: set[asyncio.Task] = field(default_factory=set)

    async def start_webhook(
        self,
        on_message: Callable[[Event], Coroutine[Any, Any, None]],
        on_poll_vote: Optional[Callable[[PollVote], Any]] = None,
    ) -> None:
        """Start the webhook server (``webhook_port``, default 3002) for Signal messages.

        With a ``webhook_secret`` it listens on all interfaces (the signal-api
        container reaches it via host.docker.internal) and rejects requests
        without the secret. Without one it listens on 127.0.0.1 only, since an
        unauthenticated webhook lets anyone fake messages from any number.

        ``on_poll_vote`` (async) receives poll votes; they are never delivered
        as chat Events. Without it, votes are ignored.
        """
        self._on_message = on_message
        self._on_poll_vote = on_poll_vote
        self._app = web.Application()
        self._app.router.add_get("/health", self._health_handler)
        self._app.router.add_post("/webhook/signal", self._webhook_handler)

        # No access log: the secret travels in the URL query.
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        if self.webhook_secret:
            host = "0.0.0.0"
        else:
            host = "127.0.0.1"
            LOG.warning(
                "No signal.webhook_secret (SIGNAL_WEBHOOK_SECRET) set: webhook listens "
                "on 127.0.0.1 only, so a signal-api container cannot reach it."
            )
        site = web.TCPSite(self._runner, host, self.webhook_port)
        await site.start()

    def _poll_vote_task_done(self, task: asyncio.Task) -> None:
        self._poll_vote_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            LOG.error("Poll vote callback failed", exc_info=exc)

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _webhook_handler(self, request: web.Request) -> web.Response:
        if self.webhook_secret:
            # Header for clients that can set one; ?token= for signal-cli-rest-api,
            # which only takes a URL (RECEIVE_WEBHOOK_URL).
            secret = request.headers.get("X-Signal-Hook-Secret") or request.query.get("token") or ""
            if not hmac.compare_digest(secret.encode(), self.webhook_secret.encode()):
                LOG.warning("Webhook request without valid secret rejected")
                return web.json_response(
                    {"status": "error", "message": "Unauthorized"},
                    status=401,
                )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"status": "error", "message": "Invalid JSON"},
                status=400,
            )

        envelope = payload.get("params", {}).get("envelope") or payload.get("envelope", payload)

        if envelope.get("typingMessage"):
            return web.json_response({"status": "ok"})

        data_msg = envelope.get("dataMessage") or (
            envelope.get("syncMessage") or {}
        ).get("sentMessage")

        if not data_msg:
            return web.json_response({"status": "ok"})

        # Poll votes: only from other users' dataMessage (a syncMessage vote
        # is one of our own linked devices). Never a chat Event.
        if envelope.get("dataMessage") and data_msg.get("pollVote"):
            vote = _parse_poll_vote(envelope, data_msg)
            if vote is None:
                LOG.warning(f"Unparseable pollVote ignored: {data_msg.get('pollVote')!r}")
            else:
                LOG.info(
                    f"Poll vote from {vote.voter} in {vote.channel_id}: "
                    f"target_ts={vote.target_ts} options={vote.option_indexes} "
                    f"vote_count={vote.vote_count}"
                )
                if self._on_poll_vote:
                    task = asyncio.create_task(self._on_poll_vote(vote))
                    self._poll_vote_tasks.add(task)
                    task.add_done_callback(self._poll_vote_task_done)
            return web.json_response({"status": "ok"})
        if any(data_msg.get(k) for k in ("pollVote", "pollCreate", "pollTerminate")):
            # Own-device votes, and poll creation/termination (no chat text).
            return web.json_response({"status": "ok"})

        attachments_data = data_msg.get("attachments", [])
        message = data_msg.get("message", "") or ""
        if not message and not attachments_data:
            return web.json_response({"status": "ok"})

        if not message:
            message = "[Attachment]"
        attachments = []
        if attachments_data:
            for a in attachments_data:
                attachments.append(Attachment(
                    id=a.get("id", ""),
                    content_type=a.get("contentType", "application/octet-stream"),
                    filename=a.get("filename")
                ))

        try:
            source = envelope.get("sourceNumber") or envelope.get("source") or ""
            destination = envelope.get("destinationNumber") or envelope.get("destination", "")
            raw_timestamp = int(envelope.get("timestamp", 0))
            # Keep the original ms timestamp for read receipts
            signal_timestamp_ms = (
                raw_timestamp if raw_timestamp > 10000000000 else None
            )
            # ms to seconds
            timestamp = raw_timestamp // 1000 if signal_timestamp_ms is not None else raw_timestamp
            
            group_info = data_msg.get("groupInfo") or {}
            group_id = group_info.get("groupId") if group_info else None
            
            mentions = []
            for m in data_msg.get("mentions", []):
                if isinstance(m, dict) and "number" in m:
                    mentions.append(m["number"])
            
            if group_id:
                destination = f"signal-group:{group_id}"

            event = Event(
                source=source,
                destination=destination,
                message=message,
                timestamp=timestamp,
                group_id=group_id,
                attachments=attachments,
                mentions=mentions,
                signal_timestamp_ms=signal_timestamp_ms,
                source_name=envelope.get("sourceName") or None,
            )
            LOG.info(f"Incoming message from {source}: {message[:200]}")
        except (KeyError, TypeError, IndexError) as e:
            return web.json_response(
                {"status": "error", "message": f"Failed to parse envelope: {e}"},
                status=400,
            )

        if self._on_message:
            asyncio.create_task(self._on_message(event))

        return web.json_response({"status": "ok"})

    async def send_message(
        self, recipient: str, message: str, edit_timestamp: Optional[int] = None,
        publish: bool = True,
    ) -> Optional[int]:
        """Send message via signal-cli REST API.

        With ``edit_timestamp`` (the original message's timestamp) the message
        edits that earlier message instead of sending a new one; the original's
        attachments are kept. Returns the sent message's timestamp, if known.
        ``publish=False`` keeps the send out of the web interface.
        """
        body: dict[str, Any] = {
            "message": message,
            "number": self.phone_number,
        }
        if edit_timestamp is not None:
            body["edit_timestamp"] = edit_timestamp

        body["recipients"] = [api_recipient(recipient)]
        api_url = self._api_url_for(recipient)

        LOG.info(
            f"Sending signal message to {recipient}: recipients={body['recipients']}, "
            f"message_len={len(message)}, edit_timestamp={edit_timestamp}"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{api_url}/v2/send",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            LOG.info(f"Send response for {recipient}: {response.status_code}")
            response.raise_for_status()
            ts = _response_timestamp(response)
        if publish:
            if edit_timestamp is not None:
                _notify_outbound("edit", {"recipient": recipient, "text": message, "edit_ts": edit_timestamp})
            else:
                _notify_outbound("message", {"recipient": recipient, "text": message, "ts": ts})
        return ts

    async def send_read_receipt(self, sender: str, timestamp_ms: int) -> None:
        """Mark a received message as read (POST /v1/receipts/{number}).

        ``timestamp_ms`` is the message's original envelope timestamp. Never
        raises: a failed receipt only logs a warning.
        """
        api_url = self._api_url_for(sender)

        body = {"receipt_type": "read", "recipient": sender, "timestamp": timestamp_ms}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{api_url}/v1/receipts/{self.phone_number}", json=body
                )
            if response.status_code >= 400:
                LOG.warning(
                    f"Read receipt for {sender} ts={timestamp_ms} failed: "
                    f"{response.status_code} {response.text[:200]}"
                )
        except Exception as e:
            LOG.warning(f"Read receipt for {sender} ts={timestamp_ms} failed: {e}")

    async def send_attachment(
        self,
        recipient: str,
        file_path: str,
        content_type: str,
        caption: Optional[str] = None,
        publish: bool = True,
    ) -> Optional[int]:
        """Send attachment via signal-cli REST API.

        Returns the sent message's timestamp, if the API reports one.

        Args:
            recipient: Phone number or group ID to send to
            file_path: Path to the file to send
            content_type: MIME type of the file (e.g., 'image/jpeg', 'application/pdf')
            caption: Optional text caption to include with the attachment
            publish: False keeps the send out of the web interface
        """
        file_data = Path(file_path).read_bytes()
        b64_data = base64.b64encode(file_data).decode("utf-8")
        filename = Path(file_path).name
        # signal-cli-rest-api's OpenAPI spec documents attachments as
        # "data:<MIME-TYPE>;filename=<FILENAME>;base64,<BASE64 DATA>" - a bare
        # base64 string forces the receiving client to sniff magic bytes, which
        # can misdetect ambiguous containers (e.g. fragmented-MP4 AAC as video).
        attachment_uri = f"data:{content_type};filename={filename};base64,{b64_data}"

        body: dict[str, Any] = {
            "number": self.phone_number,
            "base64_attachments": [attachment_uri],
        }

        if caption:
            body["message"] = caption

        body["recipients"] = [api_recipient(recipient)]
        api_url = self._api_url_for(recipient)

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{api_url}/v2/send",
                json=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            ts = _response_timestamp(response)
        if publish:
            _notify_outbound("attachment", {
                "recipient": recipient, "file_path": str(file_path), "content_type": content_type,
                "caption": caption, "ts": ts,
            })
        return ts

    def _api_url_for(self, recipient: str) -> str:
        """Base URL for a recipient: ``+0000*`` test numbers go to the mock API."""
        if recipient.startswith("+0000") and config.signal.mock_api_url:
            return config.signal.mock_api_url
        return self.api_url

    async def create_poll(
        self, recipient: str, question: str, answers: list[str], publish: bool = True
    ) -> Optional[int]:
        """Post a single-choice poll (POST /v1/polls/{number}).

        Returns the poll message's timestamp (ms), or None on any failure.
        Never raises.
        """
        body = {
            "recipient": api_recipient(recipient),
            "question": question,
            "answers": list(answers),
            "allow_multiple_selections": False,
        }
        api_url = self._api_url_for(recipient)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{api_url}/v1/polls/{self.phone_number}", json=body
                )
            if response.status_code >= 400:
                LOG.warning(
                    f"Create poll for {recipient} failed: "
                    f"{response.status_code} {response.text[:200]}"
                )
                return None
            ts = _response_timestamp(response)
            if ts is None:
                LOG.warning(f"Create poll for {recipient}: no timestamp in response {response.text[:200]}")
                return None
            LOG.info(f"Poll created for {recipient}: ts={ts} question={question[:100]!r}")
        except Exception as e:
            LOG.warning(f"Create poll for {recipient} failed: {e}")
            return None
        if publish:
            _notify_outbound("poll", {"recipient": recipient, "question": question,
                                      "answers": list(answers), "ts": ts})
        return ts

    async def delete_message(self, recipient: str, timestamp: int, publish: bool = True) -> bool:
        """Remote-delete a message we sent (DELETE /v1/remote-delete/{number}).

        Returns True on 2xx. Never raises.
        """
        body = {"recipient": api_recipient(recipient), "timestamp": int(timestamp)}
        api_url = self._api_url_for(recipient)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(
                    "DELETE", f"{api_url}/v1/remote-delete/{self.phone_number}", json=body
                )
            if 200 <= response.status_code < 300:
                LOG.info(f"Remote-deleted message {timestamp} for {recipient}")
                if publish:
                    _notify_outbound("delete", {"recipient": recipient, "ts": int(timestamp)})
                return True
            LOG.warning(
                f"Remote delete of {timestamp} for {recipient} failed: "
                f"{response.status_code} {response.text[:200]}"
            )
            return False
        except Exception as e:
            LOG.warning(f"Remote delete of {timestamp} for {recipient} failed: {e}")
            return False

    async def send_typing_indicator(self, recipient: str, start: bool = True, publish: bool = True) -> None:
        """Send typing indicator via signal-cli REST API with debouncing.

        Implements debouncing to prevent rapid start/stop cycles:
        - START: Ignored if last start was <500ms ago
        - STOP: Delayed by 500ms, cancelled if START arrives before delay

        Args:
            recipient: The recipient's phone number or group ID
            start: True to start typing, False to stop typing
        """
        now = datetime.now()
        current_state = self._typing_state.get(recipient, {})

        if start:
            last_start = current_state.get("last_start")
            if last_start:
                time_since_start_ms = (now - last_start).total_seconds() * 1000
                if time_since_start_ms < DEBOUNCE_TIMEOUT_MS:
                    return

            if current_state.get("stop_task"):
                current_state["stop_task"].cancel()
                self._typing_tasks.discard(current_state["stop_task"])

            self._typing_state[recipient] = {
                "last_start": now,
                "stop_pending": False,
                "stop_task": None,
            }

            api_url = self._api_url_for(recipient)

            async with httpx.AsyncClient(timeout=10.0) as client:
                body = {"recipient": api_recipient(recipient)}
                response = await client.put(
                    f"{api_url}/v1/typing-indicator/{self.phone_number}",
                    json=body,
                )
                response.raise_for_status()
            if publish:
                _notify_outbound("typing", {"recipient": recipient, "active": True})
        else:
            if current_state.get("stop_pending"):
                return

            api_url = self._api_url_for(recipient)

            async def delayed_stop() -> None:
                await asyncio.sleep(DEBOUNCE_TIMEOUT_MS / 1000.0)
                state = self._typing_state.get(recipient, {})
                if not state.get("stop_pending"):
                    return

                self._typing_state[recipient] = {
                    **state,
                    "stop_pending": False,
                    "stop_task": None,
                }

                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        response = await client.request(
                            "DELETE",
                            f"{api_url}/v1/typing-indicator/{self.phone_number}",
                            json={"recipient": api_recipient(recipient)},
                        )
                        response.raise_for_status()
                except Exception:
                    return
                if publish:
                    _notify_outbound("typing", {"recipient": recipient, "active": False})

            stop_task = asyncio.create_task(delayed_stop())
            self._typing_tasks.add(stop_task)
            stop_task.add_done_callback(self._typing_tasks.discard)

            self._typing_state[recipient] = {
                **current_state,
                "stop_pending": True,
                "stop_task": stop_task,
            }

    async def download_attachment(self, attachment_id: str) -> bytes:
        """Download attachment from signal-cli.

        If the live API returns 404 and `config.signal.mock_api_url` is set,
        fall back to the mock API (used by tests where the mock serves
        fixture files via /v1/attachments/{fixture_filename}).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{self.api_url}/v1/attachments/{attachment_id}")
            if response.status_code != 404 or not config.signal.mock_api_url:
                response.raise_for_status()
                return response.content
            # Fall back to mock API (testing).
            LOG.info(f"Attachment {attachment_id} not found on live API; falling back to mock")
            response = await client.get(
                f"{config.signal.mock_api_url}/v1/attachments/{attachment_id}"
            )
            response.raise_for_status()
            return response.content

    async def save_attachment(
        self, attachment_id: str, content_type: str, channel_id: str,
        original_filename: Optional[str] = None
    ) -> Optional[Attachment]:
        """Download and save attachment to channel attachments directory.

        Args:
            attachment_id: Signal attachment ID
            content_type: MIME type of the file
            channel_id: Channel ID for the attachments directory
            original_filename: Optional original filename from Signal (to preserve if extension matches)
        """
        data = await self.download_attachment(attachment_id)
        ext = content_type.split("/")[-1] if "/" in content_type else "bin"

        # Use original filename if it has the correct extension, otherwise generate safe name
        if original_filename:
            original_ext = Path(original_filename).suffix.lower()
            if original_ext == f".{ext}":
                safe_filename = original_filename
            else:
                safe_filename = f"{attachment_id}.{ext}"
        else:
            safe_filename = f"{attachment_id}.{ext}"

        attachments_base = config.paths.data_dir.resolve()
        attachments_dir = (attachments_base / channel_id / "attachments").resolve()
        
        if not is_within_directory(attachments_dir, attachments_base):
            LOG.error(f"Path traversal detected in channel_id: {channel_id}")
            return None

        attachments_dir.mkdir(parents=True, exist_ok=True)

        filepath = attachments_dir / safe_filename
        counter = 1
        while filepath.exists():
            if counter > 1000:
                LOG.error(f"Too many filename conflicts for attachment {attachment_id}")
                return None
            filepath = attachments_dir / f"{attachment_id}_{counter}.{ext}"
            counter += 1

        filepath.write_bytes(data)
        return Attachment(
            id=attachment_id,
            content_type=content_type,
            filename=safe_filename,
            local_path=str(filepath),
            size=len(data)
        )

    async def health_check(self) -> bool:
        """Check if signal-cli REST API is healthy."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.api_url}/v1/health")
            return response.status_code == 200

    async def stop(self) -> None:
        """Stop the webhook server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._app = None