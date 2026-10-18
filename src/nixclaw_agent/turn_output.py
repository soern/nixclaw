"""Per-turn Signal output state for single-message mode.

In "append"/"replace" mode the texts of one turn (chat turn, cronjob run or
subagent run) are shown in ONE Signal message - the "target" - which later
texts edit instead of sending a new message:

- append:  the target shows old text + separator + new text
- replace: the target shows only the newest text; the replaced text's stored
           DB row is flagged user_visible=0 so later turns know the user
           never saw it on screen
- off:     every text is a new message (pre-feature behaviour)

A sent attachment becomes the new target (later texts edit its caption, the
attachment stays). A new message is started instead of an edit when there is
no target, the target hit max_edits, the result would exceed max_chars, or
the edit failed. Harness notices (notice=True) are always appended so they
never wipe a partial answer.

The actual sending is injected (core.py wraps its retry/rate-limit logic),
so this class has no Signal or DB dependency and is unit-testable.
"""

import logging
from typing import Awaitable, Callable, Optional

LOG = logging.getLogger(__name__)

# send_new(text) -> timestamp of the new message, or None (failed/queued/unknown)
SendNew = Callable[[str], Awaitable[Optional[int]]]
# send_edit(target_timestamp, full_text) -> True if the edit was sent
SendEdit = Callable[[int, str], Awaitable[bool]]
# mark_hidden(row_id) -> flag a stored message row user_visible=0
MarkHidden = Callable[[str], None]


def _as_timestamp(ts: object) -> Optional[int]:
    """Only a real int is a usable edit target (mocks may return anything)."""
    if isinstance(ts, int) and not isinstance(ts, bool):
        return ts
    return None


class TurnOutput:
    def __init__(
        self,
        mode: str,
        send_new: SendNew,
        send_edit: SendEdit,
        mark_hidden: Optional[MarkHidden] = None,
        separator: str = "\n\n",
        max_edits: int = 9,
        max_chars: int = 2000,
    ):
        self.mode = mode
        self._send_new = send_new
        self._send_edit = send_edit
        self._mark_hidden = mark_hidden
        self._separator = separator
        self._max_edits = max_edits
        self._max_chars = max_chars
        self.reset_target()

    def reset_target(self) -> None:
        """Forget the target: the next text starts a new Signal message."""
        self.target_ts: Optional[int] = None
        self.displayed = ""
        self.edit_count = 0
        self.displayed_row_id: Optional[str] = None

    def note_attachment(self, ts: object, caption: Optional[str]) -> None:
        """An attachment message was sent: it becomes the target."""
        if self.mode == "off":
            return
        self.reset_target()
        self.target_ts = _as_timestamp(ts)
        self.displayed = (caption or "").strip()

    async def send_text(self, text: str, row_id: Optional[str] = None, notice: bool = False) -> None:
        """Show `text` to the user (row_id = its stored DB row, if any)."""
        if self.mode == "off":
            await self._send_new(text)
            return

        # Model texts often carry leading/trailing blank lines; strip them so
        # composed (edited) messages don't show piles of empty lines.
        text = text.strip()
        if not text:
            return

        append = notice or self.mode == "append"
        if append and self.displayed:
            new_display = self.displayed + self._separator + text
        else:
            new_display = text

        if (
            self.target_ts is None
            or self.edit_count >= self._max_edits
            or len(new_display) > self._max_chars
        ):
            await self._start_new_message(text, row_id)
            return
        if new_display == self.displayed:
            if row_id is not None:
                self.displayed_row_id = row_id  # same text, now held by this row
            return

        if not await self._send_edit(self.target_ts, new_display):
            LOG.warning(f"Edit of message {self.target_ts} failed; sending a new message instead")
            await self._start_new_message(text, row_id)
            return

        self.edit_count += 1
        self.displayed = new_display
        if not append:
            # Replace mode: the previously displayed text is gone from the
            # user's screen. Row ids are second-resolution and can collide,
            # so never flag the row that now holds the new text.
            old_id = self.displayed_row_id
            if old_id is not None and old_id != row_id and self._mark_hidden is not None:
                self._mark_hidden(old_id)
            self.displayed_row_id = row_id

    async def _start_new_message(self, text: str, row_id: Optional[str]) -> None:
        ts = await self._send_new(text)
        self.reset_target()
        self.target_ts = _as_timestamp(ts)
        self.displayed = text
        self.displayed_row_id = row_id
