"""Tests for the per-channel turn-serialization gate added in core.py
(fix/channel-turn-serialization) and later split into two independent lanes
by fix/decouple-cron-chat-lanes:

- INTERACTIVE lane (self._channel_active_task): at most one chat turn per
  channel_id at a time; a second inbound chat message on a busy channel
  queues in self._channel_inbound_queue and is drained at tool-loop
  checkpoints (or, failing that, once the active turn's task finishes)
  instead of racing a second task. This is the original fix and is
  unchanged in spirit by the lane split.
- BACKGROUND lane (self._channel_background_task/_channel_background_meta):
  at most one of {cronjob, subagent} per channel_id at a time. A busy
  background lane no longer blocks (or is blocked by) live chat on the same
  channel - the two lanes are fully independent. Cron ticks against a busy
  background lane are skipped (retried next poll); spawn_subagent_task
  against a busy background lane raises RuntimeError.

See AgentLoop._dispatch_or_queue/_register_channel_task/
_on_channel_task_done/_preprocess_incoming/_drain_inbound_checkpoint/
spawn_subagent_task/cancel_background_activity, and doc/ARCHITECTURE.md's
Concurrency Model section.

Uses the shared AgentLoop-construction harness in conftest.py (also used by
test_core_tool_result_wiring.py, test_whitespace_only_response_not_sent.py,
and, in a specialized form, by test_stt_core_integration.py).
"""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import build_mock_agent_loop as _make_agent
from src.nixclaw_agent import core
from src.nixclaw_agent.types import Event, ToolCall, ToolResult, UserTier

pytestmark = pytest.mark.usefixtures("reset_db")


def _event(channel_id: str, message: str, timestamp: int) -> Event:
    """Build a 1:1 (non-group) Event whose channel_id == source, matching
    _channel_id_for_event's non-group branch (destination is the bot's own
    number and is unused on that branch)."""
    return Event(
        source=channel_id,
        destination="+4912345678",
        message=message,
        timestamp=timestamp,
        group_id=None,
        attachments=[],
        mentions=[],
    )


async def _wait_forever(ev: asyncio.Event) -> None:
    """Stand-in for a long-running turn: a task we can complete on demand
    by setting `ev`, used to simulate an "active" channel turn without
    driving the real process_message/_process_cronjob/_process_subagent
    machinery."""
    await ev.wait()


class TestDispatchOrQueue:
    """_dispatch_or_queue / _on_signal_message: the core busy-check gate."""

    @pytest.mark.asyncio
    async def test_second_message_on_busy_channel_is_queued_not_dispatched(self, monkeypatch):
        agent = _make_agent()
        channel_id = "+000000"

        ev = asyncio.Event()
        active_task = asyncio.create_task(_wait_forever(ev))
        agent._register_channel_task(channel_id, active_task)

        process_mock = AsyncMock()
        monkeypatch.setattr(agent, "process_message", process_mock)

        second_event = _event(channel_id, "stop please", 1700000100)
        agent._dispatch_or_queue(channel_id, second_event)

        # No await point crossed the busy-check, so this must be immediate
        # and synchronous: no new task, event queued instead.
        assert agent._channel_active_task[channel_id] is active_task
        assert agent._channel_inbound_queue[channel_id] == [second_event]
        process_mock.assert_not_called()

        ev.set()
        await active_task

    @pytest.mark.asyncio
    async def test_different_idle_channel_dispatches_immediately(self, monkeypatch):
        """Serialization is per-channel, not global: a busy channel A must
        not block a fresh turn from starting on idle channel B."""
        agent = _make_agent()
        busy_channel = "+000000"
        idle_channel = "+000001"

        ev = asyncio.Event()
        active_task = asyncio.create_task(_wait_forever(ev))
        agent._register_channel_task(busy_channel, active_task)

        process_mock = AsyncMock()
        monkeypatch.setattr(agent, "process_message", process_mock)

        idle_event = _event(idle_channel, "hello", 1700000200)
        agent._dispatch_or_queue(idle_channel, idle_event)

        # A new task should have been registered synchronously for the idle
        # channel (not queued).
        assert idle_channel not in agent._channel_inbound_queue
        new_task = agent._channel_active_task[idle_channel]
        assert new_task is not active_task

        await new_task
        process_mock.assert_awaited_once_with(idle_event)

        # The busy channel's own turn/ownership is untouched.
        assert agent._channel_active_task[busy_channel] is active_task

        ev.set()
        await active_task


