"""Bash execution tool for nixclaw-agent.

This tool is only available to privileged users and executes
bash commands with a timeout.

Note: This tool uses create_subprocess_shell() instead of create_subprocess_exec()
because the LLM is the source of commands (not user input being interpolated).
Shell injection is not a concern since the LLM is trusted to generate commands
for privileged users who have explicitly been granted bash access.
"""

import asyncio
import logging
import os
import re
import signal
import zlib
from pathlib import Path
from typing import Callable, Optional

from ..types import ConsentRequest, ToolResult, UserTier
from ..config import config

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 300
# How long to keep reading the pipes after the process group was killed.
_DRAIN_SECONDS = 2.0
_READ_CHUNK = 4096

# Consent contract (see consent.py): a script prints, as a complete stderr
# line, NIXCLAW_REQUIRE_CONSENT_QUESTION_<crc>="<question>" to ask the users
# first, and NIXCLAW_CONSENT_OK_<crc> after an approved action succeeded.
# <crc> is NIXCLAW_CHANNEL_ID, so a marker only counts in its own channel.
CONSENT_ENV = "NIXCLAW_CONSENT"
CHANNEL_ENV = "NIXCLAW_CHANNEL_ID"
_CONSENT_QUESTION_PREFIX = "NIXCLAW_REQUIRE_CONSENT_QUESTION_"
_CONSENT_OK_PREFIX = "NIXCLAW_CONSENT_OK_"
CONSENT_QUESTION_MAX_CHARS = 200
CONSENT_USER_TEXT_MAX_CHARS = 1500


def effective_timeout(timeout: object) -> float:
    """Clamp the model-supplied timeout the way bash() applies it.

    Shared with tools/registry.py, which must give bash a slightly longer
    outer timeout than this one.
    """
    # `not (0 < t <= max)` also rejects NaN; inf is above the max.
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not (0 < timeout <= MAX_TIMEOUT_SECONDS)
    ):
        return DEFAULT_TIMEOUT_SECONDS
    return timeout


class _OutputCollector:
    """Reads stdout and stderr as two separate streams.

    Complete lines of both are passed to the optional on_line hook as
    (stream_name, line), so a stream can be inspected on its own, and appended
    to one merged list in arrival order (unless the hook returns True: the
    line is then left out of the output). That keeps the old stderr-into-stdout output (the model sees one
    interleaved text) without one pipe being able to block the other. With two
    pipes the interleaving is only as exact as the reads: lines written close
    together on different streams may come out in either order.
    """

    def __init__(self, on_line: Optional[Callable[[str, str], Optional[bool]]] = None) -> None:
        self._on_line = on_line
        self._pending: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        self._merged: list[bytes] = []

    async def read(self, reader: asyncio.StreamReader, name: str) -> None:
        pending = self._pending[name]
        try:
            while True:
                chunk = await reader.read(_READ_CHUNK)
                if not chunk:
                    break
                pending += chunk
                cut = pending.rfind(b"\n") + 1
                if cut:
                    self._add(name, bytes(pending[:cut]))
                    del pending[:cut]
        finally:
            # EOF or cancelled after a kill: keep an unterminated last line too.
            if pending:
                self._add(name, bytes(pending))
                pending.clear()

    def _add(self, name: str, data: bytes) -> None:
        if not self._on_line:
            self._merged.append(data)
            return
        for line in data.splitlines(keepends=True):
            if not self._on_line(name, line.decode(errors="replace")):
                self._merged.append(line)

    def merged_text(self) -> str:
        return b"".join(self._merged).decode(errors="replace")


def channel_crc(channel_id: str) -> str:
    """Value of NIXCLAW_CHANNEL_ID for a channel (crc32 as 8 hex digits)."""
    return f"{zlib.crc32(channel_id.encode()) & 0xffffffff:08x}"


