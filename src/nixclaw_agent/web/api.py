"""HTTP API of the web interface (aiohttp, behind the webui Caddy container).

Started by main.py only when ``webui.enabled``. Listens on
``webui.api_host:api_port`` (default 127.0.0.1:3003); Caddy proxies /api/*
here and sets X-Forwarded-For/-Proto, which are trusted only from a loopback
peer. Every mutating request needs the header ``X-NixClaw: 1`` (CSRF belt on
top of the SameSite=Strict cookie). See the API contract in the web
interface spec / doc/ARCHITECTURE.md.
"""

import asyncio
import ipaddress
import json
import logging
import mimetypes
import os
import tempfile
import urllib.parse
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol

from aiohttp import BodyPartReader, hdrs, web

from ..auth import AuthManager
from ..config import config
from ..db import operations as db
from ..signal import SignalClient, set_outbound_listener
from ..types import Attachment, PollVote
from . import files
from .auth import MAX_ENDPOINT_NAME_CHARS, Session, WebAuth, valid_number
from .bus import ChatBus, attachment_path, attachments_dir, message_json
from .channels import MembershipCache, decode_cid

LOG = logging.getLogger(__name__)

COOKIE_NAME = "nixclaw_session"
CSRF_HEADER = "X-NixClaw"
PUBLIC_PATHS = frozenset({"/api/auth/request", "/api/auth/verify"})
MAX_JSON_BYTES = 16 * 1024
MAX_SETTINGS_BYTES = 64 * 1024
MAX_TEXT_BYTES = 64 * 1024
SETTINGS_FILE = "webinterface.json"
SSE_HEARTBEAT_SECONDS = 25.0
REPLAY_PAGE_SIZE = 500
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200
MAX_CLIENT_IDS_PER_USER = 256
CHUNK_SIZE = 64 * 1024
# Cookie lifetime when tokens never expire (token_expiry_days = 0).
NO_EXPIRY_COOKIE_SECONDS = 10 * 365 * 86400


class AgentHooks(Protocol):
    """What the API needs from core.AgentLoop."""

    def submit_web_message(self, number: str, channel_id: str, text: str,
                           attachments: list[Attachment]) -> int: ...

    async def on_web_poll_vote(self, vote: PollVote) -> None: ...


class PayloadTooLarge(Exception):
    pass