class TestDrainInboundCheckpoint:
    """_drain_inbound_checkpoint / _preprocess_incoming: what happens to a
    queued message once the active turn reaches a tool-loop checkpoint."""

    @pytest.mark.asyncio
    async def test_queued_message_is_injected_as_steering_text(self):
        agent = _make_agent()
        channel_id = "+000000"
        queued = _event(channel_id, "what about the other file?", 1700000300)
        agent._channel_inbound_queue[channel_id] = [queued]

        messages: list[dict] = []
        abort = await agent._drain_inbound_checkpoint(channel_id, channel_id, messages)

        assert abort is False
        assert channel_id not in agent._channel_inbound_queue
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert "what about the other file?" in messages[0]["content"]

    @pytest.mark.asyncio
    async def test_queued_stop_keyword_aborts_loop_instead_of_injecting(self):
        """A "stop" hit its canned fast-path (abort_loop=True) rather than
        being injected as ordinary steering text - this is the whole point
        of the fix (a racing task used to "stop" the wrong loop)."""
        agent = _make_agent()
        channel_id = "+000000"
        stop_event = _event(channel_id, "stop", 1700000400)
        agent._channel_inbound_queue[channel_id] = [stop_event]

        messages: list[dict] = []
        abort = await agent._drain_inbound_checkpoint(channel_id, channel_id, messages)

        assert abort is True
        assert messages == []
        # The canned "Operation halted..." reply was sent directly.
        agent._signal_client.send_message.assert_awaited_once()
        sent_text = agent._signal_client.send_message.await_args.args[1]
        assert "halted" in sent_text.lower()

    @pytest.mark.asyncio
    async def test_empty_queue_is_a_no_op(self):
        agent = _make_agent()
        channel_id = "+000000"
        messages: list[dict] = []
        abort = await agent._drain_inbound_checkpoint(channel_id, channel_id, messages)
        assert abort is False
        assert messages == []


class TestProcessMessageChecksQueueAtCheckpoint:
    """End-to-end: process_message's own tool-calling loop actually calls
    the checkpoint and reacts to it (not just the checkpoint in isolation)."""

    @pytest.mark.asyncio
    async def test_queued_message_reaches_running_loop_as_synthetic_turn(self):
        agent = _make_agent()
        channel_id = "+000000"

        tool_call = ToolCall(id="1", name="noop_tool", arguments={})
        agent._tool_registry.execute_tool = AsyncMock(
            return_value=ToolResult(name="noop_tool", success=True, output="ok", error=None)
        )

        # First chat() call returns a tool call, which enters the while loop
        # and hits the checkpoint before the second chat() call is made.
        # We inject the queued message from inside that first tool-call's
        # execution so it's present in the queue by the time the *next*
        # loop iteration's checkpoint runs.
        queued_event = _event(channel_id, "actually also check the logs", 1700000500)

        async def _execute_tool_and_queue(name, args, tier):
            agent._channel_inbound_queue[channel_id] = [queued_event]
            return ToolResult(name=name, success=True, output="ok", error=None)

        agent._tool_registry.execute_tool = AsyncMock(side_effect=_execute_tool_and_queue)
        agent._llm_client.chat = AsyncMock(
            side_effect=[
                ("Let me check", [tool_call]),
                ("Checking the logs too", [tool_call]),
                ("Done", []),
            ]
        )

        event = _event(channel_id, "check something", 1700000501)
        await agent.process_message(event)

        # The second chat() call (after the tool ran and queued a message)
        # must have seen the synthetic injected user turn.
        second_call_messages = agent._llm_client.chat.await_args_list[1].args[0]
        injected = [
            m for m in second_call_messages
            if m.get("role") == "user" and "actually also check the logs" in m.get("content", "")
        ]
        assert injected, "queued message was not injected into the running tool loop"


