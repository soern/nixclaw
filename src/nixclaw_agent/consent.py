"""User consent via Signal polls.

A bash script that must not act without the users' approval (e.g. printing)
prints a consent marker on stderr; tools/bash.py kills it right there and
returns a ToolResult carrying a ConsentRequest. ConsentManager posts the
script's other output plus a Yes/No poll to the channel and resolves the
request with the first vote of a user allowed to use the tool. The approved
command is then run again with NIXCLAW_CONSENT=1 (core.py).

Chat turns wait paused on the poll (core.AgentLoop._pause_for_consent).
Background runs (cronjob/subagent, lane="background") do not wait: nobody
awaits their future, so a timer task owned by this manager deletes the poll
when it expires, and an approving vote is handed to `on_background_decision`
(core.AgentLoop._on_background_consent_decision re-runs the command).

Pending polls live in memory. Only (channel_id, poll_ts, created_at) is stored
(table consent_polls), so the polls still open at a restart can be deleted at
startup (cleanup_at_startup). A graceful shutdown deletes the chat-lane polls
itself and posts the same notice; background-lane rows stay for the startup
cleanup. Either way a channel gets the notice once. Votes on polls not in memory are ignored, so
polls humans created are never touched.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import prompts
from .auth import AuthManager
from .config import config
from .db.operations import DatabaseOperations
from .signal import SignalClient
from .types import ConsentRequest, Event, Message, PollVote

LOG = logging.getLogger(__name__)

# Startup cleanup must not hold up the agent if the Signal API is slow.
STARTUP_CLEANUP_TIMEOUT_SECONDS = 5
# Same for the notice posted when a graceful shutdown deletes a chat poll.
SHUTDOWN_NOTICE_TIMEOUT_SECONDS = 5
# Poll answers are matched by index: 0 approves, 1 rejects.
APPROVE_INDEX = 0
REJECT_INDEX = 1


@dataclass
class ConsentDecision:
    approved: bool
    # Display name of the voter (Signal profile name, else number).
    voter: str
    # The voter's number (tier of the background re-run, creator of a
    # follow-up cronjob).
    voter_number: str = ""


@dataclass
class PendingConsent:
    channel_id: str
    poll_ts: int
    request: ConsentRequest
    tool_name: str
    # "interactive" (a chat turn waits on `future`) or "background".
    lane: str
    # Resolved with the ConsentDecision of the first counting vote, or with
    # None on shutdown.
    future: "asyncio.Future[Optional[ConsentDecision]]"
    # Background lane only: deletes the poll when nobody voted in time.
    expiry_task: "Optional[asyncio.Task[None]]" = None

    @property
    def question(self) -> str:
        return self.request.question


class ConsentManager:
    def __init__(self, signal_client: SignalClient, auth_manager: AuthManager, db_ops: DatabaseOperations):
        self._signal_client = signal_client
        self._auth_manager = auth_manager
        self._db_ops = db_ops
        self._pending: dict[tuple[str, int], PendingConsent] = {}
        # Strong references to the running background expiry timers.
        self._expiry_tasks: set[asyncio.Task] = set()
        # Set by shutdown(): no new polls; deleted chat polls get a notice.
        self._shutting_down = False
        # Channels that got the shutdown notice (once per channel).
        self._shutdown_notified: set[str] = set()
        # Called after a vote resolved a background-lane poll (step 5 runs the
        # approved command there).
        self.on_background_decision: Optional[
            Callable[[PendingConsent, ConsentDecision], Awaitable[None]]
        ] = None

    @staticmethod
    def timeout_seconds(lane: str) -> float:
        """How long a poll waits for a vote (config read now, see N8)."""
        minutes = (
            config.consent.background_timeout_minutes
            if lane == "background"
            else config.consent.timeout_minutes
        )
        return minutes * 60.0

    async def request(
        self,
        channel_id: str,
        request: ConsentRequest,
        tool_name: str,
        lane: str = "interactive",
    ) -> Optional[PendingConsent]:
        """Post the script's output and the approval poll to the channel.

        Sent through SignalClient directly (no rate-limit queue, no
        single-message edits). Returns None if the poll could not be posted
        or the agent is shutting down; then nothing is pending or stored.
        """
        if self._shutting_down:
            LOG.info(f"Not posting consent poll in {channel_id}: shutting down")
            return None
        if request.user_text:
            try:
                await self._signal_client.send_message(channel_id, request.user_text)
            except Exception as e:
                LOG.warning(f"Consent info text for {channel_id} could not be sent: {e}")
        answers = [
            prompts.current().text("consent-answer-yes"),
            prompts.current().text("consent-answer-no"),
        ]
        poll_ts = await self._signal_client.create_poll(channel_id, request.question, answers)
        if poll_ts is None:
            return None
        try:
            self._db_ops.add_consent_poll(channel_id, poll_ts)
        except Exception as e:
            LOG.warning(f"Could not record consent poll {poll_ts} for {channel_id}: {e}")
        self._store_history(channel_id, poll_ts, request)
        pending = PendingConsent(
            channel_id=channel_id,
            poll_ts=poll_ts,
            request=request,
            tool_name=tool_name,
            lane=lane,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[(channel_id, poll_ts)] = pending
        if lane == "background":
            # No paused turn waits on a background poll, so the expiry needs
            # a task of its own.
            timer = asyncio.create_task(self._expire_later(pending, self.timeout_seconds(lane)))
            self._expiry_tasks.add(timer)
            timer.add_done_callback(self._expiry_tasks.discard)
            pending.expiry_task = timer
        LOG.info(f"Consent poll {poll_ts} posted in {channel_id} ({lane}): {request.question!r}")
        return pending

    def has_pending_background(self, channel_id: str, tool_name: str, arguments: dict) -> bool:
        """True if a background poll for the identical command (same tool,
        same arguments) is still open in the channel (S7: no second poll)."""
        return any(
            p.channel_id == channel_id
            and p.lane == "background"
            and p.tool_name == tool_name
            and p.request.arguments == arguments
            for p in self._pending.values()
        )

    async def _expire_later(self, pending: PendingConsent, seconds: float) -> None:
        """Background lane: delete the poll once its timeout passed."""
        await asyncio.sleep(seconds)  # cancelled by a vote, discard or shutdown
        # Cleared first, so discard() does not cancel the running task.
        pending.expiry_task = None
        LOG.info(f"Background consent poll {pending.poll_ts} in {pending.channel_id}: expired")
        try:
            await self.discard(pending)
        except Exception as e:
            LOG.warning(f"Could not discard expired consent poll {pending.poll_ts}: {e}")

    @staticmethod
    def _stop_expiry(pending: PendingConsent) -> None:
        task = pending.expiry_task
        pending.expiry_task = None
        if task is not None and not task.done():
            task.cancel()

    def _store_history(self, channel_id: str, poll_ts: int, request: ConsentRequest) -> None:
        """Store info text + poll as one outgoing message, so later turns know what was asked."""
        tag = f"[Poll: {request.question}]"
        text = f"{request.user_text}\n{tag}" if request.user_text else tag
        try:
            self._db_ops.store_message(
                Message(
                    event=Event(
                        # Unique source: the row id is "<source>:<timestamp>".
                        source=f"agent:poll:{poll_ts}",
                        destination=channel_id,
                        message=text,
                        timestamp=int(time.time()),
                        group_id=None,
                    ),
                    is_outgoing=True,
                    processed=True,
                ),
                channel_id,
            )
        except Exception as e:
            LOG.warning(f"Could not store consent poll {poll_ts} in history: {e}")

    async def on_vote(self, vote: PollVote) -> None:
        """Webhook callback for poll votes.

        Ignored: polls we are not waiting on (human polls, already resolved,
        duplicates), vote removals, unknown answers, and voters not allowed to
        use the tool (the poll stays open for someone who is).
        """
        key = (vote.channel_id, vote.target_ts)
        pending = self._pending.get(key)
        if pending is None:
            LOG.debug(f"Ignoring vote on unknown poll {vote.target_ts} in {vote.channel_id}")
            return
        if not vote.option_indexes or not vote.voter:
            return
        tier = self._auth_manager.get_user_tier(vote.voter)
        if not self._auth_manager.can_use_tool(tier, pending.tool_name):
            LOG.info(f"Ignoring vote by {vote.voter} ({tier.value}) on consent poll {vote.target_ts}: may not use {pending.tool_name}")
            return
        index = vote.option_indexes[0]
        if index not in (APPROVE_INDEX, REJECT_INDEX):
            return
        decision = ConsentDecision(
            approved=index == APPROVE_INDEX,
            voter=vote.voter_name or vote.voter,
            voter_number=vote.voter,
        )
        # Removed before any await, so a duplicate delivery finds nothing.
        del self._pending[key]
        self._stop_expiry(pending)
        if not pending.future.done():
            pending.future.set_result(decision)
        LOG.info(f"Consent poll {pending.poll_ts} in {pending.channel_id}: approved={decision.approved} by {vote.voter}")
        await self._remove_poll(pending)
        if pending.lane == "background" and self.on_background_decision is not None:
            try:
                await self.on_background_decision(pending, decision)
            except Exception as e:
                LOG.error(f"Background consent decision for poll {pending.poll_ts} failed: {e}")

    async def discard(self, pending: PendingConsent) -> None:
        """Drop a poll that ends without a vote (timeout, message, abort, ...).

        No-op once a vote resolved it (the vote already deleted it).
        """
        key = (pending.channel_id, pending.poll_ts)
        if self._pending.get(key) is not pending:
            return
        del self._pending[key]
        self._stop_expiry(pending)
        if not pending.future.done():
            pending.future.cancel()
        await self._remove_poll(pending)
        if self._shutting_down and pending.lane == "interactive":
            await self._post_shutdown_notice(pending.channel_id)

    async def _post_shutdown_notice(self, channel_id: str) -> None:
        """Tell the channel its chat poll ended with the shutdown (best effort,
        short timeout). Skipped if the channel already got it, or if a
        background poll row of the channel is left: the startup cleanup posts
        the notice for that one."""
        if channel_id in self._shutdown_notified:
            return
        try:
            if any(ch == channel_id for ch, _ in self._db_ops.get_consent_polls()):
                LOG.info(f"Shutdown notice for {channel_id} left to the startup cleanup")
                return
        except Exception as e:
            LOG.warning(f"Could not read consent polls for the shutdown notice: {e}")
        self._shutdown_notified.add(channel_id)
        try:
            await asyncio.wait_for(
                self._signal_client.send_message(
                    channel_id, prompts.current().text("consent-expired-restart")
                ),
                timeout=SHUTDOWN_NOTICE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            LOG.warning(f"Could not post consent shutdown notice to {channel_id}: {e}")

    async def _remove_poll(self, pending: PendingConsent) -> None:
        """Delete the poll for everyone and forget its DB row (best effort)."""
        try:
            await self._signal_client.delete_message(pending.channel_id, pending.poll_ts)
        except Exception as e:
            LOG.warning(f"Could not delete consent poll {pending.poll_ts} in {pending.channel_id}: {e}")
        try:
            self._db_ops.delete_consent_poll(pending.channel_id, pending.poll_ts)
        except Exception as e:
            LOG.warning(f"Could not forget consent poll {pending.poll_ts}: {e}")

    def shutdown(self) -> None:
        """End every wait: paused chat turns see None and stop (they delete
        their polls themselves via discard(), which then posts the
        consent-expired-restart notice); background polls stop expiring and
        are cleaned up at the next startup (their DB rows stay). No new polls
        are posted from now on."""
        self._shutting_down = True
        for pending in self._pending.values():
            self._stop_expiry(pending)
            if not pending.future.done():
                pending.future.set_result(None)

    async def cleanup_at_startup(self) -> None:
        """Delete the polls still open from before a restart and tell each
        channel. One attempt per poll: the row is removed either way, so the
        notice is posted only once (a poll whose delete failed stays visible,
        but votes on it are ignored), and only once per channel."""
        try:
            rows = self._db_ops.get_consent_polls()
        except Exception as e:
            LOG.warning(f"Consent poll cleanup skipped: {e}")
            return
        notified: set[str] = set()
        for channel_id, poll_ts in rows:
            try:
                deleted = await asyncio.wait_for(
                    self._signal_client.delete_message(channel_id, poll_ts),
                    timeout=STARTUP_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception as e:
                LOG.warning(f"Startup delete of consent poll {poll_ts} in {channel_id} failed: {e}")
                deleted = False
            if channel_id not in notified:
                notified.add(channel_id)
                try:
                    await asyncio.wait_for(
                        self._signal_client.send_message(
                            channel_id, prompts.current().text("consent-expired-restart")
                        ),
                        timeout=STARTUP_CLEANUP_TIMEOUT_SECONDS,
                    )
                except Exception as e:
                    LOG.warning(f"Could not post consent-expired notice to {channel_id}: {e}")
            try:
                self._db_ops.delete_consent_poll(channel_id, poll_ts)
            except Exception as e:
                LOG.warning(f"Could not forget consent poll {poll_ts}: {e}")
            LOG.info(f"Startup cleanup of consent poll {poll_ts} in {channel_id}: deleted={deleted}")
