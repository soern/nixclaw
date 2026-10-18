"""Web interface login: Signal-delivered one-time codes, session tokens
("endpoints") and the /status and /deauth commands.

* ``request_code``: a KNOWN/PRIVILEGED number gets a 6-digit code in its 1:1
  Signal chat (publish=False: never in the web history, never stored).
  Callers always get the same answer, whatever the number (no enumeration);
  only the rate limits (per number and per client IP) are visible.
* ``verify``: single use, at most MAX_CODE_ATTEMPTS tries, TTL
  ``webui.code_ttl_seconds``. Codes live in memory only.
* A login creates a ``web_endpoints`` row holding sha256(token); the token
  itself only goes to the browser cookie. Every request re-resolves the
  number's tier, so a number that became UNKNOWN gets 401 (row kept).
"""

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, TypeGuard

from .. import prompts
from ..auth import AuthManager
from ..config import config
from ..db import operations as db
from ..signal import SignalClient
from ..types import UserTier
from .bus import ChatBus, now_ms

LOG = logging.getLogger(__name__)

MAX_CODE_ATTEMPTS = 5
MAX_ENDPOINT_NAME_CHARS = 64
# last_used_at is written at most this often per endpoint.
TOUCH_INTERVAL_MS = 60_000
RATE_WINDOW_SECONDS = 3600
NUMBER_RE = re.compile(r"^\+\d{3,20}$")
_DEAUTH_RE = re.compile(r"^/deauth(?:\s+(.*))?$", re.DOTALL)


def parse_web_command(text: str) -> Optional[tuple[str, str]]:
    """("status", "") / ("deauth", arg) for "/status" and "/deauth <arg>", else None."""
    t = (text or "").strip()
    if t == "/status":
        return ("status", "")
    m = _DEAUTH_RE.match(t)
    if m:
        return ("deauth", (m.group(1) or "").strip())
    return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_number(number: Any) -> TypeGuard[str]:
    return isinstance(number, str) and bool(NUMBER_RE.match(number))


@dataclass
class _Code:
    code: str
    expires_at: float
    attempts: int = 0


@dataclass
class Session:
    number: str
    tier: UserTier
    endpoint_id: int
    endpoint_name: str


