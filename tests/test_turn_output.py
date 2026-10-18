"""Unit tests for TurnOutput (single-message mode state machine, turn_output.py).

A fake sender records every new message and edit; no Signal/DB involved.
"""

from typing import Optional
from unittest.mock import MagicMock

import pytest

from src.nixclaw_agent.turn_output import TurnOutput


class FakeSignal:
    """Records sends; new messages get increasing timestamps 1000, 1001, ..."""

    def __init__(self, new_returns_none: bool = False, edit_ok: bool = True):
        self.new: list[tuple[int, str]] = []
        self.edits: list[tuple[int, str]] = []
        self.hidden: list[str] = []
        self.new_returns_none = new_returns_none
        self.edit_ok = edit_ok
        self._next_ts = 1000

    async def send_new(self, text: str) -> Optional[int]:
        ts = self._next_ts
        self._next_ts += 1
        self.new.append((ts, text))
        return None if self.new_returns_none else ts

    async def send_edit(self, target_ts: int, text: str) -> bool:
        if not self.edit_ok:
            return False
        self.edits.append((target_ts, text))
        return True

    def mark_hidden(self, row_id: str) -> None:
        self.hidden.append(row_id)

    def output(self, mode: str, **kwargs) -> TurnOutput:
        return TurnOutput(mode, self.send_new, self.send_edit, self.mark_hidden, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["append", "replace"])
async def test_text_only_turn_is_one_message_plus_edits(mode):
    sig = FakeSignal()
    out = sig.output(mode)
    for text in ("a", "b", "c"):
        await out.send_text(text)
    assert [t for _, t in sig.new] == ["a"]
    assert [ts for ts, _ in sig.edits] == [1000, 1000]


@pytest.mark.asyncio
async def test_append_concatenates_with_separator():
    sig = FakeSignal()
    out = sig.output("append", separator=" | ")
    for text in ("a", "b", "c"):
        await out.send_text(text)
    assert [t for _, t in sig.edits] == ["a | b", "a | b | c"]


@pytest.mark.asyncio
async def test_replace_shows_only_newest_text():
    sig = FakeSignal()
    out = sig.output("replace")
    for text in ("a", "b", "c"):
        await out.send_text(text)
    assert [t for _, t in sig.edits] == ["b", "c"]


@pytest.mark.asyncio
async def test_mode_off_sends_every_text_as_new_message():
    sig = FakeSignal()
    out = sig.output("off")
    await out.send_text("a")
    out.note_attachment(5000, "caption")
    await out.send_text("b")
    assert [t for _, t in sig.new] == ["a", "b"]
    assert sig.edits == []


@pytest.mark.asyncio
async def test_text_after_attachment_edits_the_attachment_message():
    sig = FakeSignal()
    out = sig.output("append")
    await out.send_text("working")
    out.note_attachment(5000, "the picture")
    await out.send_text("done")
    assert [t for _, t in sig.new] == ["working"]
    assert sig.edits == [(5000, "the picture\n\ndone")]


@pytest.mark.asyncio
async def test_replace_after_captionless_attachment():
    sig = FakeSignal()
    out = sig.output("replace")
    out.note_attachment(5000, None)
    await out.send_text("here it is")
    assert sig.new == []
    assert sig.edits == [(5000, "here it is")]


@pytest.mark.asyncio
async def test_second_attachment_leaves_first_untouched():
    sig = FakeSignal()
    out = sig.output("append")
    out.note_attachment(5000, "first")
    await out.send_text("x")
    out.note_attachment(6000, "second")
    await out.send_text("y")
    await out.send_text("z")
    assert sig.edits[0] == (5000, "first\n\nx")
    assert all(ts == 6000 for ts, _ in sig.edits[1:])
    assert sig.edits[-1] == (6000, "second\n\ny\n\nz")


@pytest.mark.asyncio
async def test_notice_is_appended_in_replace_mode():
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("partial answer")
    await out.send_text("LLM unavailable", notice=True)
    assert sig.edits == [(1000, "partial answer\n\nLLM unavailable")]


@pytest.mark.asyncio
async def test_edit_count_rollover_starts_new_message():
    sig = FakeSignal()
    out = sig.output("replace", max_edits=2)
    for text in ("a", "b", "c", "d", "e"):
        await out.send_text(text)
    # a=new(1000), b,c=edits, d=new(1001), e=edit of 1001
    assert [t for _, t in sig.new] == ["a", "d"]
    assert sig.edits == [(1000, "b"), (1000, "c"), (1001, "e")]


@pytest.mark.asyncio
async def test_length_rollover_in_append_mode():
    sig = FakeSignal()
    out = sig.output("append", max_chars=10, separator="")
    await out.send_text("12345")
    await out.send_text("678")      # "12345678" fits
    await out.send_text("90ab")     # would be 12 chars -> new message
    assert [t for _, t in sig.new] == ["12345", "90ab"]
    assert sig.edits == [(1000, "12345678")]


@pytest.mark.asyncio
async def test_length_rollover_in_replace_mode():
    sig = FakeSignal()
    out = sig.output("replace", max_chars=10)
    await out.send_text("short")
    await out.send_text("x" * 11)
    assert [t for _, t in sig.new] == ["short", "x" * 11]
    assert sig.edits == []


@pytest.mark.asyncio
async def test_edit_failure_falls_back_to_new_message():
    sig = FakeSignal(edit_ok=False)
    out = sig.output("replace")
    await out.send_text("a", row_id="r1")
    await out.send_text("b", row_id="r2")
    assert [t for _, t in sig.new] == ["a", "b"]
    assert out.target_ts == 1001
    assert sig.hidden == []  # "a" is still on screen


