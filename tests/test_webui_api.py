"""Web interface API: channels, messages, SSE, settings, files (web/api.py, web/files.py)."""

import asyncio
import json
import os

import aiohttp
import pytest

from src.nixclaw_agent.config import config
from src.nixclaw_agent.db import operations as db
from src.nixclaw_agent.mentions import MentionEntry
from src.nixclaw_agent.web import api as api_mod
from src.nixclaw_agent.web import channels as channels_mod
from src.nixclaw_agent.web.channels import GroupInfo, decode_cid, encode_cid
from webui_support import (  # noqa: F401
    BOT, CONTACT_PROFILE_NAMES, CONTACT_UUIDS, CSRF, GROUP, KNOWN, OTHER_GROUP, PRIV, UNKNOWN,
    env, webui_config,
)

pytestmark = pytest.mark.asyncio


def _h(e, token: str, csrf: bool = False) -> dict:
    return {**e.cookie(token), **(CSRF if csrf else {})}


# --- channels ------------------------------------------------------------------

async def test_cid_roundtrip_and_invalid():
    for channel_id in ("+000000", GROUP, "signal-group:a/b+c=="):
        cid = encode_cid(channel_id)
        assert "=" not in cid and "/" not in cid and "+" not in cid
        assert decode_cid(cid) == channel_id
    assert decode_cid("") is None
    assert decode_cid("!!!") is None
    assert decode_cid(encode_cid("+000000") + "A") is None


async def test_channels_membership(env):
    token = env.login(KNOWN)
    r = await env.client.get("/api/channels", headers=_h(env, token))
    channels = (await r.json())["channels"]
    assert channels[0] == {"cid": encode_cid(KNOWN), "channel_id": KNOWN, "kind": "direct",
                           "name": config.agent.name}
    assert [c["channel_id"] for c in channels] == [KNOWN, GROUP]
    assert channels[1]["name"] == "Family" and channels[1]["kind"] == "group"


async def test_channel_access_enforced(env):
    token = env.login(KNOWN)
    for channel_id in (OTHER_GROUP, PRIV, "signal-group:unknown"):
        r = await env.client.get(f"/api/channels/{encode_cid(channel_id)}/messages", headers=_h(env, token))
        assert r.status == 403, channel_id
    r = await env.client.get("/api/channels/@@@/messages", headers=_h(env, token))
    assert r.status == 403
    assert (await env.client.get(f"/api/channels/{encode_cid(GROUP)}/messages",
                                 headers=_h(env, token))).status == 200
    assert (await env.client.get(f"/api/channels/{encode_cid(KNOWN)}/messages",
                                 headers=_h(env, token))).status == 200


async def test_non_number_members_never_match(env):
    from src.nixclaw_agent.web.channels import _parse_groups
    groups = _parse_groups([{"internal_id": "x", "name": "G", "members": ["abcd-uuid", "+000001", None]}])
    assert groups["signal-group:x"].members == {"+000001"}
    assert not env.membership.is_member("", GROUP)


async def test_refresh_notifies_changed_members(env):
    token = env.login(KNOWN)
    async with env.client.get("/api/events", headers=_h(env, token)) as resp:
        await asyncio.sleep(0.05)
        env.membership.set_groups({GROUP: GroupInfo(channel_id=GROUP, name="Family", members={PRIV})})
        line = await _next_event(resp)
        assert line[0] == "channels"
    r = await env.client.get("/api/channels", headers=_h(env, token))
    assert [c["channel_id"] for c in (await r.json())["channels"]] == [KNOWN]


# --- messages --------------------------------------------------------------------

async def test_post_message_and_history(env):
    token = env.login(KNOWN)
    cid = encode_cid(GROUP)
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("text", "hello")
    form.add_field("client_id", "c1")
    form.add_field("files", b"OggS-data", filename="voice", content_type="audio/webm;codecs=opus")
    form.add_field("files", b"png", filename="../../etc/pic.png", content_type="image/png")
    r = await env.client.post(f"/api/channels/{cid}/messages", data=form, headers=_h(env, token, True))
    assert r.status == 200, await r.text()
    msg = (await r.json())["message"]
    assert msg["text"] == "hello" and msg["origin"] == "web" and msg["cid"] == cid
    names = [a["name"] for a in msg["attachments"]]
    assert names == ["voice.webm", "pic.png"]
    assert msg["attachments"][0]["content_type"] == "audio/webm" and msg["attachments"][0]["exists"]
    att_dir = config.paths.data_dir / GROUP / "attachments"
    assert (att_dir / "pic.png").read_bytes() == b"png"
    number, channel_id, text, atts = env.agent.submitted[0]
    assert (number, channel_id, text) == (KNOWN, GROUP, "hello")
    assert atts[0].local_path == str(att_dir / "voice.webm")

    # idempotent retry with the same client_id
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("client_id", "c1")
    form.add_field("text", "hello")
    form.add_field("files", b"png", filename="pic.png", content_type="image/png")
    r = await env.client.post(f"/api/channels/{cid}/messages", data=form, headers=_h(env, token, True))
    assert (await r.json())["message"]["id"] == msg["id"]
    assert len(env.agent.submitted) == 1
    assert not (att_dir / "pic_1.png").exists()

    # collision suffix for a new message
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("files", b"png2", filename="pic.png", content_type="image/png")
    r = await env.client.post(f"/api/channels/{cid}/messages", data=form, headers=_h(env, token, True))
    assert [a["name"] for a in (await r.json())["message"]["attachments"]] == ["pic_1.png"]

    r = await env.client.get(f"/api/channels/{cid}/messages?limit=1", headers=_h(env, token))
    hist = (await r.json())["messages"]
    assert len(hist) == 1 and hist[0]["attachments"][0]["name"] == "pic_1.png"
    r = await env.client.get(f"/api/channels/{cid}/messages?before={hist[0]['id']}", headers=_h(env, token))
    assert [m["id"] for m in (await r.json())["messages"]] == [msg["id"]]


