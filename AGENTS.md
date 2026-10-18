# AGENTS.md

Guidance for coding agents (Claude Code, OpenCode, ...) working in this repository. `CLAUDE.md` only points here.

## Project Overview

NixClaw is an agent harness that communicates via Signal messenger. It runs as a Docker Compose stack with a Python asyncio agent, a signal-cli REST API gateway, and a plain Playwright browser server. Key principles: simplicity, robustness, security.

Read `doc/ARCHITECTURE.md` before any non-trivial change. Follow `process/DEVELOPMENT_PROCESS.md` for feature and bug-fix work (see Development Process below).

## Key Commands

```bash
# Test/type-check tools (once): pytest, pytest-asyncio, mypy
pip install -r requirements-dev.txt

# Type check (src/ uses absolute imports "from src.nixclaw_agent..."; inside container PYTHONPATH=/app)
mypy src/ --ignore-missing-imports --explicit-package-bases

# Run all tests
python3 -m pytest tests/ -v

# Run single test (exact class::test path)
python3 -m pytest tests/test_mock_signal_api.py::TestMockSignalAPI::test_message_uuid_is_unique -v

# Verification gate before considering a change done (both must pass)
mypy src/ --ignore-missing-imports --explicit-package-bases && python3 -m pytest tests/ -q

# Run agent outside Docker (loads config/agent.yaml from repo root)
python -m src.nixclaw_agent.main

# Full docker stack (real Signal)
docker-compose up -d --build

# Enable the pre-commit LLM privacy check (once per clone; config: cp config/dev.yaml.example config/dev.yaml)
scripts/install-git-hooks.sh

# Docker stack with mock Signal API (for testing)
docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d

# After code changes: the nixclaw container mounts .:/app, so a restart (no rebuild)
# usually picks up src/ edits. Only rebuild when dependencies/Dockerfile change.
docker-compose -f docker-compose.yml -f docker-compose.test.yml build nixclaw
docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d nixclaw
```

No `pytest.ini`/`pyproject.toml` — pytest uses rootdir auto-detection. No formatter/linter is configured; do not introduce one without being asked.

`tmp/` (gitignored) is the one temp directory for scratch files, throwaway scripts, logs and downloads; don't create competing ad-hoc temp dirs.

## Architecture Summary

### Docker Services
- **signal-api** (port 8080): built from `Dockerfile.signal-api` (pinned `bbernhard/signal-cli-rest-api` + PulseAudio + `signal-call-tunnel` for voice calls, see `docker/signal-api/`). Signal protocol client, persists the account in `.signal-data/`, forwards messages via webhook to nixclaw. Also exposes the signal-cli JSON-RPC call API (6002) and PulseAudio (4713) on host loopback only, used by the experimental voice calls
- **nixclaw** (port 3002, `network_mode: host`): Python asyncio agent - receives webhooks, processes messages, interacts with the LLM. Image baked by `Dockerfile.main` (Python 3.11 + ImageMagick, ghostscript, cups-filters, **ffmpeg** for voice-message transcription, pyipp for the print skill)
- **browser** (port 8931): plain Playwright server (`mcr.microsoft.com/playwright:v1.61.0-noble` running `npx playwright run-server`). The agent connects with the Python `playwright` client over websocket; the pip version in `requirements.txt` and the image/npx version MUST stay in lockstep. One `BrowserContext` per browser scope (chat scope per channel, ephemeral scope per cronjob/subagent run); calls on the same scope serialized via asyncio locks in `src/nixclaw_agent/tools/browse.py`. Also runs the mock-sites server (port 8932, `tests/fixtures/mock_sites/`): 18 test websites (captchas, cookie banners, slow/quirky pages) reachable as `http://localhost:8932/...` from both the headless browser and the host
- **mock-signal-api** (port 8081): test-only service, activated by `docker-compose.test.yml`. Serves fixture files from `tests/fixtures/` via `/v1/attachments/{filename}` so tests can inject audio/image attachments without a real Signal server
- **faster-whisper-server** (port 8001, optional): OpenAI-shaped `/v1/audio/transcriptions` endpoint backed by faster-whisper (CTranslate2). Deployed via `deploy/dgx-spark/faster-whisper/`. Used for voice-message transcription (set `STT_BASE_URL` and `STT_ENABLED`). On GPU hosts use `WHISPER_MODEL_ID=Systran/faster-whisper-large-v3` + `COMPUTE_TYPE=float16`; on CPU-only hosts `Systran/faster-whisper-base` + `COMPUTE_TYPE=int8`
- **flux2-image-server** (port 8002, optional): OpenAI-shaped image generation/editing endpoint (`/v1/images/generations`, `/v1/images/edits`) backed by FLUX.2 [klein] 9B on the DGX Spark (the same GPU that hosts vLLM). Deployed via `deploy/dgx-spark/flux2-klein/`. Used by the `generate_image` tool (set `IMAGEGEN_BASE_URL`/`IMAGEGEN_ENABLED`; see `image_gen:` in `config/agent.yaml`)
- **kokoro-tts-german** (port 8003, optional): OpenAI-shaped `/v1/audio/speech` endpoint backed by Kokoro-82M-ONNX-German-Martin (single voice "martin", German only, CPU-only). Deployed via `deploy/cpu/kokoro-tts-german/`. Used by the `text_to_speech` tool (set `TTS_BASE_URL`/`TTS_ENABLED`; see `tts:` in `config/agent.yaml`, one engine per language under `tts.engines`)
- **webui** (port 8443 HTTPS; 8090 HTTP only if enabled in `config/webinterface.yaml`, off by default; `network_mode: host`): Caddy (`webui/docker/`, Caddyfile rendered by `entrypoint.sh` from `config/webinterface.yaml`, fallback `.example`) serves the vanilla-JS frontend `webui/` and proxies `/api/*` (no SSE buffering) to the nixclaw web API on `127.0.0.1:3003` (`web/api.py`, started only when `webui.enabled`). Generates a self-signed cert into `config/ssl/` when HTTPS is on and none exists. See `doc/ARCHITECTURE.md` "webui Container"

