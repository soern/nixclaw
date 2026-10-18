"""Text helpers for calls: sentence splitting for pipelined TTS, filler filter."""

import re

# Pure hesitation/backchannel sounds: never a turn of their own. "ja"/"nein"
# are deliberately absent - they can be real answers.
FILLER_WORDS = frozenset({"hm", "hmm", "hmmm", "mhm", "mh", "äh", "ähm", "öh", "öhm", "eh", "ehm", "uh", "um"})

_SENTENCE_END = re.compile(r"(?<=[.!?…:])\s+|\n+")
MIN_SENTENCE_CHARS = 20


# A lone function word is what STT makes of background noise (clattering
# dishes came out as "Die.", "Ich.", "Ist.", "Punkt.") - never a turn on its
# own. Content words ("Hamburg", "ja", "stopp") still pass.
NOISE_WORDS = frozenset({
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "ich", "du", "er", "es",
    "wir", "ihr", "sie", "ist", "und", "oder", "so", "da", "punkt", "the", "a", "i",
    # a lone preposition/pronoun ("in" while the greeting played)
    "in", "im", "an", "am", "auf", "aus", "zu", "mit", "von", "vom", "bei", "nach",
    "mir", "mich", "dir", "dich", "uns", "man", "en", "un",
    # clatter transcribed as onomatopoeia
    "pip", "piep", "pump", "tak", "tack", "klack", "klick",
})


# Running water came out as "Tschttschttschttsch..."
_REPEATED_SYLLABLE = re.compile(r"(\w{2,6}?)\1{3,}")

# Acknowledgements said while the bot is talking ("ja", "okay", "mhm") are
# listening signals, not turns (see is_backchannel).
BACKCHANNEL_WORDS = frozenset({
    "ja", "jaja", "jo", "okay", "ok", "oke", "aha", "ah", "achso", "oh", "genau", "gut",
    "richtig", "stimmt", "klar", "alles", "super", "prima", "cool", "yes", "yeah", "right",
})


# Words a sentence doesn't end on: after a pause the caller is likely to go
# on. German separable particles ("ruf mich an", "hör auf", "komm mit") and
# words that end real sentences ("was ist das", "bist du da", "…, oder?")
# are deliberately missing; they count as unclear.
DANGLING_WORDS = frozenset({
    "und", "aber", "denn", "weil", "dass", "daß", "ob", "wenn", "als", "sondern", "obwohl",
    "der", "die", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "eines",
    "mein", "meine", "dein", "deine", "sein", "seine", "ihre", "unser", "unsere",
    "von", "für", "über", "unter", "bei", "beim", "im", "am", "zum", "zur", "vom", "ins",
    "wie", "wo", "wer", "warum", "wieso", "weshalb", "welche", "welcher", "welches", "welchen",
    "äh", "ähm", "öh", "öhm", "eh", "ehm", "uh",
    "and", "but", "because", "that", "the", "to", "of", "for", "with", "if", "my", "your",
})


# What a caller says to end the call. end_call is refused unless their last
# words match: the model hung up on its own after "Nein, Bad Lippspringe."
# "legen" alone: the ASR heard "auflegen" as "dann können wir legen".
_GOODBYE = re.compile(
    r"\btsch(?:ü|ue)ss?\b|\btschau\b|\bciao\b|\bservus\b|\bade\b"
    r"|\bauf wieder(?:sehen|hören|hoeren|schauen)\b|\bmach'?s gut\b|\bgute nacht\b"
    r"|\bbis (?:dann|später|spaeter|bald|morgen|nachher|gleich|die tage|zum nächsten mal)\b"
    r"|\bleg\w* (?:\w+ )*?auf\b|\b(?:auf)?legen\b|\bdas war'?s\b|\bschluss\b"
    r"|\b(?:anruf|gespräch|telefonat|call)\w* (?:\w+ )*?beend|\bbeend\w* (?:\w+ )*?(?:anruf|gespräch|telefonat|call)"
    r"|\b(?:good)?bye\b|\bhang up\b|\bsee you\b|\bend (?:the )?call\b|\bthat'?s all\b")


