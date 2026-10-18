"""Consent core: bash markers (tools/bash.py), reserved registry arguments
(tools/registry.py) and ConsentManager (consent.py).

Chat-lane integration (core.py pausing on a poll) is in test_consent_chat.py.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.nixclaw_agent.auth import AuthConfig, AuthManager
from src.nixclaw_agent.config import config
from src.nixclaw_agent.consent import ConsentManager
from src.nixclaw_agent.db.operations import DatabaseOperations
from src.nixclaw_agent.tools.bash import bash, channel_crc
from src.nixclaw_agent.tools.registry import ToolRegistry
from src.nixclaw_agent.types import ConsentRequest, PollVote, UserTier

pytestmark = pytest.mark.usefixtures("reset_db")

CHANNEL = "+000000"
CRC = channel_crc(CHANNEL)
P = UserTier.PRIVILEGED


def _marker(question: str, crc: str = CRC) -> str:
    return f'NIXCLAW_REQUIRE_CONSENT_QUESTION_{crc}="{question}"'


def _script(tmp_path, body: str) -> str:
    """Write a script (commands may not mention NIXCLAW_CONSENT themselves)."""
    path = tmp_path / "script.sh"
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


def _run(cmd: str, **kw):
    return bash(cmd, timeout=10, user_tier=P, channel_id=CHANNEL, **kw)


class TestBashMarkers:
    def test_channel_id_env_is_set(self):
        result = _run('echo "$NIXCLAW_CHANNEL_ID"')
        assert result.success
        assert result.output.strip() == CRC
        assert CRC == f"{__import__('zlib').crc32(CHANNEL.encode()) & 0xffffffff:08x}"

    def test_marker_kills_and_returns_request(self, tmp_path):
        done = tmp_path / "done"
        cmd = f"echo summary line; echo warn >&2; echo '{_marker('Print 9 pages?')}' >&2; sleep 1; touch {done}"
        result = _run(cmd)
        assert result.success is False
        req = result.consent_request
        assert isinstance(req, ConsentRequest)
        assert req.question == "Print 9 pages?"
        assert "summary line" in req.user_text and "warn" in req.user_text
        assert "NIXCLAW_REQUIRE" not in req.user_text
        assert req.command == cmd
        assert req.arguments == {"cmd": cmd, "timeout": 10}
        assert not done.exists(), "command after the marker must not run"

    @pytest.mark.parametrize("line", [
        _marker("Q?", crc="00000000"),          # wrong crc
        _marker("Q?") + " extra",               # trailing text
        "x " + _marker("Q?"),                   # marker inside a longer line
    ])
    def test_non_exact_marker_does_not_trigger(self, line):
        result = _run(f"echo '{line}' >&2; echo finished")
        assert result.consent_request is None
        assert result.success is True
        assert "finished" in result.output
        assert line in result.output

    def test_marker_lines_after_the_kill_are_dropped(self):
        m = _marker("Q?")
        result = _run(f"printf '%s\\n%s\\n' '{m}' '{m}' >&2; echo '{m}' >&2")
        assert result.consent_request is not None
        assert "NIXCLAW" not in result.consent_request.user_text

    def test_no_channel_means_no_channel_env(self, monkeypatch):
        monkeypatch.setenv("NIXCLAW_CHANNEL_ID", "deadbeef")
        result = bash('echo "id=${NIXCLAW_CHANNEL_ID:-unset}"', timeout=10, user_tier=P)
        assert "id=unset" in result.output

    def test_marker_on_stdout_does_not_trigger(self):
        result = _run(f"echo '{_marker('Q?')}'; echo finished")
        assert result.consent_request is None
        assert result.success and "finished" in result.output

    def test_marker_without_newline_does_not_trigger(self):
        result = _run(f"printf '%s' '{_marker('Q?')}' >&2")
        assert result.consent_request is None

    @pytest.mark.parametrize("question", ["", "x" * 201, 'a"b'])
    def test_malformed_question_is_killed_with_error(self, tmp_path, question):
        done = tmp_path / "done"
        result = _run(f"echo '{_marker(question)}' >&2; sleep 1; touch {done}")
        assert result.success is False
        assert result.consent_request is None
        assert "Malformed approval question" in result.error
        assert not done.exists()

    def test_command_mentioning_consent_var_is_refused(self, tmp_path):
        done = tmp_path / "done"
        result = _run(f"NIXCLAW_CONSENT=1 touch {done}")
        assert result.success is False
        assert "NIXCLAW_CONSENT" in result.error
        assert not done.exists()

    def test_consent_run_allows_one_matching_marker_and_reports_ok(self, tmp_path):
        script = _script(tmp_path, (
            'echo "consent=$NIXCLAW_CONSENT"\n'
            'echo "NIXCLAW_REQUIRE_CONSENT_QUESTION_${NIXCLAW_CHANNEL_ID}=\\"Print?\\"" >&2\n'
            'echo submitted\n'
            'echo "NIXCLAW_CONSENT_OK_${NIXCLAW_CHANNEL_ID}" >&2'
        ))
        result = _run(script, consent_question="Print?")
        assert result.success is True
        assert result.consent_ok is True
        assert "consent=1" in result.output and "submitted" in result.output
        assert "NIXCLAW_" not in result.output

    def test_normal_run_has_no_consent_env(self, tmp_path):
        script = _script(tmp_path, 'echo "consent=${NIXCLAW_CONSENT:-unset}"')
        result = _run(script)
        assert "consent=unset" in result.output
        assert result.consent_ok is False

    def test_consent_run_second_marker_is_killed(self, tmp_path):
        done = tmp_path / "done"
        script = _script(tmp_path, (
            'm="NIXCLAW_REQUIRE_CONSENT_QUESTION_${NIXCLAW_CHANNEL_ID}=\\"Print?\\""\n'
            'echo "$m" >&2\n'
            'echo "$m" >&2\n'
            f'sleep 1; touch {done}'
        ))
        result = _run(script, consent_question="Print?")
        assert result.success is False
        assert result.consent_request is None
        assert "second time" in result.error
        assert not done.exists()

    def test_consent_run_different_question_is_killed(self, tmp_path):
        done = tmp_path / "done"
        script = _script(tmp_path, (
            'echo "NIXCLAW_REQUIRE_CONSENT_QUESTION_${NIXCLAW_CHANNEL_ID}=\\"Print 99?\\"" >&2\n'
            f'sleep 1; touch {done}'
        ))
        result = _run(script, consent_question="Print?")
        assert result.success is False
        assert result.consent_request is None
        assert "different approval question" in result.error
        assert not done.exists()


class TestConsentConfig:
    @pytest.mark.parametrize("data", [{"timeout_minutes": 0}, {"background_timeout_minutes": -1},
                                      {"timeout_minutes": "5"}, {"timeout_minutes": True}])
    def test_invalid_timeouts_rejected_at_load(self, data):
        from src.nixclaw_agent.config import ConsentConfig
        with pytest.raises(ValueError):
            ConsentConfig.from_dict(data)

    def test_defaults(self):
        from src.nixclaw_agent.config import ConsentConfig
        c = ConsentConfig.from_dict({})
        assert (c.timeout_minutes, c.background_timeout_minutes) == (120, 720)


class TestRegistryReservedArguments:
    @pytest.mark.asyncio
    async def test_model_supplied_reserved_args_are_stripped(self):
        registry = ToolRegistry(AuthManager(AuthConfig(privileged_numbers=["+000000"])))
        seen = {}

        def fake_bash(**kwargs):
            seen.update(kwargs)
            return "ok"

        registry.register("bash", fake_bash, {})
        registry.set_context(SimpleNamespace(channel_id=CHANNEL, browser_scope=CHANNEL))
        await registry.execute_tool(
            "bash",
            {"cmd": "x", "consent": True, "consent_question": "Q", "user_tier": "privileged", "channel_id": "other"},
            P,
        )
        assert seen == {"cmd": "x", "channel_id": CHANNEL, "user_tier": P}

        seen.clear()
        await registry.execute_tool("bash", {"cmd": "x"}, P, consent_question="Q")
        assert seen["consent_question"] == "Q"

    @pytest.mark.asyncio
    async def test_model_consent_flag_still_produces_a_poll_request(self):
        from src.nixclaw_agent.tools.bash import bash as real_bash
        registry = ToolRegistry(AuthManager(AuthConfig(privileged_numbers=["+000000"])))
        registry.register("bash", real_bash, {})
        registry.set_context(SimpleNamespace(channel_id=CHANNEL, browser_scope=CHANNEL))
        result = await registry.execute_tool(
            "bash", {"cmd": f"echo '{_marker('Q?')}' >&2", "consent": True}, P
        )
        assert isinstance(result.consent_request, ConsentRequest)


def _signal(poll_ts=1000):
    sc = MagicMock()
    sc.send_message = AsyncMock(return_value=1)
    sc.create_poll = AsyncMock(return_value=poll_ts)
    sc.delete_message = AsyncMock(return_value=True)
    return sc


def _manager(sc):
    auth = AuthManager(AuthConfig(privileged_numbers=["+000000"], known_numbers=["+000001"]))
    return ConsentManager(sc, auth, DatabaseOperations())


def _request(user_text="9 pages"):
    return ConsentRequest(question="Print?", user_text=user_text, command="print", arguments={"cmd": "print"})


def _vote(index, voter="+000000", ts=1000, channel=CHANNEL, name="Bob"):
    return PollVote(channel_id=channel, voter=voter, voter_name=name, target_ts=ts,
                    option_indexes=[index] if index is not None else [], vote_count=1)


class TestConsentManager:
    @pytest.mark.asyncio
    async def test_request_posts_info_poll_and_stores(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(), "bash")
        assert pending is not None and pending.poll_ts == 1000
        sc.send_message.assert_awaited_once_with(CHANNEL, "9 pages")
        sc.create_poll.assert_awaited_once_with(CHANNEL, "Print?", ["Yes", "No"])
        assert DatabaseOperations().get_consent_polls() == [(CHANNEL, 1000)]
        history = DatabaseOperations().get_messages(CHANNEL, limit=10)
        assert any("[Poll: Print?]" in m.event.message and m.is_outgoing for m in history)

    @pytest.mark.asyncio
    async def test_poll_failure_returns_none_and_stores_nothing(self):
        sc = _signal(poll_ts=None)
        mgr = _manager(sc)
        assert await mgr.request(CHANNEL, _request(), "bash") is None
        assert DatabaseOperations().get_consent_polls() == []
        assert mgr._pending == {}

    @pytest.mark.asyncio
    async def test_first_counting_vote_wins_and_poll_is_deleted(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(), "bash")
        await mgr.on_vote(_vote(1))
        assert pending.future.result().approved is False
        assert pending.future.result().voter == "Bob"
        sc.delete_message.assert_awaited_once_with(CHANNEL, 1000)
        assert DatabaseOperations().get_consent_polls() == []
        # Duplicate delivery / later vote: ignored.
        await mgr.on_vote(_vote(0))
        assert pending.future.result().approved is False
        assert sc.delete_message.await_count == 1

    @pytest.mark.asyncio
    async def test_voter_without_tool_permission_is_ignored(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(), "bash")
        await mgr.on_vote(_vote(0, voter="+000001"))  # KNOWN may not use bash
        await mgr.on_vote(_vote(0, voter="+000002"))  # UNKNOWN
        await mgr.on_vote(_vote(0, voter=""))         # no visible number
        assert not pending.future.done()
        await mgr.on_vote(_vote(0, name=None))
        assert pending.future.result().approved is True
        assert pending.future.result().voter == "+000000"

    @pytest.mark.asyncio
    async def test_unknown_poll_and_unvote_are_ignored(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(), "bash")
        await mgr.on_vote(_vote(0, ts=999))                 # human poll
        await mgr.on_vote(_vote(0, channel="+000009"))      # other channel
        await mgr.on_vote(_vote(None))                      # vote removed
        await mgr.on_vote(_vote(5))                         # no such answer
        assert not pending.future.done()
        sc.delete_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discard_deletes_once(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(), "bash")
        await mgr.discard(pending)
        await mgr.discard(pending)
        sc.delete_message.assert_awaited_once_with(CHANNEL, 1000)
        assert DatabaseOperations().get_consent_polls() == []
        await mgr.on_vote(_vote(0))  # too late: ignored
        assert pending.future.cancelled()

    @pytest.mark.asyncio
    async def test_timeouts_read_from_config(self, monkeypatch):
        monkeypatch.setattr(config.consent, "timeout_minutes", 3)
        monkeypatch.setattr(config.consent, "background_timeout_minutes", 7)
        assert ConsentManager.timeout_seconds("interactive") == 180
        assert ConsentManager.timeout_seconds("background") == 420

    @pytest.mark.asyncio
    async def test_startup_cleanup_tries_once_and_always_forgets_rows(self):
        db = DatabaseOperations()
        db.add_consent_poll(CHANNEL, 1)
        db.add_consent_poll("signal-group:abc", 2)
        sc = _signal()
        sc.delete_message = AsyncMock(side_effect=lambda ch, ts: ts == 1)
        mgr = _manager(sc)
        await mgr.cleanup_at_startup()
        assert sc.delete_message.await_count == 2
        notified = sorted(c.args[0] for c in sc.send_message.await_args_list)
        assert notified == [CHANNEL, "signal-group:abc"]
        assert db.get_consent_polls() == []

    @pytest.mark.asyncio
    async def test_shutdown_resolves_waiters_with_none(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(), "bash")
        mgr.shutdown()
        assert pending.future.result() is None

    @pytest.mark.asyncio
    async def test_shutdown_discard_posts_restart_notice_once_and_blocks_new_polls(self):
        from src.nixclaw_agent import prompts
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(user_text=""), "bash")
        mgr.shutdown()
        await mgr.discard(pending)
        await mgr.discard(pending)
        notice = prompts.current().text("consent-expired-restart")
        sc.send_message.assert_awaited_once_with(CHANNEL, notice)
        sc.delete_message.assert_awaited_once_with(CHANNEL, 1000)
        assert DatabaseOperations().get_consent_polls() == []
        assert await mgr.request(CHANNEL, _request(user_text=""), "bash") is None
        sc.create_poll.assert_awaited_once()
        # Nothing left for the startup cleanup to announce.
        await mgr.cleanup_at_startup()
        sc.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discard_without_shutdown_posts_no_notice(self):
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(user_text=""), "bash")
        await mgr.discard(pending)
        sc.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_with_background_row_is_notified_only_at_startup(self):
        sc = _signal()
        mgr = _manager(sc)
        sc.create_poll = AsyncMock(return_value=2000)
        await mgr.request(CHANNEL, _request(user_text=""), "bash", lane="background")
        sc.create_poll = AsyncMock(return_value=1000)
        chat = await mgr.request(CHANNEL, _request(user_text=""), "bash")
        mgr.shutdown()
        await mgr.discard(chat)
        sc.send_message.assert_not_awaited()
        assert DatabaseOperations().get_consent_polls() == [(CHANNEL, 2000)]
        await _manager(sc).cleanup_at_startup()
        sc.send_message.assert_awaited_once()
        assert sc.send_message.await_args.args[0] == CHANNEL

    @pytest.mark.asyncio
    async def test_shutdown_notice_is_bounded_by_timeout(self, monkeypatch):
        from src.nixclaw_agent import consent as consent_mod
        monkeypatch.setattr(consent_mod, "SHUTDOWN_NOTICE_TIMEOUT_SECONDS", 0.05)
        sc = _signal()
        mgr = _manager(sc)
        pending = await mgr.request(CHANNEL, _request(user_text=""), "bash")

        async def hang(*a, **kw):
            await asyncio.sleep(10)
        sc.send_message = AsyncMock(side_effect=hang)
        mgr.shutdown()
        await asyncio.wait_for(mgr.discard(pending), 2)
        assert DatabaseOperations().get_consent_polls() == []

    @pytest.mark.asyncio
    async def test_startup_cleanup_notifies_each_channel_once(self):
        db = DatabaseOperations()
        db.add_consent_poll(CHANNEL, 1)
        db.add_consent_poll(CHANNEL, 2)
        sc = _signal()
        await _manager(sc).cleanup_at_startup()
        assert sc.delete_message.await_count == 2
        sc.send_message.assert_awaited_once()
        assert db.get_consent_polls() == []