class TestPerChannelMessagesSent:
    """self._messages_sent must be keyed per-channel, not a single scalar
    shared across concurrently-active channels."""

    @pytest.mark.asyncio
    async def test_send_with_retry_tracks_counts_independently_per_recipient(self):
        agent = _make_agent()
        tier = UserTier.PRIVILEGED
        channel_a = "+000000"
        channel_b = "+000001"
        # This test is about _messages_sent bookkeeping, not the unrelated
        # per-minute rate limiter (RATE_LIMITS_PER_TIER) - bypass it so
        # MAX_MESSAGES_PER_SESSION sends all actually go through.
        agent._check_rate_limit = MagicMock(return_value=True)

        for _ in range(core.MAX_MESSAGES_PER_SESSION):
            await agent._send_with_retry(channel_a, "msg", tier)

        assert agent._messages_sent[channel_a] == core.MAX_MESSAGES_PER_SESSION
        assert agent._messages_sent.get(channel_b, 0) == 0

        await agent._send_with_retry(channel_b, "msg", tier)
        assert agent._messages_sent[channel_b] == 1
        assert agent._messages_sent[channel_a] == core.MAX_MESSAGES_PER_SESSION

    @pytest.mark.asyncio
    async def test_saturated_channel_does_not_block_a_different_channels_turn(self):
        agent = _make_agent()
        channel_a = "+000000"
        channel_b = "+000001"

        # Simulate channel A already having hit its per-session send cap
        # from a prior turn.
        agent._messages_sent[channel_a] = core.MAX_MESSAGES_PER_SESSION

        tool_call = ToolCall(id="1", name="noop_tool", arguments={})
        agent._tool_registry.execute_tool = AsyncMock(
            return_value=ToolResult(name="noop_tool", success=True, output="ok", error=None)
        )
        agent._llm_client.chat = AsyncMock(
            side_effect=[
                ("Let me check", [tool_call]),
                ("Done", []),
            ]
        )

        event = _event(channel_b, "hi", 1700000600)
        await agent.process_message(event)

        # Channel B's turn completed normally (both chat() calls happened;
        # the MAX_MESSAGES_PER_SESSION guard inside the tool loop did not
        # fire for channel B).
        assert agent._llm_client.chat.await_count == 2
        # Channel A's counter is untouched by channel B's turn.
        assert agent._messages_sent[channel_a] == core.MAX_MESSAGES_PER_SESSION


class TestInboundQueueCap:
    """_enqueue_inbound: cap + dedup policy."""

    def test_queue_caps_at_max_and_drops_oldest(self):
        agent = _make_agent()
        channel_id = "+000000"
        events = [
            _event(channel_id, f"msg-{i}", 1700001000 + i)
            for i in range(core.MAX_QUEUED_INBOUND_PER_CHANNEL + 3)
        ]
        for ev in events:
            agent._enqueue_inbound(channel_id, ev)

        queue = agent._channel_inbound_queue[channel_id]
        assert len(queue) == core.MAX_QUEUED_INBOUND_PER_CHANNEL
        # The oldest entries were dropped; the queue holds exactly the most
        # recent MAX_QUEUED_INBOUND_PER_CHANNEL messages, still in order.
        expected_tail = events[-core.MAX_QUEUED_INBOUND_PER_CHANNEL:]
        assert [e.message for e in queue] == [e.message for e in expected_tail]

    def test_consecutive_identical_message_is_deduped_not_queued_again(self):
        agent = _make_agent()
        channel_id = "+000000"
        ev1 = _event(channel_id, "same text", 1700002000)
        ev2 = _event(channel_id, "same text", 1700002001)  # same source+message

        agent._enqueue_inbound(channel_id, ev1)
        agent._enqueue_inbound(channel_id, ev2)

        queue = agent._channel_inbound_queue[channel_id]
        assert len(queue) == 1
        assert queue[0] is ev1

    def test_non_consecutive_identical_message_is_not_deduped(self):
        """Dedup only collapses a *consecutive* repeat; if a different
        message was queued in between, both surrounding duplicates queue."""
        agent = _make_agent()
        channel_id = "+000000"
        ev1 = _event(channel_id, "same text", 1700002100)
        ev2 = _event(channel_id, "different", 1700002101)
        ev3 = _event(channel_id, "same text", 1700002102)

        agent._enqueue_inbound(channel_id, ev1)
        agent._enqueue_inbound(channel_id, ev2)
        agent._enqueue_inbound(channel_id, ev3)

        queue = agent._channel_inbound_queue[channel_id]
        assert [e.message for e in queue] == ["same text", "different", "same text"]


