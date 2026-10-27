"""Unit tests for the pure mention scan (src/nixclaw_agent/mentions.py)."""

import pytest

from src.nixclaw_agent.mentions import (
    MAX_MENTIONS,
    MAX_TOKEN_CHARS,
    PLACEHOLDER,
    MentionEntry,
    SignalMention,
    apply_mentions,
    to_wire,
    valid_token,
)

UUID = "11111111-2222-3333-4444-555555555555"


def _e(token: str, author: str = UUID) -> MentionEntry:
    return MentionEntry(author=author, token=token)


# --- apply_mentions: boundaries ---------------------------------------------------

def test_token_at_start_with_space_matches():
    raw, spans = apply_mentions("@Alice hi", [_e("@Alice")])
    assert raw == f"{PLACEHOLDER}  hi"  # placeholder + inserted space + typed space
    assert [(s.author, s.start, s.length) for s in spans] == [(UUID, 0, 1)]


def test_token_at_end_without_trailing_space_matches():
    raw, spans = apply_mentions("hi @Alice", [_e("@Alice")])
    assert raw == f"hi {PLACEHOLDER} "
    assert spans[0].start == 3


def test_email_like_prefix_does_not_match():
    raw, spans = apply_mentions("write to foo@Alice now", [_e("@Alice")])
    assert spans == []
    assert raw == "write to foo@Alice now"


def test_longer_name_does_not_match_shorter_token():
    raw, spans = apply_mentions("@AliceBlue is blue", [_e("@Alice")])
    assert spans == []
    assert raw == "@AliceBlue is blue"


def test_underscore_before_token_blocks_match():
    _, spans = apply_mentions("x_@Alice", [_e("@Alice")])
    assert spans == []


def test_underscore_after_token_blocks_match():
    _, spans = apply_mentions("@Alice_x", [_e("@Alice")])
    assert spans == []


def test_punctuation_boundaries_are_allowed():
    _, spans = apply_mentions("(hi @Alice)! @Bob.", [_e("@Alice"), _e("@Bob", "other-uuid")])
    assert [s.author for s in spans] == [UUID, "other-uuid"]


# --- apply_mentions: one span per occurrence -------------------------------------

def test_duplicate_token_occurrences_each_get_a_span():
    raw, spans = apply_mentions("@Alice and @Alice again", [_e("@Alice")])
    assert len(spans) == 2
    assert raw == f"{PLACEHOLDER}  and {PLACEHOLDER}  again"
    assert [s.start for s in spans] == [0, raw.index(PLACEHOLDER, 1)]


def test_same_author_twice_with_two_tokens_is_allowed():
    _, spans = apply_mentions("@Alice @Ann", [_e("@Alice"), _e("@Ann", UUID)])
    assert len(spans) == 2


def test_entry_with_no_occurrence_is_dropped():
    raw, spans = apply_mentions("nothing here", [_e("@Alice")])
    assert (raw, spans) == ("nothing here", [])


def test_duplicate_entries_are_deduped_on_author_and_token():
    _, spans = apply_mentions("@Alice", [_e("@Alice"), _e("@Alice")])
    assert len(spans) == 1


def test_longest_token_wins_at_the_same_position():
    raw, spans = apply_mentions("@Han Solo hi", [_e("@Han"), _e("@Han Solo", "second")])
    assert len(spans) == 1
    assert spans[0].author == "second"
    assert raw == f"{PLACEHOLDER}  hi"


def test_accented_and_ascii_variants_are_independent_tokens():
    raw, spans = apply_mentions("@André and @Andrea", [_e("@André", "u1"), _e("@Andrea", "u2")])
    assert [s.author for s in spans] == ["u1", "u2"]
    assert raw == f"{PLACEHOLDER}  and {PLACEHOLDER} "


def test_token_equal_to_body_apart_from_trailing_space():
    raw, spans = apply_mentions("@Alice ", [_e("@Alice")])
    assert raw == f"{PLACEHOLDER}  "
    assert spans[0].start == 0


# --- apply_mentions: scan_from anchor --------------------------------------------

def test_scan_from_does_not_consume_tokens_in_the_wrapper_prefix():
    # core renders "🌐 @Alice: <text>"; a sender named "@Alice" must not steal
    # the real token's span.
    prefix, _ = apply_mentions("🌐 @Alice: ", [])
    caption = prefix + "@Alice ping"
    raw, spans = apply_mentions(caption, [_e("@Alice")], scan_from=len(prefix))
    assert len(spans) == 1
    assert raw == f"🌐 @Alice: {PLACEHOLDER}  ping"
    assert spans[0].start == len(prefix)


def test_scan_from_negative_is_clamped_to_zero():
    _, spans = apply_mentions("@Alice", [_e("@Alice")], scan_from=-5)
    assert len(spans) == 1


# --- apply_mentions: caps and invalid entries ------------------------------------