class WebAuth:
    def __init__(self, auth_manager: AuthManager, signal_client: SignalClient, bus: ChatBus) -> None:
        self._auth = auth_manager
        self._signal = signal_client
        self._bus = bus
        self._codes: dict[str, _Code] = {}
        self._rate_number: dict[str, deque[float]] = {}
        self._rate_ip: dict[str, deque[float]] = {}
        self._last_touch: dict[int, int] = {}
        self._tasks: set[asyncio.Task] = set()

    def tier(self, number: str) -> UserTier:
        return self._auth.get_user_tier(number)

    def allowed(self, number: str) -> bool:
        return self.tier(number) in (UserTier.PRIVILEGED, UserTier.KNOWN)

    # --- codes -------------------------------------------------------------

    @staticmethod
    def _hit(buckets: dict[str, deque[float]], key: str, limit: int, now: float) -> bool:
        """Record one request; False if `key` already used `limit` in the window."""
        q = buckets.setdefault(key, deque())
        while q and q[0] <= now - RATE_WINDOW_SECONDS:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _prune_rate(self, now: float) -> None:
        for buckets in (self._rate_number, self._rate_ip):
            for key in [k for k, q in buckets.items() if not q or q[-1] <= now - RATE_WINDOW_SECONDS]:
                del buckets[key]

    def request_code(self, number: str, client_ip: str) -> bool:
        """Start a login. Returns False when rate limited (HTTP 429)."""
        now = time.monotonic()
        if len(self._rate_ip) + len(self._rate_number) > 10_000:
            self._prune_rate(now)
        w = config.webui
        if not self._hit(self._rate_ip, client_ip, w.code_requests_per_ip_per_hour, now):
            return False
        if not self._hit(self._rate_number, number, w.code_requests_per_number_per_hour, now):
            return False
        for n in [n for n, c in self._codes.items() if c.expires_at <= now]:
            del self._codes[n]
        if not self.allowed(number):
            LOG.info(f"Web login code requested for non-permitted number {number}")
            return True
        code = f"{secrets.randbelow(10**6):06d}"
        self._codes[number] = _Code(code=code, expires_at=now + w.code_ttl_seconds)
        text = prompts.current().render(
            "web-auth-code", code=code, minutes=max(1, round(w.code_ttl_seconds / 60))
        )
        # Sent in the background so the response time does not reveal
        # whether the number is permitted.
        task = asyncio.create_task(self._send_code(number, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        LOG.info(f"Web login code sent to {number}")
        return True

    async def _send_code(self, number: str, text: str) -> None:
        try:
            await self._signal.send_message(number, text, publish=False)
        except Exception as e:
            LOG.warning(f"Sending web login code to {number} failed: {e}")

    def verify(self, number: str, code: str, endpoint_name: str) -> Optional[str]:
        """Check a code; on success create an endpoint and return its token."""
        entry = self._codes.get(number)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._codes[number]
            return None
        entry.attempts += 1
        if not hmac.compare_digest(entry.code.encode(), str(code).strip().encode()):
            if entry.attempts >= MAX_CODE_ATTEMPTS:
                del self._codes[number]
            return None
        del self._codes[number]
        if not self.allowed(number):
            return None
        token = secrets.token_urlsafe(32)
        days = config.webui.token_expiry_days
        created = now_ms()
        expires = created + days * 86_400_000 if days > 0 else None
        endpoint_id = db.web_endpoint_create(number, endpoint_name, hash_token(token), created, expires)
        LOG.info(f"Web login for {number}: endpoint {endpoint_id} ({endpoint_name!r})")
        return token

    # --- sessions ------------------------------------------------------------

    def resolve(self, token: Optional[str]) -> Optional[Session]:
        """Session for a cookie token; None if unknown, expired or not permitted."""
        if not token:
            return None
        row = db.web_endpoint_by_hash(hash_token(token))
        if row is None:
            return None
        now = now_ms()
        if row["expires_at"] is not None and row["expires_at"] <= now:
            return None
        tier = self.tier(row["number"])
        if tier not in (UserTier.PRIVILEGED, UserTier.KNOWN):
            return None
        if now - self._last_touch.get(row["id"], 0) >= TOUCH_INTERVAL_MS:
            self._last_touch[row["id"]] = now
            db.web_endpoint_touch(row["id"], now)
        return Session(number=row["number"], tier=tier, endpoint_id=row["id"], endpoint_name=row["name"])

    def endpoints(self, number: str, current_id: Optional[int] = None) -> list[dict]:
        """Endpoint objects (API contract) of a number, oldest first."""
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "expires_at": row["expires_at"],
                "connections": self._bus.connections(row["id"]),
                "current": row["id"] == current_id,
            }
            for row in db.web_endpoints_for(number)
        ]

    def revoke(self, number: str, endpoint_id: int) -> bool:
        """Delete one of the number's endpoints and close its streams."""
        if not any(row["id"] == endpoint_id for row in db.web_endpoints_for(number)):
            return False
        db.web_endpoint_delete(endpoint_id)
        self._last_touch.pop(endpoint_id, None)
        self._bus.close_endpoint(endpoint_id)
        LOG.info(f"Web endpoint {endpoint_id} of {number} revoked")
        return True

    # --- /status and /deauth (Signal 1:1) -----------------------------------

    def handle_command(self, number: str, command: str, arg: str) -> str:
        p = prompts.current()
        endpoints = self.endpoints(number)
        if command == "status":
            if not endpoints:
                return p.text("web-status-empty")
            never = p.text("web-status-never")
            lines = [p.text("web-status-header")]
            for i, ep in enumerate(endpoints, start=1):
                lines.append(p.render(
                    "web-status-line", index=i, name=ep["name"],
                    created=_fmt_ms(ep["created_at"]) or never,
                    last_used=_fmt_ms(ep["last_used_at"]) or never,
                    expires=_fmt_ms(ep["expires_at"]) or never,
                    connections=ep["connections"],
                ))
            lines.append(p.text("web-status-footer"))
            return "\n".join(lines)
        if not arg:
            return p.text("web-deauth-usage")
        if arg.isdigit():
            idx = int(arg)
            matches = [endpoints[idx - 1]] if 1 <= idx <= len(endpoints) else []
        else:
            matches = [ep for ep in endpoints if ep["name"].casefold() == arg.casefold()]
        if not matches:
            return p.render("web-deauth-not-found", query=arg)
        if len(matches) > 1:
            return p.render("web-deauth-ambiguous", query=arg)
        self.revoke(number, matches[0]["id"])
        return p.render("web-deauth-done", name=matches[0]["name"])


def _fmt_ms(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
