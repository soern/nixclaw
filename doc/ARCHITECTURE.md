# nixclaw - Software Architecture

## Overview

nixclaw is an agent harness that communicates via Signal messenger. Built simple and robust - no overengineering.

---

## Key Design Decisions (Immutable)

The following design decisions are **IMMUTABLE** - they represent fundamental
architectural choices that must NOT be changed without explicit user approval.
These decisions serve as guardrails for automated processes and subagents.

### Security Model: 3 Layers

1. **Docker Sandboxing** - The Agent runs inside a Docker container and has full
   access to files within that container. Docker is the primary security boundary
   that keeps the agent sandboxed from the host system.

2. **User Tiers via Tool Access** - User access is controlled through three tiers:
   - UNKNOWN: Users not in config get `get_skill` only
   - KNOWN: Non-critical tools only (file ops, browse, search - no bash)
   - PRIVILEGED: All tools including bash with full system access
   
   Tool access is the enforcement mechanism for these tiers.

3. **Per-User Context Isolation** - File access tools restrict users to their own
   context directory. PRIVILEGED users have bash access which can reach beyond
   context - this is an ACCEPTED RISK.

### Design Principles

- **Simplicity**: Keep the design as simple as possible
- **No Rate Limiting on Bash**: PRIVILEGED users may execute unlimited bash commands
- **No Ad-Hoc Restrictions**: Tool access controlled exclusively through the permission model

---

## High-Level Architecture

**Important**: `nixclaw` runs with `network_mode: host` (see `docker-compose.yml`) — it is
*not* on the `nixclaw-network` bridge with the other services. It reaches `signal-api` and
the `browser` service via `localhost:<port>` (their ports are published on the host's
127.0.0.1 only), and `signal-api`'s webhook config points back at
`http://host.docker.internal:3002/webhook/signal?token=<SIGNAL_WEBHOOK_SECRET>`.
Only `signal-api` and `browser` share the `nixclaw-network` bridge network with each
other. The webhook on port 3002 is reachable from anything that can reach the host, so it
requires the shared secret (see "Signal-cli → nixclaw-agent" below).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Host Machine (host network)                    │
│                                                                              │
│   ┌───────────────────────┐        localhost:3002 (webhook)                │
│   │   nixclaw-agent       │◄───────────────────────────────────┐           │
│   │   (Python asyncio)    │                                    │           │
│   │   network_mode: host  │──── localhost:8080 ───┐             │           │
│   │   port 3002           │──── localhost:8931 ─┐  │             │           │
│   └───────────┬───────────┘                    │  │             │           │
│               │                                 │  │             │           │
│               ▼                                 ▼  ▼             │           │
│   ┌─────────────────────────────────────────────────────────┐    │           │
│   │              nixclaw-network (bridge, 172.18.60.0/24)    │    │           │
│   │  ┌──────────────────────────┐  ┌─────────────────────┐  │    │           │
│   │  │  signal-api              │  │   browser             │  │    │           │
│   │  │  bbernhard/signal-cli-   │  │   playwright server   │  │    │           │
│   │  │  rest-api, port 8080     │  │   port 8931 (ws)      │  │    │           │
│   │  └────────────┬─────────────┘  └───────────────────────┘  │    │           │
│   │               │ RECEIVE_WEBHOOK_URL=                       │    │           │
│   │               │ http://host.docker.internal:3002/webhook  ─┼────┘           │
│   │               ▼                                            │                │
│   │  ┌──────────────────────────────────┐                      │                │
│   │  │   .signal-data/ volume           │                      │                │
│   │  │   (signal-cli accounts, keys)    │                      │                │
│   │  └──────────────────────────────────┘                      │                │
│   └─────────────────────────────────────────────────────────────┘                │
│                                                                              │
│  ┌─────────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────────────────┐  │
│  │ config/     │  │ logs/    │  │ data/    │  │ data/messages.db         │  │
│  │ agent.yaml  │  │ *.log    │  │ <chan>/  │  │ (single SQLite DB)       │  │
│  └─────────────┘  └──────────┘  └──────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              │ Signal Protocol
                              ▼
                    ┌─────────────────────┐
                    │   Signal Service    │
                    │   (external)       │
                    └─────────────────────┘
```

`docker-compose.test.yml` adds a `mock-signal-api` service (port 8081) for testing; see the
Testing section below.

---

## Testing with Mock Signal API

For development and issue analysis, the agent can be tested using a mock Signal API that doesn't send real messages.

**See**: [`process/TESTING_WITH_MOCK_SIGNAL_API.md`](../process/TESTING_WITH_MOCK_SIGNAL_API.md)

**Key concepts:**
- **Test numbers**: Numbers starting with `+0000` route to mock API instead of real Signal
- **Message injection**: `send_mock_message.sh` script injects test messages via `/_mock/inject` endpoint
- **Reply capture**: Agent replies are captured in mock API and viewable via `docker logs nixclaw-mock-signal-api`
- **Tier verification**: Test numbers `+000000` (privileged), `+000001` (known), `+000002` (unknown)

**Test number routing** (`signal.py`):
```python
if recipient.startswith("+0000") and config.signal.mock_api_url:
    api_url = config.signal.mock_api_url  # Route to mock
else:
    api_url = self.api_url  # Route to real Signal API
```

**Docker compose:**
```bash
# Start with mock API
docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d

# Rebuild after code changes
docker-compose -f docker-compose.yml -f docker-compose.test.yml build nixclaw
docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d nixclaw
```

---

## Components

### 1. signal-cli-rest-api Container

**Image**: `bbernhard/signal-cli-rest-api:latest`

**Purpose**: Signal protocol client for sending/receiving messages via REST API.

**Interface**: REST API on port 8080

**Environment Variables**:
- `MODE=json-rpc`
- `RECEIVE_WEBHOOK_URL=http://host.docker.internal:3002/webhook/signal?token=${SIGNAL_WEBHOOK_SECRET}`
- `HTTPS_VERIFICATION=false`

**Webhook Server**: nixclaw-agent exposes webhook endpoint on port 3002 to receive messages from signal-cli-rest-api.

**Data Volume**: `/home/.local/share/signal-cli` (accounts, keys, data)

**Responsibilities**:
- Connect to Signal service
- Receive incoming messages, forward via webhook to nixclaw-agent
- Send outgoing messages via REST API
- Handle group messages

---

### 2. nixclaw-agent Container

**Purpose**: Core agent harness - message processing, LLM interaction, tool execution.

**Interface**: Python asyncio application

**Key Modules**:

#### `agent/core.py`
Main agent loop and event processing.

```
main() -> None
    Entry point, initializes all components.

AgentLoop.run() -> None
    Main event loop running until shutdown.
    - Listens for: Signal messages, cron triggers
    - Spawns asyncio.Task for each event, one active turn per channel_id
      PER LANE - chat via _dispatch_or_queue (interactive lane), cron/subagent
      via _register_channel_task(..., lane="background") (background lane);
      see Concurrency Model

AgentLoop._preprocess_incoming(event, channel_id, reply_destination)
        -> PreprocessedMessage
    Shared pre-loop handling, used both for immediately-processed and
    queue-drained messages:
    - Deduplicate via SQLite, loop detection
    - Queue a read receipt for the message (see "Read receipts" below)
    - Stop/reset keyword shortcuts (abort_loop=True for a running turn)
    - Mention gating + mention stripping
    - Store message
    - Download attachments from signal-cli
    - For audio attachments: transcribe via faster-whisper-server (see agent/stt.py)
      and rewrite event.message with the transcript before the LLM turn
    - Re-store the message so the row reflects the transcript

AgentLoop.process_message(event: Event) -> None
    - _preprocess_incoming (above)
    - Invoke LLM
    - Handle tool calls, draining queued inbound messages at each
      iteration (_drain_inbound_checkpoint)
    - Send response via signal-cli (single-message mode may edit the
      turn's last message instead; see agent/turn_output.py)
    - After it returns/raises, _start_channel_task flushes the turn's
      read receipts (_flush_read_receipts)
```

#### `agent/stt.py`
Voice-message transcription (audio/aac, audio/mp4, etc.) into text before the
LLM turn sees the message. Pipeline:

1. ffprobe duration check → `AudioTooLongError` placeholder if > cap
2. ffmpeg segment muxer chunks the audio into ≤chunk_seconds WAVs
   (16 kHz mono, default 28 s — below Whisper's 30 s hard limit)
3. sequential httpx multipart POSTs to `{stt.base_url}/v1/audio/transcriptions`
   (via the shared `_transcribe_chunks` helper, also used by the
   `retranscribe_audio` tool below)
4. join segments → cache as `<audio>.transcript.md` next to the file
5. the source audio file and its sidecar are now kept for `stt.retention_hours`
   (default 1h), not deleted immediately - so `retranscribe_audio` can re-run
   it through a different backend. Each message rewritten with a transcript
   is tagged per-attachment as `[Voice message: <filename>] <text>` so the
   agent has a handle to pass to that tool. A periodic sweep
   (`stt.sweep_expired_audio`, called via `asyncio.to_thread` from the main
   loop every 5 min) deletes anything - audio and/or orphaned sidecar - older
   than the retention window, whether or not transcription ever succeeded.
   It scans `data/<channel>/attachments` and the nested attachments dirs of
   group channels (`data/signal-group:<a>/<b>/attachments`; group ids contain
   `/`), but never user folders that happen to be called `attachments`. It
   does not follow symlinks. The web interface keeps the transcript in
   `chat_log` (see "webui Container"), so it survives the sweep.

Failure mode is graceful: any transcode/HTTP error becomes
`[Voice message — transcription failed]` so the agent can still respond.
The transcription is a kill-switchable preprocessing step (`stt.enabled`); it
is not a tool and does not gate on tier.

**Alternate STT backend / `retranscribe_audio` tool** (`tools/stt_ops.py`,
KNOWN+): a second, independently-configured backend (`stt.alternate`,
currently NVIDIA Canary-1b-v2 via `deploy/dgx-spark/canary-1b-v2/`) reachable
only through this on-demand tool, never through the automatic pipeline above.
Benchmarking found Canary occasionally beats
whisper on clean narration but silently returns an empty transcript on noisy
or code-switched audio and has no language auto-detection - so it's exposed
as a "second opinion" the agent can reach for when the primary transcript
looks garbled, not a fallback. Calls the same `_transcribe_chunks` helper
against `config.stt.alternate`; concurrency to that shared GPU host is capped
by a per-`base_url` semaphore (`alternate.max_concurrent_requests`, default
1), mirroring `tts.py`'s per-engine semaphore pattern. `tools/registry.py`
computes this tool's timeout dynamically (chunk count × `alternate.timeout` +
the semaphore wait budget + 30s), the same rationale as the image-gen
dynamic-timeout branch.

#### `agent/llm.py`
LLM interaction supporting two backends: Ollama and vLLM.

```
LLMClient.chat(messages: list[Message], tools: list[Tool]) -> Response
    - Send chat request to configured backend
    - Handle tool calls from model
    - Return parsed response

VLLMClient.chat(messages: list[Message], tools: list[Tool]) -> Response
    - Send chat request to vLLM /v1/chat/completions endpoint
    - Handle tool calls from model
    - Return parsed response

LLMClient.build_messages(context_messages: list[Message], event: Event, summary_text?: str, keywords?: list) -> list[dict]
    - Auto-generate structured messages from template
    - Include last 15 messages (configurable)
    - Prepend summary and keywords if available
    - Token-based soft barrier triggers background summarization
```

**Backend Selection**: Configured via `LLM_BACKEND` env var or `backend` config field ("ollama" or "vllm")

**vLLM concurrency cap**: `VLLMClient.chat()` gates its retry loop behind a process-wide
`asyncio.Semaphore` sized by `vllm.options.max_concurrent_requests` (default 2), queuing
FIFO up to `vllm.options.max_concurrent_wait_seconds` (default 240s) before raising
`VLLMConcurrencyLimitError`. See the Concurrency Model section below.

**Summarization (two independent mechanisms):**
- *Cross-turn, DB-based* (main chat agent only): `_run_summarization` in `core.py` summarizes older channel history via `summarize_conversation`/`distill_keywords`, marks messages `is_summarized=1`, and persists the summary (`store_summary`). Triggered by the token/message soft barrier in `process_message`.
- *Session-local, ephemeral* (cronjobs and sub-agents): `_maybe_compact_session` in `core.py` compacts the in-memory working message list of a single background run in place when it exceeds `chat.turn_compaction_threshold_tokens`. A pinned anchor (`messages[:anchor_len]` = system prompt, plus the inherited channel summary for sub-agents only, plus the initial prompt) and a recent tail of `chat.min_messages_to_keep` messages are kept verbatim; the middle is replaced by one `PROGRESS SUMMARY` system message produced by `summarize_working_history`. The tail boundary is snapped forward to the next `assistant` message so an assistant's `tool_calls` are never separated from their `tool` results. Nothing is persisted. Cronjobs deliberately start fresh (no channel-summary injection); sub-agents inherit it.