### Source Code (`src/nixclaw_agent/`)
- `main.py` - Entry point, initializes all components, starts the event loop (and the voice-call manager when `voice_calls.enabled`)
- `core.py` - `AgentLoop`: main loop, message dedup, context loading, LLM invocation, tool execution, response sending. Two-lane per-channel turn serialization (interactive chat lane + background cron/subagent lane) + inbound message queue (see Concurrency below). Injects voice-message transcription after attachment download (see `stt.py`). Two summarization mechanisms: cross-turn DB-based (`_run_summarization`, main agent) and session-local ephemeral (`_maybe_compact_session`, cronjobs/sub-agents — compacts the in-run working message list; see `doc/ARCHITECTURE.md`)
- `llm.py` - `LLMClient`/`VLLMClient` for Ollama/vLLM backends, prompt assembly with context windows (`build_system_prompt` is one shared implementation; all prompt text comes from `prompts.py`). vLLM auto-selects the first model from `/v1/models` if none is configured
- `prompts.py` - Prompt loader: all prompt text/canned replies live in `config/prompts/{default,custom}`; `REGISTRY` of `PromptSpec` rows (file/key, allowed `{{placeholders}}`, required tokens); `prompts.current().text()/render()/doc()`, `set_current`, `PromptError`. See Prompts below
- `turn_output.py` - `TurnOutput`: single-message mode (`chat.single_message.mode` off/append/replace, per-channel override via `tools/message_mode.py`): later texts of a turn edit the turn's last Signal message instead of sending new ones; an attachment starts a new target
- `tool_markup.py` - `chat_with_markup_guard`: detects leaked tool-call/reasoning markup in LLM text, discards the completion and re-prompts (logged as `leaked_tool_markup`)
- `config.py` - Configuration loading from `config/agent.yaml` and env vars (dataclass tree incl. `SttConfig`, `ConsentConfig`, `VoiceCallsConfig`)
- `types.py` - shared dataclasses (`UserTier`, `Tool`, `ToolCall`, `ToolResult`, `Context`, ...)
- `consent.py` - `ConsentManager`: user approval via Signal polls for opted-in bash scripts (a script prints a stderr marker, the harness kills it, posts its output + a Yes/No poll, re-runs it with `NIXCLAW_CONSENT=1` on "Yes"). Chat turns pause on the poll (`core.py` `_pause_for_consent`/`_drain_while_paused`); cronjobs/subagents don't (`_background_consent_result`, approved re-run in `_run_approved_background_command`, failure -> one-time inspection cronjob via `_schedule_consent_inspection`). Open polls in table `consent_polls`, deleted at startup. See `doc/ARCHITECTURE.md` "User consent"
- `sandbox.py` - Runs parsers of untrusted input (ffprobe/ffmpeg in `stt.py`) as `nobody` via `setpriv` + `prlimit`, input passed as an open fd (`-i fd:`), minimal env; no-op when not root. Keep `store/`, `groups/`, logs dirs at 755 (not 777) so the sandbox can't replace them
- `path_safety.py` - shared path-containment check used by every per-channel sandbox guard
- `stt.py` - Voice-message transcription: ffmpeg chunking + httpx multipart to faster-whisper-server; sidecar `.transcript.md` cache; graceful failure → placeholder text. Audio + sidecar are kept for `stt.retention_hours` (default 1h) so the `retranscribe_audio` tool can retry via a second, independently-configured backend (`stt.alternate`, e.g. NVIDIA Canary-1b-v2 - see `deploy/dgx-spark/canary-1b-v2/`) when the primary transcript looks garbled; a periodic sweep deletes anything past the retention window (`data/<channel>/attachments` + the nested `data/signal-group:<a>/<b>/attachments` dirs; no symlinks, no user folders named `attachments`). See `doc/ARCHITECTURE.md`'s `agent/stt.py` section
- `imagegen.py` - FLUX.2 [klein] image generation client (`/v1/images/generations` + `/v1/images/edits`); module-level GPU semaphore shared across all `FluxImageClient` instances (see Concurrency below)
- `tts.py` - Kokoro-82M ONNX TTS client (`/v1/audio/speech`); semaphore keyed per `base_url` (see Concurrency below)
- `voice/` - **Experimental** 1:1 Signal voice calls (off by default): `rpc.py` (signal-cli JSON-RPC call API), `manager.py` (`CallManager` tier check/accept/reject, `CallSession`), `audio.py` (PulseAudio), `vad.py`, `stt_stream.py` (optional streaming STT), `speech.py` (TTS playback, barge-in), `text.py`. Each utterance becomes an inbound message (`origin="voice"`) via `AgentLoop.submit_voice_message`; the turn's texts are spoken. See `doc/ARCHITECTURE.md` "Voice calls"
- `signal.py` - `SignalClient`: Signal CLI REST API calls (send, attachments, `create_poll`, `delete_message` = remote delete; each takes `publish=True` and reports successful sends to the module-level `set_outbound_listener` hook, shared by BOTH client instances - `main.py`'s and `tools/signal_ops.py`'s `SIGNAL_CLIENT`; `publish=False` = 🌐 mirrors + login codes), webhook server (`dataMessage.pollVote` -> `PollVote` -> `on_poll_vote` callback, never a chat event; voter = envelope source; `syncMessage` votes ignored), test number routing, attachment download fallback to mock API when live API returns 404
- `context.py` - `ContextManager`: per-channel context (data dir, summaries, attachments)
- `auth.py` - `AuthManager`/`UserTier` (PRIVILEGED/KNOWN/UNKNOWN) with per-tool permissions
- `cron.py` - `CronScheduler`: SQLite-backed recurring job scheduling
- `memory.py` - ChromaDB vector memory store with auto-memorization
- `soul.py` - Per-channel persona management (channel `SOUL.md` > `config/prompts/custom/soul.md` > `default/soul.md`)
- `debug_log.py` - debug logging helpers (see `doc/DEBUGGING.md`)
- `tools/` - Tool implementations organized by domain:
  - `files.py` - File read/write/grep/list/search (KNOWN+ tier)
  - `bash.py` - Bash execution (PRIVILEGED only): own process group (`start_new_session`, timeout = `killpg` incl. `nohup &` children), stdout/stderr as separate pipes merged for the model, partial output on timeout, output included in the error on non-zero exit, explicit message when background children held the pipes open; sets `NIXCLAW_CHANNEL_ID`, detects consent markers (`_ConsentWatch`), refuses commands mentioning `NIXCLAW_CONSENT`
  - `browse.py` - Browser tools (web_search, browser_navigate, browser_interact, browser_screenshot, browser_evaluate) on top of `browser_client.py` (KNOWN+); resolves the running turn's browser scope (`registry.get_current_browser_scope()`, channel scope if no turn Context) and serializes calls per scope; `close_browser_scope` frees a background run's scope
  - `browser_client.py` - `BrowserManager`: Python playwright over websocket to the browser container; one BrowserContext+page per browser scope (see Concurrency below), cookie persistence via `storage_state` for chat scopes only (`browser_cookies.py`)
  - `imagegen_ops.py` - `generate_image`: generate/edit an image via `imagegen.py` (one or more input images supported), writes the PNG to the channel's attachments/ and returns the bare filename for `send_signal_attachment` (KNOWN+)
  - `tts_ops.py` - `text_to_speech`: synthesize speech via `tts.py` for a supported language, writes the audio file to the channel's attachments/ and returns the bare filename for `send_signal_attachment` (KNOWN+). `list_tts_voices`/`set_tts_voice`: query the servers' `GET /v1/voices` (`KokoroTTSClient.list_voices`, not semaphore-gated; 404 = no listing; Qwen `presets`/`cloned` and Kokoro/Kyutai `voices` shapes normalized) and store a per-channel, per-language voice in `data/<channel>/tts_voice.json`; voice resolution = `voice` argument > channel setting > engine `voice` (KNOWN+)
  - `stt_ops.py` - `retranscribe_audio`: re-run a previously-received voice message (by filename, still within `stt.retention_hours`) through the alternate STT backend (`stt.alternate`) as a second opinion (KNOWN+)
  - `voice_call.py` - `end_call`: hang up the active voice call; offered only during a call (`VOICE_CALL_ONLY_TOOLS` in `core.py`), refused unless the caller said goodbye
  - `paths.py` - shared `get_context_dir`/`get_attachments_dir` (sandboxed channel attachments dir, used by `signal_ops.py`, `browse.py`, `imagegen_ops.py`, `tts_ops.py`)
  - `signal_ops.py` - `send_signal_attachment`
  - `cron.py` - schedule_cron/list_cronjobs/cancel_cronjob/stop_running_activity (PRIVILEGED); `debug_cron.py`/`debug_subagent.py` - inspect stored (incl. discarded) cronjob/subagent output (PRIVILEGED)
  - `messages.py` - Message history search (KNOWN+)
  - `memory.py` - remember/recall_memories (PRIVILEGED)
  - `soul.py` - read_soul/update_soul
  - `skills.py` - get_skill tool
  - `mute.py` - Mute/unmute handling
  - `registry.py` - `ToolRegistry`: tool definitions, execution with timeouts, circuit breaker; strips model-supplied `RESERVED_ARGUMENTS` (`consent`, `consent_question`, `channel_id`, `user_tier`); `execute_tool(..., consent_question=...)` is core-only (approved re-run)
  - `channel_mode.py` - Channel processing mode (mention_required/always_process)
  - `message_mode.py` - Single-message mode per channel: `set_message_mode`/`get_message_mode` (off/append/replace, stored in `data/<channel>/message_mode.json`, missing = global `chat.single_message.mode`) + `resolve_message_mode` used once per turn by `core.py` (KNOWN+)
