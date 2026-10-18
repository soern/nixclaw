"""end_call: hang up the active Signal voice call (offered only during a call)."""

from typing import Callable, Optional

from ..types import ToolResult

# Set by main.py to CallManager.request_end(channel_id) -> "ok", "no_goodbye"
# (the caller's last words weren't a goodbye) or "no_call".
_end_call_callback: Optional[Callable[[str], str]] = None


def set_end_call_callback(callback: Optional[Callable[[str], str]]) -> None:
    global _end_call_callback
    _end_call_callback = callback


async def end_call(channel_id: str = "") -> ToolResult:
    status = _end_call_callback(channel_id) if _end_call_callback is not None else "no_call"
    if status == "no_goodbye":
        return ToolResult(name="end_call", success=False, output=None,
                          error="Not hung up: the caller has not said goodbye. The call goes on; "
                                "answer them normally. Hang up only when the caller ends the call.")
    if status != "ok":
        return ToolResult(name="end_call", success=False, output=None,
                          error="There is no active voice call in this chat.")
    return ToolResult(name="end_call", success=True,
                      output="The call ends once your reply has been spoken. Say goodbye briefly.",
                      error=None)