async def test_post_message_rejects_bad_input(env, monkeypatch):
    token = env.login(KNOWN)
    cid = encode_cid(KNOWN)
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("text", "  ")
    r = await env.client.post(f"/api/channels/{cid}/messages", data=form, headers=_h(env, token, True))
    assert r.status == 400
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("files", b"x", filename=".bashrc", content_type="text/plain")
    r = await env.client.post(f"/api/channels/{cid}/messages", data=form, headers=_h(env, token, True))
    assert r.status == 400
    monkeypatch.setattr(config.webui, "max_upload_mb", 1)
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("files", b"x" * (1024 * 1024 + 10), filename="big.bin", content_type="application/octet-stream")
    r = await env.client.post(f"/api/channels/{cid}/messages", data=form, headers=_h(env, token, True))
    assert r.status == 413
    att_dir = config.paths.data_dir / KNOWN / "attachments"
    assert not att_dir.exists() or not any(att_dir.iterdir())
    assert env.agent.submitted == []


async def test_attachment_download_headers_and_expiry(env):
    token = env.login(KNOWN)
    att_dir = config.paths.data_dir / KNOWN / "attachments"
    att_dir.mkdir(parents=True)
    (att_dir / "a.aac").write_bytes(b"audio")
    (att_dir / "x.html").write_bytes(b"<script>alert(1)</script>")
    cid = encode_cid(KNOWN)
    r = await env.client.get(f"/api/channels/{cid}/attachments/a.aac", headers=_h(env, token))
    assert r.status == 200 and await r.read() == b"audio"
    assert r.headers["Content-Disposition"].startswith("inline")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Security-Policy"] == "sandbox"
    r = await env.client.get(f"/api/channels/{cid}/attachments/x.html", headers=_h(env, token))
    assert r.headers["Content-Disposition"].startswith("attachment")
    (att_dir / "a.aac").unlink()
    r = await env.client.get(f"/api/channels/{cid}/attachments/a.aac", headers=_h(env, token))
    assert r.status == 404
    r = await env.client.get(f"/api/channels/{cid}/attachments/..%2F..%2Fmessages.db", headers=_h(env, token))
    assert r.status == 404


async def test_poll_vote_forwarded(env):
    token = env.login(KNOWN)
    cid = encode_cid(GROUP)
    r = await env.client.post(f"/api/channels/{cid}/polls/1700/vote", json={"option": 1},
                              headers=_h(env, token, True))
    assert r.status == 200
    v = env.agent.on_web_poll_vote.call_args.args[0]
    assert (v.channel_id, v.voter, v.target_ts, v.option_indexes) == (GROUP, KNOWN, 1700, [1])
    r = await env.client.post(f"/api/channels/{cid}/polls/1700/vote", json={"option": "x"},
                              headers=_h(env, token, True))
    assert r.status == 400
    r = await env.client.post(f"/api/channels/{encode_cid(OTHER_GROUP)}/polls/1700/vote",
                              json={"option": 0}, headers=_h(env, token, True))
    assert r.status == 403


# --- SSE -------------------------------------------------------------------------

async def _next_event(resp) -> tuple:
    fields: dict = {}
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), 2)).decode().rstrip("\n")
        if line.startswith(":"):
            continue
        if not line:
            if "data" in fields:
                return fields.get("event"), fields.get("id"), json.loads(fields["data"])
            fields = {}  # bare "id:" block (initial position) - no event
            continue
        key, _, value = line.partition(": ")
        fields[key] = value


