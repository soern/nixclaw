#!/bin/bash
# LLM-driven e2e scenarios: the agent vs. the mock websites.
#
# Prerequisites:
#   docker-compose -f docker-compose.yml -f docker-compose.test.yml up -d
#   (browser container serves the mock sites on :8932; vLLM backend reachable)
#   config.yaml debug logging enabled (writes data/debug-agent.json)
#
# Sends prompts through the mock Signal API as KNOWN user +000001, then polls
# data/debug-agent.json (NOT `docker logs` text, which is unreliable across
# concurrent turns) for a fresh outgoing_message on channel +000001, and
# checks it for the per-site evidence token (tests/fixtures/mock_sites/README.md).
#
# Scenarios run strictly sequentially: each waits for its own reply before the
# next is sent. Concurrent messages to the same channel share one browser
# page/context (chat turns share the channel's browser scope, see
# doc/ARCHITECTURE.md "Concurrency Model"), so firing them back-to-back corrupts results.
#
# Usage: tests/e2e_mock_sites.sh [scenario ...]   (default: all, sequential)
#
# NOTE: only +0000* numbers may be used here - real-looking numbers would be
# routed to live Signal.

cd "$(dirname "$0")/.."
USER_NUM="+000001"
DEBUG_LOG="data/debug-agent.json"
export API_URL="${API_URL:-http://localhost:8081}"

PASS=0; FAIL=0; FAILED_NAMES=()

# Byte offset in $DEBUG_LOG at scenario start; only new lines are inspected.
new_outgoing_messages() {
    local since_offset="$1"
    tail -c "+$((since_offset + 1))" "$DEBUG_LOG" 2>/dev/null | python3 -c '
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get("type") == "outgoing_message" and e.get("channel") == "'"$USER_NUM"'":
        print(json.dumps(e.get("data", {}).get("message", "")))
'
}

run_scenario() {
    local name="$1" timeout="$2" prompt="$3" expect_re="$4"
    echo ""
    echo "=== [$name] ==="
    local start_offset
    start_offset=$(wc -c < "$DEBUG_LOG" 2>/dev/null || echo 0)
    ./send_mock_message.sh "$USER_NUM" "$prompt" >/dev/null
    echo "sent: $prompt"

    local elapsed=0 matched="" all_msgs=""
    while (( elapsed < timeout )); do
        sleep 5; elapsed=$((elapsed+5))
        all_msgs=$(new_outgoing_messages "$start_offset")
        if [[ -n "$all_msgs" ]] && echo "$all_msgs" | grep -qiE "$expect_re"; then
            matched=1
            break
        fi
    done

    if [[ -n "$matched" ]]; then
        echo "reply (matched after ${elapsed}s):"
        echo "$all_msgs" | tail -3
        echo "--- $name: PASS"
        PASS=$((PASS+1))
    else
        echo "replies within ${timeout}s (none matched /$expect_re/):"
        echo "$all_msgs"
        echo "--- $name: FAIL"
        FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    fi

    # Let the turn fully settle (typing indicator off, tool loop closed)
    # before sending the next scenario - shared per-channel browser page.
    sleep 5
}

want() { local n="$1"; shift; [[ $# -eq 0 || " $* " == *" $n "* ]]; }
ARGS=("$@")

want captcha_grid "${ARGS[@]}" && run_scenario captcha_grid 300 \
    "Please open http://localhost:8932/captcha-grid and tell me the one-time pickup code. If a captcha appears, solve it yourself." \
    "GRID-SOLVED-7391"

want captcha_text "${ARGS[@]}" && run_scenario captcha_text 300 \
    "Please open http://localhost:8932/captcha-text and tell me my support ticket number. If a captcha appears, solve it yourself." \
    "TEXT-SOLVED-2846"

want cookie_modal "${ARGS[@]}" && run_scenario cookie_modal 180 \
    "Read the news article at http://localhost:8932/cookie-modal and tell me until when the night ferry pilot project is funded." \
    "2027|FERRY-62-NIGHT"

want cookie_nested "${ARGS[@]}" && run_scenario cookie_nested 180 \
    "Open http://localhost:8932/cookie-nested and tell me the verdict score of the tablet review. Dismiss any consent dialogs." \
    "8[.,]7|SOLARIS-87"

want slow_ajax "${ARGS[@]}" && run_scenario slow_ajax 240 \
    "Open http://localhost:8932/slow-ajax and list the concerts shown there including the result code. The page may need a moment to load its results." \
    "SLOW-AJAX-OK-9174"

want slow_server "${ARGS[@]}" && run_scenario slow_server 240 \
    "What is the weekend forecast published at http://localhost:8932/slow-server? Include the bulletin code." \
    "SLOW-SRV-2421"

want quirk_404 "${ARGS[@]}" && run_scenario quirk_404 180 \
    "Open http://localhost:8932/quirk-404 and tell me what you find there." \
    "404|not found|nicht gefunden"

want quirk_paywall "${ARGS[@]}" && run_scenario quirk_paywall 180 \
    "Get me the exact container volume growth figure from the article at http://localhost:8932/quirk-paywall" \
    "paywall|subscri|premium|abo"

want quirk_jsonly "${ARGS[@]}" && run_scenario quirk_jsonly 180 \
    "Which trains depart according to http://localhost:8932/quirk-jsonly? Include the feed ID." \
    "JSONLY-1432|Bremen"

echo ""
echo "==============================="
echo "PASS: $PASS  FAIL: $FAIL"
[[ $FAIL -gt 0 ]] && echo "Failed: ${FAILED_NAMES[*]}"
exit $((FAIL > 0))