`summarize_conversation`, `summarize_working_history`, and `distill_keywords` are thin methods on **both** `LLMClient` and `VLLMClient` that delegate to `self`-free module-level helpers in `llm.py` (the `_build_history_messages` pattern), so summarization works identically on both backends. `LLMClient` and `VLLMClient` remain independent top-level classes (no shared parent — see `tests/test_build_messages_refactor.py` item #12).

Single-tool-result overflow (a result larger than the context budget on its own, which
summarizing the *middle* of the history can't help with since the recent tail is kept
verbatim) is capped at append-time: `llm.py`'s `_tool_result_content()` helper truncates
every tool result via `llm.py`'s `truncate_tool_result()` to `chat.tool_result_max_chars`
(default 8000, floor-clamped) before it's appended to the working message list, uniformly
across the main chat agent and the cronjob/sub-agent loops.

History replay (`_build_history_messages`): a stored agent row with tool calls is replayed as real
`assistant(tool_calls)` -> `tool` result messages -> (if non-empty) `assistant(text)`, like the live
loop, so nothing in history resembles text the model could echo as a reply. Each replayed result is
capped at `chat.history_tool_result_max_chars` (default 500, per result, NOT per row). The
summarization trigger's token estimate counts message text only, so replayed tool results are not
included in it (open issue, see `doc/ISSUES.md`).

#### `agent/context.py`
Context management per Signal channel. Note: all channels share one SQLite database
(`config.database.db_path`, default `/data/messages.db`) via `db/operations.py`'s single
module-level connection — `channel_id` is a row filter, not a separate file per channel.
`Context.messages_db` is set to `<data_dir>/<channel_id>/messages.db` but is not actually
read anywhere; only `attachments_dir`, `memory_dir`, and `soul_path` are used.

```
ContextManager.get_context(channel_id: str) -> Context
    - Ensure per-channel directories exist (attachments/, memories/); locate SOUL.md if present

ContextManager.get_messages_for_llm(channel_id: str) -> list[Message]
    - Load all non-summarized messages for channel from the shared DB
    - Excludes messages marked is_summarized=1

ContextManager.load_history(channel_id: str, limit: int = 10) -> list[Message]
ContextManager.load_extended_history(channel_id: str, limit: int = 50) -> list[Message]
ContextManager.get_message_count(channel_id: str) -> int

ContextManager.get_summary(channel_id: str) -> (summary: str, keywords: list) | None
    - Retrieve stored context summary and keywords for channel

ContextManager.store_summary(channel_id: str, content: str, keywords: list, version: int, token_count: int, messages_before_count: int) -> None
ContextManager.mark_summarized(channel_id: str, before_timestamp: int) -> None
    - Mark messages older than timestamp as summarized
```

#### `agent/tools/`
Tool implementations.

```
ToolRegistry.register(name: str, handler: Callable) -> None
ToolRegistry.get_tools_for_tier(tier: UserTier) -> list[Tool]
```

**Available tools:**

| Tool | Description | Tier |
|------|-------------|------|
| `file_read(path)` | Read file within context | KNOWN+ |
| `file_write(path, content)` | Write file within context | KNOWN+ |
| `file_grep(pattern, path)` | Search files within context | KNOWN+ |
| `file_list(path)` | List directory contents | KNOWN+ |
| `file_search(pattern, path)` | Glob-based file search | KNOWN+ |
| `message_search(query?, approximate_time?, before?, after?, context_lines?, limit?)` | Search message history with fuzzy keyword and time-based search; includes summarized rows; times are local (shown with UTC offset, `approximate_time` uses local day boundaries) | KNOWN+ |
| `bash(cmd, timeout?)` | Execute bash command (own process group; may ask for approval via a poll, see "User consent") | PRIVILEGED |
| `browser_navigate(url?)` | Navigate to URL and return text snapshot; empty url re-reads current page | KNOWN+ |
| `browser_interact(action, target?, value?)` | click / type / press / back on the current page (Playwright selectors) | KNOWN+ |
| `browser_screenshot(url?, filename?)` | Take screenshot, save PNG to channel attachments | KNOWN+ |
| `browser_evaluate(script)` | Run JavaScript inside the page (page.evaluate; no Playwright API access) | KNOWN+ |
| `web_search(query)` | Search the web | KNOWN+ |
| `schedule_cron(prompt, when, summary, recurring?, interval_minutes?)` | Schedule task; `summary` is a required ~20-word description shown by `list_cronjobs` | PRIVILEGED |
| `list_cronjobs(status?, job_id?)` | List scheduled jobs (id + summary + next run + status), or with `job_id` show one job's full prompt | PRIVILEGED |
| `cancel_cronjob(job_id)` | Cancel scheduled job | PRIVILEGED |
| `spawn_subagent(prompt)` | Spawn background task | PRIVILEGED |
| `get_skill(name)` | Load skill instructions | ALL |
| `send_signal_attachment(filename, caption?)` | Send attachment via Signal | KNOWN+ |
| `analyze_image(image_filename, image_query)` | Analyze image with LLM | KNOWN+ |
| `read_soul()` | Read channel persona | KNOWN+ |
| `update_soul(new_content)` | Update channel persona | PRIVILEGED |
| `remember(content, memory_type?)` | Store memory | PRIVILEGED |
| `recall_memories(query, memory_type?)` | Search memories | PRIVILEGED |
| `get_channel_mode()` | Get current channel mode (mention_required/always_process) | KNOWN+ |
| `set_channel_mode(mode)` | Set channel mode (mention_required/always_process) | KNOWN+ |
| `debug_cron(seconds?, limit?)` | View discarded cronjob outputs for debugging | PRIVILEGED |
| `debug_subagent(seconds?, limit?)` | View discarded subagent outputs for debugging | PRIVILEGED |
| `stop_running_activity(activity_id)` | Stop a running background task (cronjob or subagent) in this channel | PRIVILEGED |
| `generate_image(prompt, input_images?, size?)` | Generate or edit an image via the FLUX.2 server | KNOWN+ |
| `text_to_speech(text, language?, voice?)` | Synthesize speech via the configured TTS engine(s); voice = argument > channel setting > engine `voice` | KNOWN+ |
| `list_tts_voices(language?)` | Voice in effect per language + the voices each TTS server lists (`GET /v1/voices`, one call per distinct `base_url`; no listing/404 = only the configured voice) | KNOWN+ |
| `set_tts_voice(language, voice)` | Store this channel's voice for one language in `data/<channel>/tts_voice.json` (checked against the server's listing when it has one; empty = back to the default) | KNOWN+ |
| `retranscribe_audio(filename)` | Re-transcribe a retained voice message via the alternate STT backend | KNOWN+ |

The built-in email tools have been removed; privileged users can instruct the agent to author a personalized email-sending script in their channel workspace using the file and bash tools.