async def test_sse_live_filtering_and_replay(env, monkeypatch):
    monkeypatch.setattr(api_mod, "SSE_HEARTBEAT_SECONDS", 0.05)
    token = env.login(KNOWN)
    async with env.client.get("/api/events", headers=_h(env, token)) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        await asyncio.sleep(0.05)
        env.bus.on_outbound("message", {"recipient": OTHER_GROUP, "text": "secret", "ts": 5})
        env.bus.on_outbound("message", {"recipient": KNOWN, "text": "hi", "ts": 6})
        env.bus.on_outbound("typing", {"recipient": KNOWN, "active": True})
        kind, rev, data = await _next_event(resp)
        assert kind == "message" and data["text"] == "hi" and data["origin"] == "agent"
        first_rev = int(rev)
        kind, rev, data = await _next_event(resp)
        assert kind == "typing" and data == {"cid": encode_cid(KNOWN), "active": True} and rev is None
    env.bus.on_outbound("edit", {"recipient": KNOWN, "text": "hi there", "edit_ts": 6})
    env.bus.on_outbound("message", {"recipient": KNOWN, "text": "more", "ts": 7})
    async with env.client.get("/api/events", headers={**_h(env, token), "Last-Event-ID": str(first_rev)}) as resp:
        k1, r1, d1 = await _next_event(resp)
        k2, r2, d2 = await _next_event(resp)
        assert (d1["text"], d1["edited"]) == ("hi there", True)
        assert d2["text"] == "more" and int(r2) > int(r1) > first_rev
    # query parameter variant
    async with env.client.get(f"/api/events?last_event_id={r1}", headers=_h(env, token)) as resp:
        assert (await _next_event(resp))[2]["text"] == "more"


async def test_sse_deauthed_on_revoke(env):
    t1 = env.login(KNOWN, "Laptop")
    async with env.client.get("/api/events", headers=_h(env, t1)) as resp:
        await asyncio.sleep(0.05)
        eps = env.web_auth.endpoints(KNOWN)
        assert eps[0]["connections"] == 1
        assert "open connections: 1" in env.web_auth.handle_command(KNOWN, "status", "")
        env.web_auth.handle_command(KNOWN, "deauth", "1")
        assert (await _next_event(resp))[0] == "deauthed"
        assert await asyncio.wait_for(resp.content.read(), 2) == b""


# --- settings ----------------------------------------------------------------------

async def test_settings_roundtrip_and_limits(env):
    token = env.login(KNOWN)
    assert await (await env.client.get("/api/settings", headers=_h(env, token))).json() == {}
    data = {"theme": "dark", "categories": [{"name": "work", "channels": []}]}
    r = await env.client.put("/api/settings", json=data, headers=_h(env, token, True))
    assert r.status == 200
    assert json.loads((config.paths.data_dir / KNOWN / "webinterface.json").read_text()) == data
    assert await (await env.client.get("/api/settings", headers=_h(env, token))).json() == data
    r = await env.client.put("/api/settings", json=[1], headers=_h(env, token, True))
    assert r.status == 400
    big = {"x": "a" * (64 * 1024)}
    r = await env.client.put("/api/settings", json=big, headers=_h(env, token, True))
    assert r.status == 413


# --- files -------------------------------------------------------------------------

@pytest.fixture
def workspace(env):
    root = config.paths.data_dir / KNOWN
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("hello")
    (root / "browser").mkdir()
    (root / "browser" / "storage_state.json").write_text("{}")
    (root / "memories").mkdir()
    (config.paths.data_dir / PRIV).mkdir()
    (config.paths.data_dir / PRIV / "secret.txt").write_text("secret")
    return root


async def test_files_list_hides_hidden_paths(env, workspace):
    token = env.login(KNOWN)
    r = await env.client.get("/api/files/list?path=", headers=_h(env, token))
    body = await r.json()
    assert body["path"] == ""
    assert [e["name"] for e in body["entries"]] == ["docs"]
    r = await env.client.get("/api/files/list?path=docs", headers=_h(env, token))
    entry = (await r.json())["entries"][0]
    assert entry["name"] == "a.md" and entry["type"] == "file" and entry["size"] == 5
    assert (await env.client.get("/api/files/list?path=nope", headers=_h(env, token))).status == 404


@pytest.mark.parametrize("path", [
    "../+000000/secret.txt", "/etc/passwd", "docs/../../+000000/secret.txt",
    "browser/storage_state.json", "browser", "memories", "./browser/x",
])
async def test_files_traversal_and_hidden_rejected(env, workspace, path):
    token = env.login(KNOWN)
    for method, url in (("GET", "/api/files/raw"), ("GET", "/api/files/list"), ("DELETE", "/api/files"),
                        ("PUT", "/api/files/raw"), ("POST", "/api/files/mkdir")):
        r = await env.client.request(method, url, params={"path": path}, data=b"x",
                                     headers=_h(env, token, True))
        assert r.status == 403, (method, url, path, r.status)
    assert (config.paths.data_dir / PRIV / "secret.txt").read_text() == "secret"
    assert (workspace / "browser" / "storage_state.json").exists()


