// Shared client state + a small event bus between the modules.
import { api } from './api.js';
import { toast } from './dom.js';

export const state = {
    me: null,            // GET /api/me
    channels: [],        // [Channel], 1:1 first
    current: null,       // cid of the open chat
    view: 'chat',        // chat | files | settings
    settings: {},        // webinterface.json (client-owned schema)
    unread: new Map(),   // cid -> Set(message id)
};

const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => bus.addEventListener(name, (e) => fn(e.detail));

export const channelByCid = (cid) => state.channels.find((c) => c.cid === cid);
export const channelById = (id) => state.channels.find((c) => c.channel_id === id);

let saveTimer = null;
export function saveSettings() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
        saveTimer = null;
        api('PUT', '/api/settings', { json: state.settings })
            .catch((e) => toast(`Saving settings failed: ${e.message}`));
    }, 400);
}

// A debounced save still pending when the tab goes away: send it now with
// keepalive so the browser completes it after the page is gone.
function flushSettings() {
    if (!saveTimer) return;
    clearTimeout(saveTimer);
    saveTimer = null;
    fetch('/api/settings', {
        method: 'PUT',
        keepalive: true,
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-NixClaw': '1' },
        body: JSON.stringify(state.settings),
    }).catch(() => {});
}
addEventListener('pagehide', flushSettings);
document.addEventListener('visibilitychange', () => { if (document.hidden) flushSettings(); });

export function applyTheme() {
    const t = state.settings.theme;
    const dark = t ? t === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}
