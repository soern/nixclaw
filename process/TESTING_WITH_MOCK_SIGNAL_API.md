# Testing Agent with Mock Signal API

## Overview

The mock Signal API allows testing the nixclaw agent with crafted prompts without affecting live Signal traffic.

## Architecture

```
LIVE: Real Signal User → signal-api:8080 → nixclaw → signal-api:8080 → Real User
TEST:  Mock Client → mock-signal-api:8081 → nixclaw → mock-signal-api:8080 → Mock Client (prints reply)
```

## Important: Use Test Numbers

**Test numbers are in `config/agent.yaml`:**
- `+000000` - privileged tier (full tools access)
- `+000001` - known tier (limited tools access)
- `+000002` - unknown tier (not in config -> no tools access)

**Why test numbers matter:**
- Replies to numbers starting with `+0000` are routed to the mock API
- Replies to all other numbers go to the real Signal API
- Using test numbers lets you see the agent's responses printed in `docker logs nixclaw-mock-signal-api`
- **IMPORTANT**: use only numbers starting with `+0000` during testing, since other numbers will get routed to the signal network. contacting other numbers may result in the signal account getting banned! NEVER use numbers with actual country prefix for testing.

## Testing Steps

### 1. Start Mock Signal API and Rebuild nixclaw (Important!)

The mock-signal-api service is defined in `docker-compose.test.yml`. After making code changes, rebuild and restart the nixclaw container:

```bash
# from the repository root
docker-compose -f docker-compose.yml -f docker-compose.test.yml build nixclaw
docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d nixclaw
```

Note: The mock-signal-api container should already be running. If not, start it with:
```bash
docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d mock-signal-api
```

### 2. Inject Test Messages

```bash
# Test from privileged number (+000000) - full tools access
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "Your test prompt here"

# Test from known number (+000001) - limited tools access
API_URL=http://localhost:8081 ./send_mock_message.sh +000001 "Your test prompt here"
```

### 3. View Agent Replies

```bash
docker logs nixclaw-mock-signal-api
```

Look for lines like:
```
[MOCK API] Reply received for ['+000000']: Your agent's response here...
```

In single-message mode (`chat.single_message.mode`, default `replace`), later texts of the same turn edit the previous message and are logged as `[MOCK API] Edit received for <ts> to [...]` instead; the `[MOCK RECEIVED REPLY] Message:` lines are printed for both.

When a turn ends, the agent marks the messages it handled as read (`chat.read_receipts`), logged as `[MOCK API] Read receipt received for <ts> from <recipient>` after the turn's last reply/edit.

Polls the agent posts are logged as `[MOCK API] Poll created: <question> answers=[...] ts=<ts> to <recipient>` (the mock returns `{"timestamp": "<ts>"}` like the real API); deleting one logs `[MOCK API] Remote delete received for <ts> to <recipient>`. To vote on a poll, use its `ts`:
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh --vote <ts> 0 +000000   # option 0 (0-based)
API_URL=http://localhost:8081 ./send_mock_message.sh --vote <ts> - +000000   # remove the vote
# raw: POST /_mock/inject_vote {"source": "+000000", "target_ts": <ts>, "option_indexes": [0]}
```
The vote reaches nixclaw as a webhook envelope with `dataMessage.pollVote` (voter = envelope source) and is logged as `Poll vote from <voter> in <channel>: target_ts=... options=[...]`. Votes never become chat messages. Only 1:1 votes can be injected: like replies, only `+0000*` 1:1 recipients route polls/deletes to the mock, while group polls go to real Signal (test those live).

### 4. View Complete Conversation (User + Agent)

```bash
docker logs nixclaw --since 5m
```

Look for:
- `Incoming message from +000000: ...` - User message received (`Webhook request without valid secret rejected` = `SIGNAL_WEBHOOK_SECRET` mismatch)
- `POST http://gx10-811e.lan:11434/api/chat "HTTP/1.1 200 OK"` - LLM call
- `POST http://mock-signal-api:8080/v2/send "HTTP/1.1 200 OK"` - Agent response sent

### 5. Check Tool Execution

```bash
docker logs nixclaw --since 5m | grep -E "(web_search|browser_navigate|bash|file)"
```

### 6. Web interface

