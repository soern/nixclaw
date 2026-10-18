#!/usr/bin/env python3
"""Mock Signal API for testing - simulates signal-cli-rest-api."""

import argparse
import asyncio
import logging
import os
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

from aiohttp import web

LOG = logging.getLogger(__name__)

SIGNAL_PHONE_NUMBER = "+491234567890"
DEFAULT_WEBHOOK_URL = os.environ.get("MOCK_RECEIVE_WEBHOOK_URL", "http://host.docker.internal:3002/webhook/signal")
MESSAGES = []

# Directory containing test fixtures (audio, images, etc.) served by
# handle_attachments. Mounted ro into the mock-signal-api container at
# /app/fixtures via docker-compose.test.yml. Resolved at startup so a
# missing dir doesn't break the import path.
MOCK_ATTACHMENTS_DIR = Path(
    os.environ.get("MOCK_ATTACHMENTS_DIR", "/app/fixtures")
).resolve()


_last_send_timestamp = 0


def _next_send_timestamp() -> int:
    """Strictly increasing ms timestamp: every send gets a unique id, so
    single-message mode edits (edit_timestamp) can target it."""
    global _last_send_timestamp
    _last_send_timestamp = max(int(datetime.now().timestamp() * 1000), _last_send_timestamp + 1)
    return _last_send_timestamp


