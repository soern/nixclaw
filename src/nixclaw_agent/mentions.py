"""Server-side @-mention scan (pure: no I/O, no dependencies).

The web client sends only ``{token, author}`` pairs for the members it picked;
every offset is computed here, on the body that actually goes to Signal. One
algorithm (``apply_mentions``) is used twice on the server: once by the API as
an occurrence filter and once by ``core._send_web_mirror`` on the 🌐 caption,
which is the only place spans are produced.

Display text keeps ``@Name`` (chat history and the agent read it verbatim);
the transfer body gets ``￼ `` (U+FFFC + one space, the whitespace the user
typed after the token is NOT consumed), and a ``SignalMention`` span of
length 1 for every boundary-OK occurrence of an entry's token. ``to_wire``
converts the code-point starts to the UTF-16 code units signal-cli/libsignal
expect for the body-with-￼ (Java char / Swift NSRange convention).
"""

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

# U+FFFC OBJECT REPLACEMENT CHARACTER - what Signal clients show as the pill.
PLACEHOLDER = "￼"
# At most this many spans per message (the mirror caption is one message).
MAX_MENTIONS = 20
# Display token length cap ("@Name" including the leading '@').
MAX_TOKEN_CHARS = 80

# Token neighbours that still count as "inside a word" (so "foo@Alice" and
# "@AliceBlue" never match a member named Alice / André).
_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


@dataclass(frozen=True)
class MentionEntry:
    """One selected member: ``author`` is their UUID (never a phone number -
    signal-cli forwards the string verbatim into the mention's UUID field),
    ``token`` the display text that must appear in the body, e.g. "@Known Kid".
    """

    author: str
    token: str


@dataclass
class SignalMention:
    """A mention span: ``start`` is the code-point index of the ￼ in the
    body ``apply_mentions`` returned (``to_wire`` makes it UTF-16 units)."""

    author: str
    start: int
    length: int = 1


def valid_token(token: Any) -> bool:
    """A usable display token: non-empty '@'-prefixed text, no blank remainder."""
    return (
        isinstance(token, str)
        and 2 <= len(token) <= MAX_TOKEN_CHARS
        and token.startswith("@")
        and token[1:].strip() != ""
    )


def sanitize_sender_label(label: Optional[str]) -> Optional[str]:
    """Is this derived value allowed to be a sender attribution label?

    Policy for every derived label (chat_log names, consent voter names -
    attacker-influenceable via the sender's own Signal profile): strip ￼
    occurrences, collapse whitespace runs to single spaces + strip; reject
    (None -> caller takes the next ladder rung) when empty afterwards or when
    number-shaped - Signal's own display-name fallback IS literally the
    number, and harness-composed sends stay number-free. Digits *inside* a
    name remain fine. Caller-side on purpose: the DB/webhook sources keep
    returning their raw values (webui render source unchanged here).
    """
    if not label:
        return None
    cleaned = " ".join(label.replace(PLACEHOLDER, "").split())
    if not cleaned or re.fullmatch(r"\+\d{6,}", cleaned):
        return None
    return cleaned


def _candidate_tokens(entries: Sequence[MentionEntry]) -> list[tuple[str, str]]:
    """(token, author) pairs, deduped on (author, token), longest token first.

    Longest-first means "@Han Solo" wins over "@Han" at the same position; for
    two members with the same display name the first submitted entry wins.
    """
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        token, author = entry.token, entry.author
        if not valid_token(token) or not author:
            continue
        key = (author, token)
        if key in seen:
            continue
        seen.add(key)
        pairs.append((token, author))
    pairs.sort(key=lambda p: -len(p[0]))  # stable: equal lengths keep input order
    return pairs


def _boundary_ok(body: str, start: int, end: int) -> bool:
    """True if neither neighbour of body[start:end] belongs to a word."""
    before = body[start - 1] if start > 0 else ""
    after = body[end] if end < len(body) else ""
    return (before not in _WORD_CHARS) and (after not in _WORD_CHARS)


def _match_at(body: str, i: int, tokens: Sequence[tuple[str, str]]) -> Optional[tuple[str, str]]:
    for token, author in tokens:
        if body.startswith(token, i) and _boundary_ok(body, i, i + len(token)):
            return token, author
    return None


def apply_mentions(
    body: str, entries: Sequence[MentionEntry], scan_from: int = 0
) -> tuple[str, list[SignalMention]]:
    """Replace boundary-OK token occurrences in ``body[scan_from:]`` with "￼ ".

    Returns ``(raw_body, spans)``; ``scan_from`` anchors the scan behind a
    wrapper prefix (see ``core._send_web_mirror``) so a sender label equal to
    another member's token cannot consume that member's span. Every boundary-OK
    occurrence of an entry's token gets a span; an entry whose token never
    occurs contributes nothing. The result is unchanged (and spans empty) when
    nothing matched, so callers can send the plain text as-is.
    """
    tokens = _candidate_tokens(entries)
    if not tokens:
        return body, []
    out: list[str] = []
    spans: list[SignalMention] = []
    pos = 0       # code points written so far = index of the next char
    i = 0
    n = len(body)
    first = max(0, min(scan_from, n))
    while i < n:
        if i >= first and len(spans) < MAX_MENTIONS:
            hit = _match_at(body, i, tokens)
            if hit is not None:
                token, author = hit
                out.append(PLACEHOLDER + " ")
                spans.append(SignalMention(author=author, start=pos, length=1))
                pos += 2
                i += len(token)
                continue
        out.append(body[i])
        pos += 1
        i += 1
    return "".join(out), spans


def to_wire(body: str, spans: Sequence[SignalMention]) -> list[dict[str, Any]]:
    """JSON bodies for signal-cli-rest-api ``mentions`` (UTF-16 code units).

    Raises ValueError when a span does not point at a ￼ in ``body`` - a bug,
    never a reason to lose the message (the caller logs and sends without
    mentions).
    """
    wire: list[dict[str, Any]] = []
    for span in spans:
        if span.start < 0 or span.start >= len(body) or body[span.start] != PLACEHOLDER:
            raise ValueError(f"mention span start {span.start} is not a placeholder in the body")
        start = len(body[: span.start].encode("utf-16-le")) // 2
        wire.append({"start": start, "length": 1, "author": span.author})
    return wire