class _ConsentWatch:
    """on_line hook for _OutputCollector: finds the consent markers.

    Only complete stderr lines that match exactly count. Marker lines are
    dropped from the output. `stop` is set when the process group must be
    killed: on a question marker in a normal run (the script asks for
    approval), or on a malformed / extra / different question marker in an
    approved run (then `error` says why).
    """

    def __init__(self, crc: str, approved_question: Optional[str]) -> None:
        self._question_re = re.compile(
            re.escape(_CONSENT_QUESTION_PREFIX + crc + "=") + r'"(.*)"'
        )
        self._ok_line = _CONSENT_OK_PREFIX + crc
        self._question_prefix = _CONSENT_QUESTION_PREFIX + crc
        self._approved_question = approved_question
        self._approved_seen = False
        self.stop = asyncio.Event()
        self.question: Optional[str] = None
        self.error: Optional[str] = None
        self.ok = False

    def __call__(self, stream: str, line: str) -> bool:
        if stream != "stderr":
            return False
        if self.stop.is_set():
            # Killed already: only keep marker text (even a partial line
            # drained after the kill) out of the output.
            return line.startswith((self._question_prefix, self._ok_line))
        if not line.endswith("\n"):
            return False
        text = line[:-1]
        if text.endswith("\r"):
            text = text[:-1]
        if text == self._ok_line:
            self.ok = True
            return True
        match = self._question_re.fullmatch(text)
        if not match:
            return False
        question = match.group(1)
        if not question or len(question) > CONSENT_QUESTION_MAX_CHARS or '"' in question:
            self.error = (
                f"Malformed approval question (must be 1-{CONSENT_QUESTION_MAX_CHARS} "
                f"characters without double quotes); the command was stopped."
            )
        elif self._approved_question is None:
            self.question = question
        elif question != self._approved_question:
            self.error = (
                "The command asked a different approval question than the one the "
                "users approved; it was stopped."
            )
        elif self._approved_seen:
            self.error = (
                "The command asked for approval a second time during an approved run "
                "(one approval allows one action); it was stopped."
            )
        else:
            self._approved_seen = True
            return True
        self.stop.set()
        return True


def _cap_user_text(text: str) -> str:
    text = text.strip()
    if len(text) > CONSENT_USER_TEXT_MAX_CHARS:
        text = text[:CONSENT_USER_TEXT_MAX_CHARS].rstrip() + "\n[...]"
    return text


def _get_context_dir(channel_id: Optional[str]) -> Optional[Path]:
    """Get the context directory for a channel if channel_id is provided."""
    if not channel_id:
        return None
    context_dir = config.paths.data_dir / channel_id
    try:
        context_dir.mkdir(parents=True, exist_ok=True)
        return context_dir
    except Exception as e:
        LOG.warning(f"Could not create context directory {context_dir}: {e}")
        return None


