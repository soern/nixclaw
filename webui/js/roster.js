// Roster: channels grouped into user categories (purely visual, stored in
// webinterface.json via PUT /api/settings). Settings schema (client-owned):
//   categories: [{id, name, collapsed, channels: [channel_id, ...]}]
// The "default" category always exists and receives channels not placed anywhere.
import { h, clear, $ } from './dom.js';
import { state, emit, saveSettings, channelById } from './state.js';

const DEFAULT = 'default';
let editing = false;

function normalize() {
    const s = state.settings;
    if (!Array.isArray(s.categories)) s.categories = [];
    s.categories = s.categories.filter((c) => c && typeof c.id === 'string');
    let def = s.categories.find((c) => c.id === DEFAULT);
    if (!def) {
        def = { id: DEFAULT, name: 'Chats', collapsed: false, channels: [] };
        s.categories.unshift(def);
    }
    const seen = new Set();
    for (const c of s.categories) {
        c.channels = (Array.isArray(c.channels) ? c.channels : [])
            .filter((id) => typeof id === 'string' && !seen.has(id) && seen.add(id));
    }
    for (const ch of state.channels) if (!seen.has(ch.channel_id)) def.channels.push(ch.channel_id);
}

// Swap a channel with its nearest *visible* neighbour (ids of departed channels stay put).
function moveChannel(cat, id, dir) {
    const vis = cat.channels.filter((x) => channelById(x));
    const i = vis.indexOf(id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= vis.length) return;
    const a = cat.channels.indexOf(vis[i]);
    const b = cat.channels.indexOf(vis[j]);
    [cat.channels[a], cat.channels[b]] = [cat.channels[b], cat.channels[a]];
    changed();
}

function moveToCategory(id, catId) {
    for (const c of state.settings.categories) c.channels = c.channels.filter((x) => x !== id);
    state.settings.categories.find((c) => c.id === catId).channels.push(id);
    changed();
}

function moveCategory(cat, dir) {
    const cats = state.settings.categories;
    const i = cats.indexOf(cat);
    const j = i + dir;
    if (j < 0 || j >= cats.length) return;
    [cats[i], cats[j]] = [cats[j], cats[i]];
    changed();
}

function addCategory() {
    const name = prompt('Category name');
    if (!name || !name.trim()) return;
    state.settings.categories.push({
        id: `c${Date.now().toString(36)}`, name: name.trim().slice(0, 60), collapsed: false, channels: [],
    });
    changed();
}

function renameCategory(cat) {
    const name = prompt('Rename category', cat.name);
    if (!name || !name.trim()) return;
    cat.name = name.trim().slice(0, 60);
    changed();
}

function deleteCategory(cat) {
    if (!confirm(`Delete category "${cat.name}"? Its channels move to "${state.settings.categories.find((c) => c.id === DEFAULT).name}".`)) return;
    const s = state.settings;
    s.categories.find((c) => c.id === DEFAULT).channels.push(...cat.channels);
    s.categories = s.categories.filter((c) => c !== cat);
    changed();
}

function changed() {
    saveSettings();
    renderRoster();
}

export function channelLabel(ch) {
    return ch.kind === 'direct' ? `${ch.name || 'Agent'} (1:1)` : ch.name || ch.channel_id;
}

function channelRow(cat, ch) {
    const unread = state.unread.get(ch.cid)?.size || 0;
    const row = h('li', { class: `channel${ch.cid === state.current ? ' active' : ''}` },
        h('button', {
            class: 'channel-open',
            title: ch.channel_id,
            onclick: () => emit('open-channel', ch.cid),
        },
        h('span', { class: 'channel-icon', text: ch.kind === 'direct' ? '👤' : '👥' }),
        h('span', { class: 'channel-name', text: channelLabel(ch) }),
        unread ? h('span', { class: 'badge', text: unread > 99 ? '99+' : String(unread) }) : null));
    if (editing) {
        const sel = h('select', {
            class: 'move-to', title: 'Move to category',
            onchange: (e) => moveToCategory(ch.channel_id, e.target.value),
        }, state.settings.categories.map((c) => h('option', { value: c.id, text: c.name })));
        sel.value = cat.id;
        row.append(h('span', { class: 'edit-tools' },
            h('button', { class: 'icon small', title: 'Move up', text: '▲', onclick: () => moveChannel(cat, ch.channel_id, -1) }),
            h('button', { class: 'icon small', title: 'Move down', text: '▼', onclick: () => moveChannel(cat, ch.channel_id, 1) }),
            sel));
    }
    return row;
}

export function renderRoster() {
    normalize();
    const root = clear($('#roster'));
    root.append(h('div', { class: 'roster-head' },
        h('span', { class: 'roster-title', text: 'Channels' }),
        editing ? h('button', { class: 'small', text: '+ Category', onclick: addCategory }) : null,
        h('button', {
            class: `small${editing ? ' primary' : ''}`, text: editing ? 'Done' : 'Edit',
            title: 'Reorder channels and manage categories',
            onclick: () => { editing = !editing; renderRoster(); },
        })));

    for (const cat of state.settings.categories) {
        const chans = cat.channels.map(channelById).filter(Boolean);
        if (!chans.length && cat.id === DEFAULT && !editing && state.settings.categories.length > 1) continue;
        const unread = chans.reduce((n, ch) => n + (state.unread.get(ch.cid)?.size || 0), 0);
        const head = h('div', { class: 'cat-head' },
            h('button', {
                class: 'cat-toggle',
                onclick: () => { cat.collapsed = !cat.collapsed; changed(); },
            },
            h('span', { class: 'caret', text: cat.collapsed ? '▸' : '▾' }),
            h('span', { class: 'cat-name', text: cat.name }),
            cat.collapsed && unread ? h('span', { class: 'badge', text: String(unread) }) : null));
        if (editing) {
            head.append(h('span', { class: 'edit-tools' },
                h('button', { class: 'icon small', title: 'Move up', text: '▲', onclick: () => moveCategory(cat, -1) }),
                h('button', { class: 'icon small', title: 'Move down', text: '▼', onclick: () => moveCategory(cat, 1) }),
                h('button', { class: 'icon small', title: 'Rename', text: '✎', onclick: () => renameCategory(cat) }),
                cat.id === DEFAULT ? null
                    : h('button', { class: 'icon small', title: 'Delete', text: '✕', onclick: () => deleteCategory(cat) })));
        }
        root.append(h('div', { class: 'cat' }, head,
            cat.collapsed ? null : h('ul', { class: 'channels' }, chans.map((ch) => channelRow(cat, ch)))));
    }
}
