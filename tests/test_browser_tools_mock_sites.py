"""Integration tests: browser tools vs. the mock websites.

Requires the nixclaw-browser container (Playwright server on :8931 and the
mock-sites server from tests/fixtures/mock_sites on :8932):

    docker-compose up -d browser

The whole module skips cleanly when either port is unreachable, so it is
safe in offline runs. No LLM involved - these tests are deterministic and
assert on the unique tokens embedded in each mock site (see
tests/fixtures/mock_sites/README.md).
"""

import asyncio
import socket
import sys
import urllib.request
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nixclaw_agent.config import config
from nixclaw_agent.tools import browse
from nixclaw_agent.tools import browser_client
from nixclaw_agent.tools.browser_client import BrowserManager, SNAPSHOT_MAX_CHARS

WS_PORT = 8931
MOCK_BASE = "http://localhost:8932"


def _stack_available() -> bool:
    try:
        with socket.create_connection(("localhost", WS_PORT), timeout=2):
            pass
        with urllib.request.urlopen(f"{MOCK_BASE}/", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _stack_available(),
        reason="browser container / mock-sites server not reachable on :8931/:8932",
    ),
]


@pytest_asyncio.fixture
async def channel(monkeypatch, tmp_path):
    """Fresh manager + unique channel per test; keeps data/ clean."""
    monkeypatch.setattr(config.paths, "data_dir", tmp_path)
    monkeypatch.setenv("BROWSER_WS_URL", f"ws://localhost:{WS_PORT}")
    manager = BrowserManager()
    monkeypatch.setattr(browser_client, "manager", manager)
    monkeypatch.setattr(browse, "manager", manager)
    channel_id = f"mocktest-{uuid.uuid4().hex[:8]}"
    yield channel_id
    await manager.close()


# ---------------------------------------------------------------------------
# Baseline: a normal site works
# ---------------------------------------------------------------------------