async def test_files_symlink_out_rejected(env, workspace):
    token = env.login(KNOWN)
    os.symlink(config.paths.data_dir / PRIV, workspace / "link")
    os.symlink(workspace / "browser", workspace / "hiddenlink")
    for path in ("link/secret.txt", "hiddenlink/storage_state.json"):
        r = await env.client.get("/api/files/raw", params={"path": path}, headers=_h(env, token))
        assert r.status == 403
    r = await env.client.get("/api/files/list?path=", headers=_h(env, token))
    assert "link" not in [e["name"] for e in (await r.json())["entries"]]


async def test_root_delete_rejected(env, workspace):
    token = env.login(KNOWN)
    for path in ("", ".", "docs/.."):
        r = await env.client.delete("/api/files", params={"path": path}, headers=_h(env, token, True))
        assert r.status == 403, path
    assert workspace.is_dir()
    r = await env.client.delete("/api/files", params={"path": "docs"}, headers=_h(env, token, True))
    assert r.status == 200 and not (workspace / "docs").exists()


async def test_files_raw_etag_and_conflict(env, workspace):
    token = env.login(KNOWN)
    r = await env.client.get("/api/files/raw?path=docs/a.md", headers=_h(env, token))
    assert await r.text() == "hello"
    etag = r.headers["ETag"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    r = await env.client.put("/api/files/raw?path=docs/a.md", data="v2".encode(),
                             headers={**_h(env, token, True), "If-Match": etag})
    body = await r.json()
    assert r.status == 200 and body["ok"] and body["etag"] != etag
    r = await env.client.put("/api/files/raw?path=docs/a.md", data=b"v3",
                             headers={**_h(env, token, True), "If-Match": etag})
    assert r.status == 409
    assert (workspace / "docs" / "a.md").read_text() == "v2"
    r = await env.client.put("/api/files/raw?path=docs/a.md", data=b"v3",
                             headers={**_h(env, token, True), "If-Match": f'"{body["etag"]}"'})
    assert r.status == 200
    r = await env.client.put("/api/files/raw?path=new/b.txt", data=b"new", headers=_h(env, token, True))
    assert r.status == 200 and (workspace / "new" / "b.txt").read_text() == "new"
    r = await env.client.get("/api/files/raw?path=docs/a.md&download=1", headers=_h(env, token))
    assert r.headers["Content-Disposition"].startswith("attachment")


async def test_files_raw_put_limits(env, workspace, monkeypatch):
    token = env.login(KNOWN)
    monkeypatch.setattr(config.webui, "editor_max_kb", 1)
    r = await env.client.put("/api/files/raw?path=x.txt", data=b"x" * 2000, headers=_h(env, token, True))
    assert r.status == 413
    r = await env.client.put("/api/files/raw?path=x.txt", data=b"\xff\xfe", headers=_h(env, token, True))
    assert r.status == 400


async def test_files_upload_mkdir(env, workspace):
    token = env.login(KNOWN)
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("files", b"1", filename="C:\\Users\\me\\report.pdf", content_type="application/pdf")
    form.add_field("files", b"2", filename="report.pdf", content_type="application/pdf")
    r = await env.client.post("/api/files/upload?dir=new/deep", data=form, headers=_h(env, token, True))
    assert r.status == 200
    assert (await r.json())["names"] == ["report.pdf", "report_1.pdf"]
    assert (workspace / "new" / "deep" / "report_1.pdf").read_bytes() == b"2"
    assert not [p for p in (workspace / "new" / "deep").iterdir() if p.name.startswith(".upload-")]
    for bad in ("..", ".hidden", ""):
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("files", b"1", filename=bad or "x/", content_type="text/plain")
        r = await env.client.post("/api/files/upload?dir=docs", data=form, headers=_h(env, token, True))
        assert r.status == 400, bad
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("files", b"1", filename="x.txt", content_type="text/plain")
    r = await env.client.post("/api/files/upload?dir=browser", data=form, headers=_h(env, token, True))
    assert r.status == 403
    r = await env.client.post("/api/files/mkdir?path=a/b/c", headers=_h(env, token, True))
    assert r.status == 200 and (workspace / "a" / "b" / "c").is_dir()
    r = await env.client.post("/api/files/mkdir?path=docs/a.md", headers=_h(env, token, True))
    assert r.status == 409


# --- review round 1 ------------------------------------------------------------------

async def _first_raw_block(resp) -> str:
    lines = []
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), 2)).decode()
        if line == "\n":
            return "".join(lines)
        lines.append(line)


async def test_sse_without_last_event_id_starts_with_current_rev(env):
    token = env.login(KNOWN)
    env.bus.on_outbound("message", {"recipient": KNOWN, "text": "old", "ts": 1})
    current = env.bus.current_rev()
    async with env.client.get("/api/events", headers=_h(env, token)) as resp:
        assert await _first_raw_block(resp) == f"id: {current}\n"
        env.bus.on_outbound("message", {"recipient": KNOWN, "text": "new", "ts": 2})
        kind, rev, data = await _next_event(resp)
        assert data["text"] == "new" and int(rev) > current