class TestOwnershipAndFifo:
    """_on_channel_task_done's identity check for the INTERACTIVE lane: only
    the task actually registered as channel_id's interactive-lane owner may
    clear that slot or drain the inbound queue on completion.

    Note on provenance: this used to describe a real "takeover" scenario
    where a spawned subagent superseded its parent chat turn in the single
    shared slot pre-lanes. Post-lane-split that scenario no longer exists:
    spawn_subagent_task registers into the independent BACKGROUND lane and
    never touches self._channel_active_task (see
    TestSpawnSubagentTaskBackgroundLaneRegistration below), so the
    interactive lane can no longer be "taken over" by a subagent. What
    follows is now a synthetic setup - two tasks registered back-to-back
    into the interactive lane directly via _register_channel_task - used
    purely to exercise _on_channel_task_done's defensive identity check: a
    stale done-callback from a no-longer-owning task must not clear a newer
    owner's slot, and must not corrupt FIFO ordering of the inbound queue by
    popping-and-reappending its oldest entry while the real owner is still
    running.
    """

    @pytest.mark.asyncio
    async def test_non_owner_completion_does_not_clear_ownership_or_touch_queue(self):
        agent = _make_agent()
        channel_id = "+000000"

        # First task registers as interactive-lane owner.
        first_ev = asyncio.Event()
        first_task = asyncio.create_task(_wait_forever(first_ev))
        agent._register_channel_task(channel_id, first_task)

        # A second task is registered into the same interactive lane,
        # superseding the first (last-writer-wins) - synthetic setup (see
        # class docstring), not a path any production code takes anymore,
        # but the right way to exercise the identity check in isolation.
        second_ev = asyncio.Event()
        second_task = asyncio.create_task(_wait_forever(second_ev))
        agent._register_channel_task(channel_id, second_task)
        assert agent._channel_active_task[channel_id] is second_task

        # Messages queue up while the second (real owner) task still runs.
        e1 = _event(channel_id, "first", 1700003000)
        e2 = _event(channel_id, "second", 1700003001)
        agent._enqueue_inbound(channel_id, e1)
        agent._enqueue_inbound(channel_id, e2)

        # The ORIGINAL first task finishes now - it is no longer the
        # registered owner.
        first_ev.set()
        await first_task
        await asyncio.sleep(0)  # let the done-callback run

        # A non-owner finishing must be a complete no-op w.r.t. ownership
        # and the queue.
        assert agent._channel_active_task[channel_id] is second_task
        assert [e.message for e in agent._channel_inbound_queue[channel_id]] == ["first", "second"]

        # Now the actual owner (the second task) finishes: the queue should
        # drain FIFO - e1 (oldest) is dispatched, e2 stays queued.
        process_mock = AsyncMock()
        agent.process_message = process_mock

        second_ev.set()
        await second_task
        await asyncio.sleep(0)  # let the done-callback run and dispatch the next task

        new_task = agent._channel_active_task.get(channel_id)
        assert new_task is not None
        assert new_task is not second_task
        await new_task

        process_mock.assert_awaited_once_with(e1)
        assert [e.message for e in agent._channel_inbound_queue.get(channel_id, [])] == ["second"]


