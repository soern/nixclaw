"""Background lane (cronjob/subagent): an approval poll never pauses the run
(core.AgentLoop._background_consent_result); an approving vote re-runs the
command in a task of its own (_run_approved_background_command).

Uses the shared AgentLoop harness from conftest.py with a fake LLM and fake
Signal client; ConsentManager is real.
"""

import asyncio
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import build_mock_agent_loop
from src.nixclaw_agent import prompts
from src.nixclaw_agent.config import config
from src.nixclaw_agent.db.operations import DatabaseOperations
from src.nixclaw_agent.types import ConsentRequest, PollVote, ToolCall, ToolResult, UserTier

pytestmark = pytest.mark.usefixtures("reset_db")

CHANNEL = "+000000"
POLL_TS = 4242
QUESTION = "Print 9 pages?"
ARGS = {"cmd": "print.sh", "timeout": 30}
BASH_CALL = ToolCall(id="c1", name="bash", arguments={"cmd": "print.sh"})


def _consent_result():
    return ToolResult(
        name="bash", success=False, output=None, error="NOT RUN: needs approval",
        consent_request=ConsentRequest(question=QUESTION, user_text="9 pages, A4",
                                       command="print.sh", arguments=dict(ARGS)),
    )


def _agent(llm_replies, rerun_result=None, rerun_gate=None):
    agent = build_mock_agent_loop()
    sc = agent._signal_client
    sc.create_poll = AsyncMock(return_value=POLL_TS)
    sc.delete_message = AsyncMock(return_value=True)
    sc.send_message = AsyncMock(return_value=1)
    agent._llm_client.chat = AsyncMock(side_effect=list(llm_replies))
    calls = []

    async def execute_tool(name, args, tier, *, consent_question=None):
        calls.append((name, dict(args), tier, consent_question))
        if consent_question is None:
            return _consent_result()
        if rerun_gate is not None:
            await rerun_gate.wait()
        return rerun_result

    agent._tool_registry.execute_tool = AsyncMock(side_effect=execute_tool)
    agent.tool_calls_made = calls
    return agent


def _job(job_id="job-1"):
    return SimpleNamespace(id=job_id, channel_id=CHANNEL, creator_number=CHANNEL, prompt="print the report",
                           is_recurring=False, interval_minutes=None, cancelled=False,
                           scheduled_at="2026-10-06T08:00:00+00:00")


def _vote(index, voter="+000000", ts=POLL_TS):
    return PollVote(channel_id=CHANNEL, voter=voter, voter_name="Alice", target_ts=ts,
                    option_indexes=[index], vote_count=1)


def _tool_contents(messages):
    return {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}


async def _run_cronjob(agent, job=None):
    job = job or _job()
    task = asyncio.create_task(agent._process_cronjob(job))
    agent._register_channel_task(CHANNEL, task, lane="background",
                                 meta={"source_type": "cronjob", "source_id": job.id})
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0)  # done-callbacks
    return job


async def _reruns_done(agent):
    for _ in range(3):
        if agent._tasks:
            await asyncio.wait_for(asyncio.gather(*list(agent._tasks)), 10)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cronjob_posts_poll_continues_and_frees_lane():
    agent = _agent([("", [BASH_CALL]), ("Asked the users.", [])])
    job = await _run_cronjob(agent)

    sc = agent._signal_client
    sc.send_message.assert_any_await(CHANNEL, "9 pages, A4")
    sc.create_poll.assert_awaited_once_with(CHANNEL, QUESTION, ["Yes", "No"])
    messages = agent._llm_client.chat.await_args_list[1].args[0]
    assert _tool_contents(messages)["c1"] == "Error: " + prompts.current().text("consent-background-pending")
    agent._cron_scheduler.mark_complete.assert_called_once()
    assert agent._cron_scheduler.mark_complete.call_args.args == (job.id,)
    assert agent._cron_scheduler.mark_complete.call_args.kwargs["success"] is True
    assert CHANNEL not in agent._channel_background_task
    assert CHANNEL not in agent._channel_background_meta
    # The poll stays open (with its expiry timer) after the run ended.
    pending = next(iter(agent._consent._pending.values()))
    assert pending.lane == "background" and pending.expiry_task is not None
    assert agent._consent._expiry_tasks == {pending.expiry_task}
    assert DatabaseOperations().get_consent_polls() == [(CHANNEL, POLL_TS)]
    agent._consent.shutdown()