def is_goodbye(transcript: str) -> bool:
    """The caller ends the call ("Tschüss", "Leg auf", "Bis dann")."""
    return bool(_GOODBYE.search(transcript.lower().replace("’", "'")))


def utterance_end(transcript: str) -> str:
    """How a transcript ends after a pause: "finished" ("?", "!", or "." not
    after a dangling word), "unfinished" (dangling last word, "…") or
    "unclear" (no end punctuation; the ASR often leaves it off questions)."""
    t = transcript.strip()
    if not t or t[-1] in "?!":
        return "finished"
    if t.endswith(("…", "...")):
        return "unfinished"
    words = re.findall(r"[\wäöüß]+", t.lower())
    if words and words[-1] in DANGLING_WORDS:
        return "unfinished"
    return "finished" if t[-1] == "." else "unclear"


def is_filler(transcript: str) -> bool:
    """True for transcripts that must not become a turn: no words at all
    (STT returns "." for noise), only hesitation sounds, single letters
    ("K.", "T."), only function words ("Die.", "den den") or a repeated
    syllable (noise)."""
    words = re.findall(r"[\wäöüß]+", transcript.lower())
    if words and all(w in NOISE_WORDS for w in words):
        return True
    if words and all(len(w) == 1 for w in words):
        return True
    if any(_REPEATED_SYLLABLE.search(w) for w in words):
        return True
    return all(w in FILLER_WORDS for w in words)


def is_backchannel(transcript: str) -> bool:
    """Only acknowledgement/filler words ("Ja.", "Okay, alles klar.")."""
    words = re.findall(r"[\wäöüß]+", transcript.lower())
    return bool(words) and all(w in BACKCHANNEL_WORDS or w in FILLER_WORDS for w in words)