**Tool Execution Model**:
- Tools execute sequentially (LLM waits for result before continuing)
- Timeout: 30s default, 120s for analyze_image, bash = its own `timeout` + 5s (see "`bash` tool
  execution"); an approval poll wait happens outside the tool call. The 5 browser tools
  (web_search/browser_navigate/browser_interact/browser_evaluate/browser_screenshot)
  use fixed operation budgets: 45s for the first four, 120s for browser_screenshot
  (`tools/registry.py`: `BROWSER_TOOLS`/`BROWSER_OPERATION_BUDGET_SECONDS`).
- On timeout: return error to LLM, LLM handles gracefully
- File operations: restricted to context directory only
- Circuit breaker: After 3 failures for the same tool *with the same arguments* (keyed on `(tool_name, param_hash)`) within 60s, further calls with that argument hash are rejected
- Within a single message's tool-call loop, executed tool calls are cached (last 20, `MAX_TOOL_CACHE_SIZE` in `core.py`) so identical repeat calls don't re-execute

#### Skills System (`agent/skills/`, `agent/tools/skills.py`)

The skills system extends the agent with domain-specific knowledge and procedures.

```
SkillEntry:
    name: str              # Unique skill identifier
    description: str        # Human-readable description
    keywords: list[str]     # Trigger keywords for auto-loading
    file: Optional[str]     # Path to SKILL.md (system skills only)
```

**Skill Manifests**:

System skills defined in `config/skills.yaml`:
```yaml
skills:
  - name: weather
    file: skills/weather/SKILL.md
    description: Get current weather and forecasts
    keywords: [weather, forecast, temperature]
```

User/channel skills in `/data/<channel_id>/config/skills.yaml`:
```yaml
skills:
  - name: custom-weather   # EXTEND: adds new skill
    file: ../skills/custom-weather/SKILL.md
    description: Custom weather handling
    keywords: [rain, sunshine]
  
  - name: weather           # OVERLAY: overrides system weather
    file: ./skills/weather/SKILL.md
    description: My custom weather skill
    keywords: [weather, forecast]
```

**User Skill Overlay vs Extension**:

| Scenario | Behavior |
|----------|----------|
| User skill with **same name** as system | **OVERRIDES** system skill entirely |
| User skill with **new name** | **EXTENDS** available skills |

The merge uses dict semantics: user manifest is loaded AFTER system manifest, so same-name entries overwrite system entries.

**Skill File Locations**:
| Type | Manifest Location | Skill File Location |
|------|-------------------|---------------------|
| System | `config/skills.yaml` | `<PROJECT_ROOT>/skills/<name>/SKILL.md` |
| User | `/data/<channel_id>/config/skills.yaml` | `/data/<channel_id>/skills/<name>/SKILL.md` |

**Skill Routing**:
The `get_skill_routing_for_channel(channel_id)` function builds the SKILL LOADING section of the system prompt by:
1. Loading system manifest (`config/skills.yaml`)
2. Loading user manifest (`/data/<channel_id>/config/skills.yaml`) if exists
3. Merging via dict (same name = override)
4. Generating keyword → `get_skill(name="...")` mappings

**Skill Triggering**:
The system prompt includes a SKILL LOADING section that instructs the LLM to call `get_skill` when user mentions relevant keywords. Example:

```
# SKILL LOADING
Use the get_skill tool to load skill instructions. Call get_skill when user mentions ANY of these topics:
- weather, forecast, temperature → get_skill(name="weather")
- create skill, new skill → get_skill(name="skill-creator")

CRITICAL: When user asks about any topic above, you MUST call get_skill FIRST before answering.
```

**Skill File Resolution** (`get_skill_file_path`):
1. If `channel_id` provided, check user skill manifest first for the skill name
2. If found in user manifest, return `/data/<channel_id>/skills/<name>/SKILL.md`
3. Fall back to system manifest, return `PROJECT_ROOT/<skill.file>`

**Available System Skills**:
| Skill | File | Keywords |
|-------|------|----------|
| weather | skills/weather/SKILL.md | weather, forecast, temperature |
| skill-creator | skills/skill-creator/SKILL.md | create skill, new skill |
| analyze_image | skills/analyze_image/SKILL.md | image, picture, photo |
| memory | skills/memory/SKILL.md | remember, recall, memory |
| soul | skills/soul/SKILL.md | soul, persona, personality |
| recurring-research | skills/recurring-research/SKILL.md | news, research, monitor, recurring |
| print (example, disabled by default: commented out in `config/skills.yaml`) | skills/print/SKILL.md | print, printing, printer, pdf |
| generate_image | skills/generate_image/SKILL.md | draw, generate image, create image, picture, edit photo, illustration |
| text_to_speech | skills/text_to_speech/SKILL.md | voice, voices, speak, say out loud, audio, vorlesen, sprich, sprachnachricht, stimme |

#### `agent/auth.py`
User tier management and tool permission enforcement.

```
TOOL_PERMISSIONS: dict[UserTier, set[str]]
    - PRIVILEGED: {bash, schedule_cron, spawn_subagent, debug_cron, debug_subagent, ...}
    - KNOWN: {file_read, file_write, browser_navigate, get_channel_mode, ...}
    - UNKNOWN: {get_skill}

AuthManager.get_user_tier(phone_number: str) -> UserTier
    - Privileged: Full tools
    - Known: Non-privileged tools
    - Unknown: `get_skill` only

AuthManager.can_use_tool(tier: UserTier, tool_name: str) -> bool
    - Checks if tool_name is in TOOL_PERMISSIONS[tier]

AuthManager.get_allowed_tools(tier: UserTier) -> set[str]
    - Returns TOOL_PERMISSIONS[tier] for filtering

ToolRegistry.get_tools_for_tier(tier: UserTier) -> list[Tool]
    - Filters registry to only tools in auth.get_allowed_tools(tier)
    - ToolRegistry.execute_tool() enforces via can_use_tool() check
```

#### `agent/cron.py`
Cronjob scheduling and execution.

```
CronScheduler.schedule(when: datetime, prompt: str, channel_id: str, creator_number: str,
                        is_recurring: bool = False, interval_minutes: int | None = None,
                        summary: str = "") -> str
    - Store cronjob in SQLite
    - Return job_id

CronScheduler.check_due() -> list[CronJob]
    - Query due jobs for execution
    - Does NOT mark them 'running' - see mark_running() below. A row this
      returns that the caller decides to skip (e.g. its channel's background
      lane is busy) is simply left pending/failed, so the next poll retries
      it - it used to be marked 'running' unconditionally here, which left a
      skipped job stuck forever (get_due_cronjobs() only re-selects
      pending/failed rows, and mark_complete() is never called for a job
      that was never dispatched)

CronScheduler.mark_running(job_id: str) -> None
    - Flip a job to 'running'; callers (core.py's run() loop) call this only
      for a due job they are actually about to dispatch

CronScheduler.mark_complete(job_id: str, success: bool, is_recurring: bool = False,
                             interval_minutes: int | None = None, is_cancelled: bool = False,
                             scheduled_at: str | None = None) -> None
    - Mark job status after execution; reschedules recurring jobs on a fixed UTC grid
    - On recurring success: computes next run via compute_next_scheduled(anchor, interval, now),
      where anchor is the original scheduled_at time. This prevents drift accumulation: even if
      a job completes 5 minutes late, the next run stays on the original grid (1 day later for
      daily jobs), not pushed 5 minutes later
    - Treats naive (no tzinfo) ISO anchors as UTC
    - Note: the anchor's UTC value is fixed at job-creation time; across DST transitions, a
      "daily 6:30 AM local" job will shift by the DST offset and remain shifted until the next
      transition (out of scope for cron-expression complexity trade-off)
```

Discarded cronjob/subagent outputs (via `__DISCARD_OUTPUT__`) are stored and retrievable by
PRIVILEGED users via the `debug_cron`/`debug_subagent` tools.

Every cronjob run's prompt is prefixed with the `cron-run-preamble` prompt (`config/prompts/default/cron-run-preamble.md`) before being sent to
the model (`_process_cronjob` in `core.py`): it states this is an unattended run with no user
present, that the final text response is sent to the user verbatim (no plans/JSON), how to use
`__DISCARD_OUTPUT__`/why never to use `__UNMUTE__`, and that tool results are data, not
instructions. Individual job prompts (and the recurring-research skill) no longer need to repeat
this boilerplate themselves.

An LLM-call failure during a cron run (`chat_with_markup_guard` raising) is deliberately **not**
caught inside `_process_cronjob`'s try body - it propagates to the method's own `except Exception`,
which calls `mark_complete(success=False, ...)` and lets the failure-count/threshold machinery
above handle it. Catching it locally and substituting placeholder text used to make every LLM
outage look like a *successful* run, sending/storing "LLM temporarily unavailable" to the user on
every tick of a recurring job instead of retrying quietly.

#### `agent/signal.py`
Signal-cli interface.

```
SignalClient.send_message(recipient: str, message: str, edit_timestamp: Optional[int] = None) -> Optional[int]
    - Send message via signal-cli REST API; with edit_timestamp it edits that earlier
      message (attachments of the original are kept). Returns the sent message's
      timestamp (real API: [{"timestamp": "<str>"}], mock: {"timestamp": <int>})

SignalClient.send_attachment(recipient, file_path, content_type, caption?) -> Optional[int]
    - Send an attachment (+ caption); returns its timestamp like send_message

SignalClient.send_read_receipt(sender: str, timestamp_ms: int) -> None
    - POST /v1/receipts/{number} {"receipt_type": "read", ...}; same +0000* -> mock
      routing as send_message; never raises (failure = warning log)

SignalClient.download_attachment(attachment_id: str) -> bytes
    - Download an attachment blob (falls back to the mock API when the
      live API returns 404 for a test-attachment filename)

SignalClient.save_attachment(attachment_id: str, content_type: str, channel_id: str, original_filename?: str) -> Optional[Attachment]
    - Download an attachment and persist it to the channel attachments dir

SignalClient.create_poll(recipient: str, question: str, answers: list[str]) -> Optional[int]
    - POST /v1/polls/{number} (single choice); returns the poll's timestamp, None on any failure

SignalClient.delete_message(recipient: str, timestamp: int) -> bool
    - DELETE /v1/remote-delete/{number}: removes one of our messages (e.g. a poll) for everyone

SignalClient.start_webhook(on_message, on_poll_vote=None)
    - on_poll_vote (async) receives PollVote(channel_id, voter, voter_name, target_ts,
      option_indexes, vote_count) for each dataMessage.pollVote
```
All of these use the shared `api_recipient()` (`signal-group:` -> `group.<base64>`) and
`_api_url_for()` (`+0000*` -> mock API) helpers.

**Poll votes in the webhook:** a `dataMessage.pollVote` is parsed before the no-text early return
and handed to `on_poll_vote` in its own task (`ConsentManager.on_vote`); it never becomes a chat
`Event` or DB row. The voter is the envelope `sourceNumber`/`source` (`pollVote.author*` is the
poll's creator, i.e. us); `channel_id` is derived like chat events (`signal-group:<groupId>` or the
voter's number). Ignored: votes in a `syncMessage` (our own devices), `pollCreate`/`pollTerminate`,
votes without a voter or a parseable `targetSentTimestamp`. An empty `optionIndexes` is a vote removal.

#### `agent/turn_output.py` (single-message mode)
Reduces the number of Signal messages per turn. `chat.single_message.mode` (global, hot-reloadable;
per-channel override `data/<channel>/message_mode.json` via the `set_message_mode`/`get_message_mode`
tools) is resolved once per turn (`tools/message_mode.py:resolve_message_mode`) in `process_message`,
`_process_cronjob` and `_process_subagent`, and a `TurnOutput` is hung off that turn's `Context`
(chat and background lanes each get their own, so they never edit each other's messages).

- `off`: every text is a new Signal message (pre-feature behaviour, exact old call shape).
- `append`: later texts edit the turn's last message to old + separator + new.
- `replace`: later texts edit it to show only the newest text. The replaced text's DB row gets
  `messages.user_visible = 0`; later turns render it in history with `[Replaced; not shown to user] `
  (llm.py), and the system prompt gets the `single-message-replace` section. The in-turn message
  list is never rewritten (prompt cache).

A sent attachment (`send_signal_attachment` -> `TurnOutput.note_attachment`) becomes the new target;
later texts edit its caption (Signal keeps the attachment on a text-only edit), earlier messages are
never touched again. A new message is started instead of an edit when there is no target, the target
has `max_edits_per_message` edits (default 9; Signal drops edits beyond 10), the resulting text would
exceed `max_message_chars` (both modes), or the edit failed (edits are never retried/rate-limited).
A user message injected mid-turn by `_drain_inbound_checkpoint` resets the target. Canned in-turn
notices (llm-unavailable, too-many-messages, internal-limit, no-summary-fallback) are always appended,
even in replace mode. Edits count toward `MAX_MESSAGES_PER_SESSION`.

Known/accepted: rate-limit-queued texts flush later as separate new messages; replaced attachment
captions are not flagged; summarizer/message_search see replaced texts normally; group edits were not
live-probed (an API error falls back to a new message); edits are not retried, so an edit that times
out client-side but was actually delivered still triggers the new-message fallback (duplicate text).
A queued message drained mid-turn resets the target when it is injected or when `_preprocess_incoming`
sent a canned reply for it (e.g. a queued `/reload`); an unmentioned group message does not.

#### Read receipts (end-of-turn marker)
A user's message is marked read exactly when the agent is done with it, so "read" means the turn
is over (a text-only "fixing now:" message alone doesn't tell the user whether the agent is still
working); mid-turn the sender only sees "delivered" (signal-cli sends delivery receipts itself;
the daemon runs without `--send-read-receipts`). Switch: `chat.read_receipts` (default true,
hot-reloadable, read at use time).

- Queue: one call in `_preprocess_incoming` right after the dedup check (`_queue_read_receipt` ->
  `self._pending_read_receipts[channel_id]`), so it covers fresh and drained-mid-turn messages
  alike, incl. loop detection, `/reload` and stop/reset shortcuts.
- Flush: `process_with_semaphore` in `_start_channel_task` calls `_flush_read_receipts` after
  `process_message` returns or raises: pops the channel's list, dedupes, sends concurrently with a
  5s cap (`READ_RECEIPT_TIMEOUT_SECONDS`) so a hung API doesn't delay the next queued turn. On
  `CancelledError` (shutdown) the list is discarded and nothing is sent. The receipt goes out after
  the turn's last reply/edit - not necessarily after the typing stop, which is debounced 5s.
- Not marked: unmentioned group messages (incl. an unmentioned group "stop"), duplicates, events
  dropped by inbound-queue overflow or left in the queue after a drained stop/reset aborted the
  turn, own sync messages (sender == own number), cronjob/subagent runs (no triggering message).
  Messages still queued when a turn ends start the next turn and are marked at its end.
- Accepted: a restart mid-turn leaves the message unread; a final reply that is rate-limited and
  queued for the next turn still gets its receipt at the end of this turn. The sender only sees "read" if their own Signal client
  has read receipts enabled (pending live confirmation).
- `Event.signal_timestamp_ms` keeps the raw envelope ms timestamp (receipts need it);
  `Event.timestamp` stays seconds (dedup ids / DB). The strip-mention rebuild uses
  `dataclasses.replace` so the field survives.

#### User consent (approval polls, `agent/consent.py`)
A bash script that must not act without the users' approval (example: the disabled-by-default
print skill's `ipp_print.py print`) asks the harness to post a Yes/No Signal poll; the command only runs after a
counting "Yes". Prompt rules ("wait until the user confirms") were not enough, so this is enforced
in the harness. It is opt-in per script: only scripts that print the marker are gated.

**Script <-> harness contract** (`tools/bash.py`, generic for any bash command):

| Direction | What | Meaning |
|---|---|---|
| harness -> script | env `NIXCLAW_CHANNEL_ID=<crc32(channel_id) as 8 hex>` | always set when the channel is known (`channel_crc()`) |
| script -> harness | stderr line exactly `NIXCLAW_REQUIRE_CONSENT_QUESTION_<crc>="<question>"` | "do nothing yet, ask the users this" |
| harness -> script | env `NIXCLAW_CONSENT=1` | approved run |
| script -> harness | stderr line exactly `NIXCLAW_CONSENT_OK_<crc>` | the approved action succeeded (used by the background lane) |

- Only complete stderr lines matching exactly with this channel's crc count (`_ConsentWatch`); a
  marker on stdout, with extra text or another channel's crc is plain output. Marker lines are
  removed from the output. Question: 1-200 chars (`CONSENT_QUESTION_MAX_CHARS`), no `"`; a
  malformed question with the right crc kills the command and returns an error to the model.
- A command whose text contains `NIXCLAW_CONSENT` is refused, and both env vars are scrubbed from the
  inherited environment. The model cannot skip approval via arguments either: `registry.execute_tool`
  strips `RESERVED_ARGUMENTS` (`consent`, `consent_question`, `channel_id`, `user_tier`) from
  model-supplied arguments; only core passes `execute_tool(..., consent_question=...)`.
- First run: on the question marker the process group is SIGKILLed at once; the remaining output
  (stdout + stderr, capped at 1500 chars, `CONSENT_USER_TEXT_MAX_CHARS`) becomes the info text and
  the tool returns a `ToolResult` with `consent_request` (a `ConsentRequest`: question, user_text,
  command, arguments). The model never sees first-run output.
- Approved run (`consent_question` set -> `NIXCLAW_CONSENT=1`): the script is expected to print the
  same question marker once more right before acting. Exactly one marker with the approved question
  is accepted; a second one, or a different question, kills the group and returns an error (one
  approval = one action; also catches a lost `NIXCLAW_CONSENT`, e.g. via `env -i`/sudo, as an error
  rather than a new poll). The OK marker sets `ToolResult.consent_ok`.

**Posting** (`ConsentManager.request`): the info text, then the poll (title = question verbatim,
answers `consent-answer-yes`/`consent-answer-no` from `user-messages.yaml`, matched by index 0 = yes,
1 = no) go out via `SignalClient` directly (no rate-limit queue, never a single-message edit). The poll
is stored in chat history as one outgoing message `<info text>\n[Poll: <question>]`. If the poll
cannot be posted (`create_poll` returns None) the tool result is "Could not ask the users for
approval; the command was not run." (`CONSENT_POLL_FAILED_ERROR`); nothing is pending or stored.

**Voters** (`ConsentManager.on_vote`): a vote counts only if the voter (envelope source, not
`pollVote.author`) has a tier allowed to use the tool (`auth.can_use_tool`; bash -> PRIVILEGED).
Other votes, vote removals and unknown answers are ignored and the poll stays open. The first
counting vote wins; the poll is then remote-deleted for everyone (not just closed) and its DB row
removed. Votes on polls not pending in memory (human polls, resolved polls, duplicate webhook
deliveries) are ignored. Voters without a visible phone number never count.

**Chat lane** (`AgentLoop._pause_for_consent`): the turn pauses right after the tool call. Typing
stops, the turn's `TurnOutput` target is reset and its read receipts are flushed. The turn keeps
its interactive-lane slot and its task-semaphore slot while paused (at most one paused turn per
channel). It waits on the vote, the inbound queue (`_inbound_wake` event, set by `_enqueue_inbound`)
or the timeout (`consent.timeout_minutes`, default 120, read at request time):

| Event | Result for the paused call | Turn |
|---|---|---|
| "Yes" | second run (same tool args, the turn's tier, normal bash timeout) + `(approved by <voter>)` (`consent-approved-suffix`) | continues |
| "No" | `consent-rejected` (`NOT RUN: <voter> rejected "<question>" ...`) | continues |
| message that would start a turn (mention in `mention_required` groups; any message in 1:1/`always_process`) | `consent-not-run-message` with `{{question}}` and `{{message}}`: answer/re-call unchanged, call with the change, or no call | continues |
| stop/reset shortcut | - (existing abort path) | ends |
| unmentioned chatter, duplicates | - | still paused |
| timeout | `consent-expired` | continues |
| shutdown (`AgentLoop.shutdown` -> `ConsentManager.shutdown`) | - (poll deleted, `consent-expired-restart` posted) | ends |

- Queued events are run one at a time through `_preprocess_incoming` (`_drain_while_paused`). The
  user's message goes into the paused call's tool result, not into a user turn of its own (keeps
  the assistant tool_calls -> tool results order). If a vote and a message arrive together, the
  vote wins: the rest stays queued for the next checkpoint, and a message already consumed goes to
  `held_messages`, injected as an `inbound-drain` user message after the step's tool results.
- Remaining tool calls of the same assistant message run normally after "Yes"; after any NOT RUN
  outcome they get `consent-skipped`. NOT RUN results are failures, so the turn's tool cache never
  replays them; an approved run is not cached either, so an identical later call asks again.
- A `try/finally` deletes the poll + DB row on every outcome except a vote (which already did it).

**Background lane** (`_process_cronjob`/`_process_subagent` -> `_background_consent_result`): no
pausing. The info text + poll are posted, the tool result is `consent-background-pending`, the run
continues and frees the lane normally. If an identical command (same tool + arguments) already waits
on a background poll in the channel, no second poll is posted (same result text). A timer task
deletes the poll after `consent.background_timeout_minutes` (default 720). "No"/expiry: poll
deleted, nothing else. "Yes" (`_on_background_consent_decision`): a separate task
(`_run_approved_background_command`, not awaited by the webhook) re-runs the stored arguments with
`consent_question` set, the voter's tier and a fresh background `Context` (channel cwd, ephemeral
browser scope, closed in `finally`); no LLM. Success with exit 0 and the OK marker -> the output is
posted to the channel verbatim (capped at 1500 chars) and stored. Anything else -> one one-time
cronjob, due now, `creator_number` = the voter, prompt `consent-inspection` (command + output tail,
capped at 3000 chars), so the agent tells the users what went wrong.

**Persistence** (`consent_polls` table, same DB as `cronjobs`, `db/operations.py`): pending requests
live in memory only; the table stores `(channel_id, poll_ts, created_at)` of open agent polls.
`AgentLoop.run()` calls `ConsentManager.cleanup_at_startup()` before starting the webhook: each
recorded poll is remote-deleted, `consent-expired-restart` is posted to its channel (5s timeout
each, once per channel) and the row is removed either way (a poll whose delete failed stays
visible, votes on it are ignored). A graceful shutdown (`ConsentManager.shutdown`) posts no new
polls and ends paused chat turns; their `discard()` deletes the poll + row and posts
`consent-expired-restart` to the channel (best effort, 5s timeout, once per channel). Background
rows stay for the startup cleanup; a channel that still has one gets no shutdown notice, so it is
told only once (by the startup cleanup).

**Known limits:**
- Only opted-in scripts are gated. Raw `ipptool`/`lp`/`curl` calls or a script that never prints
  the marker run without a poll; skills must tell the agent to call the gated script.
- First-run output (the info text) is posted verbatim to the whole channel, i.e. to every group
  member, without the model seeing or filtering it.
- An approved background re-run does not occupy the background lane, so it can overlap a
  cronjob/subagent running on the same channel.
- Markers are recognized only when bash knows the channel (always true for chat/cron/subagent runs).
- Group polls cannot be tested with the mock API (only `+0000*` 1:1 recipients route there); test
  them live.

#### `bash` tool execution (`agent/tools/bash.py`)
- The shell runs in its own process group (`start_new_session=True`); timeouts and consent markers
  kill the whole group (`os.killpg(SIGKILL)`), incl. `nohup ... &` children and loop bodies.
- stdout and stderr are two separate pipes read concurrently (`_OutputCollector`) and merged in
  arrival order for the model (interleaving only as exact as the reads).
- Timeout (model-chosen `timeout`, default 30s, max 300s, `effective_timeout()`): returns
  `Command timed out after Ns. Output so far: ...` (partial output, e.g. job IDs already
  submitted). If the shell itself exited but background children kept the pipes open, the error
  says so and suggests `>/dev/null 2>&1 &`.
- Non-zero exit: the output goes into the error text (`Exit code: N\n<output>`), since the model
  only sees the error of a failed result.
- `tools/registry.py` gives bash an outer timeout of `effective_timeout(timeout) + 5s`, so bash's own
  timeout (with partial output) fires first.

#### `agent/memory.py`
ChromaDB-based vector memory system for persistent memories, one `MemoryManager` instance per channel (persisted under `<data_dir>/<channel_id>/memories/`).

```
MemoryManager.add_memory(text: str, memory_type: str, ...) -> None
    - Add memory to the channel's ChromaDB collection

MemoryManager.search(query: str, memory_type: str | None = None, limit: int = 5) -> list[dict]
    - Semantic search of memories

MemoryManager.get_recent(limit: int = 10) -> list[dict]
    - Retrieve most recent memories

MemoryManager.prune(max_memories: int = 100) -> int
    - Drop oldest memories beyond the cap

MemoryManager.reset() -> bool
    - Clear all memories for the channel
```

The `remember`/`recall_memories` tools (PRIVILEGED) are the LLM-facing surface. Memory search
results are also injected into the system prompt for every message (not just when the LLM calls
`recall_memories`) via `_format_memories_for_prompt()` in `core.py`.

#### `agent/soul.py`
Per-channel persona/personality system with mtime-based caching. The fallback persona comes from the prompt loader (see `agent/prompts.py`).

```
SoulManager.load_soul(channel_id: str) -> Optional[str]
    - Read the channel soul file (data/<channel_id>/SOUL.md); None if it does not exist

SoulManager.get_soul_content(channel_id: str) -> str
    - Same as load_soul, falling back to prompts.current().text("soul")
      (config/prompts/custom/soul.md, else config/prompts/default/soul.md)

SoulManager.write_channel_soul(channel_id: str, content: str) -> bool
    - Write/overwrite the channel's soul file

SoulManager.soul_exists(channel_id: str) -> bool
```

Soul files are stored at `/data/<channel_id>/SOUL.md` (note capitalization). Hierarchy:
channel `SOUL.md` > `config/prompts/custom/soul.md` > `config/prompts/default/soul.md`.
The legacy `/workspace/SOUL.md` layer and `paths.workspace_dir` were removed.

#### `agent/prompts.py`
Configurable prompt loader. All model-facing prompt text and canned user replies live in
`config/prompts/{default,custom}` (see `config/prompts/README.md` for the user-facing guide);
no prompt text is hardcoded in code.

```
REGISTRY: tuple[PromptSpec, ...]   # one row per prompt: name, file, yaml key,
                                   # allowed {{placeholders}}, required literal tokens
EXTRA_YAML_FILES                   # whole-document yaml (tool-descriptions.yaml)
load_prompts(default_dir?, custom_dir?) -> PromptSet   # validates everything; raises PromptError(errors)
current() / set_current(ps)        # process-wide active set (lazy-loaded on first use)
PromptSet.text(name) / .render(name, **values) / .doc(filename)
```

- Loading: `.md` files - custom replaces default wholesale; `.yaml` - per-key/leaf merge, unknown
  custom key = error. Default files are the source of truth (missing = fatal); custom files and
  directory are optional (DEBUG log of each loaded file; stray unknown custom files DEBUG only).
- Rendering: `{{name}}` single pass over the template (values never re-scanned); unknown
  placeholder = load error; omitting a placeholder is fine.
- Validation collects all problems into one `PromptError`, including required control tokens
  (e.g. `__MUTE__`) and, for `tool-descriptions.yaml`, that every tool and parameter in
  `TOOL_DEFINITIONS` has an entry (`""` allowed). `TOOL_DEFINITIONS` holds only schemas;
  `ToolRegistry.register` takes no description.
- `build_system_prompt` (`llm.py`) is a single implementation shared by both LLM clients; the
  agent name comes from `{{agent_name}}` = `config.agent.name`.
- Adding a prompt: default file (or yaml key) + one `REGISTRY` row, then `text()`/`render()`.

**Reload flow** (`AgentLoop.reload`, `core.py`): triggered by `/reload` (exact-match message from a
PRIVILEGED sender in a 1:1 chat, handled in `_preprocess_incoming`) or SIGHUP (`main.py`).
Synchronous and all-or-nothing: `plan_config_reload` (`config.py`) re-reads `agent.yaml` and
validates, `load_prompts()` validates the prompt set; on any error the previous config and prompts
stay active. On success the hot-applied sections (`HOT_SECTIONS` = auth, chat, agent, consent, plus
`logging.level`) are applied, `AuthManager.update` is called and `prompts.set_current` swaps the set.
Other changed sections are reported as "restart required". Refused if `agent.yaml` is
missing/empty or `auth.privileged_numbers` would be empty.

#### `agent/tools/browser_client.py`
Direct Playwright client for browser automation (replaced the MCP protocol stack in 2026-07).

```
BrowserManager (module-level singleton)
    - Connects with the Python playwright package over websocket
      (chromium.connect) to the `browser` container
    - ws URL: BROWSER_WS_URL env var, falling back to config.browser.ws_url
      (agent.yaml `browser:` section), default ws://localhost:8931
    - One BrowserContext + one Page per browser *scope*; every method takes
      (channel_id, ..., scope=None), scope defaulting to channel_id
    - Chat scope = channel_id (persistent); background run scope =
      "<channel_id>#run:<turn_id>" (ephemeral, closed via close_scope())
    - Cookie/localStorage persistence per channel via Playwright storage_state,
      stored through tools/browser_cookies.py; loaded for every scope of the
      channel, saved back ONLY from the chat scope (scope == channel_id)
    - page_info(channel_id, scope) -> (url, title) of an already-open page
      (used to label browser_screenshot output)
    - Reconnects automatically if the browser container restarts
    - Text snapshots via the accessibility tree (aria_snapshot, innerText fallback,
      capped at ~15k chars)
    - JS execution strictly inside the page via page.evaluate — no Playwright API
      exposure to LLM-supplied code (the old browser_run_code_unsafe surface is gone)
    - get_page(channel_id) raises ValueError if channel_id is falsy, rather than
      silently creating a shared ""-keyed context
```

The pip package version (`requirements.txt`: `playwright==1.61.0`) and the browser
container's Playwright version MUST match exactly — Playwright requires client/server
version lockstep for `run-server`.

`BrowserManager` gives every browser scope its own isolated `BrowserContext`/`Page`,
and different scopes run concurrently at the Playwright/websocket level. Chat turns
on a channel share the channel scope; each cronjob/subagent run gets its own scope,
so a background run never drives the page a chat turn on the same channel is using.
See "Browser scopes" in the Concurrency Model section below.

---

### 3. browser Container

**Image**: `mcr.microsoft.com/playwright:v1.61.0-noble` (official; no custom build)

**Command**: `sh -c "node /mock_sites/server.js & exec npx -y playwright@1.61.0 run-server --port 8931 --host 0.0.0.0"`

**Purpose**: Headless Chromium, driven remotely by the agent's Python playwright client.

**Interfaces**:
- Playwright server websocket on port 8931 (published to the host; nixclaw reaches it
  as `ws://localhost:8931`)
- Mock-sites server on port 8932 (from `tests/fixtures/mock_sites/`): test websites
  reachable as `http://localhost:8932/...` from both the headless browser and the host
- `tests/fixtures/test_site/` is mounted read-only at `/test_site` for integration tests
  (`file:///test_site/index.html`)

**Responsibilities**:
- Render web pages via a real browser (Chromium)
- All protocol/tool logic lives in the agent (`browser_client.py`/`browse.py`);
  the container is a plain browser host

### 4. faster-whisper-server Container (optional)

**Image**: Built from `deploy/dgx-spark/faster-whisper/Dockerfile`
(nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04 base — runs on GPU or CPU)

**Purpose**: OpenAI-shaped speech-to-text HTTP endpoint backed by faster-whisper
(CTranslate2). Used by nixclaw to transcribe inbound Signal voice messages.

**Interface**:
- `POST /v1/audio/transcriptions` (multipart upload, returns `{"text": "..."}`)
- `GET  /v1/models` — OpenAI-compatible model list (one entry)
- `GET  /health` — liveness probe (returns device + compute_type)

**Endpoints**: Published on port 8001 (configurable via `FW_PORT`).

**Model**: Loaded once at startup from `WHISPER_MODEL_ID` (default
`Systran/faster-whisper-large-v3`) downloaded from HuggingFace into a
`model-cache` volume. For CPU-only hosts, override with
`Systran/faster-whisper-base` (≈150 MB; ~5–10 s per 20 s German clip on CPU).

**Wiring**: The nixclaw agent reads `stt.base_url` from config (env override
`STT_BASE_URL`) and POSTs each WAV chunk to `/v1/audio/transcriptions`.

See `deploy/dgx-spark/faster-whisper/README.md` for full deployment notes.

### 5. flux2-image-server Container (optional)

**Image**: Built from `deploy/dgx-spark/flux2-klein/Dockerfile`
(nvidia/pytorch:25.11-py3 base - ARM64 + CUDA 13 for the DGX Spark / GB10)

**Purpose**: OpenAI-shaped image-generation HTTP endpoint backed by
FLUX.2 [klein] 9B (distilled, 4 inference steps, native image editing).
Used by nixclaw's `generate_image` tool so users can ask for a picture -
or an edit of a photo they sent - over Signal.

**Interface**:
- `POST /v1/images/generations` (JSON: `prompt`, `size`, `n`,
  `response_format: b64_json`, optional `steps`/`seed`) -
  returns `{"data": [{"b64_json": ...}]}`
- `POST /v1/images/edits` (multipart: `image` file + prompt) -
  routes to klein's native editing path; same response shape
- `GET  /v1/models`, `GET  /health` (model id, quant mode, resident VRAM)

**Endpoints**: Published on port 8002 (configurable via `FLUX_PORT`).

**Wiring**: The nixclaw agent reads `image_gen.base_url` from config
(env override `IMAGEGEN_BASE_URL`) and POSTs to the endpoints above.
The GPU is shared with vLLM on the same Spark - the agent caps in-flight
generations with a module-level semaphore in `imagegen.py` (see the
Concurrency Model section).

See `deploy/dgx-spark/flux2-klein/README.md` for full deployment notes
(quantization modes, benchmark table, vLLM memory-split guidance).

### 6. kokoro-tts-german Container (optional)

**Image**: Built from `deploy/cpu/kokoro-tts-german/Dockerfile`
(`python:3.11-slim` base, CPU-only by default - an 82M ONNX model runs
comfortably on CPU, so this doesn't compete with vLLM/FLUX.2 for the DGX
Spark's GPU)

**Purpose**: OpenAI-shaped text-to-speech HTTP endpoint backed by
Kokoro-82M-ONNX-German-Martin (single voice, "martin", German only). Used by
nixclaw's `text_to_speech` tool to turn agent replies into a Signal voice-note
attachment.

**Interface**:
- `POST /v1/audio/speech` (JSON: `input`, `voice`, `language`,
  `response_format`, `speed`) - returns a raw binary audio body (not JSON),
  `Content-Type` per `response_format` (default `aac` - raw ADTS, matching
  real Signal voice notes)
- `GET  /v1/voices` - available voices / default voice / default language
- `GET  /health` - liveness + model id / voices / ORT provider / sample rate

**Endpoints**: Published on port 8003 (configurable via `TTS_PORT`).

**Wiring**: The nixclaw agent reads `tts.engines.<language>.base_url` from
config (env override `TTS_BASE_URL`, applies to the `default_language` engine
only) and POSTs to `/v1/audio/speech`. Unlike the image-gen/vLLM GPU cap,
concurrency here is capped per `base_url` (not one process-wide semaphore) in
`tts.py`, since multiple language engines can each point at a different
server - see the Concurrency Model section.

See `deploy/cpu/kokoro-tts-german/README.md` for full deployment notes (model
recon, CPU vs GPU, known limitations).

---

### 7. webui Container + web interface backend (`agent/web/`)

A browser client running alongside Signal: chat in every channel the user is
in (synced both ways with Signal), plus a file browser and text editor for the
user's 1:1 workspace. The backend stays thin and the client does the work
(vanilla JS ES modules in `webui/`, no framework, bundler or CDN).

**Containers and ports**
- **webui** (`webui/docker/`: `caddy:2-alpine` + `yq-go` + `openssl`,
  `network_mode: host`): serves `webui/` read-only on HTTPS `:8443` (and plain
  HTTP `:8090` when `http.enabled`, off in the `.example`), and reverse-proxies `/api/*` to `api_upstream` (default
  `127.0.0.1:3003`) with `flush_interval -1` (no SSE buffering). The
  `entrypoint.sh` renders the Caddyfile from `config/webinterface.yaml`, or the
  tracked `.example` when that file is missing. When HTTPS is on and
  `cert_file`/`key_file` are missing, it generates a self-signed cert into
  `config/ssl/` (the only writable mount; SANs from
  `https.self_signed.hostnames`). The request body limit is `max_upload_mb`
  + 2 MiB. App pages get a strict CSP (`default-src 'self'`, `blob:`/`data:`
  for media), `nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`.
- **nixclaw** web API (`web/api.py`, aiohttp): started by `main.py` only when
  `webui.enabled`, on `webui.api_host:api_port` (default `127.0.0.1:3003`;
  nixclaw is `network_mode: host` too). If it fails to start, the agent keeps
  serving Signal without it.

**Modules** (`src/nixclaw_agent/web/`): `api.py` (routes, auth middleware,
SSE, multipart uploads), `auth.py` (`WebAuth`: login codes, endpoints,
`/status`/`/deauth`), `bus.py` (`ChatBus`: `chat_log` writer + per-user SSE
fan-out), `channels.py` (`MembershipCache`, `encode_cid`/`decode_cid`),
`files.py` (workspace sandbox, atomic writes, ETags).

**Outbound listener (`signal.py`).** The only hook on agent output is a
module-level `signal.set_outbound_listener(fn)`. Every `SignalClient`
instance reports through it, both `main.py`'s client and
`tools/signal_ops.py`'s module-level `SIGNAL_CLIENT` (which sends all tool
attachments), so the roughly 30 send call sites in core/consent/tools stay
untouched. After a 2xx, `send_message` (message or edit), `send_attachment`,
`create_poll`, `delete_message` and `send_typing_indicator` call it with
`(kind, payload)`. Each takes `publish: bool = True`; the 🌐 mirror copies and
login codes pass `publish=False`. The listener (`ChatBus.on_outbound`) is
sync and never raises into the send path. Called off the loop thread (for
example from `spawn_subagent_task`), it hops onto the loop with
`call_soon_threadsafe`.

**History: the `chat_log` table** (`config.database.db_path`, WAL; `web/bus.py`
is its only writer). The web history deliberately does not use `messages`,
whose ids collide, rows get replaced or deleted on reset, and agent rows differ
from what Signal showed. Columns: `id`, `rev`, `channel_id`, `sender`,
`sender_name`, `origin` (`signal`|`web`|`agent`), `text`, `attachments`
(JSON list incl. `transcript`), `poll` (JSON), `signal_ts`, `edited`,
`deleted`, `created_at` (ms). `rev` is a global counter bumped on every insert
or update and serves as the SSE event id. Edits, remote deletes (a deleted poll
is also marked `closed`), saved attachment names and voice transcripts update
the row found by `(channel_id, signal_ts)` or by id. Inbound rows are written
once at entry: `_on_signal_message` before dispatch, so stop/reset, `/reload`
and duplicates also appear, and `submit_web_message` for web input. Our own
number's events (linked-device echoes) are skipped. Typing events are live-only
and never stored. Transcripts are stored in the row (the `.transcript.md`
sidecar is swept), so a voice message keeps its transcript after the audio
expires: the attachment then serves 404, history reports `exists: false`, and
the client hides the play button. History before launch is not shown, and
there is no retention for now.

**SSE (`GET /api/events`).** Events: `message` (insert or update, client
upserts by id, `id: <rev>`), `typing`, `channels` (membership changed, so
refetch), `deauthed` (endpoint revoked, go to login), plus a `: ping`
heartbeat every 25 s. A fresh stream first sends a bare `id: <max rev>` line
so the browser's auto-reconnect resumes from there. A reconnect with
`Last-Event-ID` (or `?last_event_id=`) replays
`rev > Last-Event-ID` for the user's channels from the table in pages; there
is no in-memory ring buffer and no `reset` event. A subscriber that falls more
than 1000 items behind is closed and replays on reconnect.

**Sync rules**
- Web → agent: `POST /api/channels/{cid}/messages` (multipart `text`,
  `client_id`, `files`) saves files straight into
  `data/<channel>/attachments/`, then `AgentLoop.submit_web_message` builds an
  `Event(origin="web", attachments with local_path set)`, publishes the row
  and calls `_dispatch_or_queue`. From there the normal path applies: dedup
  (`message_id = web:<chat_log id>`), group mention rules, tiers, inbound
  queue, STT. `_download_attachments` skips attachments that already have a
  `local_path`. `client_id` makes retries idempotent: a bounded per-user set
  returns the existing row.
- 🌐 mirror: after the agent dispatch, a background task
  (`_send_web_mirror`, serialized per channel) posts the copy to Signal from
  the bot account with `publish=False`. 1:1: `🌐 <text>`; group:
  `🌐 <profile name or number>: <text>` (prompts `web-mirror-direct`/`-group`).
  Attachments are forwarded with the caption on the first one. Failures are
  only logged.
- 📱: Signal-originated rows have `origin="signal"`; the client renders the 📱
  marker, and the text is stored without a prefix. Profile names come from
  `envelope.sourceName` (`Event.source_name`, bounded number→name cache).
- Agent output (texts, single-message-mode edits, attachments, polls, remote
  deletes, typing) reaches the web via the outbound listener as
  `origin="agent"` rows with `sender_name = agent.name`.
- Unmentioned group messages with attachments: their attachments are still
  downloaded for web display (no STT, no LLM).
- Consent polls are votable on the web: `POST .../polls/{ts}/vote` →
  `PollVote(voter=<number>)` → `ConsentManager.on_vote`, with the same tier
  rules as Signal votes.
- Out of scope: reactions, quote-replies, stickers, read receipts on the web.

**Auth**
- `POST /api/auth/request {number}`: a KNOWN/PRIVILEGED number gets a 6-digit
  code in its 1:1 Signal chat (prompt `web-auth-code`, `publish=False`, so it is
  never in `chat_log`/`messages`, and it is sent in the background). The answer
  is identical for every number (no enumeration); only 429s are visible, from
  `code_requests_per_number_per_hour` / `code_requests_per_ip_per_hour`.
- `POST /api/auth/verify {number, code, endpoint_name}`: codes are in memory
  only, single use, at most 5 attempts, TTL `code_ttl_seconds`. The endpoint
  name (1-64 chars, for example "Brian's Laptop") is required. Success creates a
  `web_endpoints` row (`id, number, name, token_hash, created_at, last_used_at,
  expires_at`; only the sha256 of `secrets.token_urlsafe(32)` is stored) and
  sets the cookie `nixclaw_session` (HttpOnly, SameSite=Strict, Path=/; `Secure`
  only on HTTPS; max-age `token_expiry_days`, 0 = never). `POST /api/auth/logout`
  deletes the current endpoint.
- Every request re-resolves the number's tier through `AuthManager`
  (hot-reload aware). UNKNOWN or expired returns 401, and the row is kept. A
  `/reload` that drops a number closes its SSE streams. `last_used_at` is
  written at most once a minute.
- CSRF belt: every POST/PUT/DELETE needs the header `X-NixClaw: 1` (403
  otherwise), on top of SameSite=Strict.
- `X-Forwarded-For` (first hop, used for the IP rate limit) and
  `X-Forwarded-Proto` (decides `Secure`) are trusted only when the TCP peer is
  loopback, which means Caddy. Caddy overwrites client-supplied values.
- Errors are `{"error": "..."}` with 401/403/404/409/413/429.

**`/status` and `/deauth <index|name>`**
- Signal: KNOWN+ in a 1:1 chat. They are handled in `_preprocess_incoming`
  right after `/reload`, before stop/reset and the LLM, by
  `WebAuth.handle_command`. `/status` lists endpoints (index, name, created,
  last used, expiry, open SSE connections). `/deauth` takes the index or the
  case-insensitive name; an ambiguous name is refused. In a group the bot
  answers only when mentioned, with a "use 1:1" hint (`web-command-group-hint`).
  Reply texts are `web-status-*`/`web-deauth-*` in `user-messages.yaml`.
- Web: there is no command endpoint. The composer maps `/status` →
  `GET /api/endpoints` and `/deauth x` → `DELETE /api/endpoints/{id}` (index
  or name resolved client-side), and shows the result as an ephemeral local
  line: not mirrored, not sent to the LLM.
- Revoking an endpoint deletes the row and closes its SSE streams with
  `deauthed`.

**Files sandbox** (`/api/files/*`, `web/files.py`). The root is the user's 1:1
workspace `data/<number>` (`tools/paths.get_context_dir`, the same as the file
tools). Group workspaces are not exposed. Paths are relative only: absolute
paths, `..`, NUL and backslash are rejected, and `is_within_directory` covers
symlinks. A path under `webui.hidden_paths` (default `browser`, `memories`) is
hidden from listings and returns 403, also when reached through a symlink. The
root itself cannot be deleted. Writes are atomic (tmp + rename).
`GET /api/files/raw` sends `ETag: "{mtime_ns:x}-{size:x}"`. `PUT` saves UTF-8
text up to `editor_max_kb`. It returns 409 on an `If-Match` mismatch. With
`If-None-Match: *` it only creates the file and returns 409 if the file
exists. Uploads stream to disk, create missing folders and keep basenames
only (dot-leading or empty names are rejected); collisions get a suffix.
Content types lose `;codecs=…`, so a MediaRecorder `audio/webm` becomes
`.webm`, which STT accepts. File and attachment responses carry `nosniff` and
`Content-Security-Policy: sandbox`. Images, audio and video are served
inline; everything else is served as `Content-Disposition: attachment`.
`GET/PUT /api/settings` stores an opaque client-owned JSON object (theme,
categories, order; at most 64 KB) as `data/<number>/webinterface.json`. It is
never used for authorization.

**Channels and the membership cache** (`web/channels.py`). `{cid}` in URLs is
base64url(channel_id) without padding, because group ids contain `/+=`. A user
may open their own 1:1 channel (channel_id = their number) and every
`signal-group:<internal_id>` whose `members` (from signal-api
`GET /v1/groups/{bot_number}`) contain their number. Member entries that are
not numbers never match. The cache is in memory. It is refreshed at startup
(in the background), on `POST /api/channels/refresh`, and on demand when older
than 10 min (60 s back-off after a failure, which keeps the old list). With
`signal.mock_api_url` set, the mock's groups are merged in, restricted to
`+0000*` members. A change pushes an SSE `channels` event to the affected
numbers.

**Config**
- `agent.yaml` `webui:`: `enabled` (default false), `api_host`, `api_port`,
  `code_ttl_seconds` (300), `token_expiry_days` (90, 0 = never),
  `max_upload_mb` (50; `send_attachment` base64s whole files into JSON),
  `editor_max_kb` (1024), `hidden_paths`, `text_extensions` (files that get the
  📝 editor button), `code_requests_per_number_per_hour` (5),
  `code_requests_per_ip_per_hour` (20). The section is validated at load.
  `/reload` hot-applies it except `enabled`/`api_host`/`api_port` (restart
  required).
- `config/webinterface.yaml` (git-ignored; the `.example` is tracked):
  `http.{enabled,port}`, `https.{enabled,port,cert_file,key_file,self_signed.{hostnames,days}}`,
  `api_upstream`, `max_upload_mb` (must be ≥ `webui.max_upload_mb`).
  `config/ssl/` is git-ignored. Both files are hard-blocked by the pre-commit hook.

Tests: `tests/test_webui_{auth,api,core}.py` (helpers in `tests/webui_support.py`).

### 8. Voice calls (experimental, `agent/voice/`)

> **Experimental.** Off by default (`voice_calls.enabled: false`). It has only
> been tested with 1:1 calls to a bot account that is the primary Signal device,
> and its tuning (VAD timings, filler words, goodbye detection) is geared to
> German. Expect rough edges.

PRIVILEGED and KNOWN users can call the bot on Signal and talk to the agent.
Only 1:1 calls are supported, and only one call at a time (further callers are
rejected).

**Plumbing (signal-api container, `Dockerfile.signal-api`, `docker/signal-api/`):**
signal-cli ≥ 0.14.2 has a call API (`startCall`/`acceptCall`/`hangupCall`/
`rejectCall`/`listCalls`, `callEvent` notifications). Media goes through
`signal-call-tunnel` (Rust, wraps Signal's RingRTC; built from a pinned commit
with WebRTC audio processing disabled), which signal-cli spawns once per call.
Audio is exchanged over a headless PulseAudio: per call the tunnel creates null
sinks; nixclaw records the remote side from `<outputDeviceName>.monitor` and
plays into `sink_for_<inputDeviceName>`. The REST wrapper is unchanged; a socat
forward exposes the daemon's JSON-RPC socket on `127.0.0.1:6002`, PulseAudio
listens on `127.0.0.1:4713` (both host-loopback only). `docker/signal-api/README.md`
has the manual test driver (`call_debug.py`) and the source review of the tunnel.

**Agent side (`src/nixclaw_agent/voice/`, started in `main.py` when enabled):**

| Module | Role |
|---|---|
| `rpc.py` | asyncio JSON-RPC client for the call API; subscribes to call events, reconnects with backoff |
| `manager.py` | `CallManager` (tier check against `voice_calls.allowed_tiers`, accept/reject, `max_call_minutes`, cleanup) and `CallSession` (one connected call) |
| `audio.py` | PulseAudio capture/playback; sweeps the per-call modules the tunnel leaves loaded |
| `vad.py` | webrtcvad + a pure `UtteranceSegmenter` (frames in, utterance events out) |
| `stt_stream.py` | optional streaming STT over WebSocket (`voice_calls.stt_streaming`), falls back to the HTTP STT endpoint |
| `speech.py` | text → sentences → TTS → playback, synthesis runs ahead of playback; `interrupt()` for barge-in |
| `text.py` | sentence splitting, filler-word filter, goodbye detection |

**Flow per call:** incoming call → tier check → `acceptCall` → greeting
(`voice_call_greeting`) → capture → VAD → utterance → STT (the same `stt.base_url`
as voice messages; with `noise_gate` an extra auto-language pass drops noise) →
`AgentLoop.submit_voice_message` → normal interactive turn in the caller's 1:1
channel (`origin="voice"`, dedup key `voice:<id>`, the system prompt gets the
`voice-call` instruction to answer in one or two short spoken sentences) → while
the call is active, every text the turn sends is also queued for speech (TTS
engine of `tts.default_language`, the channel's TTS voice, optional
`voice_calls.tts_sampling`). The caller's words are mirrored to the Signal chat
(`🎙️ <text>`) and to the web interface. After a pause, an utterance that sounds
unfinished waits `unfinished_wait_ms`/`unclear_wait_ms` for a continuation.
Caller speech of `barge_in_ms` interrupts the bot's speech.

**Ending a call:** the caller hangs up, `idle_hangup_seconds` without speech,
`max_call_minutes`, or the `end_call` tool. `end_call` is offered to the model
only during an active call (`VOICE_CALL_ONLY_TOOLS` in `core.py`). It is refused
unless the caller's last words were a goodbye, and the call ends only after the
reply has been spoken.

**Config (`voice_calls:`, restart required, defaults in `VoiceCallsConfig`):**
`enabled`, `mode` (`agent` | `parrot` - parrot speaks each transcript back
without the agent, for testing the audio pipeline), `allowed_tiers`
(`[PRIVILEGED, KNOWN]`), `jsonrpc_host`/`jsonrpc_port`, `pulse_server`,
`vad_aggressiveness`, `start_speech_ms`, `end_silence_ms`, `min_speech_ms`,
`max_utterance_seconds`, `max_call_minutes`, `barge_in_ms`, `idle_hangup_seconds`,
`noise_gate`, `stt_streaming`, `stt_flush_ms`, `unfinished_wait_ms`,
`unclear_wait_ms`, `tts_sampling`. Requires `stt` and `tts` to be configured.

Known limitations: see `doc/ISSUES.md`. Tests: `tests/test_voice_calls.py`.

---

## Core Data Types

```python
@dataclass
class Attachment:
    id: str
    content_type: str
    filename: str | None
    local_path: str | None = None
    size: int = 0

@dataclass
class Event:
    source: str              # Phone number of sender
    destination: str         # Recipient phone number or group ID
    message: str             # Message text (mention artifacts stripped before LLM)
    timestamp: int           # Unix timestamp in milliseconds
    group_id: str | None     # None for 1:1 chats
    attachments: list[Attachment] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)  # Phone numbers mentioned in the message

@dataclass
class Message:
    event: Event
    is_outgoing: bool
    processed: bool
    tool_calls: list["ToolCall"] | None = None
    tool_results: list["ToolResult"] | None = None
    source_type: str | None = None   # e.g. "cronjob"/"subagent" for background-task messages
    source_id: str | None = None
    is_discarded: bool = False       # set when __DISCARD_OUTPUT__ suppressed the response

@dataclass
class Context:
    channel_id: str          # Phone number or group UID
    messages_db: Path        # Path to SQLite database
    attachments_dir: Path    # Path to attachments folder
    soul_path: Path | None = None    # None if channel has no custom SOUL.md
    memory_dir: Path | None = None   # ChromaDB persist dir for this channel

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON schema for arguments

@dataclass
class ToolResult:
    name: str
    success: bool
    output: str | None
    error: str | None

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

@dataclass
class PreprocessedMessage:      # result of AgentLoop._preprocess_incoming
    event: Event | None         # None = fully handled (duplicate, loop-suppressed, unmentioned)
    reply_destination: str
    abort_loop: bool = False    # a stop/reset shortcut replied; kill this channel's running turn

enum UserTier:
    PRIVILEGED
    KNOWN
    UNKNOWN
```

---

## APIs Between Components

### Signal-cli → nixclaw-agent

**Mechanism**: Webhook POST to nixclaw-agent

**Security**: `nixclaw` runs with `network_mode: host`, so the webhook listener on port 3002 is
reachable from anything that can reach the host — there is no Docker network isolation here.
Anyone who can post to it can fake a message from any number (incl. a privileged one), so
it is gated by a shared secret: `signal.webhook_secret` (env `SIGNAL_WEBHOOK_SECRET`, set in
`.env`; docker-compose refuses to start without it). `_webhook_handler` accepts it as
`?token=` (signal-cli-rest-api can only be given a URL) or `X-Signal-Hook-Secret` header,
compared in constant time; a mismatch gets 401. Without a secret the listener binds to
127.0.0.1 only (local runs outside Docker). The webhook server has no access log, since the
token is part of the URL.

```
POST /webhook/signal?token=<configured_secret>
Content-Type: application/json

{
  "source": "+491234567",
  "destination": "+498765432",
  "message": "Hello agent",
  "timestamp": 1699900000000,
  "group_id": null,
  "attachments": ["uuid-1", "uuid-2"]
}
```

### nixclaw-agent → signal-cli

**Mechanism**: REST API via HTTP (port 8080)

**Base URL**: `http://localhost:8080`

**Send Message:**
```
POST /v2/send
Content-Type: application/json

{
  "message": "Response from agent",
  "number": "+491234567890",
  "recipients": ["+491234567"]
}
```

**Typing Indicator:**
```
PUT /v1/typing-indicator/{number}
```

**Polls** (approval polls, see "User consent"):
```
POST /v1/polls/{number}
{"recipient": "<number or group.<base64>>", "question": "...", "answers": ["Yes", "No"], "allow_multiple_selections": false}
-> {"timestamp": "<poll ms timestamp>"}

DELETE /v1/remote-delete/{number}
{"recipient": "<number or group.<base64>>", "timestamp": <poll ms timestamp>}
```

**Read Receipt** (end of turn, see "Read receipts"; 204 No Content):
```
POST /v1/receipts/{number}
{"receipt_type": "read", "recipient": "+491234567", "timestamp": <original ms timestamp>}
```

**Health Check:**
```
GET /v1/health
```

### nixclaw-agent → ollama / vLLM

**Mechanism**: `LLMClient` (python-ollama library) or `VLLMClient` (httpx against the
vLLM OpenAI-compatible `/v1/chat/completions` endpoint), selected by `config.backend`.
`_build_history_messages()` in `llm.py` turns stored history into one chat message per turn
(not a single concatenated string) — one `role: "user"/"assistant"` entry per stored message,
the last `chat.min_messages_to_keep` of them, plus the current event appended last:

```python
messages = [
    {"role": "system", "content": "<system_prompt built from tools/soul/memory/skills>"},
    {"role": "user", "content": "<message 1>"},
    {"role": "assistant", "content": "<message 2>"},
    ...
    {"role": "user", "content": "<current event.message>"},
]
llm_client.chat(messages, tools=[<tool_definitions>], channel_id=channel_id)
```

### nixclaw-agent → browser

**Mechanism**: Playwright's own websocket protocol via the Python `playwright` package
(see `BrowserManager` above) — no MCP layer.

**URL**: `ws://localhost:8931` (from `BROWSER_WS_URL` env var, falling back to
`config.browser.ws_url`; nixclaw is on the host network, the browser service's port is
published to the host)

**Example** (Python, inside `browser_client.py`):
```python
browser = await pw.chromium.connect("ws://localhost:8931")
context = await browser.new_context(storage_state=state)  # per channel
page = await context.new_page()
await page.goto("https://example.com", wait_until="domcontentloaded")
snapshot = await page.locator("body").aria_snapshot()
```

**Agent Tools**: `web_search`, `browser_navigate`, `browser_interact`,
`browser_screenshot`, `browser_evaluate` (defined in `tools/__init__.py`, implemented in
`tools/browse.py` on top of `BrowserManager`).

---

## Data Flow

### Message Processing Pipeline

```
1. Signal Message Received
         │
         ▼
2. Webhook POST to /webhook/signal
         │
         ├─── dataMessage.pollVote? ───► ConsentManager.on_vote (never a chat
         │                               event or DB row; see "User consent")
         │
         ▼
2b. Channel busy? (a turn is already active for this channel_id)
         │
         ├─── Yes ───► Queue in _channel_inbound_queue (cap 5) and store the
         │             message; the active turn drains it at its next
         │             tool-loop checkpoint - injected as a new user turn, or
         │             aborting that loop outright if it is a stop/reset
         │             request (see Concurrency Model)
         │
         ▼
3. Deduplication (check message_id in SQLite)
         │
         ▼
4. Store in messages.db (ALL messages regardless of mention)
         │
         ▼
5. Check mention status
   - 1:1 chats: always process
   - Group chats: only process if bot is mentioned
         │
         ├─── Not mentioned? ───► Skip LLM, return (message stored only)
         │
         ▼
6. Strip mention artifacts (U+FFFC, @Nixclaw pattern) from message
         │
         ▼
7. Load Context (by channel_id)
         │
         ▼
8. Build Prompt (template + last 15 msgs + summary + keywords + skill routing)
         │         └──────────────────────────────┘
         │                      │
         │                      ▼
         │         Load system & user skill manifests
         │         Generate SKILL LOADING section
         │
         ▼
9. LLM.chat() ───► Tool Calls (if any)
         │                  │
         │                  ▼
         │            Execute Tool
         │                  │
         │                  ▼
         │            Return Result
         │                  │
         ◄──────────────────┘
         │
         ▼
10. Send Response via signal-cli
         │
         ▼
11. Turn ends: read receipts for the messages it handled (chat.read_receipts)
```

### Cronjob Pipeline

```
1. CronScheduler.check_due() called each loop iteration
        │
        ▼
2. For each due job whose channel's background lane is free:
   CronScheduler.mark_running(job.id), then dispatch. A job whose lane is
   busy is skipped this tick and left pending/failed (not marked running).
        │
        ▼
3. Load associated context and user privileges
        │
        ▼
4. Process as normal message (prompt = cronjob prompt)
        │
        ▼
5. Send result as plain text to user's Signal channel
        │
        ▼
6. Mark cronjob complete (failed jobs marked for retry on next poll)
```

#### Terminology

- **CronJob**: a scheduled task belonging to one Signal channel. A row in the `cronjobs` table is one
  *schedule*, not one *execution*: a recurring schedule lives in one row across many firings; a one-time
  schedule occupies a row until it succeeds or fails out. (Avoid "task", "job", "reminder" - overloaded.)
- **Failure**: a run where the LLM call or any tool call raised an exception. A clean LLM response that
  fails to be delivered via Signal is *not* a failure.
- **FailureCount**: consecutive failures of one schedule. Increments on every Failure, resets to 0 on any
  successful run.
- **FailureThreshold**: the FailureCount that triggers terminal action (`cron.max_failure_count`).
- **FastRetry**: a `failed` row whose FailureCount is below the threshold is picked up again on the next
  scheduler iteration, bypassing the regular cadence.
- **RegularCycle**: the next run of a recurring job. On success `now + interval_minutes` snapped to the
  original grid; on a threshold-driven reschedule `scheduled_at + interval_minutes`, so wall-clock
  alignment is preserved across failure streaks.
- **CronJobNotification**: the alert sent to the channel when a one-time job reaches the threshold.

#### Three-Strike Resilience

Recurring cronjobs that raise exceptions while running are no longer stuck permanently in `status='failed'`.
A `failure_count` column on the `cronjobs` table tracks consecutive failures per schedule. The `max_failure_count`
knob in `CronConfig` (default 3) controls the threshold.

**FastRetry:** the dispatcher picks up `failed` rows on the next scheduler iteration (within `poll_interval_seconds`)
when `failure_count < max_failure_count`. The `ORDER BY (status = 'pending') DESC, scheduled_at` clause guarantees
that regular-cycle pending rows are processed before FastRetry pile-up within the same tick.

**Threshold semantics:**
- **Recurring** (failure count reaches threshold) → `status='pending'`, `failure_count=0`, `scheduled_at` advanced to the
  next grid slot via `compute_next_scheduled(anchor, interval, now)` — the same helper the success path uses, so
  wall-clock alignment with the original schedule is preserved across failure streaks.
- **One-time** (failure count reaches threshold) → row is deleted and a `CronJobNotification` is sent to the channel
  reading: "Cronjob scheduled for \<HH:MM\> failed \<threshold\> times".

The counter is reset to 0 on any successful completion. Signal-send failures do **not** count as cronjob failures.
The notification time is rendered in UTC (see `doc/ISSUES.md`).

---

## Concurrency Model

- Each event (Signal message or cron trigger) spawns an `asyncio.Task`
- Task limit: interactive (chat) turns are capped at 10 concurrent via a semaphore (`MAX_CONCURRENT_TASKS` in `core.py`); background-lane cron/subagent tasks are not subject to this cap
- Per `channel_id`, turn serialization is split into two independent **lanes**
  (see below): an **interactive** lane for chat, and a **background** lane
  shared by cronjobs and subagents. A busy background lane no longer blocks
  (or is blocked by) live chat on the same channel; different channels still
  run concurrently, up to the task limit above
- SQLite writes use WAL mode for concurrent reads, serialized writes
- Cron jobs run alongside message processing with no special ordering (beyond
  the background lane's own self-serialization, below)

**Per-channel turn serialization, two lanes** (`core.py`): a channel that is
already running a turn in a given lane must not get a second, racing task
started in *that same lane*. Before the original (single-slot) version of this
gate, a "stop" sent while the agent was grinding through a multi-iteration tool
loop merely spawned an *independent* task alongside the running loop - the loop
never saw it and kept working. A later revision (`fix/decouple-cron-chat-lanes`)
split the single slot into two, because a long-running cronjob or subagent
turn was blocking all live chat on its channel until it finished. Now:

- `self._channel_active_task: dict[str, asyncio.Task]` holds the one live
  **interactive** (chat) turn per channel. Only `_on_signal_message` →
  `_dispatch_or_queue` (via `_start_channel_task`) registers into this lane.
- `self._channel_background_task: dict[str, asyncio.Task]` holds the one live
  **background** turn per channel - a cronjob or a subagent, whichever started
  more recently; the two never run concurrently on the same channel.
  `self._channel_background_meta: dict[str, dict]` records
  `{"source_type": "cronjob"|"subagent", "source_id": <id>}` for whatever
  currently occupies the lane, set right before the task starts and cleared in
  its done-callback. Cronjob dispatch (in `run()`'s poll loop) and
  `spawn_subagent_task` both register here, via `_register_channel_task(...,
  lane="background", meta=...)`.
- A chat message arriving on a channel whose **interactive** lane is busy is
  appended to `self._channel_inbound_queue[channel_id]` instead (cap
  `MAX_QUEUED_INBOUND_PER_CHANNEL` = 5, oldest dropped when full; a consecutive
  identical message from the same sender is deduped) and stored in messages.db
  right away. The busy-check and the queue/dispatch decision contain no `await`
  between them - asyncio's cooperative scheduling is what makes them atomic.
  This queue/checkpoint mechanism now only ever matters for interactive-vs-
  interactive overlap (two chat messages arriving close together) - it is
  never consulted by, or on behalf of, the background lane.
- **Draining / steering, interactive lane only**: `process_message`'s
  tool-calling loop calls `_drain_inbound_checkpoint()` at the top of every
  iteration. Queued events run through the shared `_preprocess_incoming()` -
  dedup, loop detection, stop/reset keyword shortcuts, mention gating,
  attachment download + STT, persistence - so a queued message gets identical
  treatment to one processed immediately. The result is injected into the
  running loop's `messages` as a synthetic user turn (`[New message from user
  while you were working: ...]`), letting the model decide whether to stop,
  redirect, or continue. If preprocessing instead resolved to a stop/reset
  canned shortcut (which already sent its own reply), the checkpoint returns
  True and the loop exits at once; any turn-scoped resources are released on
  that path like any other early exit. `_process_cronjob` and `_process_subagent` do **not** call this
  checkpoint (it was removed as part of the lane split): a background job now
  runs to completion, or to explicit cancellation via `stop_running_activity`
  (below), undisturbed by chat arriving on the same channel mid-run - chat
  gets its own independent turn in the interactive lane instead of being
  spliced into the background job's tool loop.
- `_on_channel_task_done` is lane-scoped: it clears a lane's dict entry only if
  the finishing task still owns that lane's slot (a stale done-callback firing
  after a newer task already claimed it is a no-op), and only the interactive
  lane's completion re-dispatches anything that queued after the turn's last
  checkpoint drain. The two lanes' done-callbacks are otherwise fully
  independent - there is no cross-lane "takeover" case to guard against
  anymore.
- Cronjobs whose channel's **background** lane is busy are **skipped for that
  tick**, not queued: `mark_complete` is never called, so `next_run`/
  `failure_count` stay untouched and the next poll (60s) retries them
  naturally. They are never blocked by, and never skip because of, an active
  chat turn.
- `spawn_subagent_task` runs off the event-loop thread (the `spawn_subagent`
  tool handler is dispatched via `asyncio.to_thread`), so it creates and
  registers its task inside a `loop.call_soon_threadsafe` callback
  (`_create_and_register_on_loop`) and hands the `asyncio.Task` back over a
  `concurrent.futures.Future` (`SPAWN_SUBAGENT_REGISTER_TIMEOUT_SECONDS`, 10s).
  Unlike the pre-lanes design, it now registers into the **background** lane
  and *does* check busy-ness against it, raising `RuntimeError` if a cronjob or
  another subagent already occupies the lane. That check must run inside
  `_create_and_register_on_loop`, on the event-loop thread, immediately before
  `loop.create_task(...)` - not earlier, in the worker-thread body of
  `spawn_subagent_task` itself - otherwise two concurrent spawns for the same
  channel (e.g. a subagent spawn racing a cron tick) could both read the lane
  as free before either registers, silently overwriting each other's dict
  entries and defeating the lane's mutual exclusion.
- **Cancelling a background run**: `stop_running_activity` (tool, in
  `tools/cron.py`) → `AgentLoop.cancel_background_activity(channel_id,
  activity_id)` looks up `self._channel_background_task`/
  `_channel_background_meta` for the channel, validates `activity_id` against
  the recorded `source_id` (a mismatch or no running task raises
  `RuntimeError` with a clear message and cancels nothing), and schedules
  `task.cancel()` onto the event loop via `loop.call_soon_threadsafe` (same
  cross-thread rationale as `spawn_subagent_task` above). `_process_cronjob`/
  `_process_subagent` catch `asyncio.CancelledError` ahead of their general
  `except Exception` (it's a `BaseException` in Python 3.8+), treat it like a
  normal successful completion for scheduling purposes (cronjob:
  `mark_complete(..., success=True, ...)`, reusing the anchor-based reschedule
  math and resetting `failure_count`; subagent: no scheduler bookkeeping),
  send no message (the interactive-lane turn that called
  `stop_running_activity` already acknowledged the stop), and swallow the
  exception (not re-raise) so the task ends in a normal, not cancelled, state
  while its existing `finally` block (closing the run's browser scope, etc.) still runs.
  This is distinct from `cancel_cronjob`, which only flips a DB flag to stop
  *future* scheduling and has no effect on a run already in flight.
- The chat system prompt (`build_system_prompt`, `llm.py`) is passed a short
  status block when `self._channel_background_meta[channel_id]` is non-empty,
  naming the background activity's type and id (never its prompt text, which
  may contain credentials) so the foreground agent can call
  `stop_running_activity` if the user asks it to stop something running in the
  background.
- `self._messages_sent` is keyed per channel (it was a process-global scalar,
  which let concurrent channels corrupt each other's `MAX_MESSAGES_PER_SESSION`
  budget).
- `__MUTE__` behaviour is unchanged.
- A chat turn paused on an approval poll (`_pause_for_consent`, see "User consent") keeps its
  interactive-lane slot and its task-semaphore slot for up to `consent.timeout_minutes`; new chat
  messages queue as usual and are drained by the paused turn. Background runs never pause on a
  poll; an approved background re-run runs in its own task outside both lanes.
- Unit tests: `tests/test_channel_turn_serialization.py`.

**Browser scopes** (`types.py` `Context.lane`/`Context.browser_scope`,
`tools/browser_client.py`, `tools/browse.py`): every turn's `Context` carries a
`lane` - `"interactive"` for chat (`process_message`) or `"background"` for
cronjob/subagent runs (`ContextManager.get_context(channel_id, lane="background")`
in `_process_cronjob`/`_process_subagent`). `Context.browser_scope` maps it to a
`BrowserManager` key:
- Chat: `channel_id`. All chat turns of a channel share one persistent
  context/page (users in one channel may see each other's browsing - they see
  the channel's messages anyway); cookies are saved back to the channel's
  `storage_state.json`.
- Background: `"<channel_id>#run:<turn_id>"`, unique per run. Seeded from the
  channel's `storage_state.json` read-only - never written back - and closed in
  the run's `finally` (`AgentLoop._close_run_browser_scope` ->
  `browse.close_browser_scope`, which also drops the scope's lock; errors are
  logged, never raised). A subagent always gets a fresh background `Context` in
  `_process_subagent`; it never inherits the spawning chat turn's scope.
- Different channels never share a context.
- `browse.py` resolves the scope with `registry.get_current_browser_scope()`
  (falls back to the channel scope when no turn Context is set, e.g. direct
  calls in tests) and serializes calls per scope with an `asyncio.Lock`
  (`_get_lock(scope)`); different scopes run concurrently. A retriable
  connection error closes only the failing scope before falling back to a full
  reconnect.
- `browser_screenshot` returns the filename on its first line followed by
  `URL:` and `Title:` of the captured page, so the model can notice it
  captured the wrong page.
- History: this replaced a process-global, turn-scoped `BrowserLease` whose
  timer-based eviction (idle 180s / hard cap 600s) let a weather cronjob take
  over the page of a ~1h chat turn on the same channel; the chat turn then
  screenshotted the cronjob's page. `browser.lease_wait_timeout_seconds` is
  still accepted in agent.yaml for compatibility but unused.
- Unit tests: `tests/test_browser_scope_isolation.py`.

**vLLM concurrency cap** (`llm.py`): live benchmarking found the shared vLLM backend
degrades badly (50-75% request stall rate) once concurrency reaches ≥4 for large-context
requests, so `VLLMClient.chat()` requests a slot from a module-level `asyncio.Semaphore`
before entering its stream-stall retry loop, and releases it once the loop finishes (success
or exhausted retries):
- The semaphore is module-level, not a `VLLMClient` instance attribute, so it is shared
  across every `VLLMClient` in the process - there are two instantiation sites (the
  long-lived client built once in `main.py`, and a fresh one created per call inside
  `tools/files.py`'s `analyze_image`), and a per-instance semaphore would let the second
  site bypass the cap entirely.
- Sized by `vllm.options.max_concurrent_requests` (default 2, benchmark-derived for the
  current model/hardware). Later `VLLMClient` instantiations with a different value don't
  resize the already-created semaphore (unsafe at runtime) - they just log a warning.
- A request waits FIFO for a free slot up to `vllm.options.max_concurrent_wait_seconds`
  (default 240s) before raising
  `VLLMConcurrencyLimitError`.
- It is a plain counting semaphore with no reentrancy or eviction: a vLLM request isn't
  held across multiple tool calls, so there's no "turn" to scope it to.
- Unit tests: `tests/test_vllm_streaming.py` (cap shared across separate client instances,
  wait-timeout raises `VLLMConcurrencyLimitError`).

**Image generation concurrency cap** (`imagegen.py`): the FLUX.2 server shares the
DGX Spark's GPU with vLLM, so concurrent diffusion runs would thrash both. The same
pattern as the vLLM cap above is used: a module-level `asyncio.Semaphore` sized by
`image_gen.max_concurrent_requests` (default 1) is acquired inside
`FluxImageClient.generate()` before the HTTP call and released in a `finally`; a
request queues up to `image_gen.max_concurrent_wait_seconds` (default 240s) before
raising `ImageGenConcurrencyLimitError`. Because the wait happens *inside* the
`generate_image` tool handler, the outer per-tool timeout in `tools/registry.py`
for that tool is `max_concurrent_wait_seconds + image_gen.timeout ×
(steps/default_steps) + 30s` - a flat value would truncate the queue wait. `steps` is
the optional tool argument (defaults to `image_gen.default_steps`, clamped to
`image_gen.max_steps`, default 40); each sampling pass costs roughly linear
GPU time, so the generation budget scales with the steps ratio. For multi-image
edits (multiple entries in
`input_images`, capped at `image_gen.max_input_images`, default 8) an extra
`0.5 × image_gen.timeout × (steps/default_steps)` is added per additional input
image, since each reference image adds VAE-encode/attention work on the
server. Unit tests:
`tests/test_imagegen.py`, `tests/test_imagegen_tool.py`.

**Text-to-speech concurrency cap** (`tts.py`): same semaphore-around-the-HTTP-call
pattern as the image-gen cap above, with one deliberate difference: TTS can have
*multiple* engines configured (one per language, each with its own `base_url` -
`TTSConfig.engines`), so `_get_shared_tts_semaphore()` keys its module-level dict
by `base_url` rather than using a single global semaphore. Engines that happen to
share a `base_url` (one multilingual server) correctly share its slots; engines on
different `base_url`s each get an independent cap sized by their own
`max_concurrent_requests` (default 2). A request queues up to that engine's
`max_concurrent_wait_seconds` (default 180s) before raising
`TTSConcurrencyLimitError`. Because `text_to_speech` may be configured with more
than one engine, the outer per-tool timeout in `tools/registry.py` takes the max
of `max_concurrent_wait_seconds + timeout` across every configured engine, plus
30s (falling back to `TOOL_TIMEOUT_SECONDS` if no engines are configured, to avoid
`max()` on an empty sequence). Unit tests: `tests/test_tts.py`,
`tests/test_tts_tool.py`.

**vLLM image auto-shrink cache** (`llm.py`): the served model's actual image-size limit is
not discoverable ahead of time - vLLM's `/v1/models` (`ModelCard`) exposes only `id` and
`max_model_len`, nothing about the multimodal processor's constraints - and it isn't
config-facing in `agent.yaml` either, so it has to be learned empirically against the real
backend and re-learned whenever the served model changes:
- Images are always sent at native resolution on the first attempt. If the backend's
  processor rejects one outright, the 400 response body is matched by
  `_IMAGE_PROCESSOR_ERROR_RE` against the class of error vLLM raises for this (observed
  verbatim: `"Failed to apply Qwen3VLProcessor on data={'text': ..., 'images': [<PIL.Image...>]}
  ..."`, e.g. after a model swap made a previously-fine image resolution unsupported), which
  raises `ImageProcessingError`.
- `VLLMClient.chat()` catches this and retries with the image(s) downscaled to the first step
  of `IMAGE_SHRINK_LADDER_PIXELS` (`[2048, 1536, 1024, 768, 512]`, longest edge in pixels),
  re-deriving from the *original* base64 data each attempt (never re-shrinking an
  already-shrunk copy, so quality loss doesn't compound across retries). No backoff wait
  between shrink attempts - unlike a stream stall, this failure is deterministic for a given
  image/model pair, so retrying at the same size would just fail identically.
- On success, the working cap is written to the module-level `_vllm_image_max_dimension_cache`
  dict, keyed by model id - not a `VLLMClient` instance attribute, for the same reason as the
  concurrency semaphore above (the `analyze_image` call site builds a fresh client every call,
  so an instance-level cache would never be reused). Later calls for the same model id start
  from the cached cap directly instead of re-paying the failed-native-attempt cost on every
  image.
- Keying by model id is also the invalidation strategy: swapping the served model changes the
  cache key, so a cap learned for one model is never (mis)applied to another - the new model
  just gets its own from-scratch discovery on its first oversized image.
- If every rung of the ladder still gets rejected (or the request had no images to shrink in
  the first place), `chat()` gives up and raises `ImageProcessingError`, which propagates like
  any other LLM error (surfaced to the user as "LLM temporarily unavailable" - see
  `doc/DEBUGGING.md`'s "Oversized/Rejected Images" section for how to recognize this in the
  debug log versus other causes of that message).
- Unit tests: `tests/test_vllm_streaming.py` (shrink-then-succeed-then-cache, cache reuse
  skips the native attempt, ladder exhaustion raises `ImageProcessingError`, no-images
  defensive case).

**Rate Limits Per Tier**:
| Tier | Messages/min |
|------|--------------|
| PRIVILEGED | 10 |
| KNOWN | 5 |
| UNKNOWN | 2 |

**Loop Detection**: If the same message appears 3 times within 60 seconds, the sender is blocked.

**Max Tool Iterations Per Message**:
| Tier | Max Iterations |
|------|---------------|
| PRIVILEGED | 100 |
| KNOWN | 30 |
| UNKNOWN | 20 |

---

## Control Markers

Control markers are **LLM output tokens** - the LLM embeds them in its responses based on system prompt instructions when specific conditions are met. The agent parses these from LLM output before sending.

| Marker | LLM Trigger Condition | Effect |
|--------|----------------------|--------|
| `__MUTE__` | User asks LLM to be quiet/silent | Mute notifications for this channel |
| `__UNMUTE__` | User asks LLM to resume talking | Unmute notifications for this channel |
| `__DISCARD_OUTPUT__` | LLM decides response should not be sent | Discard the response without sending |
| `__ISSUE_DETECTED__` | LLM encounters error or unexpected behavior | Trigger immediate debug log flush |
| `__AGENT_CONTINUE__` | User request needs multiple steps (e.g., "find X then summarize") | Request internal continuation turn (max 10 per channel; no tier restriction — `MAX_CONTINUATION_TURNS` in `core.py`) |

**User-Triggered Keywords** (detected in user input, processed by agent):

| Keyword | Effect |
|---------|--------|
| `stop`, `halt`, `cancel`, `abort`, `enough`, `cease` (prefix match) | Abort the current turn: canned "Operation halted" reply, no LLM call |
| `reset context`, `reset your context`, `forget everything`, `start fresh`, `clear context`, `clear my context`, `new conversation`, `forget prior`, `forget previous`, `don't remember`, `stop remembering` | Reset conversation context |

**Script -> harness markers** (NOT LLM output: complete stderr lines printed by a bash script,
matched only with the channel's `NIXCLAW_CHANNEL_ID` crc; see "User consent"):

| Marker | Effect |
|--------|--------|
| `NIXCLAW_REQUIRE_CONSENT_QUESTION_<crc>="<question>"` | Kill the command, post its output + a Yes/No poll; run again with `NIXCLAW_CONSENT=1` after approval |
| `NIXCLAW_CONSENT_OK_<crc>` | The approved action succeeded (background lane: post output instead of scheduling an inspection cronjob) |

---

## Error Handling

| Failure | Behavior |
|---------|----------|
| LLM unavailable | Log error, send "LLM temporarily unavailable" to user |
| Signal send failure | Log error, retry once after 5s, give up and log |
| Tool timeout | Return error result to LLM, LLM handles gracefully |
| Database error | Log error, abort processing for that event |
| Invalid webhook | Return 400, log details |

---

## Project Structure

```
nixclaw/
├── docker-compose.yml
├── config/
│   ├── agent.yaml          # Agent configuration
│   ├── webinterface.yaml   # webui container (git-ignored; .example tracked)
│   ├── ssl/                # webui HTTPS cert/key (git-ignored; self-signed generated if missing)
│   ├── prompts/            # default/ (tracked) + custom/ (git-ignored overrides); see config/prompts/README.md
│   └── skills.yaml         # System skills index
├── data/                   # bind-mounted at /data (config.paths.data_dir)
│   ├── messages.db         # single shared SQLite DB: messages, context_summaries,
│   │                       #   cronjobs tables (config.database.db_path, default /data/messages.db)
│   └── <channel_id>/       # Per-channel context data
│       ├── config/
│       │   └── skills.yaml # Channel-specific skills index
│       ├── skills/         # Channel-specific skill files
│       │   └── <skill_name>/
│       │       └── SKILL.md
│       ├── SOUL.md         # Channel persona (optional; falls back to config/prompts/{custom,default}/soul.md)
│       ├── memories/       # ChromaDB persist dir (per-channel vector memory)
│       ├── webinterface.json  # web interface settings (1:1 workspaces only)
│       └── attachments/
├── skills/                 # System skill definitions
│   ├── weather/
│   │   └── SKILL.md
│   ├── skill-creator/
│   │   └── SKILL.md
│   ├── print/
│   │   └── SKILL.md
│   └── ...
├── webui/                  # web interface frontend (vanilla JS) + docker/ (Caddy image)
└── src/
    └── nixclaw_agent/           # Python package (underscore)
        ├── __init__.py
        ├── main.py              # Entry point
        ├── core.py              # AgentLoop, Event processing
        ├── llm.py               # LLMClient, VLLMClient, prompt assembly (text from prompts.py)
        ├── config.py            # Configuration loading
        ├── context.py           # ContextManager
        ├── signal.py            # SignalClient (incl. polls, remote delete, pollVote webhook parsing)
        ├── consent.py           # ConsentManager: approval polls for opted-in bash scripts
        ├── prompts.py           # Prompt loader (config/prompts/{default,custom})
        ├── turn_output.py       # Single-message mode (edit the turn's last message)
        ├── tool_markup.py       # Leaked tool-call markup guard
        ├── sandbox.py           # Run untrusted-input parsers as nobody
        ├── path_safety.py       # Path traversal checks
        ├── stt.py               # faster-whisper STT client + attachment pipeline
        ├── imagegen.py          # FLUX.2 image generation client + shared GPU semaphore
        ├── tts.py               # Kokoro-82M TTS client + per-base_url shared semaphore
        ├── auth.py              # AuthManager, UserTier
        ├── cron.py              # CronScheduler
        ├── memory.py            # ChromaDB memory store
        ├── soul.py              # Persona management
        ├── types.py             # Type definitions
        ├── debug_log.py         # Debug logging
        ├── skills/              # Skills module
        │   └── (skills loading)
        ├── tools/               # Tool implementations
        │   ├── __init__.py      # TOOL_DEFINITIONS
        │   ├── registry.py     # ToolRegistry
        │   ├── files.py        # File operations
        │   ├── messages.py     # Message search
        │   ├── bash.py         # Bash execution
        │   ├── browse.py       # Web browsing
        │   ├── cron.py         # Scheduling
        │   ├── skills.py       # get_skill tool
        │   ├── memory.py       # remember/recall_memories
        │   ├── soul.py         # read_soul/update_soul
        │   ├── signal_ops.py   # send_signal_attachment
        │   ├── imagegen_ops.py # generate_image tool
        │   ├── tts_ops.py      # text_to_speech, list_tts_voices, set_tts_voice tools
        │   ├── stt_ops.py      # retranscribe_audio tool (alternate STT backend)
        │   ├── paths.py        # shared sandboxed channel context-dir / attachments-dir helpers
        │   ├── browser_client.py  # BrowserManager (playwright over websocket, one context per browser scope)
        │   ├── browser_cookies.py  # Cookie/storage-state persistence per channel
        │   ├── channel_mode.py # get_channel_mode/set_channel_mode
        │   ├── debug_cron.py   # debug_cron tool
        │   ├── debug_subagent.py  # debug_subagent tool
        │   ├── message_mode.py # get/set_message_mode (single-message mode per channel)
        │   ├── voice_call.py   # end_call tool (only during a voice call)
        │   └── mute.py        # Mute handling
        ├── voice/               # Voice calls (experimental): rpc, manager, audio, vad, stt_stream, speech, text
        ├── web/                 # Web interface backend: api, auth, bus, channels, files
        └── db/                  # Database operations
```

Unit tests live in the top-level `tests/` directory.

---

## Configuration Schema (config/agent.yaml)

All sections are optional — every field has an in-code default (see `src/nixclaw_agent/config.py`).
`config/agent.yaml.example` (checked in) only sets a subset of these; the rest fall back to the
dataclass defaults below.

```yaml
backend: "ollama"                  # "ollama" or "vllm"

ollama:
  host: "http://localhost:11434"
  model: "llama3"
  options:                         # OllamaOptionsConfig
    temperature: 1.0
    top_k: 20
    top_p: 0.95
    num_predict: 2048

vllm:
  host: "http://gx10-811e.lan:8000"
  model: null                       # null = auto-detect from vLLM server
  vision_model: null                # optional dedicated model for analyze_image; falls back to `model`
  options:                         # VllmOptionsConfig
    temperature: 1.0
    top_p: 0.95
    top_k: 64
    max_tokens: 220000
    timeout: 120.0
    context_window: 131072          # set 0 to disable dynamic max_tokens clamping
    stream_idle_timeout: 30.0       # max seconds between SSE chunks before retry
    max_retries: 10                 # backoff = 3s * attempt_number
    max_concurrent_requests: 2      # client-side concurrency cap (module-level semaphore in llm.py)
    max_concurrent_wait_seconds: 240.0  # FIFO queue wait before VLLMConcurrencyLimitError

stt:
  enabled: true                     # kill switch for the automatic voice-message pipeline (default false; STT_ENABLED env var overrides when non-empty)
  base_url: "http://172.17.222.155:8001"  # faster-whisper server (deploy/dgx-spark/faster-whisper/); STT_BASE_URL env var overrides
  model: "large-v3"                 # informational; echoed back by the server
  language: "en"                   # forced ISO-639-1 code (default "en") or null for auto-detect; STT_LANGUAGE env var overrides when non-empty ("auto" = auto-detect)
  timeout: 60.0
  max_duration_seconds: 300.0       # reject longer clips (AudioTooLongError)
  chunk_seconds: 28.0               # 16 kHz mono WAV chunks (below Whisper's 30 s limit)
  retention_hours: 1.0             # audio + .transcript.md sidecar kept this long; <= 0 keeps forever
  alternate:                        # second STT backend, reached only via retranscribe_audio
    enabled: false
    base_url: "http://172.17.222.155:8007"  # deploy/dgx-spark/canary-1b-v2/ (no STT_ALT_* env var overrides for this backend - YAML only)
    model: "canary-1b-v2"
    language: null                  # no auto-detect; tool caller must supply if unset
    timeout: 90.0
    max_concurrent_requests: 1      # per-base_url cap (keyed semaphore dict in tools/stt_ops.py)
    max_concurrent_wait_seconds: 120.0

signal:
  api_url: "http://localhost:8080"
  phone_number: "+491234567890"
  webhook_port: 3002
  webhook_path: "/webhook/signal"
  webhook_secret: null             # usually via env SIGNAL_WEBHOOK_SECRET (.env); unset = webhook on 127.0.0.1 only
  mock_api_url: null               # For testing: "http://mock-signal-api:8081"

browser:
  ws_url: "ws://localhost:8931"     # Playwright server websocket; BROWSER_WS_URL env var overrides

image_gen:
  enabled: false                    # kill switch; set true when the flux2 server is running
  base_url: "http://gx10-811e.lan:8002"  # DGX Spark running deploy/dgx-spark/flux2-klein/; IMAGEGEN_BASE_URL env var overrides
  model: "black-forest-labs/FLUX.2-klein-9B"
  timeout: 180.0                    # IMAGEGEN_TIMEOUT env var overrides
  default_size: "1024x1024"         # both dims 16-multiples, max 2048
  default_steps: 4                      # klein is distilled; 4 passes is enough; IMAGEGEN_STEPS env var overrides
  max_steps: 40                         # backend supports up to 40 passes; IMAGEGEN_MAX_STEPS env var overrides
  max_concurrent_requests: 1        # process-wide GPU cap (module-level semaphore in imagegen.py)
  max_concurrent_wait_seconds: 240.0

tts:
  enabled: false                    # kill switch; set true when a TTS server is running
  default_language: "en"            # code default "en" (single English Kokoro engine)
  engines:
    en:
      base_url: "http://gx10-811e.lan:8005"  # deploy/cpu/kokoro-tts-english/ (German: kokoro-tts-german, :8003); TTS_BASE_URL env var overrides the default_language engine
      model: "hexgrad/Kokoro-82M"
      voice: "af_heart"
      speed: 1.0
      response_format: "aac"        # aac | m4a | mp3 | ogg | wav
      timeout: 120.0                # TTS_TIMEOUT env var overrides
      max_input_chars: 20000
      max_concurrent_requests: 2    # per-base_url cap (module-level, keyed semaphore dict in tts.py)
      max_concurrent_wait_seconds: 180.0

auth:
  privileged_numbers: []
  known_numbers: []

cron:
  poll_interval_seconds: 60
  max_failure_count: 3              # consecutive failures before terminal action

paths:
  data_dir: "/data"
  tools_dir: "/tools"

database:
  db_path: "/data/messages.db"      # single shared DB for all channels (messages, cronjobs, summaries)

logging:
  level: "INFO"
  file: "/logs/nixclaw-agent.log"

debug_logging:
  mode: "auto"
  file: "/logs/debug-agent.json"
  buffer_size: 400

agent:
  name: "Nixclaw"                   # used for @-mention detection in groups

chat:
  sliding_window_size: 10        # Ignored (legacy)
  min_messages_to_keep: 15        # Always show last N messages to LLM
  context_size_threshold: 50000    # Token count threshold for summarization
  max_keywords: 50                 # Max keywords in summary preamble
  min_messages_to_summarize: 10   # Minimum msgs beyond min_keep before triggering
  tool_call_args_max_chars: 256    # UTF-8 byte floor; values below 256 are clamped with a warning
  tool_result_max_chars: 8000      # within-turn tier-1 truncation: per-tool result cap (bytes)
  history_tool_result_max_chars: 500  # replayed-history tool results: per-result cap (chars)
  turn_compaction_threshold_tokens: 60000  # within-turn tier-2 threshold: clear old tool results
  read_receipts: true              # mark the user's message read only when its turn ends

consent:                           # approval polls (consent.py); hot-reloadable, read per poll
  timeout_minutes: 120             # chat: the turn waits paused on the poll
  background_timeout_minutes: 720  # polls posted by cronjobs/subagents

webui:                             # web interface backend (web/); see "webui Container"
  enabled: false                   # start the API (restart required)
  api_host: "127.0.0.1"            # restart required
  api_port: 3003                   # restart required; = api_upstream in config/webinterface.yaml
  code_ttl_seconds: 300            # login code lifetime
  token_expiry_days: 90            # session cookie lifetime; 0 = never
  max_upload_mb: 50                # per request; webinterface.yaml max_upload_mb must be >= this
  editor_max_kb: 1024              # largest file the text editor saves
  hidden_paths: [browser, memories]  # workspace paths hidden from the file browser
  text_extensions: [.md, .txt, .py, ...]  # files offered in the editor
  code_requests_per_number_per_hour: 5
  code_requests_per_ip_per_hour: 20

voice_calls:                       # EXPERIMENTAL 1:1 Signal voice calls; see "Voice calls"
  enabled: false                   # restart required (whole section)
  mode: "agent"                    # agent | parrot (echo transcripts, pipeline test)
  allowed_tiers: [PRIVILEGED, KNOWN]
  # further tuning keys (VAD, barge-in, streaming STT, hangup timers): see "Voice calls"
```

---

## User Tiers

| Tier        | File Ops | Message Search | Bash | Browse | Cron | Memory | Soul |
|-------------|----------|----------------|------|--------|------|--------|------|
| Privileged  | ✓        | ✓              | ✓    | ✓      | ✓    | ✓      | ✓    |
| Known       | ✓        | ✓              | ✗    | ✓      | ✗    | ✗      | ✓    |
| Unknown     | ✗        | ✗              | ✗    | ✗      | ✗    | ✗      | ✗    |

---

## Out of Scope

- Langchain/deepagents integration
- HTTP client for web browsing (use the Playwright browser service instead)
- Complex subagent hierarchy (subagent = immediate cronjob)
- Multi-modal LLM support beyond the `analyze_image` tool: images (and all other attachments) on a chat turn are never inlined; the user turn only names the file (`[Attachment received: <filename>]`, added by `llm.py`'s `with_attachment_note` in `core.py`'s `_preprocess_incoming` and persisted with the message so history and `message_search` keep the filename; audio is left out when STT is on, since its `[Voice message: <filename>]` tag already names it), and the model uses `analyze_image`/`generate_image` on it
- Message encryption beyond Signal E2E