@pytest.mark.asyncio
async def test_identical_command_twice_posts_one_poll():
    agent = _agent([("", [BASH_CALL]), ("asked", []), ("", [BASH_CALL]), ("asked again", [])])
    await _run_cronjob(agent, _job("job-1"))
    await _run_cronjob(agent, _job("job-2"))
    assert agent._signal_client.create_poll.await_count == 1
    pending_text = "Error: " + prompts.current().text("consent-background-pending")
    assert _tool_contents(agent._llm_client.chat.await_args_list[3].args[0])["c1"] == pending_text
    assert len(agent._consent._pending) == 1
    agent._consent.shutdown()


@pytest.mark.asyncio
async def test_poll_send_failure_is_a_tool_error():
    agent = _agent([("", [BASH_CALL]), ("could not ask", [])])
    agent._signal_client.create_poll = AsyncMock(return_value=None)
    await _run_cronjob(agent)
    content = _tool_contents(agent._llm_client.chat.await_args_list[1].args[0])["c1"]
    assert "Could not ask the users for approval" in content
    assert agent._consent._pending == {}


@pytest.mark.asyncio
async def test_yes_with_ok_marker_posts_output_verbatim_and_stores_it():
    output = "job 645 submitted\n" + "x" * 3000
    ok = ToolResult(name="bash", success=True, output=output, error=None, consent_ok=True)
    agent = _agent([("", [BASH_CALL]), ("asked", [])], rerun_result=ok)
    await _run_cronjob(agent)
    agent._signal_client.send_message.reset_mock()

    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)

    assert agent.tool_calls_made[-1] == ("bash", ARGS, UserTier.PRIVILEGED, QUESTION)
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    sent = agent._signal_client.send_message.await_args.args
    assert sent[0] == CHANNEL and sent[1].startswith("job 645 submitted")
    assert len(sent[1]) <= 1501
    agent._cron_scheduler.schedule.assert_not_called()
    history = DatabaseOperations().get_messages(CHANNEL, limit=10)
    assert any(m.event.message == sent[1] and m.is_outgoing and m.source_type == "consent" for m in history)
    # Re-run used a background Context and closed its browser scope.
    agent._context_manager.get_context.assert_called_with(CHANNEL, lane="background")
    assert DatabaseOperations().get_consent_polls() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    ToolResult(name="bash", success=True, output="sent, but no OK marker", error=None, consent_ok=False),
    ToolResult(name="bash", success=False, output="out", error="Exit code: 2\n" + "e" * 4000 + "TAIL", consent_ok=True),
])
async def test_yes_with_failure_schedules_one_inspection_cronjob(result):
    agent = _agent([("", [BASH_CALL]), ("asked", [])], rerun_result=result)
    await _run_cronjob(agent)
    sends_before = agent._signal_client.send_message.await_count

    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)

    assert agent._signal_client.send_message.await_count == sends_before
    agent._cron_scheduler.schedule.assert_called_once()
    kw = agent._cron_scheduler.schedule.call_args.kwargs
    assert kw["channel_id"] == CHANNEL and kw["creator_number"] == "+000000"
    assert kw["when"].tzinfo is not None and kw["when"].utcoffset() == timezone.utc.utcoffset(None)
    assert kw.get("is_recurring", False) is False
    assert "print.sh" in kw["prompt"]
    if result.success:
        assert "sent, but no OK marker" in kw["prompt"]
    else:
        assert kw["prompt"].count("e") <= 3100 and "TAIL" in kw["prompt"]


