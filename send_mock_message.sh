#!/bin/bash
# Mock Signal message sender for testing
# Usage: send_mock_message [options] <source-number> <message>
# Options:
#   -t timeout       Timeout in seconds (default: 120)
#   --channel        Channel ID (default: 1:1 chat)
#   -n count         Number of messages to wait for (default: 1, 0 = until timeout)
#   -a fixture       Filename in MOCK_ATTACHMENTS_DIR (default: tests/fixtures) to attach.
#                    Repeatable. The filename is also used as the Signal attachment id,
#                    so the mock API can serve it back from MOCK_ATTACHMENTS_DIR.
#   --vote ts idx    Instead of a message, vote option <idx> (0-based) on the poll
#                    with timestamp <ts> (see "Poll created: ... ts=<ts>" in the mock
#                    log). idx "-" removes the vote. 1:1 only (group polls go to
#                    real Signal, never the mock).

set -e

TIMEOUT=120
CHANNEL="1:1"
COUNT=1
SOURCE=""
MESSAGE=""
ATTACHMENTS=()
VOTE_TS=""
VOTE_IDX=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -t)
            TIMEOUT="$2"
            shift 2
            ;;
        --channel)
            CHANNEL="$2"
            shift 2
            ;;
        -n)
            COUNT="$2"
            shift 2
            ;;
        -a)
            ATTACHMENTS+=("$2")
            shift 2
            ;;
        --vote)
            VOTE_TS="$2"
            VOTE_IDX="$3"
            shift 3
            ;;
        -*)
            echo "Unknown option: $1"
            exit 1
            ;;
        *)
            if [[ -z "$SOURCE" ]]; then
                SOURCE="$1"
            elif [[ -z "$MESSAGE" ]]; then
                MESSAGE="$1"
            else
                echo "Too many arguments"
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$SOURCE" ]]; then
    echo "Usage: send_mock_message [-t timeout] [--channel channel-id] [-n count] [-a fixture]... <source-number> <message>"
    echo "       send_mock_message --vote <poll-ts> <index|-> <source-number>"
    exit 1
fi

API_URL="${API_URL:-http://localhost:8081}"

if [[ -n "$VOTE_TS" ]]; then
    if ! [[ "$VOTE_TS" =~ ^[0-9]+$ ]] || ! [[ "$VOTE_IDX" =~ ^([0-9]+|-)$ ]]; then
        echo "Usage: --vote <poll-timestamp> <option-index|->"
        exit 1
    fi
    if [[ "$VOTE_IDX" == "-" ]]; then
        INDEXES="[]"
    else
        INDEXES="[$VOTE_IDX]"
    fi
    VOTE_PAYLOAD="{\"source\": \"$SOURCE\", \"target_ts\": $VOTE_TS, \"option_indexes\": $INDEXES}"
    echo "Injecting vote from $SOURCE: poll $VOTE_TS option(s) $INDEXES"
    RESPONSE=$(curl -s -X POST "$API_URL/_mock/inject_vote" \
        -H "Content-Type: application/json" \
        -d "$VOTE_PAYLOAD")
    echo "Response: $RESPONSE"
    exit 0
fi

# Build attachments JSON array. id == filename so the mock can echo it back
# via /v1/attachments/<id>.
ATTACHMENTS_JSON=""
if [[ ${#ATTACHMENTS[@]} -gt 0 ]]; then
    parts=""
    for fn in "${ATTACHMENTS[@]}"; do
        ext="${fn##*.}"
        ext_lower="${ext,,}"
        case "$ext_lower" in
            aac) ctype="audio/aac" ;;
            m4a) ctype="audio/mp4" ;;
            mp3) ctype="audio/mpeg" ;;
            wav) ctype="audio/wav" ;;
            jpg|jpeg) ctype="image/jpeg" ;;
            png) ctype="image/png" ;;
            *) ctype="application/octet-stream" ;;
        esac
        [[ -n "$parts" ]] && parts="$parts,"
        parts="$parts{\"id\":\"$fn\",\"contentType\":\"$ctype\",\"filename\":\"$fn\"}"
    done
    ATTACHMENTS_JSON=",\"attachments\":[$parts]"
fi

PAYLOAD="{\"source\": \"$SOURCE\", \"destination\": \"+491234567890\", \"message\": \"$MESSAGE\"$ATTACHMENTS_JSON}"

echo "Injecting message from $SOURCE: $MESSAGE"
if [[ ${#ATTACHMENTS[@]} -gt 0 ]]; then
    echo "Attachments: ${ATTACHMENTS[*]}"
fi

RESPONSE=$(curl -s -X POST "$API_URL/_mock/inject" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

echo "Response: $RESPONSE"