async def test_sse_replay_pages_through_everything(env, monkeypatch):
    monkeypatch.setattr(api_mod, "REPLAY_PAGE_SIZE", 3)
    token = env.login(KNOWN)
    for i in range(8):
        env.bus.on_outbound("message", {"recipient": KNOWN, "text": f"m{i}", "ts": 100 + i})
    async with env.client.get("/api/events", headers={**_h(env, token), "Last-Event-ID": "0"}) as resp:
        texts = [(await _next_event(resp))[2]["text"] for _ in range(8)]
    assert texts == [f"m{i}" for i in range(8)]


async def test_files_raw_supports_range(env, workspace):
    token = env.login(KNOWN)
    r = await env.client.get("/api/files/raw?path=docs/a.md", headers={**_h(env, token), "Range": "bytes=1-3"})
    assert r.status == 206 and await r.read() == b"ell"
    assert r.headers["Content-Security-Policy"] == "sandbox"


async def test_mock_groups_only_for_test_numbers(monkeypatch):
    from src.nixclaw_agent.web import channels as channels_mod

    answers = {
        "http://live": [{"internal_id": "real", "name": "Real", "members": ["+4912345678", "+000001"]}],
        "http://mock": [
            {"internal_id": "real", "name": "Real", "members": ["+000002"]},
            {"internal_id": "fake", "name": "Fake", "members": ["+4987654321"]},
            {"internal_id": "test", "name": "Test", "members": ["+000001", "+4987654321"]},
        ],
    }

    class _Client:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            base = url.split("/v1/")[0]
            return type("R", (), {"raise_for_status": lambda self: None,
                                  "json": lambda self, b=base: answers[b]})()

    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _Client)
    cache = channels_mod.MembershipCache("+000999", "http://live", "http://mock")
    await cache.refresh()
    assert cache.is_member("+4912345678", "signal-group:real")
    assert cache.is_member("+000002", "signal-group:real")
    assert not cache.is_member("+4987654321", "signal-group:fake")
    assert not cache.is_member("+4987654321", "signal-group:test")
    assert cache.is_member("+000001", "signal-group:test")


async def test_files_raw_put_create_only(env, workspace):
    token = env.login(KNOWN)
    hdr = {**_h(env, token, True), "If-None-Match": "*"}
    r = await env.client.put("/api/files/raw?path=docs/a.md", data=b"", headers=hdr)
    assert r.status == 409 and await r.json() == {"error": "file already exists"}
    assert (workspace / "docs" / "a.md").read_text() == "hello"
    os.symlink(workspace / "docs" / "a.md", workspace / "docs" / "link.md")
    r = await env.client.put("/api/files/raw?path=docs/link.md", data=b"", headers=hdr)
    assert r.status == 409 and (workspace / "docs" / "a.md").read_text() == "hello"
    r = await env.client.put("/api/files/raw?path=new/n.md", data=b"", headers=hdr)
    assert r.status == 200 and (await r.json())["etag"]
    assert (workspace / "new" / "n.md").read_text() == ""
    r = await env.client.put("/api/files/raw?path=new/n.md", data=b"x", headers=hdr)
    assert r.status == 409


async def test_files_channel_scoped(env):
    """File endpoints respect the cid query param and enforce membership."""
    group_root = config.paths.data_dir / GROUP
    (group_root / "notes").mkdir(parents=True)
    (group_root / "notes" / "group_file.md").write_text("group content")
    kn_root = config.paths.data_dir / KNOWN
    kn_root.mkdir(parents=True, exist_ok=True)
    (kn_root / "my_file.txt").write_text("personal")

    token = env.login(KNOWN)
    group_cid = encode_cid(GROUP)

    # No cid = default 1:1 workspace
    r = await env.client.get("/api/files/list", params={"path": ""}, headers=_h(env, token))
    names = [e["name"] for e in (await r.json())["entries"]]
    assert "my_file.txt" in names
    assert "notes" not in names

    # Group workspace via cid (KNOWN is a member of GROUP)
    r = await env.client.get("/api/files/list", params={"path": "", "cid": group_cid},
                             headers=_h(env, token))
    names = [e["name"] for e in (await r.json())["entries"]]
    assert "notes" in names
    assert "my_file.txt" not in names

    # Raw read from the group workspace
    r = await env.client.get("/api/files/raw", params={"path": "notes/group_file.md", "cid": group_cid},
                             headers=_h(env, token))
    assert r.status == 200
    assert await r.text() == "group content"

    # Write + delete into the group workspace
    r = await env.client.put("/api/files/raw?path=new.txt&cid=" + group_cid, data=b"hi",
                             headers=_h(env, token, True))
    assert r.status == 200
    assert (group_root / "new.txt").read_text() == "hi"
    r = await env.client.delete("/api/files", params={"path": "new.txt", "cid": group_cid},
                                headers=_h(env, token, True))
    assert r.status == 200 and not (group_root / "new.txt").exists()

    # Non-member (KNOWN not in OTHER_GROUP) gets 403
    r = await env.client.get("/api/files/list",
                             params={"path": "", "cid": encode_cid(OTHER_GROUP)},
                             headers=_h(env, token))
    assert r.status == 403

    # Invalid cid gets 403
    r = await env.client.get("/api/files/list", params={"path": "", "cid": "!!!"},
                             headers=_h(env, token))
    assert r.status == 403


