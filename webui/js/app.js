// Bootstrap: session check, layout (roster / views), SSE event stream.
import { api } from './api.js';
import { $, toast } from './dom.js';
import { state, on, emit, applyTheme, saveSettings, channelByCid } from './state.js';
import { showLogin } from './login.js';
import { renderRoster, channelLabel } from './roster.js';
import { initChat, openChannel, onMessage, onTyping, resetHistory } from './chat.js';
import { initFiles, showFiles } from './files.js';
import { initSettings, showSettings } from './settings.js';

const portrait = matchMedia('(orientation: portrait)');
let events = null;
let lastRev = null;
let retryTimer = null;
let initialized = false;

// ---- layout ------------------------------------------------------------------

function setView(view) {
    state.view = view;
    for (const v of ['chat', 'files', 'settings']) $(`#view-${v}`).hidden = v !== view;
    document.querySelectorAll('.views button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    if (view === 'files') showFiles();
    if (view === 'settings') showSettings();
    updateTitle();
}

function updateTitle() {
    const ch = channelByCid(state.current);
    $('#topbar-title').textContent = state.view === 'chat' ? (ch ? channelLabel(ch) : 'NixClaw')
        : state.view === 'files' ? 'Files' : 'Settings';
    const unread = [...state.unread.values()].reduce((n, s) => n + s.size, 0);
    document.title = unread ? `(${unread}) NixClaw` : 'NixClaw';
}

function applyRosterLayout() {
    const app = $('#app');
    app.classList.toggle('roster-collapsed', !portrait.matches && !!state.settings.roster_collapsed);
    if (!portrait.matches) app.classList.remove('roster-open');
}

function toggleRoster() {
    if (portrait.matches) {
        $('#app').classList.toggle('roster-open');
    } else {
        state.settings.roster_collapsed = !state.settings.roster_collapsed;
        saveSettings();
        applyRosterLayout();
    }
}

// ---- event stream ------------------------------------------------------------

// Resolves once the stream is open. The server starts every fresh stream with
// `id: <current max rev>`, so the browser's own auto-reconnects resume from
// there. Our manual reconnects (after the stream closed for good) pass the last
// rev we saw as ?last_event_id=; if we never saw one, reload history instead.
function connectEvents() {
    clearTimeout(retryTimer);
    if (events) events.close();
    const resume = lastRev;
    events = new EventSource(resume ? `/api/events?last_event_id=${encodeURIComponent(resume)}` : '/api/events');
    const track = (e) => { if (e.lastEventId) lastRev = e.lastEventId; };
    events.addEventListener('message', (e) => {
        track(e);
        try { onMessage(JSON.parse(e.data)); } catch (ex) { console.error('bad message event', ex); }
    });
    events.addEventListener('typing', (e) => onTyping(JSON.parse(e.data)));
    events.addEventListener('channels', () => loadChannels());
    events.addEventListener('deauthed', () => emit('logged-out'));
    const opened = new Promise((resolve) => {
        events.addEventListener('open', () => resolve(true), { once: true });
        setTimeout(() => resolve(false), 5000);   // don't block the UI on a slow stream
    });
    events.onerror = () => {
        if (events.readyState !== EventSource.CLOSED) return;   // browser reconnects itself
        // Closed for good (e.g. 401 / 5xx): check the session, then retry.
        const retry = () => {
            retryTimer = setTimeout(() => connectEvents().then(() => { if (!lastRev) resetHistory(); }), 5000);
        };
        api('GET', '/api/me').then(retry).catch((ex) => { if (ex.status !== 401) retry(); });
    };
    return opened;
}

// ---- data --------------------------------------------------------------------

async function loadChannels(list) {
    try {
        state.channels = list || (await api('GET', '/api/channels')).channels;
    } catch (ex) {
        return toast(`Loading channels failed: ${ex.message}`);
    }
    if (!channelByCid(state.current) && state.channels.length) emit('open-channel', state.channels[0].cid);
    renderRoster();
    updateTitle();
}

async function start() {
    try {
        state.me = await api('GET', '/api/me');
    } catch (ex) {
        $('#boot').hidden = true;
        if (ex.status === 401) return showLogin(start);
        $('#boot').hidden = false;
        $('#boot').textContent = `NixClaw is not reachable (${ex.message}). Retrying…`;
        setTimeout(start, 5000);
        return;
    }
    try {
        const s = await api('GET', '/api/settings');
        state.settings = s && typeof s === 'object' && !Array.isArray(s) ? s : {};
    } catch {
        state.settings = {};
    }
    applyTheme();
    $('#boot').hidden = true;
    $('#login').hidden = true;
    $('#app').hidden = false;
    if (!initialized) {
        initialized = true;
        initChat($('#view-chat'));
        initFiles($('#view-files'));
        initSettings($('#view-settings'));
    }
    applyRosterLayout();
    state.current = null;
    resetHistory();
    lastRev = null;
    // Stream first, history second: anything published while history loads
    // arrives via SSE and is merged by id (no gap between the two).
    await connectEvents();
    await loadChannels();
    setView('chat');
}

function stop() {
    if (events) events.close();
    events = null;
    clearTimeout(retryTimer);
    state.me = null;
    state.unread.clear();
    showLogin(start);
}

// ---- wiring ------------------------------------------------------------------

on('open-channel', (cid) => {
    openChannel(cid);
    $('#app').classList.remove('roster-open');
    setView('chat');
    renderRoster();
});
on('unread-changed', () => { renderRoster(); updateTitle(); });
on('channels', (list) => loadChannels(list));
on('logged-out', () => { if (state.me) stop(); });
window.addEventListener('nixclaw:unauthorized', () => { if (state.me) stop(); });

document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.current && state.unread.delete(state.current)) {
        renderRoster();
        updateTitle();
    }
});

document.addEventListener('DOMContentLoaded', () => {
    $('#roster-toggle').addEventListener('click', toggleRoster);
    $('#roster-backdrop').addEventListener('click', () => $('#app').classList.remove('roster-open'));
    document.querySelectorAll('.views button').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
    portrait.addEventListener('change', applyRosterLayout);
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
    applyTheme();
    start();
});
