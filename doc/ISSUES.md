# Known Issues

Open bugs and limitations. Remove an entry when it is fixed.

## Cross-turn summarization never triggers on tool-heavy channels

**Status:** open (found 2026-10-01)

**Where:** `src/nixclaw_agent/core.py`, `process_message`, the `can_summarize`
check that sets `self._summarization_due[channel_id]`.

**What:** The trigger estimates context size from message text only:

```python
message_dicts = [{"content": msg.event.message} for msg in history]
current_tokens = LLMClient.estimate_token_count(
    message_dicts + [{"content": event.message}], system_prompt=""
)
```

Since history replays real tool-call/result pairs
(`_build_history_messages` in `llm.py`), most of the prompt on an agentic
channel is tool-call arguments and tool results, which this estimate
ignores. Agent rows often have empty text (`"\n\n"`), so a channel with
hundreds of tool-heavy turns stays far below
`chat.context_size_threshold` and `_run_summarization` never runs.

**Evidence:** on 2026-10-01 a live deployment's `context_summaries` table was
empty for every channel. One channel had ~300 recent rows,
almost all agent rows with tool calls and blank text.

**Why it mattered less until now:** `init_database()` used to flag every
row except the 16 newest (globally, across all channels) as
`is_summarized=1` on each process start, without writing a summary. That
silently capped history and hid this issue, but it also wiped all context
on every restart (a user's follow-up after a restart reached a model with
no history at all). The startup wipe has been removed, so history is now
bounded only by `HISTORY_LOAD_LIMIT` / `chat.min_messages_to_keep` in the
history builder, and old rows are never summarized.

**Consequences:**
- No conversation summary is ever available for context older than the
  history window.
- `messages.db` grows unbounded per channel. `message_search` now includes
  summarized rows, so correctness doesn't depend on summarization, but it
  only scans the newest 500 rows in the requested range.

**Suggested fix:** estimate tokens on what is actually sent: include
serialized `tool_calls` arguments and (truncated, per
`chat.history_tool_result_max_chars`) `tool_results` in the estimate, or
reuse the output of `_build_history_messages` for the count. Add a test
with a tool-heavy history and empty assistant text that must cross the
threshold.

## Inbound message dedup never matches (redelivered webhooks are reprocessed)

**Status:** open (found 2026-10-01)

**Where:** `src/nixclaw_agent/core.py`, `_preprocess_incoming`, vs.
`src/nixclaw_agent/db/operations.py`, `store_message`.

**What:** The dedup key and the stored row id differ:

```python
# core.py, _preprocess_incoming
content_hash = hashlib.sha1((event.message or "").encode()).hexdigest()[:8]
message_id = f"{event.source}:{event.timestamp}:{content_hash}"
...
if self._db_ops.is_message_processed(message_id, channel_id): ...   # WHERE id = <with hash>
self._db_ops.mark_message_processed(message_id, channel_id)          # UPDATE ... WHERE id = <with hash>

# operations.py, store_message
message_id = f"{message.event.source}:{message.event.timestamp}"     # stored id, no hash
```

`mark_message_processed` updates no row, so `is_message_processed` is
always False. The content hash was added (2026-07-15) to keep
two distinct messages sent in the same second apart, but only on the dedup
side; `store_message` still keys rows on `source:timestamp` (so two such
messages still overwrite each other in the DB).

**Evidence:** in a live deployment's `messages.db` on 2026-10-01, every inbound
user row with `processed=1` is from 2026-07-15 or earlier (2640 rows);
all 3501 inbound rows since have `processed=0`. No other dedup layer
exists (the inbound queue only collapses consecutive identical messages
while a turn is running).

**Consequences:** a webhook redelivered by signal-cli-rest-api (e.g. after
a slow 200 or a restart) starts a second LLM turn for the same message.

**Suggested fix:** use one id for both: either add the content hash to the
`store_message` key (also fixes the same-second overwrite; check other
readers of `messages.id`, e.g. `message_search` context and history
replay, for assumptions about the format) or pass the stored id to
`is_message_processed`/`mark_message_processed`. Add a test that
processes the same Event twice and asserts the second call is skipped.

## Cronjob failure notification shows the time in UTC

**Status:** open

`CronJobNotification` ("Cronjob scheduled for <HH:MM> failed <n> times")
renders `scheduled_at`, which is stored in UTC. Per-channel time zones are
not tracked. Related: recurring jobs are anchored to a fixed UTC grid, so a
"daily 06:30 local" job shifts by one hour across DST changes.

## Unknown-sender gate in groups: limits

**Status:** open (by design / out of scope)

The `auth.unknown_users` gate (`core.py` `_unknown_sender_allowed`) only
decides whether an inbound Signal message is handled:
- Cronjobs created in a group keep running (and posting) after every
  KNOWN/PRIVILEGED member has left the group.
- The group member list is cached for up to 10 min (`web/channels.py`), so
  unknown senders may still be answered for up to 10 min after the last
  trusted member left.
- Members listed by signal-api only by UUID (no phone number) never count as
  trusted, so such a group fails closed for unknown senders.
- A dropped unknown group sender's messages (`allow_in_groups: false`, or no
  trusted member) are not stored, so a KNOWN user asking the bot about them
  gets no context. This is intended.

## Browser tools

**Status:** open (minor)

- **`browser_evaluate` expression-vs-body heuristic**: the script is wrapped
  as a bare expression (`return (…)`) only when it contains neither
  `return`, `;` nor a newline; anything else is wrapped as a function body.
  A one-line multi-statement script like `let x = 1; x` therefore returns
  nothing - use a newline or an explicit `return`. A word-boundary check for
  `return` would be more robust.
- **`web_search` result extraction**: the tool returns the Bing results page
  as an accessibility-tree snapshot. Small models sometimes miss the
  snippets in it; a dedicated snippet extractor would give tighter output.

## Voice calls (experimental)

**Status:** open

- `signal-call-tunnel` hard-codes sender device 1 for incoming ICE
  candidates, so calls may fail when the bot account is a linked device
  rather than the primary one. Its events are JSON assembled with `format!`
  without escaping.
- Tuning is German-centric: filler-word filter and goodbye detection
  (`voice/text.py`) mostly know German phrases, call speech uses the TTS
  engine of `tts.default_language`.
- One call at a time; group calls are not supported.