# --- members endpoint (mention menu) -------------------------------------------------

async def test_members_group_lists_members_and_bot(env):
    token = env.login(KNOWN)
    r = await env.client.get(f"/api/channels/{encode_cid(GROUP)}/members", headers=_h(env, token))
    assert r.status == 200
    rows = (await r.json())["members"]
    # members sorted by number, then the bot; names from the contact profile names
    assert [m["identifier"] for m in rows] == [PRIV, KNOWN, BOT]
    assert [m["uuid"] for m in rows] == [CONTACT_UUIDS[PRIV], CONTACT_UUIDS[KNOWN], ""]
    assert [m["bot"] for m in rows] == [False, False, True]
    assert [m["name"] for m in rows] == [
        CONTACT_PROFILE_NAMES[PRIV], CONTACT_PROFILE_NAMES[KNOWN], config.agent.name,
    ]


async def test_members_direct_channel_is_empty(env):
    token = env.login(KNOWN)
    r = await env.client.get(f"/api/channels/{encode_cid(KNOWN)}/members", headers=_h(env, token))
    assert r.status == 200 and (await r.json()) == {"members": []}


async def test_members_access_control(env):
    # anonymous -> 401, foreign channel -> 403
    assert (await env.client.get(f"/api/channels/{encode_cid(GROUP)}/members")).status == 401
    token = env.login(KNOWN)
    r = await env.client.get(f"/api/channels/{encode_cid(OTHER_GROUP)}/members", headers=_h(env, token))
    assert r.status == 403


async def test_members_name_preference(env):
    """display_name (last seen Signal profile name) > contact profile_name > number."""
    token = env.login(KNOWN)
    stranger = "+000009"  # member without a contact entry
    env.membership.set_groups({GROUP: GroupInfo(channel_id=GROUP, name="Family",
                                                members={PRIV, KNOWN, stranger})})
    rows = (await (await env.client.get(f"/api/channels/{encode_cid(GROUP)}/members",
                                        headers=_h(env, token))).json())["members"]
    assert [m["name"] for m in rows[:3]] == [
        CONTACT_PROFILE_NAMES[PRIV], CONTACT_PROFILE_NAMES[KNOWN], stranger,
    ]
    assert rows[2]["uuid"] == ""
    env.bus.note_name(KNOWN, "Kid Nickname")
    rows = (await (await env.client.get(f"/api/channels/{encode_cid(GROUP)}/members",
                                        headers=_h(env, token))).json())["members"]
    assert rows[1]["name"] == "Kid Nickname"


async def test_members_never_exposes_owner_contact_label(env):
    """The contact `name` field (the bot owner's private label) is never returned."""
    env.contacts.set_contacts([{"uuid": "u-1", "number": PRIV, "name": "Secret Label",
                                "profile_name": "Public Name"}])
    token = env.login(KNOWN)
    rows = (await (await env.client.get(f"/api/channels/{encode_cid(GROUP)}/members",
                                        headers=_h(env, token))).json())["members"]
    assert rows[0]["name"] == "Public Name"
    assert "Secret Label" not in await (await env.client.get(
        f"/api/channels/{encode_cid(GROUP)}/members", headers=_h(env, token))).text()


# --- mentions on POST /messages -------------------------------------------------------

def _mention_form(text: str, mentions, client_id: str | None = None) -> aiohttp.FormData:
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("text", text)
    if client_id:
        form.add_field("client_id", client_id)
    form.add_field("mentions", mentions if isinstance(mentions, str) else json.dumps(mentions),
                 content_type="application/json")  # forces multipart, like fetch()
    return form


async def _post(env, channel_id: str, form: aiohttp.FormData, token: str):
    return await env.client.post(f"/api/channels/{encode_cid(channel_id)}/messages",
                                 data=form, headers=_h(env, token, True))


async def test_post_message_with_mentions_forwards_uuid_authors(env):
    token = env.login(PRIV)
    form = _mention_form("hi @My Good Friend @My Good Friend again",
                         [{"token": "@My Good Friend", "author": KNOWN},
                          {"token": "@My Good Friend", "author": KNOWN}])  # deduped
    r = await _post(env, GROUP, form, token)
    assert r.status == 200, await r.text()
    # history keeps the plain display text, deduped UUID entries go to core
    assert (await r.json())["message"]["text"] == "hi @My Good Friend @My Good Friend again"
    assert env.agent.submitted_mentions[-1] == [MentionEntry(author=CONTACT_UUIDS[KNOWN],
                                                             token="@My Good Friend")]
    assert env.agent.submitted[-1][:3] == (PRIV, GROUP, "hi @My Good Friend @My Good Friend again")