@pytest.mark.asyncio
async def test_no_does_nothing_but_delete_the_poll():
    agent = _agent([("", [BASH_CALL]), ("asked", [])])
    await _run_cronjob(agent)
    sends_before = agent._signal_client.send_message.await_count
    await agent._consent.on_vote(_vote(1))
    await _reruns_done(agent)
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    assert len(agent.tool_calls_made) == 1
    assert agent._signal_client.send_message.await_count == sends_before
    agent._cron_scheduler.schedule.assert_not_called()
    assert agent._consent._pending == {}


@pytest.mark.asyncio
async def test_expiry_deletes_poll_and_runs_nothing(monkeypatch):
    monkeypatch.setattr(config.consent, "background_timeout_minutes", 0.001)  # 60 ms
    agent = _agent([("", [BASH_CALL]), ("asked", [])])
    await _run_cronjob(agent)
    for _ in range(200):
        if not agent._consent._pending:
            break
        await asyncio.sleep(0.01)
    assert agent._consent._pending == {}
    agent._signal_client.delete_message.assert_awaited_once_with(CHANNEL, POLL_TS)
    assert DatabaseOperations().get_consent_polls() == []
    assert len(agent.tool_calls_made) == 1
    agent._cron_scheduler.schedule.assert_not_called()


@pytest.mark.asyncio
async def test_vote_cancels_expiry_timer_and_shutdown_keeps_rows():
    agent = _agent([("", [BASH_CALL]), ("asked", [])],
                   rerun_result=ToolResult(name="bash", success=True, output="ok", error=None, consent_ok=True))
    await _run_cronjob(agent)
    pending = next(iter(agent._consent._pending.values()))
    timer = pending.expiry_task
    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)
    await asyncio.sleep(0)
    assert timer.cancelled() and pending.expiry_task is None
    assert agent._consent._expiry_tasks == set()

    # Shutdown: timers stop, the DB row stays for the startup cleanup.
    agent._signal_client.create_poll = AsyncMock(return_value=POLL_TS + 1)
    agent._llm_client.chat = AsyncMock(side_effect=[("", [BASH_CALL]), ("asked", [])])
    await _run_cronjob(agent, _job("job-2"))
    second = next(iter(agent._consent._pending.values()))
    timer = second.expiry_task
    await agent.shutdown()
    await asyncio.sleep(0)
    assert timer.cancelled()
    assert DatabaseOperations().get_consent_polls() == [(CHANNEL, POLL_TS + 1)]


@pytest.mark.asyncio
async def test_low_tier_vote_is_ignored():
    agent = _agent([("", [BASH_CALL]), ("asked", [])])
    await _run_cronjob(agent)
    await agent._consent.on_vote(_vote(0, voter="+000001"))  # may not use bash
    await _reruns_done(agent)
    assert len(agent.tool_calls_made) == 1
    assert len(agent._consent._pending) == 1
    agent._signal_client.delete_message.assert_not_awaited()
    agent._consent.shutdown()


@pytest.mark.asyncio
async def test_subagent_posts_poll_and_yes_reruns():
    ok = ToolResult(name="bash", success=True, output="printed", error=None, consent_ok=True)
    agent = _agent([("", [BASH_CALL]), ("asked", [])], rerun_result=ok)
    await asyncio.wait_for(agent._process_subagent("print", CHANNEL, UserTier.PRIVILEGED), 5)
    agent._signal_client.create_poll.assert_awaited_once()
    content = _tool_contents(agent._llm_client.chat.await_args_list[1].args[0])["c1"]
    assert content == "Error: " + prompts.current().text("consent-background-pending")

    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)
    agent._signal_client.send_message.assert_any_await(CHANNEL, "printed")


@pytest.mark.asyncio
async def test_vote_handler_does_not_wait_for_the_rerun():
    gate = asyncio.Event()
    ok = ToolResult(name="bash", success=True, output="printed", error=None, consent_ok=True)
    agent = _agent([("", [BASH_CALL]), ("asked", [])], rerun_result=ok, rerun_gate=gate)
    await _run_cronjob(agent)
    await asyncio.wait_for(agent._consent.on_vote(_vote(0)), 1)
    await asyncio.sleep(0.05)
    assert agent.tool_calls_made[-1][3] == QUESTION  # re-run started, blocked
    assert not any(c.args == (CHANNEL, "printed") for c in agent._signal_client.send_message.await_args_list)
    gate.set()
    await _reruns_done(agent)
    agent._signal_client.send_message.assert_any_await(CHANNEL, "printed")


