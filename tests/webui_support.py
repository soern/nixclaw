"""Shared helpers for the web interface tests (test_webui_*.py)."""

import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from mock_signal_api import MOCK_CONTACTS
from src.nixclaw_agent.auth import AuthManager, AuthConfig
from src.nixclaw_agent.config import WebUIConfig, config
from src.nixclaw_agent.mentions import MentionEntry
from src.nixclaw_agent.types import Attachment, Event, PollVote
from src.nixclaw_agent.web.api import COOKIE_NAME, WebAPI
from src.nixclaw_agent.web.auth import WebAuth
from src.nixclaw_agent.web.bus import ChatBus
from src.nixclaw_agent.web.channels import ContactBook, GroupInfo, MembershipCache

BOT = "+000999"
PRIV = "+000000"
KNOWN = "+000001"
UNKNOWN = "+000002"
GROUP = "signal-group:abc/def+ghi="
OTHER_GROUP = "signal-group:other=="
CSRF = {"X-NixClaw": "1"}

# Mention fixtures: the same literals the mock signal API serves, so unit and
# acceptance tests assert on identical UUIDs.
CONTACT_UUIDS: dict[str, str] = {c["number"]: c["uuid"] for c in MOCK_CONTACTS}
CONTACT_PROFILE_NAMES: dict[str, str] = {c["number"]: c["profile_name"] for c in MOCK_CONTACTS}


class FakeAgent:
    """AgentHooks stand-in: publishes like core.submit_web_message, records calls."""

    def __init__(self, bus: ChatBus) -> None:
        self.bus = bus
        self.submitted: list[tuple[str, str, str, list[Attachment]]] = []
        # Parallel to `submitted`: the (already UUID-authored) mention entries.
        self.submitted_mentions: list[Optional[list[MentionEntry]]] = []
        self.on_web_poll_vote = AsyncMock()

    def submit_web_message(self, number: str, channel_id: str, text: str,
                           attachments: list[Attachment],
                           mentions: Optional[list[MentionEntry]] = None) -> int:
        self.submitted.append((number, channel_id, text, attachments))
        self.submitted_mentions.append(list(mentions) if mentions else None)
        event = Event(source=number, destination=channel_id, message=text or "[Attachment]",
                      timestamp=int(time.time()), group_id=None, attachments=list(attachments))
        return self.bus.publish_inbound(event, channel_id, origin="web")


class Env:
    def __init__(self, auth_manager: AuthManager, signal: Any, membership: MembershipCache,
                 bus: ChatBus, web_auth: WebAuth, agent: FakeAgent, api: WebAPI,
                 contacts: ContactBook) -> None:
        self.auth_manager = auth_manager
        self.signal = signal
        self.membership = membership
        self.bus = bus
        self.web_auth = web_auth
        self.agent = agent
        self.api = api
        self.contacts = contacts
        self.client: Optional[TestClient] = None

    def login(self, number: str, name: str = "Laptop") -> str:
        """Create an endpoint directly (bypassing the code) and return its token."""
        from src.nixclaw_agent.web.auth import _Code
        self.web_auth._codes[number] = _Code(code="123456", expires_at=time.monotonic() + 60)
        token = self.web_auth.verify(number, "123456", name)
        assert token
        return token

    @staticmethod
    def cookie(token: str) -> dict[str, str]:
        return {"Cookie": f"{COOKIE_NAME}={token}"}


@pytest.fixture
def webui_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "webui", WebUIConfig(enabled=True))
    monkeypatch.setattr(config.signal, "phone_number", BOT)
    return config.webui


def make_env() -> Env:
    auth_manager = AuthManager(AuthConfig(privileged_numbers=[PRIV], known_numbers=[KNOWN]))
    signal = MagicMock()
    signal.send_message = AsyncMock(return_value=1)
    membership = MembershipCache(BOT, "")
    membership.set_groups({
        GROUP: GroupInfo(channel_id=GROUP, name="Family", members={PRIV, KNOWN}),
        OTHER_GROUP: GroupInfo(channel_id=OTHER_GROUP, name="Work", members={PRIV}),
    })
    membership._fetched_at = time.monotonic()
    bus = ChatBus(membership, BOT)
    membership.on_change = bus.notify_channels
    web_auth = WebAuth(auth_manager, signal, bus)
    agent = FakeAgent(bus)
    contacts = ContactBook(BOT, "")
    contacts.set_contacts(list(MOCK_CONTACTS))
    api = WebAPI(web_auth, bus, membership, agent, contacts)
    return Env(auth_manager, signal, membership, bus, web_auth, agent, api, contacts)


@pytest_asyncio.fixture
async def env(reset_db, webui_config):
    e = make_env()
    client = TestClient(TestServer(e.api.make_app()))
    await client.start_server()
    e.client = client
    yield e
    await client.close()


def vote(channel_id: str, voter: str, ts: int) -> PollVote:
    return PollVote(channel_id=channel_id, voter=voter, voter_name=None, target_ts=ts,
                    option_indexes=[0], vote_count=1)