async def test_post_message_mentions_are_group_only(env):
    token = env.login(KNOWN)
    form = _mention_form("hi @Privileged Pat", [{"token": "@Privileged Pat", "author": PRIV}])
    r = await _post(env, KNOWN, form, token)
    assert r.status == 200
    assert env.agent.submitted_mentions[-1] is None


async def test_post_message_mentions_capped_at_twenty(env):
    token = env.login(PRIV)
    names = [f"@Mate{i}" for i in range(25)]
    form = _mention_form(" ".join(names), [{"token": t, "author": KNOWN} for t in names])
    assert (await _post(env, GROUP, form, token)).status == 200
    entries = env.agent.submitted_mentions[-1]
    assert entries is not None and len(entries) == 20
    assert entries[0].author == CONTACT_UUIDS[KNOWN]


async def test_post_message_mentions_of_others_are_dropped(env):
    token = env.login(PRIV)
    form = _mention_form("hi @Known Kid @Not A Member",
                         [{"token": "@Not A Member", "author": "+000042"},   # not a member
                          {"token": "@Unknown Ursula", "author": UNKNOWN},   # not in this group
                          {"token": "@Nixclaw", "author": BOT}])  # the bot
    assert (await _post(env, GROUP, form, token)).status == 200
    assert env.agent.submitted_mentions[-1] is None


async def test_post_message_mentions_never_forward_a_number_author(env):
    """An author the ContactBook does not know degrades to plain text."""
    env.contacts._by_key = {}  # empty cache: every lookup misses
    token = env.login(PRIV)
    form = _mention_form("hi @Known Kid", [{"token": "@Known Kid", "author": KNOWN}])
    assert (await _post(env, GROUP, form, token)).status == 200
    assert env.agent.submitted_mentions[-1] is None


async def test_post_message_mentions_token_must_occur_in_text(env):
    token = env.login(PRIV)
    form = _mention_form("no tokens here", [{"token": "@Known Kid", "author": KNOWN}])
    assert (await _post(env, GROUP, form, token)).status == 200
    assert env.agent.submitted_mentions[-1] is None


async def test_post_message_mentions_bot_row_is_dropped(env):
    """The members list offers the bot's own row (identifier = bot number,
    empty uuid): selecting it passes the membership check (the bot is allowed in
    its own channel), but the bot is not in its own contact list, so the
    cache-only lookup misses and the entry drops to plain text - never
    forwarded as a non-UUID author. The message is still sent, and the miss
    only kicks the throttled background refresh (no permanent breakage)."""
    token = env.login(PRIV)
    form = _mention_form("hi @Nixclaw", [{"token": "@Nixclaw", "author": BOT}])
    r = await _post(env, GROUP, form, token)
    assert r.status == 200, await r.text()
    # the display text survives untouched, no mention entry is forwarded
    assert (await r.json())["message"]["text"] == "hi @Nixclaw"
    assert env.agent.submitted_mentions[-1] is None
    # the next real-member mention still works: the throttle does not break
    # the contact book (entry dropped this round, cache intact)
    form = _mention_form("hi @Known Kid", [{"token": "@Known Kid", "author": KNOWN}])
    assert (await _post(env, GROUP, form, token)).status == 200
    assert env.agent.submitted_mentions[-1] == [MentionEntry(author=CONTACT_UUIDS[KNOWN],
                                                             token="@Known Kid")]


async def test_post_message_mentions_bad_input_never_fails_the_message(env):
    token = env.login(PRIV)
    cases = [
        "not json at all",                      # broken JSON
        '{"token": "@Known Kid"}',              # dict, not a list
        "[]",                                    # empty list
        "[1, null, []]",                         # non-dict items
        '[{"token": "Known Kid", "author": "+000001"}]',   # token without '@'
        '[{"token": "@", "author": "+000001"}]',            # blank remainder
        json.dumps([{"token": "@" + "a" * 100, "author": KNOWN}]),  # over-long token
        '[{"author": "+000001"}]',               # no token
        '[{"token": "@Known Kid"}]',             # no author
        '[{"token": "@Known Kid", "author": 7}]',  # non-str author
    ]
    for raw in cases:
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("text", "@Known Kid ping")
        form.add_field("mentions", raw, content_type="application/json")
        r = await _post(env, GROUP, form, token)
        assert r.status == 200, (raw, await r.text())
    assert all(m is None for m in env.agent.submitted_mentions)
    assert len(env.agent.submitted) == len(cases)