- `web/` - Web interface backend (API only when `webui.enabled`; `channels.py` always): `api.py` (aiohttp routes on `webui.api_host:api_port`, auth middleware, `X-NixClaw: 1` CSRF header on POST/PUT/DELETE, SSE `/api/events`), `auth.py` (`WebAuth`: Signal-delivered 6-digit login codes, `web_endpoints` table (sha256 token) + `nixclaw_session` cookie, `/status`/`/deauth`), `bus.py` (`ChatBus`: sole writer of table `chat_log` = web history, global `rev` = SSE id/replay; outbound listener target), `channels.py` (`MembershipCache` from signal-api `GET /v1/groups/{bot}`, TTL 10 min; always created and initially refreshed by `main.py`, shared by the web API and core's unknown-sender gate (`group_members`); `{cid}` = base64url(channel_id)), `files.py` (file browser sandbox = 1:1 workspace `data/<number>` minus `webui.hidden_paths`, atomic writes, ETag `{mtime_ns:x}-{size:x}`). Frontend: `webui/` (no framework/bundler/CDN; never `innerHTML` with message/file data)
- `db/operations.py` - SQLite operations on ONE database file, `database.db_path` (default `/data/messages.db`, WAL mode): tables `messages`, `context_summaries`, `cronjobs`, `consent_polls`, `chat_log`, `web_endpoints`
- `skills/` - Python skill implementations loaded by the `get_skill` tool (separate from the top-level `skills/` dir, which holds each skill's `SKILL.md` instructions; `config/skills.yaml` is the static skill catalog)

### Message Processing Pipeline
1. Webhook POST to `/webhook/signal?token=<SIGNAL_WEBHOOK_SECRET>` (401 without it; no secret configured = listener on 127.0.0.1 only; a `pollVote` envelope goes to `ConsentManager.on_vote` instead and stops here)
   - Unknown-sender gate (`_unknown_sender_allowed`, first thing in `_on_signal_message`, before the `chat_log` publish): an UNKNOWN sender (bot's own number exempt) is dropped silently and entirely (not stored/published, no task/reply/typing/read receipt; one INFO log line without content) unless `auth.unknown_users` allows it: 1:1 needs `allow_direct` (default false), groups need `allow_in_groups` (default true) AND a KNOWN/PRIVILEGED member (bot excluded) in the `MembershipCache` member list (uncached group -> one rate-limited refresh, still unknown -> dropped; UUID-only members don't count). Web/voice messages are tier-gated elsewhere and skip this
2. Channel-busy gate (`_dispatch_or_queue`): if a chat turn is already active for this `channel_id` (interactive lane), store the message and queue it for the running turn instead of starting a second task; a background cron/subagent run on the same channel does NOT block this (see Concurrency)
3. Deduplication check (SQLite message_id; currently ineffective, see `doc/ISSUES.md`)
4. Store message (ALL messages, regardless of mention)
5. Check mention (1:1 = always process, groups = bot must be mentioned); exact `/reload` from a PRIVILEGED sender in a 1:1 chat triggers config+prompt reload instead of an LLM turn; next to it `/status` and `/deauth <index|name>` (KNOWN+, 1:1; web login endpoints, `web/auth.py`; in a group only a "use 1:1" hint when mentioned) are answered without the LLM when the web interface is enabled
6. Strip mention artifacts from message text
7. Load channel context, build prompt (template + recent messages + summary + skills)
8. LLM.chat() with tool definitions
9. Execute tool calls (sequential, with timeouts and circuit breaker), draining queued inbound messages at each iteration; a bash call that asks for consent pauses the turn on an approval poll (see `consent.py` above, Control Markers below)
10. Send response via signal-cli (single-message mode: later texts of the turn edit its last Signal message instead of sending new ones; see `turn_output.py`); during a voice call the texts are also spoken
11. Read receipts: after the turn's last reply/edit, `_start_channel_task` sends a Signal read receipt for every message the turn handled (queued in `_preprocess_incoming`; not for unmentioned group msgs, duplicates, dropped events, cron/subagent runs; still-queued ones get it at the end of the turn they start). Mid-turn the user sees only "delivered", so "read" = agent done. See `doc/ARCHITECTURE.md` "Read receipts"

Web-originated messages (`POST /api/channels/{cid}/messages`) enter at step 2 via `AgentLoop.submit_web_message`: files are saved to the channel's attachments/ first (`local_path` set, not re-downloaded), the `chat_log` row is published, then `_dispatch_or_queue` runs the same path (dedup key `web:<chat_log id>`); the 🌐 mirror to Signal (`🌐 <text>`, groups `🌐 <name or number>: <text>`, `publish=False`) is sent afterwards in a background task. Voice-call utterances enter the same way via `submit_voice_message` (dedup key `voice:<id>`, Signal mirror `🎙️ <text>`). Inbound Signal messages are published to `chat_log` once at entry (`_on_signal_message`, before step 2; own-number echoes skipped), agent output via the outbound listener.

Steps 3-6 (plus attachment download + STT and DB persistence) live in the shared `_preprocess_incoming()` helper, so a message drained from the queue mid-turn gets identical treatment to one processed immediately.

### Concurrency
- Each event spawns an `asyncio.Task`, max 10 concurrent (`MAX_CONCURRENT_TASKS`)
- Per-channel turn serialization (`core.py`) in two independent **lanes**: an **interactive** lane for chat and a **background** lane shared by cronjobs and subagents. Only one run per `channel_id` per lane; the lanes don't block each other (a long cronjob must not block live chat). Different channels run concurrently up to the task limit.
  - **Interactive lane**: `self._channel_active_task: dict[str, asyncio.Task]` holds the one live chat turn per channel, registered via `_dispatch_or_queue`/`_start_channel_task`. A chat message arriving while the lane is busy is stored and appended to `self._channel_inbound_queue[channel_id]` (cap `MAX_QUEUED_INBOUND_PER_CHANNEL` = 5, oldest dropped; consecutive identical messages from the same sender deduped) rather than spawning a racing task, so a "stop" reaches the running turn. `process_message`'s tool loop calls `_drain_inbound_checkpoint()` at the top of each iteration: queued events go through `_preprocess_incoming()` and are injected as a synthetic `user` turn (`[New message from user while you were working: ...]`) so the model itself decides to stop/redirect/continue; if preprocessing resolved to a stop/reset canned shortcut, the checkpoint returns True and the loop exits immediately (existing `finally` blocks release turn-scoped resources). Background runs don't call this checkpoint — they are not interrupted by chat.
  - **Background lane**: `self._channel_background_task: dict[str, asyncio.Task]` holds the one live cronjob-or-subagent run per channel; `self._channel_background_meta: dict[str, dict]` records `{"source_type": "cronjob"|"subagent", "source_id": <id>}`. Cronjob dispatch and `spawn_subagent_task` both register via `_register_channel_task(..., lane="background", meta=...)`. Cronjobs whose channel's background lane is busy are skipped for that tick (no `mark_complete`, retried next 60s poll). `spawn_subagent_task` runs off the event-loop thread and registers its task via `loop.call_soon_threadsafe` + a `concurrent.futures.Future`; it raises `RuntimeError` if the background lane is occupied — that check must happen on the event-loop thread, immediately before `create_task`, to avoid racing a cron tick.
  - **Stopping a background run**: the `stop_running_activity` tool (PRIVILEGED; `tools/cron.py`) → `AgentLoop.cancel_background_activity(channel_id, activity_id)` validates `activity_id` against `self._channel_background_meta` and cancels the task (scheduled onto the event loop thread). `_process_cronjob`/`_process_subagent` catch `asyncio.CancelledError` ahead of their general `except Exception`, treat it like a successful completion for scheduling purposes (cronjob: `mark_complete(..., success=True, ...)`; subagent: no bookkeeping), send no message, and swallow the exception so the task ends normally while its `finally` block still runs. Distinct from `cancel_cronjob`, which only stops *future* scheduling. The chat system prompt (`build_system_prompt`, `llm.py`) gets a short `★ BACKGROUND ACTIVITY` block (type + id only, never the prompt text, which can contain credentials) whenever a background run is active, so the foreground agent can call `stop_running_activity` if asked.
  - **Paused on an approval poll**: a chat turn waiting on a consent poll keeps its interactive-lane slot AND its task-semaphore slot (max one paused turn per channel) for up to `consent.timeout_minutes`; inbound messages queue as usual and are drained by the paused turn (`_drain_while_paused`). Background runs never pause; an approved background re-run runs in its own task outside both lanes (may overlap a cronjob on the same channel).
  - `self._messages_sent` is keyed per channel. Tests: `tests/test_channel_turn_serialization.py`
- SQLite WAL mode, serialized writes
- Browser: one BrowserContext+page per **browser scope** (`BrowserManager` in `tools/browser_client.py`, all methods take `(channel_id, ..., scope=None)`, scope defaulting to channel_id). `Context.lane` (`types.py`, set via `ContextManager.get_context(channel_id, lane=...)`) picks the scope via `Context.browser_scope`: chat turns (`lane="interactive"`) share the persistent channel scope `channel_id` - users in one channel may see each other's browsing; cronjob and subagent runs (`lane="background"`, set in `_process_cronjob`/`_process_subagent`; a subagent never inherits the spawning chat turn's scope) each get an ephemeral scope `"<channel_id>#run:<turn_id>"`. Different channels never share a context. Background scopes are seeded from the channel's `storage_state.json` but never write it back (`save_state`/`close_scope` persist only when scope == channel_id), and background runs close their scope in `finally` (`_close_run_browser_scope` -> `browse.close_browser_scope`, which also drops the scope's lock). Calls on one scope are serialized by the per-scope `asyncio.Lock` in `browse.py` (`_get_lock`); different scopes run concurrently. `browser_screenshot` output carries the captured page's `URL:`/`Title:` after the filename so the model can notice a wrong page.
- vLLM: the shared vLLM backend degrades badly (50-75% stall rate) at concurrency ≥4 for large-context requests, so `VLLMClient.chat()` (`llm.py`) gates its retry loop behind a module-level `asyncio.Semaphore` (shared across every `VLLMClient` instance in the process - `main.py`'s long-lived client and a fresh one per call in `tools/files.py`'s `analyze_image`) sized by `vllm.options.max_concurrent_requests` (default 2). Requests queue FIFO up to `vllm.options.max_concurrent_wait_seconds` (default 240s) before raising `VLLMConcurrencyLimitError`.
- Image generation: the FLUX.2 server shares the DGX Spark's GPU with vLLM, so `FluxImageClient.generate()` (`imagegen.py`) gates its HTTP call behind a module-level `asyncio.Semaphore` (same create-on-first-use pattern) sized by `image_gen.max_concurrent_requests` (default 1). Requests queue FIFO up to `image_gen.max_concurrent_wait_seconds` (default 240s) before raising `ImageGenConcurrencyLimitError`; the semaphore is released in a `finally` around the post-acquire work only, so a wait-timeout can't release a slot that was never held. Because the queue wait happens *inside* the handler, the tool's outer timeout is computed dynamically (see Enforcement Details).
- Text-to-speech: `KokoroTTSClient.synthesize()` (`tts.py`) uses the same pattern, but keyed per `base_url` (`_shared_tts_semaphores: dict[str, asyncio.Semaphore]`), since `tts.engines` can point different languages at different servers - each `base_url` gets its own cap (that engine's `max_concurrent_requests`, default 2), engines sharing a `base_url` share its slots. Requests queue FIFO up to `max_concurrent_wait_seconds` (default 180s) before raising `TTSConcurrencyLimitError`.
- Alternate STT (`retranscribe_audio`, `tools/stt_ops.py`): same per-`base_url` pattern (`stt.alternate.max_concurrent_requests`, default 1), queuing up to `stt.alternate.max_concurrent_wait_seconds` (default 120s) before returning a clean busy error. The chunk-transcribe-join loop is shared with the primary pipeline via `stt.py`'s `_transcribe_chunks()`.
- vLLM image handling: images are sent at native resolution by default. If the backend's multimodal processor rejects an image (`ImageProcessingError`, matched from the 400 body, e.g. vLLM's `Failed to apply Qwen3VLProcessor on data=...`), `VLLMClient.chat()` downscales along `IMAGE_SHRINK_LADDER_PIXELS` (2048/1536/1024/768/512px longest edge) and resends. The working cap is cached in `_vllm_image_max_dimension_cache` keyed by model id, so a model swap triggers fresh discovery. See `doc/DEBUGGING.md` "Oversized/Rejected Images".
- Voice calls: one call at a time (`CallManager`); further callers are rejected.