class TestSpawnSubagentTaskBackgroundLaneRegistration:
    """spawn_subagent_task runs off the event-loop thread (via
    asyncio.to_thread from the tool handler) and must safely create+register
    its task back on the loop thread, into the independent BACKGROUND lane -
    NOT the interactive lane. Pre-lanes this used to "take over" the single
    shared slot from the parent chat turn that spawned it; post-lane-split
    the interactive and background lanes are fully independent, so the
    parent turn's interactive-lane ownership must be left completely
    untouched."""

    @pytest.mark.asyncio
    async def test_spawn_subagent_task_registers_into_background_lane_only(self, monkeypatch):
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"

        # An interactive (chat) turn currently owns the interactive lane, as
        # it would while executing the spawn_subagent tool call mid-turn.
        parent_ev = asyncio.Event()
        parent_task = asyncio.create_task(_wait_forever(parent_ev))
        agent._register_channel_task(channel_id, parent_task)

        subagent_started = asyncio.Event()
        subagent_may_finish = asyncio.Event()

        async def _fake_process_subagent(prompt, cid, tier):
            subagent_started.set()
            await subagent_may_finish.wait()

        monkeypatch.setattr(agent, "_process_subagent", _fake_process_subagent)

        # spawn_subagent_task blocks (via concurrent.futures.Future.result)
        # until the event-loop thread has created+registered the task, so it
        # must be driven from a worker thread while this test's event loop
        # keeps running - exactly like the real asyncio.to_thread tool
        # dispatch in registry.py.
        subagent_task = await asyncio.to_thread(
            agent.spawn_subagent_task, "do something", channel_id, "known"
        )

        await subagent_started.wait()

        assert subagent_task is not parent_task
        # Registered into the background lane, with source-tracking meta...
        assert agent._channel_background_task[channel_id] is subagent_task
        assert agent._channel_background_meta[channel_id]["source_type"] == "subagent"
        # ...and the interactive lane (still owned by the parent turn) is
        # completely untouched - no takeover.
        assert agent._channel_active_task[channel_id] is parent_task

        # Cleanup: let both tasks finish.
        parent_ev.set()
        await parent_task
        subagent_may_finish.set()
        await subagent_task


class TestLaneIndependence:
    """The whole point of the lane split: a chat turn and a background
    (cron/subagent) run on the SAME channel_id no longer block each other -
    both lanes can be "active" simultaneously without either blocking the
    other's dispatch."""

    @pytest.mark.asyncio
    async def test_chat_turn_dispatches_and_completes_while_background_task_runs(self, monkeypatch):
        agent = _make_agent()
        channel_id = "+000000"

        # A background task (as if a cronjob or subagent) already occupies
        # the background lane for this channel.
        bg_ev = asyncio.Event()
        bg_task = asyncio.create_task(_wait_forever(bg_ev))
        agent._register_channel_task(
            channel_id, bg_task, lane="background",
            meta={"source_type": "cronjob", "source_id": "job-1"},
        )

        process_mock = AsyncMock()
        monkeypatch.setattr(agent, "process_message", process_mock)

        chat_event = _event(channel_id, "hi, are you there?", 1700004000)
        agent._dispatch_or_queue(channel_id, chat_event)

        # The chat turn dispatched immediately - NOT queued behind the
        # background task.
        assert channel_id not in agent._channel_inbound_queue
        chat_task = agent._channel_active_task[channel_id]
        assert chat_task is not bg_task

        await chat_task
        process_mock.assert_awaited_once_with(chat_event)

        # The background lane is completely unaffected by the chat turn
        # starting and completing - both were "active" at once.
        assert agent._channel_background_task[channel_id] is bg_task
        assert not bg_task.done()

        bg_ev.set()
        await bg_task


