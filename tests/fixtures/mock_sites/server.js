// Mock-website server for browser-tool robustness testing.
//
// Runs inside the nixclaw-browser container (plain Node, no dependencies)
// alongside `playwright run-server`. The headless browser reaches it as
// http://localhost:8932 and, because the port is published, so do
// host-side test scripts.
//
// Static pages live in sites/; dynamic behaviors (delays, status codes,
// UA sniffing) are implemented as routes below.

'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.env.MOCK_SITES_PORT || '8932', 10);
const SITES_DIR = path.join(__dirname, 'sites');

function send(res, status, body, type = 'text/html; charset=utf-8', extra = {}) {
  res.writeHead(status, Object.assign({ 'Content-Type': type, 'Cache-Control': 'no-store' }, extra));
  res.end(body);
}

function page(res, file, status = 200) {
  fs.readFile(path.join(SITES_DIR, file), (err, data) => {
    if (err) return send(res, 500, `missing fixture: ${file}`);
    send(res, status, data);
  });
}

// Routes that simply serve a static fixture with status 200.
const STATIC_ROUTES = {
  '/': 'index.html',
  '/captcha-grid': 'captcha-grid.html',
  '/captcha-text': 'captcha-text.html',
  '/cookie-simple': 'cookie-simple.html',
  '/cookie-modal': 'cookie-modal.html',
  '/cookie-nested': 'cookie-nested.html',
  '/cookie-delayed': 'cookie-delayed.html',
  '/quirk-jsonly': 'quirk-jsonly.html',
  '/quirk-scroll': 'quirk-scroll.html',
  '/quirk-redirect': 'quirk-redirect-hop1.html',
  '/quirk-redirect-hop2': 'quirk-redirect-hop2.html',
  '/quirk-redirect-final': 'quirk-redirect-final.html',
  '/quirk-popup': 'quirk-popup.html',
  '/quirk-paywall': 'quirk-paywall.html',
  '/quirk-longpoll': 'quirk-longpoll.html',
  '/quirk-iframe': 'quirk-iframe.html',
  '/quirk-iframe-content': 'quirk-iframe-content.html',
  '/slow-ajax': 'slow-ajax.html',
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const route = url.pathname.replace(/\/+$/, '') || '/';

  if (STATIC_ROUTES[route]) return page(res, STATIC_ROUTES[route]);

  switch (route) {
    // --- quirks with dynamic behavior -------------------------------
    case '/quirk-404':
      return page(res, 'quirk-404.html', 404);

    case '/quirk-ua': {
      const ua = req.headers['user-agent'] || '';
      if (/headless/i.test(ua)) return page(res, 'quirk-ua-blocked.html', 403);
      return page(res, 'quirk-ua-allowed.html');
    }

    case '/quirk-huge': {
      // Way past SNAPSHOT_MAX_CHARS (15000) to exercise truncation.
      let body = '<!DOCTYPE html><html><head><title>MegaMart - Full Catalog</title></head><body>' +
        '<h1>MegaMart Full Product Catalog</h1>' +
        '<p>Support hotline: 0800-MEGAMART-42 (listed again at the very end of this page).</p>';
      for (let i = 1; i <= 900; i++) {
        body += `<div><h3>Product ${i}: Deluxe Widget Model ${i}</h3>` +
          `<p>SKU MW-${1000 + i}. High quality widget number ${i} with extended warranty, ` +
          'free shipping on orders above 50 EUR, available in silver, black and forest green.</p></div>';
      }
      body += '<p id="bottom-fact">Clearance code for newsletter readers: MEGA-CLEAR-5512</p></body></html>';
      return send(res, 200, body);
    }

    // --- slow sites -------------------------------------------------
    case '/slow-server':
      // Server holds the response; under the 30s navigation timeout.
      return setTimeout(() => page(res, 'slow-server.html'), 20000);

    // --- API endpoints used by the pages ----------------------------
    case '/api/slow-data':
      return setTimeout(() => send(res, 200, JSON.stringify({
        concerts: [
          { artist: 'The Fjord Quartet', venue: 'Elbphilharmonie', date: '2026-07-18' },
          { artist: 'Nebelhorn Brass', venue: 'Stadtpark Open Air', date: '2026-07-19' },
          { artist: 'DJ Quirin', venue: 'Uebel & Gefaehrlich', date: '2026-07-19' },
        ],
        code: 'SLOW-AJAX-OK-9174',
      }), 'application/json'), 8000);

    case '/api/longpoll':
      // Never responds within any sane window; keeps network busy so
      // "networkidle" never fires. Socket closes with the request.
      return setTimeout(() => send(res, 200, '{}', 'application/json'), 300000);

    case '/api/scroll': {
      const pageNo = parseInt(url.searchParams.get('page') || '1', 10);
      if (pageNo > 5) return send(res, 200, JSON.stringify({ items: [], done: true }), 'application/json');
      const items = [];
      for (let i = (pageNo - 1) * 10 + 1; i <= pageNo * 10; i++) {
        items.push({
          id: i,
          text: i === 42
            ? 'Listing 42: WANTED - vintage synthesizer, contact code SCROLL-GEM-4242'
            : `Listing ${i}: ordinary classified ad number ${i}, nothing special.`,
        });
      }
      return setTimeout(() => send(res, 200, JSON.stringify({ items, done: false }), 'application/json'), 300);
    }

    default:
      return send(res, 404, '<h1>404 - no such mock route</h1><p>See <a href="/">the index</a>.</p>', 'text/html; charset=utf-8');
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`mock-sites server listening on :${PORT}`);
});