def _err(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _is_loopback(peer: Optional[str]) -> bool:
    try:
        return ipaddress.ip_address(peer or "").is_loopback
    except ValueError:
        return False


def client_ip(request: web.Request) -> str:
    """X-Forwarded-For (first hop) when the TCP peer is loopback (Caddy), else the peer."""
    peer = request.remote or ""
    if _is_loopback(peer):
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer


def is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    return _is_loopback(request.remote) and request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _content_disposition(kind: str, name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_").replace("?", "_")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


def _file_headers(path: Path, download: bool) -> dict[str, str]:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    inline = files.is_inline_type(content_type) and not download
    return {
        hdrs.CONTENT_TYPE: content_type,
        hdrs.CONTENT_DISPOSITION: _content_disposition("inline" if inline else "attachment", path.name),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }


async def _read_limited(request: web.Request, limit: int) -> bytes:
    if request.content_length is not None and request.content_length > limit:
        raise PayloadTooLarge()
    data = bytearray()
    while True:
        chunk = await request.content.read(CHUNK_SIZE)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise PayloadTooLarge()
    return bytes(data)


async def _read_json(request: web.Request, limit: int = MAX_JSON_BYTES) -> Any:
    raw = await _read_limited(request, limit)
    try:
        return json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON"}), content_type="application/json")


async def _read_part_text(part: BodyPartReader, limit: int) -> str:
    data = bytearray()
    while True:
        chunk = await part.read_chunk(CHUNK_SIZE)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise PayloadTooLarge()
    return data.decode("utf-8", errors="replace")


async def _save_part(part: BodyPartReader, directory: Path, name: str, budget: list[int]) -> Path:
    """Stream one multipart file part into `directory` (collision-safe name).

    `budget` is a one-element list of bytes still allowed for this request.
    """
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".upload-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await part.read_chunk(CHUNK_SIZE)
                if not chunk:
                    break
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    raise PayloadTooLarge()
                f.write(chunk)
        target = files.unique_path(directory, name)
        os.replace(tmp, target)
        return target
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or not value.strip().lstrip("-").isdigit():
        return None
    return int(value)


class WebAPI:
    def __init__(self, web_auth: WebAuth, bus: ChatBus, membership: MembershipCache, agent: AgentHooks) -> None:
        self.auth = web_auth
        self.bus = bus
        self.membership = membership
        self.agent = agent
        self._client_ids: dict[str, "OrderedDict[str, int]"] = {}
        self._runner: Optional[web.AppRunner] = None
        self._tasks: set[asyncio.Task] = set()

    # --- app -----------------------------------------------------------------

    def make_app(self) -> web.Application:
        app = web.Application(
            middlewares=[self._middleware],
            client_max_size=(config.webui.max_upload_mb + 1) * 1024 * 1024,
        )
        r = app.router
        r.add_post("/api/auth/request", self.auth_request)
        r.add_post("/api/auth/verify", self.auth_verify)
        r.add_post("/api/auth/logout", self.auth_logout)
        r.add_get("/api/me", self.me)
        r.add_get("/api/endpoints", self.endpoints)
        r.add_delete("/api/endpoints/{id}", self.endpoint_delete)
        r.add_get("/api/settings", self.settings_get)
        r.add_put("/api/settings", self.settings_put)
        r.add_get("/api/channels", self.channels)
        r.add_post("/api/channels/refresh", self.channels_refresh)
        r.add_get("/api/channels/{cid}/messages", self.messages_get)
        r.add_post("/api/channels/{cid}/messages", self.messages_post)
        r.add_post("/api/channels/{cid}/polls/{ts}/vote", self.poll_vote)
        r.add_get("/api/channels/{cid}/attachments/{name}", self.attachment_get)
        r.add_get("/api/events", self.events)
        r.add_get("/api/files/list", self.files_list)
        r.add_get("/api/files/raw", self.files_raw_get)
        r.add_put("/api/files/raw", self.files_raw_put)
        r.add_post("/api/files/upload", self.files_upload)
        r.add_post("/api/files/mkdir", self.files_mkdir)
        r.add_delete("/api/files", self.files_delete)
        return app

    async def start(self, host: str, port: int) -> None:
        self._runner = web.AppRunner(self.make_app())
        await self._runner.setup()
        await web.TCPSite(self._runner, host, port).start()
        LOG.info(f"Web interface API listening on {host}:{port}")
        # Initial membership load in the background (signal-api may be slow).
        task = asyncio.create_task(self.membership.refresh())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self.bus.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _middleware(self, request: web.Request,
                          handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get(CSRF_HEADER) != "1":
            return _err(403, f"missing {CSRF_HEADER} header")
        if request.path not in PUBLIC_PATHS:
            session = self.auth.resolve(request.cookies.get(COOKIE_NAME))
            if session is None:
                return _err(401, "not logged in")
            request["session"] = session
        try:
            response = await handler(request)
        except files.FileAccessError as e:
            return _err(403, str(e) or "path not allowed")
        except (FileNotFoundError, NotADirectoryError):
            return _err(404, "not found")
        except PayloadTooLarge:
            return _err(413, "too large")
        except web.HTTPRequestEntityTooLarge:
            return _err(413, "too large")
        except web.HTTPException:
            raise
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as e:
            LOG.exception(f"Web API error on {request.method} {request.path}: {e}")
            return _err(500, "internal error")
        if not response.prepared:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @staticmethod
    def _session(request: web.Request) -> Session:
        return request["session"]

    # --- auth ------------------------------------------------------------------

    async def auth_request(self, request: web.Request) -> web.Response:
        body = await _read_json(request)
        number = body.get("number") if isinstance(body, dict) else None
        if not valid_number(number):
            return _err(400, "invalid number")
        if not self.auth.request_code(number, client_ip(request)):
            return _err(429, "too many requests, try again later")
        return web.json_response({"ok": True})

    async def auth_verify(self, request: web.Request) -> web.Response:
        body = await _read_json(request)
        if not isinstance(body, dict):
            return _err(400, "invalid request")
        number, code = body.get("number"), body.get("code")
        name = body.get("endpoint_name")
        name = name.strip() if isinstance(name, str) else ""
        if not valid_number(number) or not isinstance(code, str):
            return _err(400, "invalid request")
        if not name or len(name) > MAX_ENDPOINT_NAME_CHARS:
            return _err(400, f"endpoint name must be 1-{MAX_ENDPOINT_NAME_CHARS} characters")
        token = self.auth.verify(number, code, name)
        if token is None:
            return _err(400, "wrong or expired code")
        response = web.json_response({"ok": True, "number": number})
        days = config.webui.token_expiry_days
        response.set_cookie(
            COOKIE_NAME, token, max_age=days * 86400 if days > 0 else NO_EXPIRY_COOKIE_SECONDS,
            path="/", httponly=True, samesite="Strict", secure=is_https(request),
        )
        return response

    async def auth_logout(self, request: web.Request) -> web.Response:
        s = self._session(request)
        self.auth.revoke(s.number, s.endpoint_id)
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def me(self, request: web.Request) -> web.Response:
        s = self._session(request)
        w = config.webui
        return web.json_response({
            "number": s.number,
            "tier": s.tier.value,
            "agent_name": config.agent.name,
            "endpoint": {"id": s.endpoint_id, "name": s.endpoint_name},
            "limits": {
                "max_upload_mb": w.max_upload_mb,
                "editor_max_kb": w.editor_max_kb,
                "text_extensions": list(w.text_extensions),
            },
        })

    async def endpoints(self, request: web.Request) -> web.Response:
        s = self._session(request)
        return web.json_response({"endpoints": self.auth.endpoints(s.number, s.endpoint_id)})

    async def endpoint_delete(self, request: web.Request) -> web.Response:
        s = self._session(request)
        endpoint_id = _parse_int(request.match_info["id"])
        if endpoint_id is None or not self.auth.revoke(s.number, endpoint_id):
            return _err(404, "no such endpoint")
        return web.json_response({"ok": True})

    # --- settings --------------------------------------------------------------

    def _settings_path(self, number: str) -> Path:
        return files.workspace_root(number) / SETTINGS_FILE

    async def settings_get(self, request: web.Request) -> web.Response:
        path = self._settings_path(self._session(request).number)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return web.json_response(data if isinstance(data, dict) else {})

    async def settings_put(self, request: web.Request) -> web.Response:
        raw = await _read_limited(request, MAX_SETTINGS_BYTES)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _err(400, "invalid JSON")
        if not isinstance(data, dict):
            return _err(400, "settings must be a JSON object")
        path = self._settings_path(self._session(request).number)
        path.parent.mkdir(parents=True, exist_ok=True)
        files.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
        return web.json_response({"ok": True})

    # --- channels & messages ---------------------------------------------------

    async def channels(self, request: web.Request) -> web.Response:
        await self.membership.ensure_fresh()
        return web.json_response({"channels": self.membership.channels_for(self._session(request).number)})

    async def channels_refresh(self, request: web.Request) -> web.Response:
        await self.membership.refresh()
        return web.json_response({"channels": self.membership.channels_for(self._session(request).number)})

    async def _channel(self, request: web.Request) -> Optional[str]:
        """channel_id of {cid} if the user may access it, else None."""
        channel_id = decode_cid(request.match_info["cid"])
        if channel_id is None:
            return None
        number = self._session(request).number
        if channel_id == number:
            return channel_id
        await self.membership.ensure_fresh()
        return channel_id if self.membership.is_member(number, channel_id) else None

    async def messages_get(self, request: web.Request) -> web.Response:
        channel_id = await self._channel(request)
        if channel_id is None:
            return _err(403, "channel not allowed")
        before = _parse_int(request.query.get("before"))
        limit = _parse_int(request.query.get("limit")) or DEFAULT_HISTORY_LIMIT
        limit = max(1, min(limit, MAX_HISTORY_LIMIT))
        rows = db.chat_log_history(channel_id, before, limit)
        return web.json_response({"messages": [message_json(r) for r in rows]})

    def _known_client_id(self, number: str, client_id: Optional[str]) -> Optional[int]:
        if not client_id:
            return None
        return self._client_ids.get(number, OrderedDict()).get(client_id)

    def _remember_client_id(self, number: str, client_id: Optional[str], row_id: int) -> None:
        if not client_id:
            return
        ids = self._client_ids.setdefault(number, OrderedDict())
        ids[client_id] = row_id
        while len(ids) > MAX_CLIENT_IDS_PER_USER:
            ids.popitem(last=False)

    def _existing_message(self, row_id: int) -> web.Response:
        row = db.chat_log_get(row_id)
        if row is None:
            return _err(404, "message not found")
        return web.json_response({"message": message_json(row)})

    async def messages_post(self, request: web.Request) -> web.Response:
        channel_id = await self._channel(request)
        if channel_id is None:
            return _err(403, "channel not allowed")
        number = self._session(request).number
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError, KeyError):
            return _err(400, "multipart/form-data expected")
        text = ""
        client_id: Optional[str] = None
        saved: list[tuple[Path, str]] = []
        budget = [config.webui.max_upload_mb * 1024 * 1024]
        directory = attachments_dir(channel_id)
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if not isinstance(part, BodyPartReader):
                    continue
                if part.name == "text":
                    text = await _read_part_text(part, MAX_TEXT_BYTES)
                elif part.name == "client_id":
                    client_id = (await _read_part_text(part, 256)).strip() or None
                    existing = self._known_client_id(number, client_id)
                    if existing is not None:
                        _remove([p for p, _ in saved])
                        return self._existing_message(existing)
                elif part.name in ("files", "files[]"):
                    name = files.sanitize_filename(part.filename)
                    if name is None:
                        _remove([p for p, _ in saved])
                        return _err(400, "invalid file name")
                    content_type = files.clean_content_type(part.headers.get(hdrs.CONTENT_TYPE), name)
                    if not Path(name).suffix:
                        name += files.extension_for(content_type)
                    directory.mkdir(parents=True, exist_ok=True)
                    saved.append((await _save_part(part, directory, name, budget), content_type))
                else:
                    await part.release()
        except BaseException:
            _remove([p for p, _ in saved])
            raise
        existing = self._known_client_id(number, client_id)
        if existing is not None:
            _remove([p for p, _ in saved])
            return self._existing_message(existing)
        if not text.strip() and not saved:
            return _err(400, "empty message")
        attachments = [
            Attachment(id=p.name, content_type=ct, filename=p.name, local_path=str(p), size=p.stat().st_size)
            for p, ct in saved
        ]
        row_id = self.agent.submit_web_message(number, channel_id, text, attachments)
        self._remember_client_id(number, client_id, row_id)
        return self._existing_message(row_id)

    async def poll_vote(self, request: web.Request) -> web.Response:
        channel_id = await self._channel(request)
        if channel_id is None:
            return _err(403, "channel not allowed")
        ts = _parse_int(request.match_info["ts"])
        body = await _read_json(request)
        option = body.get("option") if isinstance(body, dict) else None
        if ts is None or not isinstance(option, int) or isinstance(option, bool) or option < 0:
            return _err(400, "invalid vote")
        number = self._session(request).number
        await self.agent.on_web_poll_vote(PollVote(
            channel_id=channel_id, voter=number, voter_name=self.bus.display_name(number),
            target_ts=ts, option_indexes=[option], vote_count=1,
        ))
        return web.json_response({"ok": True})

    async def attachment_get(self, request: web.Request) -> web.StreamResponse:
        channel_id = await self._channel(request)
        if channel_id is None:
            return _err(403, "channel not allowed")
        path = attachment_path(channel_id, request.match_info["name"])
        if path is None or not path.is_file():
            return _err(404, "not found")
        return web.FileResponse(path, headers=_file_headers(path, download=False))

    # --- events (SSE) ----------------------------------------------------------

    async def events(self, request: web.Request) -> web.StreamResponse:
        s = self._session(request)
        last = _parse_int(request.headers.get("Last-Event-ID") or request.query.get("last_event_id"))
        response = web.StreamResponse(headers={
            hdrs.CONTENT_TYPE: "text/event-stream",
            hdrs.CACHE_CONTROL: "no-cache",
            "X-Accel-Buffering": "no",
        })
        # Subscribed before the response starts (the client's `open` fires
        # on prepare), and no await between reading the start position and
        # subscribing, so nothing published in between is missed or doubled.
        start_rev = last if last is not None else self.bus.current_rev()
        sub = self.bus.subscribe(s.number, s.endpoint_id)
        sent_rev = start_rev
        try:
            await response.prepare(request)
            if last is None:
                # Bare id line: sets the browser's lastEventId (auto-reconnect
                # resumes here) without dispatching an event.
                await response.write(f"id: {start_rev}\n\n".encode())
            else:
                while True:
                    rows = self.bus.replay(s.number, sent_rev, REPLAY_PAGE_SIZE)
                    for row in rows:
                        sent_rev = max(sent_rev, row["rev"])
                        await self._write_event(response, "message", row["rev"], message_json(row))
                    if len(rows) < REPLAY_PAGE_SIZE:
                        break
            while True:
                try:
                    kind, rev, data = await asyncio.wait_for(sub.queue.get(), SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": ping\n\n")
                    continue
                if kind == "close":
                    break
                if kind == "message" and rev is not None:
                    if rev <= sent_rev:
                        continue
                    sent_rev = rev
                await self._write_event(response, kind, rev, data)
                if kind == "deauthed":
                    break
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self.bus.unsubscribe(sub)
        return response

    @staticmethod
    async def _write_event(response: web.StreamResponse, kind: str, rev: Optional[int], data: dict) -> None:
        lines = f"id: {rev}\n" if rev is not None else ""
        lines += f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        await response.write(lines.encode("utf-8"))

    # --- files -----------------------------------------------------------------

    def _root(self, request: web.Request) -> Path:
        return files.workspace_root(self._session(request).number)

    async def _file_root(self, request: web.Request) -> Path:
        """Workspace root for the channel indicated by ?cid= (defaults to the user's 1:1)."""
        number = self._session(request).number
        cid = request.query.get("cid")
        if not cid:
            return files.workspace_root(number)
        channel_id = decode_cid(cid)
        if channel_id is None:
            raise files.FileAccessError("invalid channel")
        if channel_id == number:
            return files.workspace_root(channel_id)
        await self.membership.ensure_fresh()
        if not self.membership.is_member(number, channel_id):
            raise files.FileAccessError("channel not allowed")
        return files.workspace_root(channel_id)

    async def files_list(self, request: web.Request) -> web.Response:
        root = await self._file_root(request)
        return web.json_response(files.list_dir(root, request.query.get("path", "")))

    async def files_raw_get(self, request: web.Request) -> web.StreamResponse:
        root = await self._file_root(request)
        path = files.resolve(root, request.query.get("path", ""))
        if not path.is_file():
            return _err(404, "not found")
        # FileResponse: Range requests (media seeking) + ETag in the same
        # format as files.etag_for, which PUT's If-Match is compared with.
        return web.FileResponse(path, headers=_file_headers(path, download=request.query.get("download") == "1"))

    async def files_raw_put(self, request: web.Request) -> web.Response:
        root = await self._file_root(request)
        rel = request.query.get("path", "")
        files.resolve(root, rel)
        raw = await _read_limited(request, config.webui.editor_max_kb * 1024)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _err(400, "body must be UTF-8 text")
        create_only = request.headers.get(hdrs.IF_NONE_MATCH, "").strip() == "*"
        try:
            etag = files.write_text(root, rel, text, request.headers.get(hdrs.IF_MATCH), create_only)
        except files.AlreadyExists:
            return _err(409, "file already exists")
        except FileExistsError:
            return _err(409, "the file was changed by someone else")
        return web.json_response({"ok": True, "etag": etag})

    async def files_upload(self, request: web.Request) -> web.Response:
        root = await self._file_root(request)
        directory = files.resolve(root, request.query.get("dir", ""))
        if directory.exists() and not directory.is_dir():
            return _err(400, "not a directory")
        try:
            reader = await request.multipart()
        except (AssertionError, ValueError, KeyError):
            return _err(400, "multipart/form-data expected")
        budget = [config.webui.max_upload_mb * 1024 * 1024]
        saved: list[Path] = []
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if not isinstance(part, BodyPartReader):
                    continue
                if part.name not in ("files", "files[]"):
                    await part.release()
                    continue
                name = files.sanitize_filename(part.filename)
                if name is None:
                    _remove(saved)
                    return _err(400, "invalid file name")
                directory.mkdir(parents=True, exist_ok=True)
                saved.append(await _save_part(part, directory, name, budget))
        except BaseException:
            _remove(saved)
            raise
        return web.json_response({"ok": True, "names": [p.name for p in saved]})

    async def files_mkdir(self, request: web.Request) -> web.Response:
        root = await self._file_root(request)
        try:
            files.make_dir(root, request.query.get("path", ""))
        except FileExistsError:
            return _err(409, "a file with that name exists")
        return web.json_response({"ok": True})

    async def files_delete(self, request: web.Request) -> web.Response:
        root = await self._file_root(request)
        files.delete(root, request.query.get("path", ""))
        return web.json_response({"ok": True})


async def start_web_api(agent: Any, signal_client: SignalClient, auth_manager: AuthManager) -> WebAPI:
    """Build the web interface backend, hook it into signal.py/core and start the API.

    `agent` is the core.AgentLoop (provides AgentHooks + attach_web).
    """
    membership = MembershipCache(config.signal.phone_number, config.signal.api_url,
                                 config.signal.mock_api_url)
    bus = ChatBus(membership, config.signal.phone_number)
    membership.on_change = bus.notify_channels
    web_auth = WebAuth(auth_manager, signal_client, bus)
    set_outbound_listener(bus.on_outbound)
    agent.attach_web(bus, web_auth)
    api = WebAPI(web_auth, bus, membership, agent)
    await api.start(config.webui.api_host, config.webui.api_port)
    return api