Prerequisites: `webui.enabled: true` in `config/agent.yaml`, nixclaw restarted, `http.enabled: true` in `config/webinterface.yaml` (copy from `.example`; plain HTTP is off by default), and `docker-compose up -d --build webui` (Caddy on `:8090` HTTP / `:8443` HTTPS, proxies `/api/*` to `127.0.0.1:3003`). The mock answers `GET /v1/groups/{number}` with fixed groups ("Mock Family": `+000000`, `+000001`; "Mock Strangers": `+000002`). Only `+0000*` members count. Do not send messages into these groups: group sends go to the real signal-api.

Log in with curl (code request → read the mock log → verify → cookie):
```bash
B=http://localhost:8090; H='X-NixClaw: 1'; J='Content-Type: application/json'
curl -s -X POST $B/api/auth/request -H "$H" -H "$J" -d '{"number":"+000000"}'   # always {"ok": true}
docker logs nixclaw-mock-signal-api --since 1m | grep "login code"            # Reply received for ['+000000']: Your web interface login code: 123456 ...
curl -s -c /tmp/nc.jar -X POST $B/api/auth/verify -H "$H" -H "$J" \
  -d '{"number":"+000000","code":"123456","endpoint_name":"curl test"}'
curl -s -b /tmp/nc.jar $B/api/me
curl -s -b /tmp/nc.jar $B/api/channels                                          # cid = base64url(channel_id)
curl -s -b /tmp/nc.jar -X POST $B/api/channels/<cid>/messages -H "$H" -F text=hello -F client_id=$(uuidgen)
curl -s -N -b /tmp/nc.jar $B/api/events                                         # SSE stream
```
- Every POST/PUT/DELETE needs the header `X-NixClaw: 1`; without it you get 403 `missing X-NixClaw header`. Requests without a valid `nixclaw_session` cookie get 401.
- Codes are valid for `webui.code_ttl_seconds` (300) and allow 5 attempts. Requests are rate limited per number (5/h) and per IP (20/h); a nixclaw restart resets the limits and drops pending codes.
- A web message is mirrored to Signal as `🌐 <text>`, which the mock logs as `Reply received for ['+000000']: 🌐 hello`. The agent's reply arrives on the SSE stream and in the mock log.
- `/status` and `/deauth <n>` sent from the mock (1:1) list and remove the logins. In the web composer the client handles them through `/api/endpoints`.
- From the headless browser container (Playwright, e.g. `tests/` scripts connecting to `ws://localhost:8931`), the UI is reachable through the bridge gateway at `http://172.18.60.1:8090`, because webui runs with `network_mode: host`. Plain HTTP on a non-localhost origin is not a secure context, so the voice-record button is hidden there.

## Common Issues

### 400 Bad Request on Reply

If you see `HTTP/1.1 400 Bad Request` on the reply, you're likely NOT using a test number (starting with `+0000`) as the recipient. The mock API only handles replies to `+0000*` numbers.

### Message Not Processed

If a message doesn't trigger a response:
1. Check `docker logs nixclaw --since 30s` for processing
2. Verify the message was forwarded: `docker logs nixclaw-mock-signal-api --since 30s`
3. Ensure you're using the correct test number as the source

### Code Changes Not Applied

If you see old behavior after making code changes:
1. Check `docker-compose -f docker-compose.yml -f docker-compose.test.yml build nixclaw` was run
2. Check `docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d nixclaw` was run after build
3. Verify container is using new image: `docker inspect nixclaw`

## Cleanup After Testing

**Important:** the mock stack shares the *same* persistent `/data/messages.db` as your live instance — there is no separate test database, only the `+0000*` channel-id convention. Any `schedule_cron` call made during testing (e.g. testing reminder/scheduling behavior, "remind me tomorrow at 9am") creates a real row that the live scheduler will keep firing forever, hourly/daily/weekly, indefinitely — it does NOT get cleaned up when the mock stack is stopped or the container restarts.

**If your test scenarios touched scheduling/reminders at all**, cancel whatever you created before wrapping up:

```bash
docker exec nixclaw python3 -c "
from src.nixclaw_agent.db import operations as db_ops
count = 0
for ch in ('+000000', '+000001', '+000002'):
    for job in db_ops.get_channel_cronjobs(ch):
        if job.cancelled or job.status not in ('pending', 'running'):
            continue
        if job.is_recurring:
            db_ops.update_cronjob(job.id, cancelled=True)
        else:
            db_ops.delete_cronjob(job.id)
        count += 1
        print('cancelled', job.id, ch, job.prompt[:40].replace(chr(10), ' '))
print(f'{count} job(s) cancelled')
"
```

