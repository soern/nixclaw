"""Signal channel membership (signal-api ``GET /v1/groups/{bot_number}``).

Always created by main.py (with or without the web interface) and used by:
- core.py's unknown-sender gate: an UNKNOWN sender in a group is answered
  only while the group has a KNOWN/PRIVILEGED member (group_members()).
- the web interface: a web user may open their own 1:1 channel (channel_id =
  their number) and every group whose member list contains their number.
The list is cached in memory: refreshed at startup, on POST
/api/channels/refresh, when older than CACHE_TTL_SECONDS and (rate-limited)
when the gate asks for a group that is not cached.
"""

import asyncio
import base64
import binascii
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from ..config import config

LOG = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600
# After a failed refresh, wait this long before trying again on demand.
RETRY_AFTER_FAILURE_SECONDS = 60
GROUP_PREFIX = "signal-group:"


def encode_cid(channel_id: str) -> str:
    """URL-safe channel id: base64url(channel_id) without padding."""
    return base64.urlsafe_b64encode(channel_id.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cid(cid: str) -> Optional[str]:
    """Inverse of encode_cid; None if `cid` is not valid."""
    if not cid or len(cid) > 512:
        return None
    try:
        raw = base64.urlsafe_b64decode(cid + "=" * (-len(cid) % 4))
        channel_id = raw.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if encode_cid(channel_id) != cid or any(ord(ch) < 32 for ch in channel_id):
        return None
    return channel_id


@dataclass
class GroupInfo:
    channel_id: str
    name: str
    members: set[str] = field(default_factory=set)


def _parse_groups(data: Any) -> dict[str, GroupInfo]:
    groups: dict[str, GroupInfo] = {}
    if not isinstance(data, list):
        return groups
    for g in data:
        if not isinstance(g, dict):
            continue
        internal_id = g.get("internal_id")
        if not isinstance(internal_id, str) or not internal_id:
            continue
        members_raw = g.get("members") or []
        # Entries that are not phone numbers (e.g. bare UUIDs) never match.
        members = {m for m in members_raw if isinstance(m, str) and m.startswith("+")}
        channel_id = GROUP_PREFIX + internal_id
        name = g.get("name") if isinstance(g.get("name"), str) else ""
        groups[channel_id] = GroupInfo(channel_id=channel_id, name=name or internal_id, members=members)
    return groups


# Test numbers; only these are taken from the mock API's group list.
MOCK_NUMBER_PREFIX = "+0000"


class MembershipCache:
    def __init__(self, phone_number: str, api_url: str, mock_api_url: Optional[str] = None) -> None:
        self._phone_number = phone_number
        # (url, mock?) - the mock signal API (tests) only contributes +0000* members.
        self._api_urls = [(api_url, False)] if api_url else []
        if mock_api_url and mock_api_url != api_url:
            self._api_urls.append((mock_api_url, True))
        self._groups: dict[str, GroupInfo] = {}
        self._fetched_at: float = 0.0
        # Last refresh triggered by group_members() for an uncached group.
        self._last_miss_refresh: float = float("-inf")
        self._lock = asyncio.Lock()
        # Called with the numbers whose channel list changed after a refresh.
        self.on_change: Optional[Callable[[set[str]], None]] = None

    async def _fetch(self) -> Optional[dict[str, GroupInfo]]:
        merged: dict[str, GroupInfo] = {}
        ok = False
        async with httpx.AsyncClient(timeout=10.0) as client:
            for url, is_mock in self._api_urls:
                try:
                    response = await client.get(f"{url}/v1/groups/{self._phone_number}")
                    response.raise_for_status()
                    groups = _parse_groups(response.json())
                    if is_mock:
                        for g in groups.values():
                            g.members = {m for m in g.members if m.startswith(MOCK_NUMBER_PREFIX)}
                        groups = {cid: g for cid, g in groups.items() if g.members}
                    for cid, g in groups.items():
                        if cid in merged:
                            merged[cid].members |= g.members
                        else:
                            merged[cid] = g
                    ok = True
                except Exception as e:
                    LOG.warning(f"Fetching Signal groups from {url} failed: {e}")
        return merged if ok else None

    async def refresh(self) -> None:
        """Re-read group membership; keeps the old list if every API fails."""
        async with self._lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        groups = await self._fetch()
        self._fetched_at = time.monotonic()
        if groups is None:
            self._fetched_at -= CACHE_TTL_SECONDS - RETRY_AFTER_FAILURE_SECONDS
            return
        self.set_groups(groups)

    def _stale(self) -> bool:
        return time.monotonic() - self._fetched_at >= CACHE_TTL_SECONDS

    async def ensure_fresh(self) -> None:
        if self._stale():
            async with self._lock:
                # Re-checked under the lock: concurrent callers share one fetch.
                if self._stale():
                    await self._refresh_locked()

    def set_groups(self, groups: dict[str, GroupInfo]) -> None:
        """Replace the cached groups and report numbers whose channels changed."""
        old = self._groups
        self._groups = groups
        changed: set[str] = set()
        for cid in set(old) | set(groups):
            before = old[cid].members if cid in old else set()
            after = groups[cid].members if cid in groups else set()
            changed |= before ^ after
            if cid in old and cid in groups and old[cid].name != groups[cid].name:
                changed |= after
        if changed and self.on_change is not None:
            try:
                self.on_change(changed)
            except Exception as e:
                LOG.warning(f"Membership change callback failed: {e}")

    async def group_members(self, channel_id: str) -> Optional[set[str]]:
        """Phone numbers of a group's members, or None if the group is unknown.

        Refreshes a stale cache first. A group missing from the cache (e.g.
        the bot was just added) triggers one extra refresh, at most once per
        RETRY_AFTER_FAILURE_SECONDS; the timestamp is set before awaiting so
        a burst of messages causes a single fetch (the others wait for it).
        Members listed only by UUID are not included.
        """
        await self.ensure_fresh()
        group = self._groups.get(channel_id)
        if group is None:
            if time.monotonic() - self._last_miss_refresh >= RETRY_AFTER_FAILURE_SECONDS:
                self._last_miss_refresh = time.monotonic()
                await self.refresh()
            else:
                async with self._lock:  # wait for a refresh still in flight
                    pass
            group = self._groups.get(channel_id)
        return set(group.members) if group is not None else None

    def is_member(self, number: str, channel_id: str) -> bool:
        if not number:
            return False
        if channel_id == number:
            return True
        group = self._groups.get(channel_id)
        return group is not None and number in group.members

    def channels_for(self, number: str) -> list[dict]:
        """Channel objects (API contract) for a number: 1:1 first, then groups by name."""
        result = [{
            "cid": encode_cid(number), "channel_id": number, "kind": "direct",
            "name": config.agent.name,
        }]
        groups = sorted(
            (g for g in self._groups.values() if number in g.members),
            key=lambda g: (g.name.lower(), g.channel_id),
        )
        for g in groups:
            result.append({
                "cid": encode_cid(g.channel_id), "channel_id": g.channel_id,
                "kind": "group", "name": g.name,
            })
        return result

    def channel_ids_for(self, number: str) -> list[str]:
        return [c["channel_id"] for c in self.channels_for(number)]


def _parse_contacts(data: Any) -> list[dict]:
    """ListContactsResponse rows we can use (uuid required; the rest optional)."""
    contacts: list[dict] = []
    if not isinstance(data, list):
        return contacts
    for c in data:
        if not isinstance(c, dict):
            continue
        uuid = c.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            continue
        contact = {"uuid": uuid}
        for key in ("number", "name", "profile_name"):
            value = c.get(key)
            contact[key] = value if isinstance(value, str) else ""
        contacts.append(contact)
    return contacts


def _log_background_refresh_failure(task: "asyncio.Task[None]") -> None:
    """Done-callback for the fire-and-forget refresh: log, never raise (the
    module's style) — an unobserved task exception would only surface as an
    asyncio 'exception was never retrieved' warning at GC time."""
    if task.cancelled():  # task.exception() would raise CancelledError here
        return
    exc = task.exception()
    if exc is not None:
        LOG.warning(f"Background contact refresh task failed: {exc}")


class ContactBook:
    """The bot account's Signal contacts (signal-api ``GET /v1/contacts/{bot}``).

    Mentions need a member's UUID (signal-cli writes the author string into
    the mention's UUID field verbatim, so a phone number would corrupt it), and
    the member menu shows each member's public profile name - both come from
    this feed. Only ``profile_name`` is ever shown to a web user: the owner-side
    contact ``name`` is a private label. Same cache pattern as MembershipCache
    (TTL, one refresh at a time, a failed fetch keeps the old rows, the mock
    API only contributes +0000* entries).
    """

    def __init__(self, phone_number: str, api_url: str, mock_api_url: Optional[str] = None) -> None:
        self._phone_number = phone_number
        # (url, mock?) - the mock signal API (tests) only contributes +0000* contacts.
        self._api_urls = [(api_url, False)] if api_url else []
        if mock_api_url and mock_api_url != api_url:
            self._api_urls.append((mock_api_url, True))
        # Lowercased uuid and number -> contact (contacts without a uuid are dropped).
        self._by_key: dict[str, dict] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        # One-shot refresh started by lookup() on a cache miss (send hot path).
        self._refresh_task: Optional[asyncio.Task] = None
        # Monotonic timestamp of the last background kick (throttle below).
        self._last_kick: float = float("-inf")

    async def _fetch(self) -> Optional[list[dict]]:
        merged: list[dict] = []
        ok = False
        async with httpx.AsyncClient(timeout=10.0) as client:
            for url, is_mock in self._api_urls:
                try:
                    response = await client.get(f"{url}/v1/contacts/{self._phone_number}")
                    response.raise_for_status()
                    contacts = _parse_contacts(response.json())
                    if is_mock:
                        contacts = [c for c in contacts if c["number"].startswith(MOCK_NUMBER_PREFIX)]
                    merged.extend(contacts)
                    ok = True
                except Exception as e:
                    LOG.warning(f"Fetching Signal contacts from {url} failed: {e}")
        return merged if ok else None

    async def refresh(self) -> None:
        """Re-read the contact list; keeps the old one if every API fails."""
        async with self._lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        contacts = await self._fetch()
        self._fetched_at = time.monotonic()
        if contacts is None:
            self._fetched_at -= CACHE_TTL_SECONDS - RETRY_AFTER_FAILURE_SECONDS
            return
        self._index(contacts)

    def _index(self, contacts: list[dict]) -> None:
        by_key: dict[str, dict] = {}
        for c in contacts:
            for key in (c["uuid"].lower(), c["number"].lower()):
                if key:
                    by_key.setdefault(key, c)
        self._by_key = by_key

    def _stale(self) -> bool:
        return time.monotonic() - self._fetched_at >= CACHE_TTL_SECONDS

    async def ensure_fresh(self) -> None:
        if self._stale():
            async with self._lock:
                # Re-checked under the lock: concurrent callers share one fetch.
                if self._stale():
                    await self._refresh_locked()

    def set_contacts(self, contacts: list[dict]) -> None:
        """Replace the cache (test/mock injection); parse like the API feed."""
        self._index(_parse_contacts(contacts))
        self._fetched_at = time.monotonic()

    def background_refresh_once(self) -> None:
        """Kick a refresh without waiting for it (at most one task in flight).

        Used by the sync ``lookup`` on a cache miss: a stale cache must never
        delay a user's message. A no-op outside the event loop. Kicks are also
        throttled by ``RETRY_AFTER_FAILURE_SECONDS`` so a hammering client
        cannot serialise repeated 10 s-timeout contact fetches behind misses.
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        now = time.monotonic()
        if now - self._last_kick < RETRY_AFTER_FAILURE_SECONDS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_kick = now
        self._refresh_task = loop.create_task(self.refresh())
        self._refresh_task.add_done_callback(_log_background_refresh_failure)

    def _find(self, identifier: str) -> Optional[dict]:
        if not identifier:
            return None
        return self._by_key.get(identifier.lower())

    def lookup(self, identifier: str) -> Optional[str]:
        """UUID of a contact by number or UUID (case-insensitive), None if unknown.

        Sync and cache-only; a miss kicks ``background_refresh_once`` so the
        next send resolves (this send degrades to plain text).
        """
        contact = self._find(identifier)
        if contact is None:
            self.background_refresh_once()
            return None
        return contact["uuid"]

    def profile_name_for(self, identifier: str) -> Optional[str]:
        """Public Signal profile name of a contact, None if unknown.

        Deliberately reads only ``profile_name``: the contact ``name`` is the
        bot owner's private label for that person and is never exposed.
        """
        contact = self._find(identifier)
        if contact is None:
            return None
        return contact["profile_name"] or None