async def test_normal_site_readable(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/cookie-simple", channel_id=channel)
    assert result.success, result.error
    assert "CAKE-1755" in result.output


# ---------------------------------------------------------------------------
# Cookie banners
# ---------------------------------------------------------------------------

async def test_cookie_simple_accept(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/cookie-simple", channel_id=channel)
    result = await browse.browser_interact("click", target="#accept-cookies", channel_id=channel)
    assert result.success, result.error
    assert "We use cookies" not in result.output
    assert "CAKE-1755" in result.output


async def test_cookie_modal_reject(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/cookie-modal", channel_id=channel)
    result = await browse.browser_interact("click", target="#reject-all", channel_id=channel)
    assert result.success, result.error
    assert "We value your privacy" not in result.output
    assert "FERRY-62-NIGHT" in result.output


async def test_cookie_nested_settings_path(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/cookie-nested", channel_id=channel)
    result = await browse.browser_interact("click", target="#open-settings", channel_id=channel)
    assert result.success, result.error
    assert "Save selection" in result.output
    result = await browse.browser_interact("click", target="#save-selection", channel_id=channel)
    assert result.success, result.error
    assert "Before you continue" not in result.output
    assert "SOLARIS-87" in result.output


async def test_cookie_delayed_banner_dismiss(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/cookie-delayed", channel_id=channel)
    await asyncio.sleep(3.5)  # banner slides in after 3s
    result = await browse.browser_interact("click", target="#late-accept", channel_id=channel)
    assert result.success, result.error
    assert "LUEBECK-350" in result.output
    assert "Understood, accept" not in result.output


# ---------------------------------------------------------------------------
# Captchas: the page must remain usable so the screenshot->vision->click
# flow can solve it.
# ---------------------------------------------------------------------------

async def test_captcha_grid_is_flagged_but_page_stays_usable(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/captcha-grid", channel_id=channel)
    # The captcha must be signalled to the agent together with solving
    # guidance - and the page content must NOT be withheld, otherwise the
    # agent cannot find the elements to click.
    text = (result.output or "") + (result.error or "")
    assert "CAPTCHA" in text.upper()
    assert "browser_screenshot" in text, "captcha result must point to the screenshot/vision flow"
    assert "square" in text, "snapshot with clickable elements must be included"


async def test_captcha_grid_screenshot_renders(channel):
    manager = browser_client.manager
    await manager.navigate(channel, f"{MOCK_BASE}/captcha-grid")
    data = await manager.screenshot_bytes(channel)
    assert data.startswith(b"\x89PNG")
    assert len(data) > 5000  # an actually rendered page, not a blank strip


async def test_captcha_grid_solvable_by_clicks(channel):
    """Simulates the clicks a vision model would instruct (tiles 2, 5, 7)."""
    await browse.browser_navigate(f"{MOCK_BASE}/captcha-grid", channel_id=channel)
    for tile in ("#tile-2", "#tile-5", "#tile-7"):
        result = await browse.browser_interact("click", target=tile, channel_id=channel)
        assert result.success, result.error
    result = await browse.browser_interact("click", target="#verify", channel_id=channel)
    assert result.success, result.error
    assert "GRID-SOLVED-7391" in result.output


async def test_invalid_selector_gets_helpful_error(channel):
    """The exact failure observed in e2e: LLM invents 'button "square 2"'."""
    await browse.browser_navigate(f"{MOCK_BASE}/captcha-grid", channel_id=channel)
    result = await browse.browser_interact(
        "click", target='button "square 2"', channel_id=channel
    )
    assert not result.success
    assert "Accepted selector forms" in result.error
    assert "role=button" in result.error


async def test_captcha_grid_wrong_tiles_fail(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/captcha-grid", channel_id=channel)
    await browse.browser_interact("click", target="#tile-1", channel_id=channel)
    result = await browse.browser_interact("click", target="#verify", channel_id=channel)
    assert result.success, result.error
    assert "Verification failed" in result.output
    assert "GRID-SOLVED-7391" not in result.output


async def test_captcha_text_solvable_by_typing(channel):
    """Simulates the answer a vision model would read from the canvas."""
    await browse.browser_navigate(f"{MOCK_BASE}/captcha-text", channel_id=channel)
    result = await browse.browser_interact("type", target="#answer", value="X7K4P9", channel_id=channel)
    assert result.success, result.error
    result = await browse.browser_interact("click", target="#submit", channel_id=channel)
    assert result.success, result.error
    assert "TEXT-SOLVED-2846" in result.output


async def test_captcha_text_code_not_in_snapshot(channel):
    """The canvas code must not leak into the text snapshot (vision required)."""
    result = await browse.browser_navigate(f"{MOCK_BASE}/captcha-text", channel_id=channel)
    text = (result.output or "") + (result.error or "")
    assert "X7K4P9" not in text


# ---------------------------------------------------------------------------
# Quirks
# ---------------------------------------------------------------------------

async def test_404_status_surfaced(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/quirk-404", channel_id=channel)
    assert result.success, result.error
    assert "HTTP status: 404" in result.output


async def test_ua_block_status_surfaced(channel):
    # Headless chromium is blocked with 403 by the mock bot protection.
    result = await browse.browser_navigate(f"{MOCK_BASE}/quirk-ua", channel_id=channel)
    assert result.success, result.error
    assert "HTTP status: 403" in result.output
    assert "Automated traffic detected" in result.output


async def test_jsonly_content_rendered(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/quirk-jsonly", channel_id=channel)
    assert result.success, result.error
    assert "JSONLY-1432" in result.output


async def test_redirect_chain_settles(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/quirk-redirect", channel_id=channel)
    await asyncio.sleep(3)  # meta refresh (1s) + JS replace (0.8s)
    result = await browse.browser_navigate(channel_id=channel)  # re-snapshot
    assert result.success, result.error
    assert "REDIR-FINAL-21" in result.output


async def test_popup_dismissable(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/quirk-popup", channel_id=channel)
    await asyncio.sleep(2.5)  # popup appears after 2s
    result = await browse.browser_interact("click", target="#nl-close", channel_id=channel)
    assert result.success, result.error
    assert "Don't miss out" not in result.output
    assert "HYDRANGEA-FEB" in result.output


async def test_paywall_content_absent(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/quirk-paywall", channel_id=channel)
    assert result.success, result.error
    assert "Read on with WK Premium" in result.output
    assert "subscribers only" in result.output


async def test_longpoll_snapshot_not_blocked(channel):
    # The page keeps one request in flight forever; navigation and snapshot
    # must still return promptly (networkidle wait is capped best-effort).
    result = await asyncio.wait_for(
        browse.browser_navigate(f"{MOCK_BASE}/quirk-longpoll", channel_id=channel),
        timeout=15,
    )
    assert result.success, result.error
    assert "LONGPOLL-21847" in result.output


async def test_huge_page_truncated(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/quirk-huge", channel_id=channel)
    assert result.success, result.error
    assert len(result.output) <= SNAPSHOT_MAX_CHARS
    assert result.output.endswith("[truncated]")
    assert "MEGAMART" in result.output.upper()


async def test_iframe_content_reachable_somehow(channel):
    """Documents the aria-snapshot iframe limitation and the workaround."""
    result = await browse.browser_navigate(f"{MOCK_BASE}/quirk-iframe", channel_id=channel)
    assert result.success, result.error
    if "IFRAME-HOURS-0920" not in result.output:
        # Snapshot doesn't descend into the iframe; navigating to the frame
        # src directly must work as fallback.
        result = await browse.browser_navigate(
            f"{MOCK_BASE}/quirk-iframe-content", channel_id=channel
        )
        assert result.success, result.error
        assert "IFRAME-HOURS-0920" in result.output


async def test_infinite_scroll_reaches_target(channel):
    await browse.browser_navigate(f"{MOCK_BASE}/quirk-scroll", channel_id=channel)
    for _ in range(6):
        await browse.browser_evaluate(
            "window.scrollTo(0, document.body.scrollHeight)", channel_id=channel
        )
        await asyncio.sleep(0.7)
    result = await browse.browser_evaluate(
        "document.body.innerText", channel_id=channel
    )
    assert result.success, result.error
    assert "SCROLL-GEM-4242" in result.output


# ---------------------------------------------------------------------------
# Slow sites
# ---------------------------------------------------------------------------

async def test_slow_server_within_timeout(channel):
    # Server holds the response for 20s; navigation timeout is 30s.
    result = await browse.browser_navigate(f"{MOCK_BASE}/slow-server", channel_id=channel)
    assert result.success, result.error
    assert "SLOW-SRV-2421" in result.output


async def test_slow_ajax_content_after_wait(channel):
    result = await browse.browser_navigate(f"{MOCK_BASE}/slow-ajax", channel_id=channel)
    assert result.success, result.error
    assert "Loading results" in result.output  # snapshot taken before AJAX lands
    await asyncio.sleep(9)
    result = await browse.browser_navigate(channel_id=channel)  # re-snapshot
    assert result.success, result.error
    assert "SLOW-AJAX-OK-9174" in result.output