_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Structural tags the model copies from its history ("[Voice message: <uuid>.ogg]",
# "[End of Signal voice call.]"): read out, they are letters and numbers.
_BRACKET_TAG = re.compile(r"\[[^\[\]]*\]")
_MD_MARKUP = re.compile(r"[*_`~#>|]+")
_ARROW = re.compile(r"\s*[→⇒➜]\s*")
_LIST_BULLET = re.compile(r"^\s*(?:[-•–]|\d+[.)])\s+", re.MULTILINE)
# Emoji and pictographs (incl. variation selectors / joiners)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")


def to_speakable(text: str) -> str:
    """Strip what a TTS engine would read out literally or choke on:
    markdown, list bullets, URLs, emoji, [bracketed tags]. Line structure is kept (split
    points for split_sentences)."""
    text = _MD_LINK.sub(r"\1", text)
    text = _BRACKET_TAG.sub("", text)
    text = _URL.sub("", text)
    text = _MD_MARKUP.sub("", text)
    text = _LIST_BULLET.sub("", text)
    text = _EMOJI.sub("", text)
    text = _ARROW.sub(", ", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


MAX_CHUNK_CHARS = 90
_CLAUSE_END = re.compile(r"(?<=[,;–—])\s+")


def _chunk(sentence: str) -> list[str]:
    """Split a long sentence at clause boundaries into pieces of at most
    ~MAX_CHUNK_CHARS: a long sentence after a short one would otherwise
    leave a multi-second gap (its synthesis takes longer than the short
    one's playback). Clauses that are still too long split at a space."""
    if len(sentence) <= MAX_CHUNK_CHARS:
        return [sentence]
    pieces: list[str] = []
    cur = ""
    for clause in _CLAUSE_END.split(sentence):
        cand = f"{cur} {clause}".strip()
        if cur and len(cand) > MAX_CHUNK_CHARS:
            pieces.append(cur)
            cur = clause
        else:
            cur = cand
    if cur:
        pieces.append(cur)
    out: list[str] = []
    for piece in pieces:
        while len(piece) > MAX_CHUNK_CHARS * 3 // 2:
            cut = piece.rfind(" ", 0, MAX_CHUNK_CHARS)
            cut = cut if cut > 0 else MAX_CHUNK_CHARS
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        out.append(piece)
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"[\wäöüß]+", text.lower())


# Too common to show that a transcript repeats our own speech ("But it
# sounds like you have a German voice" shares you/have/a/it with any English
# reply).
ECHO_STOP_WORDS = NOISE_WORDS | frozenset({
    "you", "it", "is", "to", "and", "of", "in", "have", "be", "we", "that", "this", "can",
    "but", "like", "just", "yes", "no", "or", "on", "with", "for", "me", "my", "your",
    "are", "was", "do", "not", "what", "if", "then", "now", "m", "ll", "re", "s", "t",
    "nicht", "mit", "von", "zu", "auf", "für", "im", "in", "an", "habe", "hast", "hat",
    "bin", "bist", "sind", "kann", "mal", "noch", "auch", "wie", "was", "wenn", "dass",
    "dann", "ja", "nein", "mir", "dir", "mich", "dich", "uns", "aber", "schon", "jetzt",
})


def is_echo(transcript: str, spoken: list[str], min_overlap: float = 0.6) -> bool:
    """True if `transcript` (heard while we were speaking) is mostly our own
    words coming back down the line (speakerphone echo). Compares content
    words only and needs >= 2 of them: fewer can't be told apart from a
    short real answer."""
    words = [w for w in _words(transcript) if w not in ECHO_STOP_WORDS]
    if len(words) < 2:
        return False
    ours = {w for text in spoken for w in _words(text)}
    return sum(w in ours for w in words) / len(words) >= min_overlap


FIRST_CHUNK_CHARS = 40
MIN_HEAD_CHARS = 12
_FIRST_CUT = re.compile(r"(?<=[.!?…:,;–—])\s+")


def _split_first(chunk: str) -> list[str]:
    """Cut a short head off the reply's first chunk: synthesis time grows
    with length, and the caller hears nothing until the first chunk is done.
    Cuts at a mark up to 1.5x FIRST_CHUNK_CHARS (a sentence end within
    FIRST_CHUNK_CHARS first, then a clause mark within it, then the first
    mark beyond); without one, a chunk longer than that is cut at a space.
    The rest keeps >= MIN_SENTENCE_CHARS."""
    if len(chunk) <= FIRST_CHUNK_CHARS + MIN_SENTENCE_CHARS:
        return [chunk]
    soft = FIRST_CHUNK_CHARS * 3 // 2
    cuts = [m.start() for m in _FIRST_CUT.finditer(chunk)
            if MIN_HEAD_CHARS <= m.start() <= soft
            and len(chunk) - m.end() >= MIN_SENTENCE_CHARS]
    within = [c for c in cuts if c <= FIRST_CHUNK_CHARS]
    sentence_ends = [c for c in within if chunk[c - 1] in ".!?…"]
    if cuts:
        cut = (sentence_ends or within or cuts)[-1 if within else 0]
    elif len(chunk) > soft:
        cut = chunk.rfind(" ", MIN_HEAD_CHARS, FIRST_CHUNK_CHARS + 1)
        if cut < 0:
            return [chunk]
    else:
        return [chunk]
    return [chunk[:cut].strip(), chunk[cut:].strip()]


def split_sentences(text: str, short_first: bool = False) -> list[str]:
    """Split at sentence ends (long sentences further at clauses, see
    _chunk); pieces shorter than MIN_SENTENCE_CHARS are merged into the next
    one (one TTS call per tiny fragment sounds choppy and costs a round
    trip each). `short_first`: also cut a short head off the first piece
    (see _split_first) - for text that starts playing from silence."""
    parts = [p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()]
    out: list[str] = []
    pending = ""
    for part in parts:
        pending = f"{pending} {part}".strip()
        if len(pending) >= MIN_SENTENCE_CHARS:
            out.append(pending)
            pending = ""
    if pending:
        if out and len(pending) < MIN_SENTENCE_CHARS:
            out[-1] = f"{out[-1]} {pending}"
        else:
            out.append(pending)
    chunks = [chunk for sentence in out for chunk in _chunk(sentence)]
    if short_first and chunks:
        chunks[:1] = _split_first(chunks[0])
    return chunks
