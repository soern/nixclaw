# Mock websites for browser-tool robustness testing

A dependency-free Node HTTP server (`server.js`) that serves 18 mock sites
simulating real-world website behaviors the agent has struggled with. It runs
**inside the nixclaw-browser container** (mounted at `/mock_sites`, started
alongside `playwright run-server`) and listens on port **8932**.

Because the headless browser lives in the same container and the port is
published, the identical URL `http://localhost:8932/<route>` works from three
perspectives: the Playwright browser, the nixclaw agent (host network), and
host-side test scripts.

Every site embeds a unique **token** that only becomes readable once the site's
obstacle is overcome — tests assert on the token, not on fuzzy behavior.

| Route | Behavior | Token (proof of success) |
|---|---|---|
| `/captcha-grid` | Image-grid captcha: select the 3 tiles with red circles (tiles 2, 5, 7; SVG only, invisible to the aria tree), press Verify | `GRID-SOLVED-7391` |
| `/captcha-text` | Canvas-rendered distorted code `X7K4P9` (never in the DOM), type + submit | `TEXT-SOLVED-2846` |
| `/cookie-simple` | Bottom banner, single Accept button | `CAKE-1755` (visible before accept) |
| `/cookie-modal` | Full-screen consent modal (Accept all / Only necessary / Manage) | `FERRY-62-NIGHT` |
| `/cookie-nested` | Consent needs "Manage settings" → "Save selection" (or "Agree & continue") | `SOLARIS-87` |
| `/cookie-delayed` | Banner slides in 3 s after load, covers lower half | `LUEBECK-350` |
| `/quirk-404` | Friendly page served with **HTTP 404** | — (test asserts `HTTP status: 404` in snapshot) |
| `/quirk-jsonly` | Empty HTML shell; content rendered synchronously by JS | `JSONLY-1432` |
| `/quirk-scroll` | Infinite scroll; target listing on feed page 5 (item #42) | `SCROLL-GEM-4242` |
| `/quirk-redirect` | Meta-refresh (1 s) → JS `location.replace` (0.8 s) → final page | `REDIR-FINAL-21` |
| `/quirk-popup` | Newsletter modal with small × appears after 2 s | `HYDRANGEA-FEB` |
| `/quirk-paywall` | Article teaser; the rest genuinely absent from the DOM | — (agent should report the paywall, not invent content) |
| `/quirk-ua` | **403** + block page for `Headless*` user agents, content otherwise | `UA-HUMAN-0530` (only with non-headless UA) |
| `/quirk-longpoll` | A fetch that never completes — network never goes idle | `LONGPOLL-21847` (in static content) |
| `/quirk-huge` | ~150 KB page, far beyond `SNAPSHOT_MAX_CHARS` | `MEGA-CLEAR-5512` (at the very bottom, truncated away) |
| `/quirk-iframe` | Main content inside an iframe (aria snapshot doesn't descend) | `IFRAME-HOURS-0920` |
| `/slow-server` | Server holds the response 20 s (< 30 s navigation timeout) | `SLOW-SRV-2421` |
| `/slow-ajax` | Instant shell, results arrive via 8 s AJAX call | `SLOW-AJAX-OK-9174` |

`/` serves an index of all routes.

## Running locally (outside Docker)

```bash
node tests/fixtures/mock_sites/server.js   # listens on 0.0.0.0:8932
```

## Tests using these sites

- `tests/test_browser_tools_mock_sites.py` — deterministic tool-level
  integration tests (drives BrowserManager/browse tools directly; needs the
  browser container + this server).
- `tests/e2e_mock_sites.sh` — LLM-driven e2e scenarios via the mock Signal
  API as KNOWN user `+000001` (needs the full test stack + vLLM).