class TestBackgroundLaneSerialization:
    """The background lane still serializes among itself: a cron tick or
    spawn_subagent_task call against a channel whose background lane is
    already occupied does NOT start a second background task."""

    @pytest.mark.asyncio
    async def test_cron_tick_skips_when_background_lane_busy(self, monkeypatch):
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"

        # Occupy the background lane already, as if a subagent (or a prior
        # cronjob) is still running for this channel. Deliberately NOT
        # wired through _register_channel_task here (which would also add
        # it to self._tasks, which run()'s trailing shutdown-drain
        # `asyncio.gather(*self._tasks)` would then block on forever since
        # we never complete it mid-test) - the run() loop's skip-check below
        # only reads these two dicts directly, so setting them directly is
        # sufficient and keeps this test's cleanup simple.
        busy_ev = asyncio.Event()
        busy_task = asyncio.create_task(_wait_forever(busy_ev))
        agent._channel_background_task[channel_id] = busy_task
        agent._channel_background_meta[channel_id] = {"source_type": "subagent", "source_id": "sub-1"}

        job = SimpleNamespace(id="job-1", channel_id=channel_id)

        # check_due() returns the due job exactly once, then nothing -
        # combined with the asyncio.sleep patch below, this drives exactly
        # one real iteration of AgentLoop.run()'s loop body (actual
        # production code, not reimplemented test logic).
        agent._cron_scheduler.check_due = MagicMock(side_effect=[[job], []])
        agent._signal_client.start_webhook = AsyncMock()

        process_cronjob_mock = AsyncMock()
        monkeypatch.setattr(agent, "_process_cronjob", process_cronjob_mock)

        async def _stop_after_one_tick(_seconds):
            agent._shutdown = True

        monkeypatch.setattr(core.asyncio, "sleep", _stop_after_one_tick)

        await agent.run()

        # The due job was skipped (background lane busy), not dispatched.
        process_cronjob_mock.assert_not_called()
        assert agent._channel_background_task[channel_id] is busy_task

        busy_ev.set()
        await busy_task

    @pytest.mark.asyncio
    async def test_second_spawn_subagent_task_raises_before_second_task_starts(self, monkeypatch):
        """A straightforward (non-concurrent) sequential check: once a
        subagent occupies the background lane, a second spawn_subagent_task
        call for the same channel raises RuntimeError immediately - it must
        NOT create/start a second task first."""
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"
        # Isolate this test from the unrelated cooldown/session-count guards
        # (SUBAGENT_COOLDOWN_SECONDS=60s by default) - this test is about the
        # background-lane busy-check specifically.
        monkeypatch.setattr(core, "SUBAGENT_COOLDOWN_SECONDS", 0)

        first_started = asyncio.Event()
        first_may_finish = asyncio.Event()
        second_started = asyncio.Event()

        async def _fake_process_subagent(prompt, cid, tier):
            if not first_started.is_set():
                first_started.set()
                await first_may_finish.wait()
            else:
                second_started.set()

        monkeypatch.setattr(agent, "_process_subagent", _fake_process_subagent)

        first_task = await asyncio.to_thread(
            agent.spawn_subagent_task, "job one", channel_id, "known"
        )
        await first_started.wait()

        with pytest.raises(RuntimeError, match="already running"):
            await asyncio.to_thread(
                agent.spawn_subagent_task, "job two", channel_id, "known"
            )

        # The rejected second call never got as far as starting its own
        # _process_subagent run, and the lane still holds only the first task.
        assert not second_started.is_set()
        assert agent._channel_background_task[channel_id] is first_task

        first_may_finish.set()
        await first_task

    @pytest.mark.asyncio
    async def test_concurrent_spawns_for_same_channel_exactly_one_wins(self, monkeypatch):
        """Race-reproducing version of the above: two spawn_subagent_task
        calls for the same channel fired concurrently from two worker
        threads (mirroring a real subagent-spawn racing a cron-tick). This
        is the scenario item 1's fix (moving the busy-check onto the
        event-loop thread, inside _create_and_register_on_loop) makes safe:
        with the check left on the worker thread instead, both calls could
        observe the lane as free and both succeed, silently overwriting each
        other's registration. Post-fix, exactly one call must succeed and
        the other must raise - deterministically, not just "usually"."""
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"
        # Isolate this test from the unrelated cooldown guard, same rationale
        # as the sequential version of this test above.
        monkeypatch.setattr(core, "SUBAGENT_COOLDOWN_SECONDS", 0)

        hold = asyncio.Event()
        started_count = 0

        async def _fake_process_subagent(prompt, cid, tier):
            nonlocal started_count
            started_count += 1
            await hold.wait()

        monkeypatch.setattr(agent, "_process_subagent", _fake_process_subagent)

        results = await asyncio.gather(
            asyncio.to_thread(agent.spawn_subagent_task, "job A", channel_id, "known"),
            asyncio.to_thread(agent.spawn_subagent_task, "job B", channel_id, "known"),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, asyncio.Task)]
        errors = [r for r in results if isinstance(r, RuntimeError)]

        assert len(successes) == 1, f"expected exactly one winning spawn, got {results!r}"
        assert len(errors) == 1
        assert "already running" in str(errors[0])
        assert agent._channel_background_task[channel_id] is successes[0]

        hold.set()
        await successes[0]


