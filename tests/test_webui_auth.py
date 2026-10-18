"""Web interface login: codes, rate limits, tokens, CSRF (web/auth.py, web/api.py)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from src.nixclaw_agent.config import config
from src.nixclaw_agent.db import operations as db
from src.nixclaw_agent.web import api as api_mod
from src.nixclaw_agent.web.auth import MAX_CODE_ATTEMPTS, hash_token, parse_web_command
from webui_support import CSRF, KNOWN, PRIV, UNKNOWN, env, webui_config  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _sent_code(e, number: str) -> str:
    await asyncio.gather(*e.web_auth._tasks)
    args, kwargs = e.signal.send_message.call_args
    assert args[0] == number
    assert kwargs.get("publish") is False
    return e.web_auth._codes[number].code


async def _request(e, number: str, headers=None):
    return await e.client.post("/api/auth/request", json={"number": number}, headers={**CSRF, **(headers or {})})


async def test_code_login_sets_cookie_and_is_single_use(env):
    r = await _request(env, KNOWN)
    assert r.status == 200 and await r.json() == {"ok": True}
    code = await _sent_code(env, KNOWN)
    assert "Laptop" not in env.signal.send_message.call_args.args[1]
    r = await env.client.post("/api/auth/verify", headers=CSRF,
                              json={"number": KNOWN, "code": code, "endpoint_name": "Laptop"})
    assert r.status == 200 and await r.json() == {"ok": True, "number": KNOWN}
    set_cookie = r.headers["Set-Cookie"]
    assert "nixclaw_session=" in set_cookie and "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie and "Secure" not in set_cookie
    rows = db.web_endpoints_for(KNOWN)
    assert len(rows) == 1 and rows[0]["name"] == "Laptop"
    token = set_cookie.split("nixclaw_session=")[1].split(";")[0]
    assert rows[0]["token_hash"] == hash_token(token) and token not in rows[0]["token_hash"]
    # single use
    r = await env.client.post("/api/auth/verify", headers=CSRF,
                              json={"number": KNOWN, "code": code, "endpoint_name": "Laptop"})
    assert r.status == 400


async def test_secure_cookie_when_forwarded_https(env):
    await _request(env, KNOWN)
    code = await _sent_code(env, KNOWN)
    r = await env.client.post("/api/auth/verify", headers={**CSRF, "X-Forwarded-Proto": "https"},
                              json={"number": KNOWN, "code": code, "endpoint_name": "Phone"})
    assert "Secure" in r.headers["Set-Cookie"]


async def test_code_expires(env):
    await _request(env, KNOWN)
    code = await _sent_code(env, KNOWN)
    env.web_auth._codes[KNOWN].expires_at = 0
    assert env.web_auth.verify(KNOWN, code, "x") is None
    assert KNOWN not in env.web_auth._codes


async def test_code_attempts_limited(env):
    await _request(env, KNOWN)
    code = await _sent_code(env, KNOWN)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_CODE_ATTEMPTS):
        assert env.web_auth.verify(KNOWN, wrong, "x") is None
    assert env.web_auth.verify(KNOWN, code, "x") is None


async def test_no_enumeration_for_unknown_number(env):
    r = await _request(env, UNKNOWN)
    assert r.status == 200 and await r.json() == {"ok": True}
    await asyncio.gather(*env.web_auth._tasks)
    env.signal.send_message.assert_not_called()
    r = await env.client.post("/api/auth/verify", headers=CSRF,
                              json={"number": UNKNOWN, "code": "123456", "endpoint_name": "x"})
    assert r.status == 400


async def test_malformed_requests(env):
    r = await _request(env, "12345")
    assert r.status == 400
    r = await env.client.post("/api/auth/verify", headers=CSRF,
                              json={"number": KNOWN, "code": "1", "endpoint_name": " "})
    assert r.status == 400


async def test_rate_limit_per_number(env, monkeypatch):
    monkeypatch.setattr(config.webui, "code_requests_per_number_per_hour", 2)
    for i in range(2):
        r = await _request(env, KNOWN, {"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status == 200
    r = await _request(env, KNOWN, {"X-Forwarded-For": "10.0.0.9"})
    assert r.status == 429
    # unknown numbers are limited the same way
    for i in range(2):
        assert (await _request(env, UNKNOWN, {"X-Forwarded-For": f"10.0.1.{i}"})).status == 200
    assert (await _request(env, UNKNOWN, {"X-Forwarded-For": "10.0.1.9"})).status == 429


async def test_rate_limit_per_ip_uses_forwarded_for_from_loopback(env, monkeypatch):
    monkeypatch.setattr(config.webui, "code_requests_per_ip_per_hour", 2)
    numbers = ["+0000100", "+0000101", "+0000102"]
    assert (await _request(env, numbers[0], {"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})).status == 200
    assert (await _request(env, numbers[1], {"X-Forwarded-For": "203.0.113.5"})).status == 200
    assert (await _request(env, numbers[2], {"X-Forwarded-For": "203.0.113.5"})).status == 429
    # a different client IP behind the same proxy is not affected
    assert (await _request(env, numbers[2], {"X-Forwarded-For": "203.0.113.6"})).status == 200


def _mocked(peer: str, headers: dict) -> object:
    transport = MagicMock()
    transport.get_extra_info = lambda name, default=None: (peer, 1234) if name == "peername" else default
    return make_mocked_request("POST", "/api/auth/request", headers=headers, transport=transport)


async def test_forwarded_headers_ignored_from_non_loopback_peer():
    req = _mocked("198.51.100.7", {"X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https"})
    assert api_mod.client_ip(req) == "198.51.100.7"
    assert api_mod.is_https(req) is False
    req = _mocked("127.0.0.1", {"X-Forwarded-For": "1.2.3.4", "X-Forwarded-Proto": "https"})
    assert api_mod.client_ip(req) == "1.2.3.4"
    assert api_mod.is_https(req) is True


async def test_token_expiry(env):
    token = env.login(KNOWN)
    r = await env.client.get("/api/me", headers=env.cookie(token))
    assert r.status == 200
    me = await r.json()
    assert me["number"] == KNOWN and me["tier"] == "known" and me["endpoint"]["name"] == "Laptop"
    assert me["limits"]["max_upload_mb"] == config.webui.max_upload_mb
    with db._db_connection() as conn:
        conn.execute("UPDATE web_endpoints SET expires_at = 1")
        conn.commit()
    r = await env.client.get("/api/me", headers=env.cookie(token))
    assert r.status == 401


async def test_token_expiry_zero_means_never(env, monkeypatch):
    monkeypatch.setattr(config.webui, "token_expiry_days", 0)
    env.login(KNOWN)
    assert db.web_endpoints_for(KNOWN)[0]["expires_at"] is None


async def test_tier_downgrade_gives_401_and_keeps_row(env):
    token = env.login(KNOWN)
    assert (await env.client.get("/api/me", headers=env.cookie(token))).status == 200
    env.auth_manager.update(SimpleNamespace(privileged_numbers=[PRIV], known_numbers=[]))
    assert (await env.client.get("/api/me", headers=env.cookie(token))).status == 401
    assert len(db.web_endpoints_for(KNOWN)) == 1


async def test_no_cookie_or_bad_token_is_401(env):
    assert (await env.client.get("/api/channels")).status == 401
    assert (await env.client.get("/api/channels", headers=env.cookie("nope"))).status == 401


async def test_csrf_header_required(env):
    token = env.login(PRIV)
    r = await env.client.post("/api/auth/request", json={"number": KNOWN})
    assert r.status == 403
    r = await env.client.put("/api/settings", json={}, headers=env.cookie(token))
    assert r.status == 403
    r = await env.client.put("/api/settings", json={}, headers={**env.cookie(token), "X-NixClaw": "0"})
    assert r.status == 403
    r = await env.client.put("/api/settings", json={}, headers={**env.cookie(token), **CSRF})
    assert r.status == 200


async def test_endpoints_list_delete_and_logout(env):
    t1 = env.login(KNOWN, "Laptop")
    t2 = env.login(KNOWN, "Phone")
    other = env.login(PRIV, "Desk")
    r = await env.client.get("/api/endpoints", headers=env.cookie(t1))
    eps = (await r.json())["endpoints"]
    assert [e["name"] for e in eps] == ["Laptop", "Phone"]
    assert [e["current"] for e in eps] == [True, False]
    assert eps[0]["connections"] == 0 and eps[0]["expires_at"] > eps[0]["created_at"]
    # foreign endpoint: 404
    other_id = db.web_endpoints_for(PRIV)[0]["id"]
    r = await env.client.delete(f"/api/endpoints/{other_id}", headers={**env.cookie(t1), **CSRF})
    assert r.status == 404
    r = await env.client.delete(f"/api/endpoints/{eps[1]['id']}", headers={**env.cookie(t1), **CSRF})
    assert r.status == 200
    assert (await env.client.get("/api/me", headers=env.cookie(t2))).status == 401
    r = await env.client.post("/api/auth/logout", headers={**env.cookie(t1), **CSRF})
    assert r.status == 200 and "nixclaw_session=" in r.headers["Set-Cookie"]
    assert (await env.client.get("/api/me", headers=env.cookie(t1))).status == 401
    assert (await env.client.get("/api/me", headers=env.cookie(other))).status == 200


@pytest.mark.parametrize("text,expected", [
    ("/status", ("status", "")),
    ("  /status ", ("status", "")),
    ("/deauth 2", ("deauth", "2")),
    ("/deauth Brian's Laptop", ("deauth", "Brian's Laptop")),
    ("/deauth", ("deauth", "")),
    ("/statusx", None),
    ("/deauthx 1", None),
    ("please /status", None),
    ("", None),
])
async def test_parse_web_command(text, expected):
    assert parse_web_command(text) == expected


async def test_handle_command_status_and_deauth(env):
    env.login(KNOWN, "Laptop")
    env.login(KNOWN, "Phone")
    env.login(KNOWN, "Phone")
    status = env.web_auth.handle_command(KNOWN, "status", "")
    assert "1. Laptop" in status and "2. Phone" in status and "3. Phone" in status
    assert "Several" in env.web_auth.handle_command(KNOWN, "deauth", "phone")
    assert "No web login" in env.web_auth.handle_command(KNOWN, "deauth", "9")
    assert "Usage" in env.web_auth.handle_command(KNOWN, "deauth", "")
    assert "Laptop" in env.web_auth.handle_command(KNOWN, "deauth", "laptop")
    assert "Phone" in env.web_auth.handle_command(KNOWN, "deauth", "1")
    assert [r["name"] for r in db.web_endpoints_for(KNOWN)] == ["Phone"]
    env.web_auth.handle_command(KNOWN, "deauth", "1")
    assert env.web_auth.handle_command(KNOWN, "status", "") == "No web interface logins."