### User Tiers
Defined in `src/nixclaw_agent/auth.py` (`TOOL_PERMISSIONS` dict); enforced in `src/nixclaw_agent/tools/registry.py`.
- **PRIVILEGED**: All tools including bash, cron, subagents, memory, debug tools
- **KNOWN**: File ops, browse, message search, read_soul, analyze_image, attachments, image generation, TTS, channel/message mode - NO bash/cron/memory
- **UNKNOWN**: `get_skill` only. Whether an UNKNOWN sender is answered at all is a separate rule (`auth.unknown_users`, see Message Processing Pipeline step 1): 1:1 off by default, groups only while a KNOWN/PRIVILEGED user is a member

There are no built-in email tools; privileged users can have the agent author an email-sending script in their channel workspace using the file and bash tools.

### Enforcement Details
- Path traversal protection on every file tool; per-user context dir isolation for file tools
- Tool timeouts: default 30s (`TOOL_TIMEOUT_SECONDS`); `analyze_image` 120s; `bash` gets its own (model-chosen, default 30s, max 300s, `bash.effective_timeout`) timeout + 5s, so bash's internal timeout fires first and returns partial output (a paused consent wait is outside the tool call). The 5 browser tools use fixed budgets (`BROWSER_OPERATION_BUDGET_SECONDS`: 45s for `web_search`/`browser_navigate`/`browser_interact`/`browser_evaluate`, 120s for `browser_screenshot`). Dynamic timeouts (queue wait happens inside the handler):
  - `generate_image`: `image_gen.max_concurrent_wait_seconds + image_gen.timeout × (steps/default_steps) + 30s + 0.5 × image_gen.timeout × (steps/default_steps) × (n_input_images − 1)`; `steps` defaults to `image_gen.default_steps`, clamped to `image_gen.max_steps` (default 40); `n_input_images` capped at `image_gen.max_input_images` (default 8)
  - `text_to_speech`: max of `max_concurrent_wait_seconds + timeout` across all configured `tts.engines`, plus 30s
  - `retranscribe_audio`: `ceil(stt.max_duration_seconds / stt.chunk_seconds) × stt.alternate.timeout + stt.alternate.max_concurrent_wait_seconds + 30s`