This mirrors `cancel_cronjob`'s own semantics (recurring jobs get `cancelled=1`, one-time jobs get deleted outright) and only ever touches `+0000*` mock channels — never real user channels.

## Quick Command Reference

```bash
# Send test message
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "test message"

# View recent conversation
docker logs nixclaw --since 5m | grep -E "(webhook|chat|send)"

# View agent responses only
docker logs nixclaw-mock-signal-api --since 5m

# Check tool usage
docker logs nixclaw --since 5m | grep -E "(web_search|browser_navigate)"

# Cancel any cronjobs left over from testing (see Cleanup After Testing above)
```

## Test Scenarios

### Test 0: Capability Verification (MANDATORY for ALL changes)

Before testing specific functionality, verify the agent properly exposes its capabilities:

**Purpose:** Ensure changes are properly integrated and visible to the LLM, not just functional in isolation.

**Test from PRIVILEGED user (+000000):**
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "list all your available tools"
docker logs nixclaw-mock-signal-api --since 30s | grep -E "(tool|available)"
```

**What to look for:**
- New tools should appear in the list
- Fixed tools should work correctly
- Removed capabilities should be absent
- Permission changes should be reflected

**This test catches:**
- Tools registered but not in TOOL_PERMISSIONS (not exposed to LLM)
- Features implemented but not wired to permissions
- Regressions where a tool stops being exposed

**For KNOWN users (+000001):**
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh +000001 "list all your available tools"
```

**For UNKNOWN users (+000002):**
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh +000002 "list all your available tools"
# Expected: Very limited or no tools available
```

### Test 1: Simple Response
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "hello"
```
Expected: "Hi!" response with no tool calls

### Test 2: Tool Execution (web_search)
```bash
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "search for Schulferien in Niedersachsen 2026"
```
Expected: web_search tool called, agent provides dates

### Test 3: History Independence (Critical Bug Fix Verification)
```bash
# First: Send a tool command
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "search for Schulferien in Niedersachsen 2026"

# Wait for completion, then send unrelated message
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "hello"
```
Expected for "hello": Simple "Hi!" response with NO repeated web fetch

This test verifies that previous tool executions don't repeat when new unrelated messages arrive.

### Test 4: Negative Tests (Security check)
For security, we need to ensure that unknown users have no access to tools.
We also need to verify that access to privileged tools is only granted to privileged users.

- Run 3 tests to invoke a prompt that would trigger tool execution from an unknown user `+000002`.
  - Expected: MUST NOT CALL ANY TOOL
- Run 3 tests to invoke a prompt that would trigger bash tool execution from a known (but non-privileged) user `+000001`.
  - Expected: MUST NOT EXECUTE BASH TOOL

### Test 5: Debug Logging (Issue Detection)

This test verifies the debug logging feature described in `doc/DEBUGGING.md`.

**Prerequisites:**
- `debug_logging.mode` must be set to `"auto"` in `config/agent.yaml`

**Test the __ISSUE_DETECTED__ keyword:**

```bash
# Ask the LLM to include the issue detection marker
API_URL=http://localhost:8081 ./send_mock_message.sh +000000 "Please include __ISSUE_DETECTED__ in your response"
```

**Expected behavior:**
1. The LLM includes `__ISSUE_DETECTED__` in its response
2. The system detects the marker and flushes the debug buffer
3. The marker is stripped from the user-visible response
4. The debug log file (path from `debug_logging.file` in `config/agent.yaml`; the code default is `/logs/debug-agent.json`, container-local — use `/data/debug-agent.json` for host visibility) contains a `buffer_flush` entry with `"trigger": "llm_reported_issue"`

**Verification:**
```bash
# Check that flush was triggered
tail -20 /data/debug-agent.json | grep buffer_flush  # replace with your debug_logging.file path

# Should see: {"type": "buffer_flush", "trigger": "llm_reported_issue", ...}
```

**Check the response does NOT contain the marker:**
```bash
docker logs nixclaw-mock-signal-api --since 30s | grep ISSUE
# Should NOT find __ISSUE_DETECTED__ in the output
```

For more details on debug logging, see `doc/DEBUGGING.md`.

