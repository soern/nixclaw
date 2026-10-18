"""Signal channel membership for the web interface.

A web user may open their own 1:1 channel (channel_id = their number) and
every group whose member list (signal-api ``GET /v1/groups/{bot_number}``)
contains their number. The list is cached in memory: refreshed at startup,
on POST /api/channels/refresh and when older than CACHE_TTL_SECONDS.
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
            groups = await self._fetch()
            self._fetched_at = time.monotonic()
            if groups is None:
                self._fetched_at -= CACHE_TTL_SECONDS - RETRY_AFTER_FAILURE_SECONDS
                return
            self.set_groups(groups)

    async def ensure_fresh(self) -> None:
        if time.monotonic() - self._fetched_at >= CACHE_TTL_SECONDS:
            await self.refresh()

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
