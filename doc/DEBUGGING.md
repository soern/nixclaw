# Debug Logging System

NixClaw includes a comprehensive debug logging system that captures detailed information about agent operations for troubleshooting and analysis.

## Overview

The debug logger captures:
- **Inputs**: Prompts sent to the LLM, user messages received
- **LLM Thinking**: Responses from the LLM (when available)
- **Tool Calls**: Tools invoked by the LLM with their parameters
- **Tool Results**: Outputs and errors from tool executions
- **Outputs**: Responses sent back to users

## Configuration

Debug logging is configured in `config/agent.yaml`:

```yaml
debug_logging:
  mode: "auto"        # off | auto | on
  file: "/data/debug-agent.json"  # Output file path (code default is /logs/debug-agent.json, which is container-local; set to /data/... for host visibility)
  buffer_size: 400     # Circular buffer size (auto mode only)
```

### Modes

| Mode | Behavior |
|------|----------|
| `off` | No debug logging |
| `auto` | 400-line circular buffer, flushed to file on error or `__ISSUE_DETECTED__` |
| `on` | Every debug event written directly to file |

### Environment Variable Override

```bash
export DEBUG_LOGGING_MODE=on  # Override mode setting
```

## Log File Format

Debug logs use NDJSON (newline-delimited JSON) format:

```json
{"ts": "2026-04-02T10:30:00.123Z", "type": "event_type", "channel": "+49...", "data": {...}}
```

## Event Types

| Event Type | Description | Data Fields |
|------------|-------------|-------------|
| `incoming_event` | User message received | source, destination, message, group_id, attachments_count |
| `llm_input` | LLM request sent | messages_count, tools_count |
| `llm_output` | LLM response received | response_text, tool_calls |
| `tool_call` | Tool execution started | tool_name, arguments, tier |
| `tool_result` | Tool execution completed | tool_name, success, output, error |
| `outgoing_message` | Response sent to user | recipient, message |
| `error` | Error recorded (auto mode also flushes the buffer) | error_context |
| `leaked_tool_markup` | LLM emitted tool-call markup as plain text; guard discarded the completion and re-prompted (see below) | source, channel_id, attempt |
| `leaked_reasoning` | LLM emitted raw reasoning-tag content as plain text; same guard as `leaked_tool_markup` | source, channel_id, attempt |

## Automatic Error Flush

In `auto` mode, the debug buffer is automatically flushed when:

1. **Tool errors**: A tool raises an exception (`tool_error:{tool_name}`) — handler-returned `success: false` results do NOT flush
2. **LLM errors**: The LLM API call fails
3. **Tool timeouts**: A tool exceeds its timeout (`tool_timeout:{tool_name}`)
4. **Signal send failures**: Sending a message fails

## LLM-Reported Issues

When the LLM encounters an issue it cannot resolve, it can trigger a debug buffer flush by including the marker `__ISSUE_DETECTED__` in its response.

The system:
1. Detects the marker in the LLM response
2. Flushes the 400-line buffer to the debug log file
3. Strips the marker from the response before sending to the user

### System Prompt Instruction

When in `auto` mode, the LLM receives this instruction (text lives in `config/prompts/default/debug-mode.md`, overridable via `config/prompts/custom/`; it must keep the `__ISSUE_DETECTED__` token):

```
★ DEBUG MODE: This conversation uses buffered logging for debugging. 
If you encounter ANY error, unexpected behavior, or issue, include the marker __ISSUE_DETECTED__ in your response to trigger diagnostic logging.
Important: Include __ISSUE_DETECTED__ ONLY when you need to report an actual issue, not for normal responses.
```

## Prompt Loading and Reload Logs

- DEBUG: `Loaded prompt <file> from <path>` / `Loaded prompt overrides for <file> from <path>` show which
  default or custom file is effective; `Ignoring unknown custom prompt file` flags a misnamed file in
  `config/prompts/custom/`.
