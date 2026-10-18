// Settings view: theme, refresh channels, authorized endpoints, logout.
import { api } from './api.js';
import { h, clear, toast, fmtTime } from './dom.js';
import { state, emit, saveSettings, applyTheme } from './state.js';

let root = null;

async function endpointList() {
    const ul = h('ul', { class: 'endpoints' }, h('li', { text: 'Loading…' }));
    try {
        const { endpoints } = await api('GET', '/api/endpoints');
        clear(ul);
        for (const e of endpoints) {
            ul.append(h('li', {},
                h('div', { class: 'ep-name', text: `${e.name}${e.current ? ' (this device)' : ''}` }),
                h('div', {
                    class: 'meta',
                    text: `created ${fmtTime(e.created_at, true)} · last used ${e.last_used_at ? fmtTime(e.last_used_at, true) : 'never'} · ${e.expires_at ? `expires ${fmtTime(e.expires_at, true)}` : 'no expiry'} · ${e.connections} connection${e.connections === 1 ? '' : 's'}`,
                }),
                e.current ? h('span', { class: 'meta', text: 'current' }) : h('button', {
                    class: 'small danger', text: 'Remove',
                    onclick: async () => {
                        if (!confirm(`Remove access for "${e.name}"?`)) return;
                        try {
                            await api('DELETE', `/api/endpoints/${e.id}`);
                        } catch (ex) {
                            toast(ex.message);
                        }
                        showSettings();
                    },
                })));
        }
    } catch (ex) {
        clear(ul).append(h('li', { class: 'error', text: ex.message }));
    }
    return ul;
}

export async function showSettings() {
    const theme = h('select', {
        onchange: (e) => { state.settings.theme = e.target.value; applyTheme(); saveSettings(); },
    }, h('option', { value: 'dark', text: 'Dark' }), h('option', { value: 'light', text: 'Light' }));
    theme.value = document.documentElement.dataset.theme;

    const refresh = h('button', {
        class: 'small', text: '⟳ Refresh channels',
        onclick: async () => {
            refresh.disabled = true;
            try {
                const res = await api('POST', '/api/channels/refresh');
                emit('channels', res.channels);
                toast(`${res.channels.length} channel${res.channels.length === 1 ? '' : 's'}`, 'info');
            } catch (ex) {
                toast(`Refresh failed: ${ex.message}`);
            }
            refresh.disabled = false;
        },
    });

    const logout = h('button', {
        class: 'small danger', text: 'Log out',
        onclick: async () => {
            try { await api('POST', '/api/auth/logout'); } catch { /* already gone */ }
            emit('logged-out');
        },
    });

    const eps = await endpointList();
    clear(root).append(
        h('h2', { text: 'Settings' }),
        h('p', { class: 'meta', text: `Logged in as ${state.me.number} (${state.me.tier}) on "${state.me.endpoint.name}"` }),
        h('section', {}, h('h3', { text: 'Appearance' }), h('label', { class: 'row' }, h('span', { text: 'Theme' }), theme)),
        h('section', {}, h('h3', { text: 'Channels' }),
            h('p', { class: 'hint', text: 'Ask Signal again which groups you are in.' }), refresh),
        h('section', {}, h('h3', { text: 'Authorized devices' }),
            h('p', { class: 'hint', text: 'Also available as /status and /deauth in any chat.' }), eps),
        h('section', {}, logout));
}

export function initSettings(el) {
    root = el;
}