@pytest.mark.asyncio
async def test_rerun_exception_is_logged_not_raised():
    agent = _agent([("", [BASH_CALL]), ("asked", [])])

    async def boom(name, args, tier, *, consent_question=None):
        if consent_question is None:
            return _consent_result()
        raise RuntimeError("boom")

    agent._tool_registry.execute_tool = AsyncMock(side_effect=boom)
    await _run_cronjob(agent)
    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)
    agent._cron_scheduler.schedule.assert_called_once()
    kw = agent._cron_scheduler.schedule.call_args.kwargs
    assert "Internal error: boom" in kw["prompt"] and "print.sh" in kw["prompt"]
    assert kw["creator_number"] == "+000000"


@pytest.mark.asyncio
async def test_inspection_scheduling_failure_is_logged_not_raised():
    agent = _agent([("", [BASH_CALL]), ("asked", [])],
                   rerun_result=ToolResult(name="bash", success=False, output=None, error="Exit code: 1"))
    agent._cron_scheduler.schedule.side_effect = RuntimeError("db locked")
    await _run_cronjob(agent)
    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)  # would raise if the task failed
    agent._cron_scheduler.schedule.assert_called_once()


@pytest.mark.asyncio
async def test_real_registry_and_bash_rerun_gets_channel_env(tmp_path):
    """cron run -> poll -> yes -> bash re-run in the channel dir with
    NIXCLAW_CHANNEL_ID and NIXCLAW_CONSENT=1; output posted without markers."""
    from src.nixclaw_agent.auth import AuthConfig, AuthManager
    from src.nixclaw_agent.tools.bash import bash, channel_crc
    from src.nixclaw_agent.tools.registry import ToolRegistry
    from src.nixclaw_agent.types import Context

    agent = _agent([])
    registry = ToolRegistry(AuthManager(AuthConfig(privileged_numbers=[CHANNEL])))
    registry.register("bash", bash, {})
    agent._tool_registry = registry
    channel_dir = tmp_path / CHANNEL  # reset_db points data_dir at tmp_path
    channel_dir.mkdir()
    agent._context_manager.get_context.side_effect = lambda cid, lane="interactive": Context(
        channel_id=cid, messages_db=tmp_path / "m.db", attachments_dir=channel_dir / "attachments", lane=lane)
    (channel_dir / "print.sh").write_text(
        'm="NIXCLAW_REQUIRE_CONSENT_QUESTION_${NIXCLAW_CHANNEL_ID}=\\"Print 9 pages?\\""\n'
        'echo "9 pages, A4"\n'
        'echo "$m" >&2\n'
        'if [ "$NIXCLAW_CONSENT" = 1 ]; then\n'
        '  echo "$PWD|$NIXCLAW_CHANNEL_ID|$NIXCLAW_CONSENT" > env.txt\n'
        '  echo "job 7 submitted"\n'
        '  echo "NIXCLAW_CONSENT_OK_${NIXCLAW_CHANNEL_ID}" >&2\n'
        'fi\n'
    )
    call = ToolCall(id="c1", name="bash", arguments={"cmd": "bash print.sh"})
    agent._llm_client.chat = AsyncMock(side_effect=[("", [call]), ("Asked.", [])])

    await _run_cronjob(agent)
    pending = next(iter(agent._consent._pending.values()))
    assert pending.question == "Print 9 pages?"
    assert not (channel_dir / "env.txt").exists()

    await agent._consent.on_vote(_vote(0))
    await _reruns_done(agent)

    cwd, crc, consent = (channel_dir / "env.txt").read_text().strip().split("|")
    assert cwd == str(channel_dir) and crc == channel_crc(CHANNEL) and consent == "1"
    last = agent._signal_client.send_message.await_args.args
    assert last[0] == CHANNEL and "job 7 submitted" in last[1] and "NIXCLAW" not in last[1]
    agent._cron_scheduler.schedule.assert_not_called()