async def test_post_message_oversized_mentions_part_is_ignored(env):
    token = env.login(PRIV)
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("text", "@Known Kid ping")
    form.add_field("mentions", json.dumps([{"token": "@Known Kid", "author": KNOWN}])
                   + " " * (20 * 1024), content_type="application/json")
    r = await _post(env, GROUP, form, token)
    assert r.status == 200, await r.text()
    assert (await r.json())["message"]["text"] == "@Known Kid ping"
    assert env.agent.submitted_mentions[-1] is None


async def test_post_message_retry_with_same_client_id_does_not_reprocess_mentions(env):
    token = env.login(PRIV)
    mentions = json.dumps([{"token": "@Known Kid", "author": KNOWN}])
    form = aiohttp.FormData(quote_fields=False)
    form.add_field("text", "hi @Known Kid")
    form.add_field("client_id", "c1")
    form.add_field("mentions", mentions, content_type="application/json")
    first = (await _post(env, GROUP, form, token)).json()
    row_id = (await first)["message"]["id"]
    # same client_id: the retry short-circuits before any mention processing
    again = await _post(env, GROUP, _mention_form("different text", mentions, client_id="c1"), token)
    assert (await again.json())["message"]["id"] == row_id
    assert len(env.agent.submitted) == 1 and len(env.agent.submitted_mentions) == 1
    assert env.agent.submitted_mentions[0] == [MentionEntry(author=CONTACT_UUIDS[KNOWN],
                                                            token="@Known Kid")]


# --- ContactBook (mention UUIDs + public profile names) ------------------------------

class _ContactsClient:
    """httpx.AsyncClient stand-in serving canned /v1/contacts/{bot} answers."""

    def __init__(self, timeout: float) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        base = url.split("/v1/")[0]
        data = _ContactsClient.answers[base]
        if isinstance(data, Exception):
            raise data
        return type("R", (), {"raise_for_status": lambda self: None,
                              "json": lambda self, d=data: d})()

    answers: dict[str, object] = {}


async def test_contact_book_fetch_mock_contributes_only_test_numbers(monkeypatch):
    _ContactsClient.answers = {
        "http://live": [{"uuid": "UUID-LIVE", "number": "+4912345678",
                         "name": "Owner Label", "profile_name": "Live Public"}],
        "http://mock": [
            {"uuid": "UUID-MOCK", "number": "+000001", "name": "Owner Label 2", "profile_name": "Kid"},
            {"uuid": "UUID-NO", "number": "+4999999999", "name": "x", "profile_name": "Nope"},
            {"number": "+000003", "name": "no uuid"},          # unusable without a uuid
            "not a dict",
        ],
    }
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _ContactsClient)
    book = channels_mod.ContactBook("+000999", "http://live", "http://mock")
    await book.ensure_fresh()
    assert book.lookup("+4912345678") == "UUID-LIVE"
    assert book.lookup("+000001") == "UUID-MOCK"
    assert book.lookup("+4999999999") is None        # mock feed: +0000* only
    assert book.lookup("+000003") is None             # dropped: no uuid
    assert book.lookup("+000004") is None
    # profile_name only; the owner-side contact label never leaks
    assert book.profile_name_for("+4912345678") == "Live Public"
    assert book.profile_name_for("+000001") == "Kid"
    assert book.profile_name_for("+000004") is None
    # lookup is case-insensitive on both number and uuid
    assert book.lookup("uuid-live") == "UUID-LIVE"


async def test_contact_book_failure_keeps_old_data_and_delays_retry(monkeypatch):
    _ContactsClient.answers = {"http://live": [{"uuid": "U1", "number": "+4912345678",
                                                "name": "n", "profile_name": "p"}]}
    monkeypatch.setattr(channels_mod.httpx, "AsyncClient", _ContactsClient)
    book = channels_mod.ContactBook("+000999", "http://live")
    await book.refresh()
    assert book.lookup("+4912345678") == "U1"
    _ContactsClient.answers = {"http://live": RuntimeError("signal-api down")}
    await book.refresh()
    assert book.lookup("+4912345678") == "U1"
    # The failed fetch keeps the old rows and holds off the next refresh; once
    # that window passes the cache is stale again (same rule as MembershipCache).
    assert not book._stale()
    book._fetched_at -= channels_mod.RETRY_AFTER_FAILURE_SECONDS
    assert book._stale()


async def test_contact_book_lookup_on_miss_kicks_one_background_refresh(monkeypatch):
    started = []

    class _Book(channels_mod.ContactBook):
        async def refresh(self) -> None:
            started.append(1)
            await asyncio.sleep(0)

    book = _Book(BOT, "")
    # A miss never blocks the caller; consecutive misses share one refresh task.
    assert book.lookup("+4912345678") is None
    assert book.lookup("+4912345679") is None
    assert len(started) <= 1
    await asyncio.gather(book._refresh_task)
    assert started == [1]