async def handle_send(request):
    """Handle POST /v2/send - accept outgoing messages (log them).

    A body with edit_timestamp is an edit of an earlier message: logged as
    "Edit received for <ts>" instead of "Reply received", so e2e tests can
    count new messages vs edits. The printed message lines are the same.
    """
    try:
        payload = await request.json()
        message = payload.get("message", "")
        recipients = payload.get("recipients", [])
        edit_timestamp = payload.get("edit_timestamp")
        print(f"\n[MOCK RECEIVED REPLY] To: {recipients}")
        print(f"[MOCK RECEIVED REPLY] Message: {message}\n")
        if edit_timestamp is not None:
            LOG.info(f"[MOCK API] Edit received for {edit_timestamp} to {recipients}: {message[:50]}...")
        else:
            LOG.info(f"[MOCK API] Reply received for {recipients}: {message[:50]}...")
        atts = payload.get("base64_attachments") or []
        if atts:
            LOG.info(f"[MOCK API] ... with {len(atts)} attachment(s): {[a[:60] for a in atts]}")
        return web.json_response({"status": "ok", "timestamp": _next_send_timestamp()})
    except Exception as e:
        LOG.error(f"Error handling send: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def handle_health(request):
    """Handle GET /v1/health."""
    return web.json_response({"status": "ok"})


async def handle_typing_indicator(request):
    """Handle PUT/DELETE /v1/typing-indicator."""
    return web.json_response({"status": "ok"})


async def handle_receipt(request):
    """Handle POST /v1/receipts/{number} - log the read receipt, 204 like the real API."""
    try:
        payload = await request.json()
        LOG.info(
            f"[MOCK API] Read receipt received for {payload.get('timestamp')} "
            f"from {payload.get('recipient')}"
        )
        return web.Response(status=204)
    except Exception as e:
        LOG.error(f"Error handling receipt: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)


async def handle_poll_create(request):
    """Handle POST /v1/polls/{number} - log the poll, return its timestamp
    as a string like the real API."""
    try:
        payload = await request.json()
        ts = _next_send_timestamp()
        LOG.info(
            f"[MOCK API] Poll created: {payload.get('question')} "
            f"answers={payload.get('answers')} ts={ts} to {payload.get('recipient')}"
        )
        return web.json_response({"timestamp": str(ts)}, status=201)
    except Exception as e:
        LOG.error(f"Error handling poll create: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)


async def handle_remote_delete(request):
    """Handle DELETE /v1/remote-delete/{number} - log the delete."""
    try:
        payload = await request.json()
        LOG.info(
            f"[MOCK API] Remote delete received for {payload.get('timestamp')} "
            f"to {payload.get('recipient')}"
        )
        return web.json_response({"timestamp": str(_next_send_timestamp())}, status=201)
    except Exception as e:
        LOG.error(f"Error handling remote delete: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)


async def handle_mock_inject_vote(request):
    """Inject a 1:1 poll vote: {"source", "target_ts", "option_indexes"}.

    No group votes: group polls go to the real Signal API (only +0000*
    recipients route to the mock), so a mock group vote would be a footgun.

    Delivered to nixclaw as an envelope with dataMessage.pollVote (shape per
    signal-cli-rest-api receive.PollVote); the voter is the envelope source.
    An empty option_indexes list means "vote removed".
    """
    try:
        payload = await request.json()
        source = payload.get("source")
        target_ts = payload.get("target_ts")
        option_indexes = payload.get("option_indexes", [])
        if (
            not source
            or not isinstance(target_ts, int)
            or isinstance(target_ts, bool)
            or not isinstance(option_indexes, list)
        ):
            return web.json_response(
                {"status": "error", "message": "source, int target_ts and list option_indexes required"},
                status=400,
            )
        data_message = {
            "message": None,
            "pollVote": {
                "author": SIGNAL_PHONE_NUMBER,
                "authorNumber": SIGNAL_PHONE_NUMBER,
                "targetSentTimestamp": target_ts,
                "optionIndexes": option_indexes,
                "voteCount": int(payload.get("vote_count", 1)),
            },
        }
        envelope = {
            "source": source,
            "sourceNumber": source,
            "sourceName": payload.get("source_name", "Mock User"),
            "timestamp": _next_send_timestamp(),
            "dataMessage": data_message,
        }
        LOG.info(
            f"[MOCK API] Vote queued from {source}: target_ts={target_ts} "
            f"option_indexes={option_indexes}"
        )
        asyncio.create_task(post_envelope(envelope))
        return web.json_response({"status": "ok"})
    except Exception as e:
        LOG.error(f"Error injecting vote: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


_ATTACHMENT_CONTENT_TYPES = {
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


async def handle_attachments(request):
    """Handle GET /v1/attachments/{attachment_id}.

    Serves files from MOCK_ATTACHMENTS_DIR by attachment_id (= basename).
    Used by tests to inject real audio / image fixtures into the agent's
    attachment download path without needing a real Signal server.
    """
    attachment_id = request.match_info.get("attachment_id", "")
    if not attachment_id or "/" in attachment_id or ".." in attachment_id:
        return web.Response(status=400, text="bad attachment id")

    if not MOCK_ATTACHMENTS_DIR.exists():
        LOG.warning(f"MOCK_ATTACHMENTS_DIR does not exist: {MOCK_ATTACHMENTS_DIR}")
        return web.Response(status=404)

    target = (MOCK_ATTACHMENTS_DIR / attachment_id).resolve()
    try:
        target.relative_to(MOCK_ATTACHMENTS_DIR)
    except ValueError:
        return web.Response(status=400, text="path traversal blocked")
    if not target.exists() or not target.is_file():
        LOG.warning(f"Attachment not found: {attachment_id}")
        return web.Response(status=404)

    ext = target.suffix.lower()
    content_type = _ATTACHMENT_CONTENT_TYPES.get(ext, "application/octet-stream")
    return web.Response(body=target.read_bytes(), content_type=content_type)


async def handle_mock_inject(request):
    """Handle internal mock endpoint to inject messages."""
    try:
        payload = await request.json()
        source = payload.get("source")
        destination = payload.get("destination", SIGNAL_PHONE_NUMBER)
        message = payload.get("message", "")

        if not source:
            return web.json_response(
                {"status": "error", "message": "source required"},
                status=400,
            )
        if not message and not payload.get("attachments"):
            return web.json_response(
                {"status": "error", "message": "message or attachments required"},
                status=400,
            )

        event = {
            "source": source,
            "destination": destination,
            "message": message,
            "timestamp": int(datetime.now().timestamp() * 1000),
            "group_id": None,
            "attachments": payload.get("attachments", []),
            "mentions": payload.get("mentions", []),
        }

        MESSAGES.append(event)
        LOG.info(
            f"[MOCK API] Message queued from {source}: "
            f"text={message[:50]!r} attachments={len(event['attachments'])}"
        )

        asyncio.create_task(forward_to_webhook(event))

        return web.json_response({"status": "ok", "message_uuid": str(uuid.uuid4())})
    except Exception as e:
        LOG.error(f"Error injecting message: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def forward_to_webhook(event):
    """Forward event to the nixclaw webhook."""
    await post_envelope({
        "sourceNumber": event["source"],
        "destinationNumber": event["destination"],
        "timestamp": event["timestamp"],
        "dataMessage": {
            "message": event["message"],
            "attachments": event.get("attachments", []),
            "mentions": [{"number": m} for m in event.get("mentions", [])],
        },
        "groupInfo": [],
    })


async def post_envelope(envelope):
    """POST one envelope to the nixclaw webhook."""
    import httpx
    try:
        webhook_payload = {"envelope": envelope}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                _webhook_url,
                json=webhook_payload,
                headers={"Content-Type": "application/json"},
            )
            LOG.info(f"[MOCK API] Forwarded to webhook, status: {response.status_code}")
    except Exception as e:
        LOG.error(f"[MOCK API] Failed to forward to webhook: {e}")


# Fixed group list for GET /v1/groups/{number} (web interface membership
# tests). Shape per signal-cli-rest-api: id = "group." + base64(internal_id).
# Read-side only: group sends route to the REAL signal-api (only +0000*
# recipients go to the mock), so never send into these groups.
MOCK_GROUPS = [
    {
        "id": "group.bW9ja0dyb3VwQUJDZGVmMDE=",
        "internal_id": "mockGroupABCdef01",
        "name": "Mock Family",
        "members": ["+000000", "+000001"],
        "blocked": False,
        "pending_invites": [],
        "pending_requests": [],
        "invite_link": "",
        "admins": ["+000000"],
    },
    {
        "id": "group.bW9ja0dyb3VwWFlaZGVmMDI=",
        "internal_id": "mockGroupXYZdef02",
        "name": "Mock Strangers",
        "members": ["+000002"],
        "blocked": False,
        "pending_invites": [],
        "pending_requests": [],
        "invite_link": "",
        "admins": ["+000002"],
    },
]


async def handle_groups(request):
    """Handle GET /v1/groups/{number} - fixed mock group list."""
    LOG.info(f"[MOCK API] Groups listed for {request.match_info.get('number')}")
    return web.json_response(MOCK_GROUPS)


def create_app():
    """Create the aiohttp application."""
    # aiohttp's default client_max_size is 1 MB, which rejects the
    # generated-image PNGs (~1.5 MB) that the generate_image E2E flow
    # uploads via /v2/send. Real Signal handles arbitrarily large
    # attachments, so the mock should too.
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.router.add_post("/v2/send", handle_send)
    app.router.add_get("/v1/health", handle_health)
    app.router.add_put("/v1/typing-indicator/{number}", handle_typing_indicator)
    app.router.add_delete("/v1/typing-indicator/{number}", handle_typing_indicator)
    app.router.add_get("/v1/attachments/{attachment_id}", handle_attachments)
    app.router.add_post("/v1/receipts/{number}", handle_receipt)
    app.router.add_post("/_mock/inject", handle_mock_inject)
    app.router.add_post("/v1/polls/{number}", handle_poll_create)
    app.router.add_delete("/v1/remote-delete/{number}", handle_remote_delete)
    app.router.add_post("/_mock/inject_vote", handle_mock_inject_vote)
    app.router.add_get("/v1/groups/{number}", handle_groups)
    return app


_webhook_url = DEFAULT_WEBHOOK_URL


def main():
    """Main entry point."""
    global _webhook_url
    parser = argparse.ArgumentParser(description="Mock Signal API")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=DEFAULT_WEBHOOK_URL,
        help="Webhook URL to forward messages (env: MOCK_RECEIVE_WEBHOOK_URL)",
    )
    args = parser.parse_args()
    _webhook_url = args.webhook_url
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    app = create_app()
    
    def signal_handler(sig, frame):
        LOG.info("Shutting down...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    LOG.info(f"Starting Mock Signal API on port {args.port}")
    LOG.info(f"Webhook URL: {_webhook_url}")
    web.run_app(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