@pytest.mark.asyncio
async def test_rate_limited_new_message_clears_target():
    sig = FakeSignal(new_returns_none=True)
    out = sig.output("append")
    await out.send_text("a")
    await out.send_text("b")
    assert [t for _, t in sig.new] == ["a", "b"]
    assert sig.edits == []


@pytest.mark.asyncio
async def test_non_int_timestamp_is_not_a_target():
    """A mock send returning a MagicMock must not be used as edit target."""
    async def send_new(text):
        return MagicMock()

    edits = []

    async def send_edit(ts, text):
        edits.append(ts)
        return True

    out = TurnOutput("replace", send_new, send_edit)
    await out.send_text("a")
    await out.send_text("b")
    out.note_attachment(MagicMock(), "c")
    await out.send_text("d")
    assert edits == []


@pytest.mark.asyncio
async def test_reset_target_starts_new_message():
    sig = FakeSignal()
    out = sig.output("append")
    await out.send_text("a", row_id="r1")
    out.reset_target()
    await out.send_text("b", row_id="r2")
    assert [t for _, t in sig.new] == ["a", "b"]
    assert sig.edits == []


@pytest.mark.asyncio
async def test_identical_text_is_not_re_sent():
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("same")
    await out.send_text("same")
    assert len(sig.new) == 1 and sig.edits == []


@pytest.mark.asyncio
async def test_lanes_have_independent_targets():
    sig = FakeSignal()
    chat = sig.output("append")
    background = sig.output("append")
    await chat.send_text("chat 1")
    await background.send_text("cron 1")
    await chat.send_text("chat 2")
    await background.send_text("cron 2")
    assert sig.edits == [(1000, "chat 1\n\nchat 2"), (1001, "cron 1\n\ncron 2")]


# --- replace mode: flagging replaced rows user_visible=0 (amendment A2) ---

@pytest.mark.asyncio
async def test_replace_flags_previous_row():
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("a", row_id="r1")
    await out.send_text("b", row_id="r2")
    await out.send_text("c")  # new text without a row still hides "b"
    assert sig.hidden == ["r1", "r2"]


@pytest.mark.asyncio
async def test_replace_never_flags_row_holding_new_text():
    """Same-second row ids collide (INSERT OR REPLACE): never hide the new text."""
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("a", row_id="agent:1")
    await out.send_text("b", row_id="agent:1")
    assert sig.hidden == []


@pytest.mark.asyncio
async def test_append_never_flags():
    sig = FakeSignal()
    out = sig.output("append")
    await out.send_text("a", row_id="r1")
    await out.send_text("b", row_id="r2")
    assert sig.hidden == []


@pytest.mark.asyncio
async def test_notice_does_not_flag():
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("a", row_id="r1")
    await out.send_text("notice", notice=True)
    assert sig.hidden == []


@pytest.mark.asyncio
async def test_no_flag_after_attachment():
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("a", row_id="r1")
    out.note_attachment(5000, "pic")
    await out.send_text("b", row_id="r2")
    assert sig.hidden == []
    assert sig.edits == [(5000, "b")]


@pytest.mark.asyncio
async def test_no_flag_on_rollover():
    sig = FakeSignal()
    out = sig.output("replace", max_edits=1)
    await out.send_text("a", row_id="r1")
    await out.send_text("b", row_id="r2")  # edit -> flags r1
    await out.send_text("c", row_id="r3")  # rollover: new message, b stays visible
    assert sig.hidden == ["r1"]


@pytest.mark.asyncio
async def test_no_flag_after_reset_target():
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("a", row_id="r1")
    out.reset_target()
    await out.send_text("b", row_id="r2")
    await out.send_text("c", row_id="r3")
    assert sig.hidden == ["r2"]


@pytest.mark.asyncio
async def test_identical_text_moves_displayed_row():
    """Same text again under a new row: that row now holds what is on screen,
    so the next replacement hides it (not the older row)."""
    sig = FakeSignal()
    out = sig.output("replace")
    await out.send_text("a", row_id="r1")
    await out.send_text("a", row_id="r2")
    await out.send_text("b", row_id="r3")
    assert sig.hidden == ["r2"]


@pytest.mark.asyncio
async def test_segments_are_stripped_in_append_mode():
    sig = FakeSignal()
    out = sig.output("append")
    await out.send_text("\n\nNow opening /slow-page.\n\n")
    await out.send_text("\n  \n")  # empty after stripping: skipped
    await out.send_text("\nDone.\n")
    assert [t for _, t in sig.new] == ["Now opening /slow-page."]
    assert sig.edits == [(1000, "Now opening /slow-page.\n\nDone.")]


@pytest.mark.asyncio
async def test_segments_are_stripped_in_replace_mode_and_after_attachment():
    sig = FakeSignal()
    out = sig.output("replace")
    out.note_attachment(5000, "\npic\n")
    await out.send_text("x", notice=True)
    await out.send_text("\n\nfinal\n\n")
    assert sig.edits == [(5000, "pic\n\nx"), (5000, "final")]


@pytest.mark.asyncio
async def test_mode_off_sends_text_unchanged():
    sig = FakeSignal()
    out = sig.output("off")
    await out.send_text("\n\nraw\n\n")
    assert sig.new == [(1000, "\n\nraw\n\n")]