def bash(cmd: str, timeout: float = DEFAULT_TIMEOUT_SECONDS, user_tier: Optional[UserTier] = None,
         channel_id: Optional[str] = None, consent_question: Optional[str] = None) -> ToolResult:
    """Execute a bash command (privileged users only).

    Args:
        cmd: The bash command to execute
        timeout: Timeout in seconds (default: 30)
        user_tier: The user's tier (automatically set by tool registry)
        channel_id: The channel ID for context directory (automatically set by tool registry)
        consent_question: Internal (core.py only, never from the model): the
            question the users approved. Runs the command with
            NIXCLAW_CONSENT=1 and allows exactly one matching marker.

    Returns:
        ToolResult with command output or error
    """
    LOG.info(f"BASH_TOOL: received user_tier={user_tier}, channel_id={channel_id}, expected {UserTier.PRIVILEGED}")
    
    if user_tier is not UserTier.PRIVILEGED:
        return ToolResult(
            name="bash",
            success=False,
            output=None,
            error="Bash execution is only available to privileged users",
        )

    timeout = effective_timeout(timeout)

    if not cmd or not cmd.strip():
        return ToolResult(
            name="bash",
            success=False,
            output=None,
            error="Empty command",
        )

    if CONSENT_ENV in cmd:
        return ToolResult(
            name="bash",
            success=False,
            output=None,
            error=(
                f"Commands must not mention {CONSENT_ENV}*: approval is handled by the "
                f"agent harness. Run the script without it; it will ask the users itself."
            ),
        )

    env = dict(os.environ)
    env.pop(CONSENT_ENV, None)
    env.pop(CHANNEL_ENV, None)
    crc = channel_crc(channel_id) if channel_id else None
    if crc:
        env[CHANNEL_ENV] = crc
        if consent_question is not None:
            env[CONSENT_ENV] = "1"

    async def _run_bash():
        context_dir = _get_context_dir(channel_id)
        # Markers are only recognized when the channel (and so the crc) is known.
        watch = _ConsentWatch(crc, consent_question) if crc else None
        try:
            # start_new_session=True puts the shell in its own process group, so a
            # timeout can kill everything it started (`nohup ... &`, loop bodies),
            # not just the shell itself. stdout and stderr are separate pipes.
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(context_dir) if context_dir else None,
                env=env,
                start_new_session=True,
            )
            collector = _OutputCollector(on_line=watch)
            assert process.stdout is not None and process.stderr is not None
            readers = [
                asyncio.ensure_future(collector.read(process.stdout, "stdout")),
                asyncio.ensure_future(collector.read(process.stderr, "stderr")),
            ]
            waiter = asyncio.ensure_future(process.wait())
            # asyncio.wait (not wait_for) so the readers are not cancelled on
            # timeout and the output read so far stays available. A consent
            # marker (watch.stop) ends the wait early.
            finisher = asyncio.ensure_future(asyncio.wait([*readers, waiter]))
            stopper = asyncio.ensure_future(watch.stop.wait()) if watch else None
            try:
                await asyncio.wait(
                    [finisher, *([stopper] if stopper else [])],
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                finisher.cancel()
                if stopper:
                    stopper.cancel()
            pending = [t for t in (*readers, waiter) if not t.done()]
            if watch and watch.stop.is_set():
                # Consent marker: stop the command right here, before it acts.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await asyncio.wait([*readers, waiter], timeout=_DRAIN_SECONDS)
                for task in (*readers, waiter):
                    task.cancel()
                await asyncio.gather(*readers, waiter, return_exceptions=True)
                output = collector.merged_text()
                if watch.error:
                    error = watch.error
                    if output:
                        error += f" Output so far:\n{output}"
                    return ToolResult(name="bash", success=False, output=None, error=error)
                assert watch.question is not None
                return ToolResult(
                    name="bash",
                    success=False,
                    output=None,
                    # Seen by the model only where no approval poll can be
                    # posted (core.py replaces it with the poll's outcome).
                    error="NOT RUN: this command needs the users' approval, which cannot be asked for here.",
                    consent_request=ConsentRequest(
                        question=watch.question,
                        user_text=_cap_user_text(output),
                        command=cmd,
                        arguments={"cmd": cmd, "timeout": timeout},
                    ),
                )
            if pending:
                # The shell may have exited already while a background child
                # still holds the pipes open; that is not a slow command. (Read the
                # returncode, not waiter.done(): wait() may only resolve once the
                # pipes are closed.)
                shell_returncode = process.returncode
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Drain what is still buffered in the pipes after the kill.
                await asyncio.wait([*readers, waiter], timeout=_DRAIN_SECONDS)
                for task in (*readers, waiter):
                    task.cancel()
                await asyncio.gather(*readers, waiter, return_exceptions=True)
                partial = collector.merged_text()
                if shell_returncode is not None:
                    error = (
                        f"Command exited with code {shell_returncode}, but background "
                        f"processes kept its output open; they were killed after "
                        f"{timeout} seconds. Redirect background output "
                        f"(>/dev/null 2>&1 &) to let them run."
                    )
                    if partial:
                        error += f" Output so far:\n{partial}"
                else:
                    error = f"Command timed out after {timeout} seconds"
                    if partial:
                        # Failed results are rendered to the model as "Error: <error>"
                        # only (llm._tool_result_content), so the partial output goes
                        # into the error text.
                        error += f". Output so far:\n{partial}"
                return ToolResult(
                    name="bash",
                    success=False,
                    output=None,
                    error=error,
                )
            output = collector.merged_text()
            returncode = process.returncode
            error = None
            if returncode != 0:
                # Same as the timeout path: the model sees only the error text
                # of a failed result, so the output (e.g. a script's reason for
                # exit 1) goes into it.
                error = f"Exit code: {returncode}"
                if output:
                    error += f"\n{output}"
            return ToolResult(
                name="bash",
                success=returncode == 0,
                output=output if output else f"Command exited with code {returncode}",
                error=error,
                consent_ok=bool(watch and watch.ok),
            )
        except FileNotFoundError:
            return ToolResult(
                name="bash",
                success=False,
                output=None,
                error=f"Command not found",
            )
        except PermissionError:
            return ToolResult(
                name="bash",
                success=False,
                output=None,
                error=f"Permission denied",
            )
        except Exception as e:
            return ToolResult(
                name="bash",
                success=False,
                output=None,
                error=str(e),
            )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(_run_bash())
    finally:
        loop.close()

    return result