def test_total_spans_are_capped():
    body = " ".join(["@Alice"] * (MAX_MENTIONS + 5))
    raw, spans = apply_mentions(body, [_e("@Alice")])
    assert len(spans) == MAX_MENTIONS
    # the tail keeps its plain display text (never half-converted)
    assert raw.count(PLACEHOLDER) == MAX_MENTIONS
    assert raw.count("@Alice") == 5


def test_overlong_token_is_rejected():
    token = "@" + "a" * MAX_TOKEN_CHARS
    assert not valid_token(token)
    raw, spans = apply_mentions(token, [_e(token)])
    assert (raw, spans) == (token, [])


@pytest.mark.parametrize("token", ["", "Alice", "@", "@ ", "@\t", None, 5, True])
def test_invalid_tokens_are_rejected(token):
    assert not valid_token(token)


def test_token_with_inner_space_is_valid():
    assert valid_token("@Known Kid")


# --- to_wire ---------------------------------------------------------------------

def test_to_wire_passes_ascii_starts_through():
    body = f"hi {PLACEHOLDER} you"
    wire = to_wire(body, [SignalMention(author=UUID, start=3)])
    assert wire == [{"start": 3, "length": 1, "author": UUID}]


def test_to_wire_counts_utf16_code_units():
    # 😀 is 2 UTF-16 code units, the space 1 -> the placeholder sits at unit 3.
    body = f"😀 {PLACEHOLDER}"
    wire = to_wire(body, [SignalMention(author=UUID, start=2)])
    assert wire == [{"start": 3, "length": 1, "author": UUID}]


def test_to_wire_rejects_wrong_char_at_start():
    with pytest.raises(ValueError):
        to_wire("hello", [SignalMention(author=UUID, start=1)])


def test_to_wire_rejects_out_of_range_start():
    with pytest.raises(ValueError):
        to_wire(f"hi {PLACEHOLDER}", [SignalMention(author=UUID, start=9)])
    with pytest.raises(ValueError):
        to_wire(f"hi {PLACEHOLDER}", [SignalMention(author=UUID, start=-1)])


def test_to_wire_on_the_full_mirror_caption():
    # 🌐(2) + space(1) + "Contact"(7) + ":"(1) + " "(1) + 😀(2) + " "(1) = 15
    # (label rung: a sanitized chat_log name - never a number - same math)
    body = f"🌐 Contact: 😀 {PLACEHOLDER}  ping"
    start = body.index(PLACEHOLDER)
    assert to_wire(body, [SignalMention(author=UUID, start=start)]) == [
        {"start": 15, "length": 1, "author": UUID}
    ]


# --- sender-label sanitizer (mirror ladder + consent render chain) --------------------

def test_sanitize_sender_label_acceptable_labels_pass_through():
    from src.nixclaw_agent.mentions import sanitize_sender_label
    assert sanitize_sender_label("Alice") == "Alice"
    assert sanitize_sender_label("Bob 2nd") == "Bob 2nd"  # digits inside a name are fine
    assert sanitize_sender_label("  A\n\tB  ") == "A B"  # whitespace runs collapse + strip


def test_sanitize_sender_label_accepts_digit_bearing_mixed_labels():
    """Accept-side pin (review round: guard the fullmatch): ONLY a wholly
    number-shaped label is rejected; a number run next to / inside a name
    always survives. Drift from fullmatch to search would reject the two
    "4912345678" cases (each embeds a "+<6 digits>" substring a bare search
    would hit)."""
    from src.nixclaw_agent.mentions import sanitize_sender_label
    assert sanitize_sender_label("Agent +49") == "Agent +49"
    assert sanitize_sender_label("Agent +4912345678") == "Agent +4912345678"
    # leading +digits but not the WHOLE string -> not number-shaped
    assert sanitize_sender_label("+49 hotline crew") == "+49 hotline crew"
    # the shape check runs on the collapsed string; collapse itself keeps working
    assert sanitize_sender_label("  +49   hotline\tcrew ") == "+49 hotline crew"


def test_sanitize_sender_label_rejects_numbers_and_empty():
    from src.nixclaw_agent.mentions import sanitize_sender_label
    # number-shaped display names never become labels (Signal's own fallback IS
    # the number); the reject makes the caller take the next rung
    assert sanitize_sender_label("+491761234567") is None
    assert sanitize_sender_label(" +491761234567 ") is None
    assert sanitize_sender_label("+12345") == "+12345"  # below the 6-digit shape
    assert sanitize_sender_label("") is None
    assert sanitize_sender_label(None) is None
    assert sanitize_sender_label("   \t\n ") is None


def test_sanitize_sender_label_strips_placeholder():
    from src.nixclaw_agent.mentions import sanitize_sender_label
    assert sanitize_sender_label("a￼b") == "ab"
    assert sanitize_sender_label(PLACEHOLDER * 2) is None  # tofu-only -> empty -> reject