- httpx calls capped at 10s
- Consent: a gated command runs only after a "Yes" from a voter whose tier may use the tool (bash -> PRIVILEGED; others ignored, poll stays open); first counting vote wins, then the poll is remote-deleted. Only scripts that print the marker are gated (raw `ipptool`/`lp`/`curl` are not). First-run output goes verbatim to the whole channel/group. Voters without a visible number never count
- Dedup check on every inbound message (SQLite `message_id`)

## Control Markers
Special tokens in LLM output that trigger agent behavior (case-insensitive):
- `__MUTE__` / `__UNMUTE__` - Mute/unmute notifications
- `__DISCARD_OUTPUT__` - Discard response (cronjob/subagent output stays retrievable via `debug_cron`/`debug_subagent`)
- `__ISSUE_DETECTED__` - Trigger debug log flush
- `__AGENT_CONTINUE__` - Internal continuation turn (max 10/channel)

Script -> harness markers (NOT LLM output; complete **stderr** lines of a bash command, matched only with this channel's crc; `<crc>` = env `NIXCLAW_CHANNEL_ID` = crc32(channel_id) as 8 hex, set by the bash tool):
- `NIXCLAW_REQUIRE_CONSENT_QUESTION_<crc>="<question>"` (1-200 chars, no `"`) - "ask the users first": bash kills the process group, the output (minus marker, capped 1500 chars) is posted verbatim + a Yes/No poll; the model never sees it. Chat: the turn pauses (Yes -> re-run with env `NIXCLAW_CONSENT=1` + `(approved by …)`; No/timeout/message-instead-of-vote -> NOT RUN result, the message goes into the tool result; stop/reset ends the turn; later calls of the same step get `consent-skipped`). Background: tool result `consent-background-pending`, run goes on; identical pending command = no second poll. In the approved run the script must print the same marker once more right before acting; a second/different marker = killed + error (one approval = one action). Malformed marker = killed + error
- `NIXCLAW_CONSENT_OK_<crc>` - approved action succeeded; background lane posts the re-run output verbatim, otherwise (no OK marker or non-zero exit) schedules one one-time cronjob (`consent-inspection`, creator = voter)
- A bash command containing `NIXCLAW_CONSENT` is refused; `2>&1`/pipes move the marker to stdout, where it doesn't count

`leaked_tool_markup` is NOT a control marker but a debug/error event name (see `tool_markup.py`, `doc/DEBUGGING.md`).

## Testing

### Unit Tests
Run with `python3 -m pytest tests/ -v`. Tests are standalone (no Docker required). Manual integration scripts against a live vLLM/FLUX server live in `scripts/test/` (see its README).

### Mock Signal API Testing
Start with: `docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d`

Test numbers: `+000000` (PRIVILEGED), `+000001` (KNOWN), `+000002` (UNKNOWN). The mock does no tiering; a caller's tier comes from `auth.privileged_numbers` / `auth.known_numbers` in `config/agent.yaml`. Replies to `+0000*` numbers route to the mock; everything else goes to the real Signal API — **never** use real country-prefix numbers in tests, or replies go to live Signal and may get the account banned. A `400 Bad Request` on a reply usually means the recipient wasn't a `+0000*` number — fix the test, don't bypass.

Prefer the helper script over raw curl:
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "your prompt"
# options: -t SECS  --channel CHAN_ID  -n N  (N=0 means wait until timeout)
# attach an audio/image fixture (filename relative to tests/fixtures/, served by mock-signal-api):
API_URL=http://localhost:8081 ./send_mock_message.sh -a voice_test.aac +000000 "transcribe please"
# repeatable: ./send_mock_message.sh -a foo.png -a bar.aac +000000 "msg"
```
Or inject directly: `curl -X POST http://localhost:8081/_mock/inject -H "Content-Type: application/json" -d '{"source": "+000000", "destination": "+491234567890", "message": "test"}'`

View replies: `docker logs nixclaw-mock-signal-api`
Full conversation incl. LLM/tool calls: `docker logs nixclaw --since 5m`

Approval polls: the mock logs `Poll created: <question> answers=[...] ts=<ts> to <recipient>` and `Remote delete received for <ts>`; vote with `API_URL=http://localhost:8081 ./send_mock_message.sh --vote <ts> 0 +000000` (0 = yes, 1 = no, `-` removes the vote; raw: `POST /_mock/inject_vote`). 1:1 only - group polls/deletes go to real Signal, test those live. See `process/TESTING_WITH_MOCK_SIGNAL_API.md`.

Note: `send_mock_message.sh` only *injects* — its `-t`/`-n` options are parsed but unused; replies never appear on its stdout. Poll the mock API container logs for `Reply received` instead (in single-message mode, later texts of a turn are edits, logged as `Edit received for <ts>`). The end-of-turn read receipt is logged as `Read receipt received for <ts> from <recipient>`.

### Voice-message e2e
`tests/test_audio_e2e.py` injects a voice attachment via the mock API and asserts the mock received a reply and nixclaw logged a transcription. Requires the faster-whisper-server running and reachable (default `STT_BASE_URL=http://localhost:8001`); self-skips if either is down.

### Voice calls
Unit tests: `tests/test_voice_calls.py`. Live calls need a real phone; `docker/signal-api/README.md` describes the manual `call_debug.py` driver.

### Web interface
Unit tests: `tests/test_webui_{auth,api,core}.py` (offline, helpers in `tests/webui_support.py`). Manual/e2e: set `webui.enabled: true` in `config/agent.yaml` and `http.enabled: true` in `config/webinterface.yaml` (off by default), restart nixclaw, `docker-compose up -d --build webui`. The mock serves a fixed `GET /v1/groups/{number}` ("Mock Family" with `+000000`/`+000001`; only `+0000*` members count). Log in as `+000000`/`+000001`: `POST /api/auth/request`, the code appears in `docker logs nixclaw-mock-signal-api` (`Reply received ... login code`), then `/api/auth/verify` sets the cookie; every POST/PUT/DELETE needs `X-NixClaw: 1`. From the browser container the UI is `http://172.18.60.1:8090` (bridge gateway). Never send into the mock groups (group sends go to real Signal). See `process/TESTING_WITH_MOCK_SIGNAL_API.md` "Web interface".

### Browser robustness e2e
`tests/e2e_mock_sites.sh [scenario ...]` sends browsing prompts as KNOWN user `+000001` against the mock sites (captcha solving via screenshot→analyze_image→click, cookie banners, slow pages) and greps replies for per-site tokens (`tests/fixtures/mock_sites/README.md`). Deterministic tool-level equivalents: `tests/test_browser_tools_mock_sites.py` (needs only the browser container).

## Development Process

Follow `process/DEVELOPMENT_PROCESS.md` for feature and bug-fix work: Analysis -> Planning -> Subagent Review -> Implementation -> Subagent Review -> Testing (unit + mock Signal acceptance test) -> Documentation -> Signal Summary. Key rules: the orchestrator delegates implementation and testing to subagents; explain the root cause before fixing a bug; keep solutions simple.

## Safety Rules

1. **NEVER** run `git clean`, `git reset --hard`, or `rm -rf` without verifying no data files are affected
2. Files that must never be committed: `config/agent.yaml` (gitignored; copy from `config/agent.yaml.example`), `config/dev.yaml` (same, from `config/dev.yaml.example`), `config/webinterface.yaml` (same, from `config/webinterface.yaml.example`), `config/ssl/` (webui certs/keys), `config/prompts/custom/*`, `.env`, `.signal-data/`, `.signal-cli-data/`, `store/`, `data/`, `groups/`, `log-archive/`, `issues/`, `tmp/`, `*.db-shm`/`*.db-wal`
3. Pre-commit hook (`scripts/precommit_privacy_check.py`, enabled via `scripts/install-git-hooks.sh`): hard-blocks the paths of rule 2, then runs the pi coding agent (`--no-tools`, throwaway `PI_CODING_AGENT_DIR` with only the configured provider) with prompt `precommit-privacy-check` on the staged diff; the last `VERDICT: PASS|FAIL` decides, FAIL always blocks, failure to get a verdict follows `precommit_privacy_check.on_error` (block/warn) in git-ignored `config/dev.yaml` (template `config/dev.yaml.example`; missing file = block). `SKIP_PRIVACY_CHECK=1` skips the LLM part only. Do not bypass a FAIL with `--no-verify` without asking the user. Tests: `tests/test_precommit_privacy_check.py` (stub pi, no LLM)
4. Check for private files before staging: `git status | grep -E "(agent\.yaml|\.env|\.signal-data)"` — unstage matches with `git restore --staged <f>`
5. Always use path traversal protection for file operations
6. Enforce tool timeouts and user tier permissions

## Known Quirks

- Browser tools fall back to stubs ("Browser tools not available…") only if the `playwright` pip package is missing; it is pinned in `requirements.txt` and must match the browser container's Playwright version exactly.
- Bot name for @-mention detection comes from `config.agent.name`, not the Signal display name.
- The container sets `PYTHONPATH=/app`; local runs need `cd` to repo root or `PYTHONPATH=.`.
- `tests/fixtures/test_site/` is mounted read-only into the browser container at `/test_site`.
- `signal.py`'s `send_attachment` declares outgoing attachments as `data:<mime>;filename=...;base64,<data>`, not a bare base64 string, so Signal clients don't have to sniff magic bytes to detect the type; a bare string could misdetect ambiguous containers (e.g. fragmented-MP4 AAC audio as a video). Affects every attachment type.

## Prompts

- **Never hardcode prompt text in code.** To add a prompt: put the default text in `config/prompts/default/<file>.md` (or a key in a default `.yaml`), add ONE `PromptSpec` row to `prompts.REGISTRY`, use `prompts.current().text(name)` / `.render(name, **values)`. Extra whole-document yaml goes in `EXTRA_YAML_FILES`.
- `custom/` is git-ignored and optional (custom `.md` replaces default; `.yaml` merges per leaf key; unknown custom key = error; missing default = fatal at startup). `{{name}}` placeholders, single-pass, unknown = error, omitted OK; required control tokens (e.g. `__MUTE__`) validated. User guide: `config/prompts/README.md`.
- Tool descriptions are in `tool-descriptions.yaml`; `TOOL_DEFINITIONS` holds only schemas and `ToolRegistry.register` takes no description. Every tool param needs a default entry (`""` allowed).
- Reload: `/reload` (PRIVILEGED, 1:1) or SIGHUP re-reads `agent.yaml` + prompts, all-or-nothing (on error the previous state stays). Hot-applied: `auth.*` (numbers + `unknown_users`), `chat.*`, `agent.name`, `consent.*`, `logging.level`, prompts/tool descriptions; other changed sections report "restart required". Refused if `agent.yaml` missing/empty or `privileged_numbers` empty.
- Structural tags (`[Voice message: ...]`), stop/reset/mute keyword lists and tool error messages deliberately stay in code.

## Configuration

`config/agent.yaml` (gitignored; start from `config/agent.yaml.example`; every field has an in-code default in `config.py`). Main sections:
- LLM backend: `config.backend` `"ollama"` (default) or `"vllm"`, both read from the same dataclass tree; `vllm.vision_model` optionally routes `analyze_image` to a dedicated vision model; `vllm.options.max_concurrent_requests`/`max_concurrent_wait_seconds` (default 2 / 240s, see Concurrency)
- `signal`, `browser.ws_url` (the deprecated `browser.lease_wait_timeout_seconds` key is still accepted but ignored), `auth.{privileged,known}_numbers`, `auth.unknown_users` (`allow_direct` default false, `allow_in_groups` default true; non-bool = startup/reload error), `cron` (incl. `max_failure_count`), `paths`, `database.db_path`, `logging`, `debug_logging`
- `agent.name`: @-mention detection in groups and `{{agent_name}}` in the system prompt
- `chat`: context window + summarization parameters; `chat.single_message` (`mode` off/append/replace, default replace - quote it in YAML, `separator`, `max_edits_per_message`, `max_message_chars`); `chat.read_receipts` (default true)
- `stt`: voice transcription (`enabled`, `retention_hours` default 1h); `stt.alternate`: second backend for `retranscribe_audio` (`enabled`, `base_url`, `language` (required - no auto-detect), `max_concurrent_requests`/`max_concurrent_wait_seconds`; YAML-only)
- `image_gen`: FLUX.2 (`enabled`, `base_url`, `max_concurrent_requests`/`max_concurrent_wait_seconds`; `IMAGEGEN_*` env vars override)
- `tts`: `enabled`, `default_language`, one engine per language under `engines` (`base_url`/`voice`/`max_concurrent_requests`/`max_concurrent_wait_seconds`); `TTS_ENABLED`/`TTS_BASE_URL`/`TTS_TIMEOUT` env vars override the `default_language` engine only
- `consent`: `timeout_minutes` (default 120, chat) / `background_timeout_minutes` (default 720); hot-reloadable, read per poll
- `webui`: `enabled` (default false), `api_host`/`api_port` (127.0.0.1:3003), `code_ttl_seconds` 300, `token_expiry_days` 90 (0 = never), `max_upload_mb` 50, `editor_max_kb` 1024, `hidden_paths` [browser, memories], `text_extensions`, `code_requests_per_number_per_hour` 5 / `code_requests_per_ip_per_hour` 20; hot-reloadable except `enabled`/`api_host`/`api_port`
- `voice_calls` (**experimental**, default off, restart required): `enabled`, `mode` (agent/parrot), `allowed_tiers`, VAD/barge-in/hangup tuning; see `doc/ARCHITECTURE.md` "Voice calls"

`config/webinterface.yaml` (webui container, git-ignored): `http`/`https` enable+port, `https.cert_file`/`key_file` (default `config/ssl/`), `https.self_signed.{hostnames,days}`, `api_upstream`, `max_upload_mb` (must be >= `webui.max_upload_mb`). Prompt text is NOT in agent.yaml: see `config/prompts/`. Full schema: `doc/ARCHITECTURE.md`.

## Pointers

- Architecture: `doc/ARCHITECTURE.md` (web interface: "7. webui Container", voice calls: "8. Voice calls")
- Known issues / open bugs: `doc/ISSUES.md`
- Debug logging: `doc/DEBUGGING.md`; the live debug log file is set by `debug_logging.file` in `config/agent.yaml` (code default: container-local `/logs/debug-agent.json`)
- Development process: `process/DEVELOPMENT_PROCESS.md`
- Mock Signal testing: `process/TESTING_WITH_MOCK_SIGNAL_API.md`
- Prompts: `config/prompts/README.md`