- INFO/ERROR on `/reload` or SIGHUP: `Reload (<source>) OK: applied=[...] restart_required=[...]` or
  `Reload (<source>) failed, keeping previous config and prompts: [...]`.
- A prompt error at startup aborts with all problems listed in one message.

## Leaked Tool-Call Markup

The vLLM backend (Qwen) sometimes emits tool-call markup (`<tool_call><function=…><parameter=…>`) as plain message text instead of a structured tool call — usually when the markup is malformed and the server-side `--tool-call-parser` falls through. `src/nixclaw_agent/tool_markup.py` (`chat_with_markup_guard`) wraps the user-facing `chat()` call sites in `core.py`: on detecting markup it discards the entire completion (text and any parsed tool_calls), re-prompts the model, and retries up to 3×. If all retries fail it returns empty so nothing is sent.

Each failed attempt is recorded as a debug event `leaked_tool_markup` and an error context `leaked_tool_markup:{source}:{channel_id}` (source ∈ `chat` | `cron` | `subagent`). If you see these, the upstream fix is to verify the vLLM host's `--tool-call-parser` matches the model family (e.g. `qwen3_xml`/`qwen3_coder`); client-side detection is only the safety net, since malformed markup always slips past any server parser.

## Oversized/Rejected Images

The vLLM backend's multimodal processor sometimes rejects an attached image outright with an
HTTP 400 - e.g. `{"error":{"message":"Failed to apply Qwen3VLProcessor on data={'text': ...,
'images': [<PIL.Image.Image image mode=RGB size=4080x1836 ...>]} ...","type":"BadRequestError",
"code":400}}`. This showed up in practice as a real-world incident: two phone-camera photos
(4080×1836) sent to a channel both got the generic `"LLM temporarily unavailable"` reply right
after a model swap (qwen3.6 → qwen3.8), while smaller images (browser screenshots via
`analyze_image`) kept working fine on the same backend - the new model's vision processor
simply couldn't handle that resolution.

`VLLMClient.chat()` (`llm.py`) now retries this automatically: on catching `ImageProcessingError`
(matched from the 400 body via `_IMAGE_PROCESSOR_ERROR_RE`), it downscales the image(s) along
`IMAGE_SHRINK_LADDER_PIXELS` and resends, caching the size that worked per model id so it isn't
re-discovered on every image (see `doc/ARCHITECTURE.md`'s "vLLM image auto-shrink cache"
section for the full mechanism). If you still see `"LLM temporarily unavailable"` for an
image-carrying message, check the debug log for the error context first:

```bash
grep -A1 '"attachments_count": 1' data/debug-agent.json | grep '"type": "error"'
```

- `error_context` containing `"vLLM call failed: ...Processor..."` or mentioning
  `PIL.Image.Image` / `image_processor`, immediately followed by more `llm_input`/`tool_call`
  entries at increasing shrink sizes and then a normal `outgoing_message` → the auto-shrink
  path handled it; no action needed.
- The same error repeated `1 + len(IMAGE_SHRINK_LADDER_PIXELS)` times in a row for one message
  (i.e. every rung of the ladder failed) → the backend can't handle this image at any size the
  ladder tries; the shrink-ladder constants in `llm.py` may need a lower floor for this
  model/hardware, or the model's vision support may be broken (check the vLLM container logs
  directly for a stack trace under `Failed to apply ...Processor`).
- No `Processor`/`image` text in the error body at all (e.g. `context length exceeded`, a
  connection error, a stall) → an unrelated cause; see the other sections in this doc and
  `ContextLengthExceededError`/`StreamStallError`/`ModelNotFoundError` in `llm.py`.

## Group Messages Silently Undelivered (One Group Only)

**Symptom**: the agent shows a typing indicator for a specific Signal *group* and then nothing
— no reply ever arrives, in that group only. Other groups and 1:1 chats with the same account
work normally. The debug log shows the full turn completing normally: `incoming_event` →
`llm_input`/`llm_output` (non-empty `response_text`) → `outgoing_message`. `docker logs
nixclaw-signal-api` shows `POST /v2/send` returning `201 Created` for that message. Nothing in
any log indicates failure — because nothing failed at the HTTP/API layer.