class TestCancelBackgroundActivity:
    """AgentLoop.cancel_background_activity / the stop_running_activity tool
    wrapper (tools/cron.py): cancels whatever occupies a channel's
    background lane right now, distinct from cancel_cronjob (which only
    stops FUTURE scheduling)."""

    @pytest.mark.asyncio
    async def test_cancels_running_cronjob_and_reschedules_via_mark_complete(self, monkeypatch):
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"

        job = SimpleNamespace(
            id="job-1",
            channel_id=channel_id,
            creator_number="+000000",
            prompt="do the thing",
            scheduled_at="2026-01-01T00:00:00+00:00",
            is_recurring=True,
            interval_minutes=60,
            cancelled=False,
        )

        chat_hang = asyncio.Event()

        async def _hanging_chat(messages, tools, channel_id_arg):
            await chat_hang.wait()
            return "unused", []  # pragma: no cover - never reached, task is cancelled first

        agent._llm_client.chat = AsyncMock(side_effect=_hanging_chat)
        mark_complete_mock = MagicMock(
            return_value=SimpleNamespace(terminal_action="none", job_id="job-1")
        )
        agent._cron_scheduler.mark_complete = mark_complete_mock

        task = asyncio.create_task(agent._process_cronjob(job))
        agent._register_channel_task(
            channel_id, task, lane="background",
            meta={"source_type": "cronjob", "source_id": job.id},
        )

        # Let the task actually reach the hanging chat() call (i.e. it is
        # genuinely running, not merely scheduled) before cancelling.
        await asyncio.sleep(0)

        result = agent.cancel_background_activity(channel_id, "job-1")
        assert "job-1" in result

        await task  # the CancelledError branch swallows it; task ends up done, not cancelled

        assert task.cancelled() is False
        mark_complete_mock.assert_called_once_with(
            "job-1", success=True, is_recurring=True, interval_minutes=60,
            is_cancelled=False, scheduled_at=job.scheduled_at,
        )
        # No duplicate reply from the background lane's own cancellation path.
        agent._signal_client.send_message.assert_not_called()

        await asyncio.sleep(0)  # let the done-callback clear the lane
        assert channel_id not in agent._channel_background_task

    @pytest.mark.asyncio
    async def test_cancels_running_subagent_without_sending_a_message(self, monkeypatch):
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"

        chat_hang = asyncio.Event()

        async def _hanging_chat(messages, tools, channel_id_arg):
            await chat_hang.wait()
            return "unused", []  # pragma: no cover - never reached, task is cancelled first

        agent._llm_client.chat = AsyncMock(side_effect=_hanging_chat)

        task = asyncio.create_task(agent._process_subagent("do it", channel_id, UserTier.PRIVILEGED))
        agent._register_channel_task(
            channel_id, task, lane="background",
            meta={"source_type": "subagent", "source_id": "sub-1"},
        )
        await asyncio.sleep(0)

        result = agent.cancel_background_activity(channel_id, "sub-1")
        assert "sub-1" in result

        await task

        assert task.cancelled() is False
        agent._signal_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_activity_id_errors_and_does_not_cancel_real_task(self, monkeypatch):
        agent = _make_agent()
        agent._loop = asyncio.get_running_loop()
        channel_id = "+000000"

        bg_ev = asyncio.Event()
        bg_task = asyncio.create_task(_wait_forever(bg_ev))
        agent._register_channel_task(
            channel_id, bg_task, lane="background",
            meta={"source_type": "cronjob", "source_id": "real-job-id"},
        )

        with pytest.raises(RuntimeError, match="does not match"):
            agent.cancel_background_activity(channel_id, "wrong-id")

        # Give any (incorrectly) scheduled cancellation a chance to land -
        # there must not be one, but this proves it deterministically rather
        # than just by absence of an immediate exception.
        await asyncio.sleep(0)
        assert not bg_task.cancelled()
        assert not bg_task.done()
        assert agent._channel_background_task[channel_id] is bg_task

        bg_ev.set()
        await bg_task