**Root cause**: Signal's GroupV2 protocol encrypts group messages with a *sender key* — a
symmetric key the sender (this bot) generates once per group and distributes to every member's
device, then reuses for every subsequent message to that group. 1:1 messages and group *typing
indicators* use a different mechanism (per-member Double Ratchet sessions) and are unaffected —
which is exactly why typing indicators keep working while the actual message silently vanishes.
If a member's device loses sync with the cached sender key (reinstall, device change, corrupted
local state, etc.), signal-cli keeps encrypting with the now-stale key. The Signal *server* just
relays ciphertext and has no way to detect this, so `POST /v2/send` keeps returning `201`. The
recipient's client silently fails to decrypt and drops the message with no visible error on
either side. Leaving and rejoining the group does **not** fix this — the group's internal ID
(derived from its master key) is unchanged, so signal-cli's cached, still-stale sender-key rows
survive the round trip.

**Diagnosis**: confirm it's this specific issue, not a NixClaw bug, by sending a message
directly to the bridge, bypassing the agent entirely:

```bash
curl -s -X POST http://localhost:8080/v2/send -H "Content-Type: application/json" -d '{
  "number": "<bot phone number>",
  "recipients": ["group.<base64 of \"signal-group:\" internal_id>"],
  "message": "delivery test"
}'
```

If this also fails to arrive (while the same call to a *different* group or to a 1:1 number
succeeds), it's confirmed to be group-specific stale sender-key state, not an agent bug.

**Fix**: `scripts/reset_group_sender_keys.py` deletes the stale `sender_key`/`sender_key_shared`
rows for the affected group's `distribution_id` in signal-cli's local SQLite database
(`account.db`), forcing a fresh sender key to be generated and redistributed on the next
message — the same thing that naturally happens for a brand-new group. It only touches those
two tables (never membership, permissions, message history, or 1:1 sessions), takes an automatic
timestamped backup before writing, and refuses to run unless the `signal-api` container is
confirmed stopped (required — this is a live SQLite WAL file otherwise). See the script's
docstring for full usage; short version:

```bash
docker-compose stop signal-api
python3 scripts/reset_group_sender_keys.py --group-id "<internal_id>" --yes   # one group
# or: python3 scripts/reset_group_sender_keys.py --all --yes                  # every group
docker-compose start signal-api
```

`--dry-run` is safe to run anytime, including while `signal-api` is up, and reports what would
be deleted without changing anything. Resetting a group that wasn't actually affected is
harmless (just an unnecessary-but-invisible key refresh), so `--all` is reasonable to run
whenever you're not sure which group is broken.

## Approval Polls (consent)

Approval polls (`consent.py`, see `doc/ARCHITECTURE.md` "User consent") log to the normal agent log
(`docker logs nixclaw`), not the debug buffer. Grep for `poll` / `Consent`:

| Log line | Meaning |
|---|---|
| `Poll created for <recipient>: ts=<ts> question=...` (signal.py) | Signal API accepted the poll; `ts` identifies it everywhere below |
| `Create poll for <recipient> failed: ...` | Poll not posted; the model got "Could not ask the users for approval; the command was not run." |
| `Consent poll <ts> posted in <channel> (interactive\|background): '<question>'` | Request is pending in memory (+ `consent_polls` row) |
| `Poll vote from <voter> in <channel>: target_ts=<ts> options=[...] vote_count=N` (signal.py) | Webhook received a vote; `options=[]` is a vote removal |
| `Ignoring vote on unknown poll <ts> in <channel>` (DEBUG) | Not a pending agent poll (human poll, already resolved, duplicate delivery, or channel mismatch) |
| `Ignoring vote by <voter> (<tier>) on consent poll <ts>: may not use <tool>` | Voter's tier too low; poll stays open |
| `Consent poll <ts> in <channel>: approved=<bool> by <voter>` | First counting vote; poll is deleted next |
| `Consent poll <ts> in <channel>: answered with a message` / `: expired` / `: ended by shutdown` | Chat-lane outcomes without a vote |
| `Ending turn paused on a consent poll in <channel>: stop/reset shortcut` | Stop/reset while paused |
| `Remote-deleted message <ts> for <recipient>` / `Remote delete of <ts> for <recipient> failed: ...` | Poll removal |
| `Background consent poll <ts> in <channel>: expired` | Background poll timed out (`consent.background_timeout_minutes`) |
| `Background run in <channel>: identical <tool> command already waits on a poll` | Dedup: no second poll |
| `Approved background command (poll <ts>) in <channel> succeeded` / `failed` / `crashed: ...` | Result of the approved re-run |
| `Cronjob <id> will report the failed approved command (poll <ts>) in <channel>` | Inspection cronjob scheduled after a failed approved re-run |
| `Startup cleanup of consent poll <ts> in <channel>: deleted=<bool>` | Poll left open by a restart was cleaned up |
| `Tool 'bash': dropping reserved arguments [...] supplied by the model` | Model tried to pass `consent`/`channel_id`/... itself; ignored |

A poll that never appears although the script should ask: the marker must be a complete **stderr**
line with the channel's crc (`NIXCLAW_CHANNEL_ID`) - a `2>&1` redirect or pipe moves it to stdout
and it becomes plain output. The raw `pollVote` JSON is logged at DEBUG (`Raw pollVote: ...`).

## Buffer Flush Markers

When the buffer is flushed, a marker entry is written:

```json
{"ts": "2026-04-02T10:30:00.123Z", "type": "buffer_flush", "trigger": "error|llm_reported_issue", "entries_follow": 400}
```

## Viewing Debug Logs

### Check Log File
```bash
# View recent entries
tail -f /data/debug-agent.json

# Count entries
wc -l /data/debug-agent.json

# Pretty print with jq
cat /data/debug-agent.json | jq .
```

### Parse Specific Event Types
```bash
# View only tool calls
grep '"type": "tool_call"' /data/debug-agent.json | jq .

# View only errors
grep '"type": "error"' /data/debug-agent.json | jq .
```

## Debug Log Location

The debug log file path is configured via `debug_logging.file`. By default in Docker:

| Environment | Default Path |
|-------------|--------------|
| Docker | `/logs/debug-agent.json` (code default; container-local unless overridden — set `debug_logging.file` explicitly, e.g. to `/data/debug-agent.json` which is mapped to `./data/` on host) |

## Usage Example

### Scenario: Agent produces unexpected response

1. Enable debug logging (if not already):
   ```yaml
   debug_logging:
     mode: "auto"
   ```

2. Trigger the issue by asking the agent to perform the problematic task

3. If the LLM reports an issue, it includes `__ISSUE_DETECTED__` in its response when in auto debug_logging mode.

4. Check the debug log:
   ```bash
   tail -50 /data/debug-agent.json
   ```

5. Look for:
   - The `buffer_flush` marker with trigger `"llm_reported_issue"`
   - The preceding entries showing what happened

## Security Considerations

- Debug logs may contain sensitive information (message content, tool parameters)
- Ensure log file permissions are restricted in production
- Consider using `off` mode in production environments

## Implementation Details

The debug logging system is implemented in:

| File | Purpose |
|------|---------|
| `src/nixclaw_agent/debug_log.py` | DebugLogger class, buffer management |
| `src/nixclaw_agent/core.py` | Event logging, issue marker detection |
| `src/nixclaw_agent/llm.py` | LLM input/output logging, system prompt instruction |
| `src/nixclaw_agent/tools/registry.py` | Tool call/result logging |
| `src/nixclaw_agent/config.py` | Configuration dataclass |
