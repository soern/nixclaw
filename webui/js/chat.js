// Chat view: history (scroll back via ?before=), live upserts from SSE,
// composer with attachments (picker, drag & drop, paste), voice recording,
// polls and the client-side /status and /deauth commands.
import { api, q, attachmentUrl } from './api.js';
import { h, clear, append, toast, fmtSize, fmtTime, uuid } from './dom.js';
import { state, emit, channelByCid } from './state.js';
import { runCommand, isCommand } from './commands.js';
import { showOverlay } from './overlay.js';

const PAGE = 50;
const TYPING_TIMEOUT_MS = 15000;
const store = new Map();    // cid -> {byId: Map, loaded, exhausted, loading}
const typing = new Map();   // cid -> timeout handle
const voted = new Map();    // `${cid}:${ts}` -> option index (local only)
let el = {};                // view elements
let pending = [];           // File[] waiting to be sent

const chan = (cid) => {
    if (!store.has(cid)) store.set(cid, { byId: new Map(), loaded: false, exhausted: false, loading: false });
    return store.get(cid);
};

// ---- rendering ---------------------------------------------------------------

function isOwn(m) {
    return m.origin !== 'agent' && m.sender === state.me.number;
}

function renderAttachment(m, a) {
    const url = attachmentUrl(m.cid, a.name);
    const type = (a.content_type || '').split(';')[0];
    if (a.exists === false && a.transcript == null) {
        return h('div', { class: 'att missing', text: `📎 ${a.name} (no longer available)` });
    }
    if (type.startsWith('audio/')) return renderVoice(a, url);
    if (a.exists === false) return h('div', { class: 'att missing', text: `📎 ${a.name} (no longer available)` });
    if (type.startsWith('image/')) {
        const node = h('div', { class: 'att image clickable', onclick: () => showOverlay(url, { name: a.name, contentType: type }) });
        node.append(h('img', {
            src: url, alt: a.name, loading: 'lazy',
            onerror: () => node.replaceWith(h('div', { class: 'att missing', text: `📎 ${a.name} (no longer available)` })),
        }));
        return node;
    }
    if (type.startsWith('video/')) {
        const video = h('video', { class: 'att video', src: url, controls: true, preload: 'metadata' });
        video.addEventListener('click', () => showOverlay(url, { name: a.name, contentType: type }));
        return video;
    }
    // PDF, text and other non-media files: click opens the overlay viewer
    const clickable = type === 'application/pdf' || type.startsWith('text/');
    if (clickable) {
        return h('div', { class: 'att file clickable', onclick: () => showOverlay(url, { name: a.name, contentType: type }) },
            h('span', { text: '📎' }), h('span', { class: 'att-name', text: a.name }),
            h('span', { class: 'att-size', text: fmtSize(a.size) }));
    }
    return h('a', { class: 'att file', href: url, download: a.name },
        h('span', { text: '📎' }), h('span', { class: 'att-name', text: a.name }),
        h('span', { class: 'att-size', text: fmtSize(a.size) }));
}

function renderVoice(a, url) {
    const box = h('div', { class: 'att voice' });
    if (a.exists !== false) {
        const audio = new Audio();
        audio.preload = 'metadata';
        const btn = h('button', { class: 'play', title: 'Play', text: '▶' });
        const time = h('span', { class: 'voice-time' });
        btn.onclick = () => (audio.paused ? audio.play().catch(() => {}) : audio.pause());
        audio.onplay = () => { btn.textContent = '⏸'; };
        audio.onpause = audio.onended = () => { btn.textContent = '▶'; };
        audio.ontimeupdate = audio.onloadedmetadata = () => {
            const t = audio.paused && !audio.currentTime ? audio.duration : audio.currentTime;
            time.textContent = Number.isFinite(t) ? `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}` : '';
        };
        // 404 (expired by the STT retention sweep) -> drop the button, keep the transcript.
        audio.onerror = () => { btn.remove(); time.remove(); };
        audio.src = url;
        box.append(btn, time);
    }
    box.append(h('span', {
        class: 'transcript',
        text: `🎤 ${a.transcript != null ? a.transcript : a.exists === false ? '(voice message expired)' : 'voice message'}`,
    }));
    return box;
}

function renderPoll(m) {
    const p = m.poll;
    const key = `${m.cid}:${p.ts}`;
    return h('div', { class: 'poll' },
        h('div', { class: 'poll-q', text: `📊 ${p.question}` }),
        h('div', { class: 'poll-answers' }, p.answers.map((ans, i) => h('button', {
            class: voted.get(key) === i ? 'primary' : '',
            disabled: p.closed,
            text: ans,
            onclick: async (e) => {
                const buttons = e.target.parentNode.querySelectorAll('button');
                buttons.forEach((b) => { b.disabled = true; });
                try {
                    await api('POST', `/api/channels/${m.cid}/polls/${p.ts}/vote`, { json: { option: i } });
                    voted.set(key, i);
                    buttons.forEach((b, j) => b.classList.toggle('primary', j === i));
                } catch (ex) {
                    toast(`Vote failed: ${ex.message}`);
                }
                buttons.forEach((b) => { b.disabled = p.closed; });
            },
        }))),
        p.closed ? h('div', { class: 'meta', text: 'Poll closed' }) : null);
}

function renderMessage(m) {
    const ch = channelByCid(m.cid);
    const own = isOwn(m);
    const node = h('div', { class: `msg ${own ? 'own' : 'other'} origin-${m.origin}`, 'data-id': m.id });
    const bubble = node.appendChild(h('div', { class: 'bubble' }));
    if (ch && ch.kind === 'group' && !own) {
        bubble.append(h('div', { class: 'sender', text: m.sender_name || m.sender || '' }));
    }
    // Origin icon: in front of the text ("📱 hello"), or in the meta line if there is no text.
    let icon = m.origin === 'signal' ? '📱 ' : m.origin === 'web' ? '🌐 ' : '';
    const via = icon ? `sent via ${m.origin === 'web' ? 'the web' : 'Signal'}` : '';
    if (m.deleted) {
        bubble.append(h('div', { class: 'text deleted', text: 'This message was deleted.' }));
    } else {
        if (m.text) {
            bubble.append(h('div', { class: 'text' },
                icon ? h('span', { class: 'origin', title: via, text: icon }) : null, m.text));
            icon = '';
        }
        for (const a of m.attachments || []) bubble.append(renderAttachment(m, a));
        if (m.poll) bubble.append(renderPoll(m));
    }
    bubble.append(h('div', {
        class: 'meta',
        title: `${new Date(m.created_at).toLocaleString()}${via ? ` · ${via}` : ''}`,
        text: `${icon}${m.edited && !m.deleted ? 'edited · ' : ''}${fmtTime(m.created_at)}`,
    }));
    return node;
}

function nearBottom() {
    const s = el.list;
    return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}

function scrollToBottom() {
    el.list.scrollTop = el.list.scrollHeight;
}

// Insert or replace a message node in id order.
function placeMessage(m) {
    const node = renderMessage(m);
    const old = el.list.querySelector(`.msg[data-id="${m.id}"]`);
    if (old) return old.replaceWith(node);
    for (const n of el.list.querySelectorAll('.msg[data-id]')) {
        if (Number(n.dataset.id) > m.id) return n.before(node);
    }
    el.list.append(node);
}

function renderAll() {
    clear(el.list);
    const c = chan(state.current);
    if (c.exhausted) el.list.append(h('div', { class: 'history-start', text: 'Beginning of the web history' }));
    for (const m of [...c.byId.values()].sort((a, b) => a.id - b.id)) el.list.append(renderMessage(m));
}

export function localLine(lines, kind = 'system') {
    el.list.append(h('div', { class: `local ${kind}` }, lines.map((l) => h('div', { text: l }))));
    scrollToBottom();
}

// ---- data --------------------------------------------------------------------

async function loadOlder() {
    const cid = state.current;
    const c = chan(cid);
    if (c.loading || c.exhausted) return;
    c.loading = true;
    const ids = [...c.byId.keys()];
    const before = ids.length ? Math.min(...ids) : undefined;
    try {
        const res = await api('GET', `/api/channels/${cid}/messages?${q({ before, limit: PAGE })}`);
        if (cid !== state.current) return;
        for (const m of res.messages) if (!c.byId.has(m.id)) c.byId.set(m.id, m);
        c.exhausted = res.messages.length < PAGE;
        c.loaded = true;
        const prevHeight = el.list.scrollHeight;
        const prevTop = el.list.scrollTop;
        renderAll();
        if (before === undefined) scrollToBottom();
        else el.list.scrollTop = prevTop + el.list.scrollHeight - prevHeight;
    } catch (ex) {
        toast(`Loading messages failed: ${ex.message}`);
    } finally {
        c.loading = false;
    }
}

export function openChannel(cid) {
    state.current = cid;
    state.unread.delete(cid);
    pending = [];
    selections = new Map();      // mention picks belong to one message
    closeMenu();
    renderPending();
    const ch = channelByCid(cid);
    el.input.placeholder = ch && ch.kind === 'group'
        ? `Message ${ch.name} (mention @${state.me.agent_name} to ask the agent)` : 'Message';
    updateTyping();
    const c = chan(cid);
    if (c.loaded) {
        renderAll();
        scrollToBottom();
    } else {
        clear(el.list);
        loadOlder();
    }
}

export function onMessage(m) {
    const c = chan(m.cid);
    const isNew = !c.byId.has(m.id);
    // Keep events that arrive while the first page loads; loadOlder() won't overwrite them.
    if (c.loaded || c.loading) c.byId.set(m.id, m);
    if (m.cid === state.current && c.loaded) {
        const stick = nearBottom();
        placeMessage(m);
        if (stick || (isNew && isOwn(m))) scrollToBottom();
    }
    if (m.origin === 'agent' && m.cid === state.current) setTyping(m.cid, false);
    if ((m.cid !== state.current || document.hidden) && !isOwn(m) && !m.deleted) {
        if (!state.unread.has(m.cid)) state.unread.set(m.cid, new Set());
        state.unread.get(m.cid).add(m.id);
        emit('unread-changed');
    }
}

function setTyping(cid, active) {
    clearTimeout(typing.get(cid));
    typing.delete(cid);
    if (active) typing.set(cid, setTimeout(() => setTyping(cid, false), TYPING_TIMEOUT_MS));
    if (cid === state.current) updateTyping();
}

export const onTyping = (d) => setTyping(d.cid, d.active);

function updateTyping() {
    el.typing.textContent = typing.has(state.current) ? `${state.me.agent_name || 'Agent'} is typing…` : '';
}

// Forget everything (e.g. after an SSE gap); the open channel reloads.
export function resetHistory() {
    store.clear();
    if (state.current) openChannel(state.current);
}

// ---- composer ----------------------------------------------------------------

function addFiles(files) {
    const max = state.me.limits.max_upload_mb * 1024 * 1024;
    for (const f of files) {
        if (f.size > max) toast(`${f.name} is larger than ${state.me.limits.max_upload_mb} MB`);
        else pending.push(f);
    }
    renderPending();
}

function renderPending() {
    clear(el.pending);
    el.pending.hidden = !pending.length;
    pending.forEach((f, i) => el.pending.append(h('span', { class: 'chip' },
        h('span', { text: `${f.name} (${fmtSize(f.size)})` }),
        h('button', { class: 'icon small', title: 'Remove', text: '✕', onclick: () => { pending.splice(i, 1); renderPending(); } }))));
}

async function postMessage(cid, text, files, mentions = []) {
    const clientId = uuid();
    // The mentions JSON is decided once, outside build(): the network retry
    // below re-invokes build() with the same client_id and must send
    // byte-identical parts.
    const mentionsJson = mentions.length ? JSON.stringify(mentions) : null;
    const build = () => {
        const fd = new FormData();
        fd.append('text', text);
        fd.append('client_id', clientId);
        if (mentionsJson) fd.append('mentions', mentionsJson);
        for (const f of files) fd.append('files', f, f.name);
        return fd;
    };
    let res;
    try {
        res = await api('POST', `/api/channels/${cid}/messages`, { body: build() });
    } catch (ex) {
        if (ex.status) throw ex;
        // Network error: retry once with the same client_id (server dedups).
        res = await api('POST', `/api/channels/${cid}/messages`, { body: build() });
    }
    onMessage(res.message);
}

async function send() {
    const text = el.input.value.trim();
    const cid = state.current;
    if (!cid || (!text && !pending.length)) return;
    if (isCommand(text) && !pending.length) {
        el.input.value = '';
        localLine([`> ${text}`], 'command');
        localLine(await runCommand(text));
        return;
    }
    const files = pending;
    const mentions = mentionsFor(text);
    el.send.disabled = true;
    try {
        await postMessage(cid, text, files, mentions);
        el.input.value = '';
        pending = [];
        selections = new Map();
        closeMenu();
        renderPending();
        autosize();
    } catch (ex) {
        toast(`Sending failed: ${ex.message}`);
    } finally {
        el.send.disabled = false;
        el.input.focus();
    }
}

function autosize() {
    el.input.classList.toggle('multiline', el.input.value.includes('\n') || el.input.scrollHeight > 60);
}

// ---- @-member autocomplete -----------------------------------------------------
// Typing "@" at a word boundary opens the channel's member list above the
// composer. Choosing only inserts "@Name " text: the offsets are computed by
// the server (mentions.py), so the client sends {token, author} pairs for what
// it picked. Menu keys never fire while an IME is composing (candidate list).
const MENTION_TTL_MS = 300000;
const MENTION_TRIGGER = /(?:^|\s)@([^\s@]{0,32})$/;
const MENTION_MAX_ITEMS = 12;
const MENTION_MAX_SEND = 20;          // the server's MAX_MENTIONS
const memberCache = new Map();        // cid -> {at, members}
let selections = new Map();           // `${author}\u0000${token}` -> {author, token}
let menu = null;                      // {box, rows, sel, runStart, caret}
let menuPointerDown = false;          // row clicks blur the textarea first
let menuGeneration = 0;               // invalidates updateMenu runs with a fetch in flight

async function membersOf(cid) {
    const hit = memberCache.get(cid);
    if (hit && Date.now() - hit.at < MENTION_TTL_MS) return hit.members;
    try {
        const res = await api('GET', `/api/channels/${cid}/members`);
        const members = (res.members || []).filter((m) => m.identifier !== state.me.number);
        memberCache.set(cid, { at: Date.now(), members });
        return members;
    } catch (ex) {
        return hit ? hit.members : [];   // stale beats nothing; the server filters anyway
    }
}

function closeMenu() {
    if (menu) menu.box.remove();
    menu = null;
    menuPointerDown = false;
}

function mentionRows(members, filter) {
    const lc = filter.toLowerCase();
    const byName = (m) => (m.name || '').toLowerCase();
    const starts = members.filter((m) => byName(m).startsWith(lc));
    return (starts.length ? starts : members.filter((m) => byName(m).includes(lc))).slice(0, MENTION_MAX_ITEMS);
}

async function updateMenu() {
    const value = el.input.value;
    const caret = el.input.selectionStart ?? value.length;
    // Newest call wins: any older run still waiting on the members fetch must
    // not touch the menu afterwards (its rows/runStart are already obsolete).
    const gen = ++menuGeneration;
    // A menu that no longer matches the input text is stale the moment the
    // user typed: drop it right away (not only when the fetch below resolves),
    // so Enter/click can never act on it while the fetch is in flight.
    if (menu && (value !== menu.full || caret !== menu.caret)) closeMenu();
    const m = value.slice(0, caret).match(MENTION_TRIGGER);
    const ch = channelByCid(state.current);
    if (!m || !ch || ch.kind !== 'group') return closeMenu();
    const members = await membersOf(state.current);
    if (gen !== menuGeneration) return;
    // The user may have typed (or the value changed) while the member list was
    // in flight: drop the possibly-open stale menu rather than keep it — an
    // Enter/click on old runStart/caret would splice text around those keys.
    // The keystroke's own updateMenu run (newer gen) rebuilds the menu.
    if (el.input.value !== value || (el.input.selectionStart ?? caret) !== caret) {
        closeMenu();
        return;
    }
    const rows = mentionRows(members, m[1]);
    closeMenu();
    if (!rows.length) return;
    const box = h('div', { class: 'mention-menu', role: 'listbox' });
    // full/caret pin the exact text the rows/runStart were computed for;
    // chooseMention refuses to splice anything but this exact state.
    const entry = { box, rows, sel: 0, runStart: caret - m[1].length - 1, caret, full: value };
    rows.forEach((mem, i) => box.append(h('div', {
        class: `mention-row${mem.uuid ? '' : ' muted'}`, role: 'option',
        'aria-selected': i === 0 ? 'true' : 'false',
        title: mem.uuid ? null : 'cannot be highlighted in Signal (not a contact)',
        onmousedown: () => { menuPointerDown = true; },
        onmouseup: () => { menuPointerDown = false; },
        onmouseleave: () => { menuPointerDown = false; },
        onclick: () => { menuPointerDown = false; chooseMention(i); },
    }, h('span', { class: 'mention-name', text: mem.name }), h('span', { class: 'mention-id', text: mem.identifier }))));
    // The chat view is position:relative; the menu floats above the composer.
    el.root.append(box);
    box.style.bottom = `${el.composer.offsetHeight + 4}px`;
    menu = entry;
}

function moveMenu(delta) {
    if (!menu) return;
    const n = menu.rows.length;
    menu.sel = (menu.sel + delta + n) % n;
    [...menu.box.children].forEach((node, i) => {
        node.classList.toggle('active', i === menu.sel);
        node.setAttribute('aria-selected', i === menu.sel ? 'true' : 'false');
    });
    menu.box.children[menu.sel].scrollIntoView({ block: 'nearest' });
}

function chooseMention(index) {
    if (!menu) return;
    // A menu is only valid for the exact input text/caret it was built for.
    // If the user typed (or the value changed) since, the menu was already
    // dropped by updateMenu - never splice text around unseen keystrokes.
    const checkValue = el.input.value;
    if (checkValue !== menu.full
        || (el.input.selectionStart ?? checkValue.length) !== menu.caret) {
        closeMenu();
        return;
    }
    const mem = menu.rows[index];
    const token = `@${mem.name}`;
    const value = el.input.value;
    // Replace the "@filter" run at the caret with "@Name " (auto-space, refocus).
    el.input.value = `${value.slice(0, menu.runStart)}${token} ${value.slice(menu.caret)}`;
    const caret = menu.runStart + token.length + 1;
    el.input.setSelectionRange(caret, caret);
    // The server gates authors on group member identifiers (numbers) and
    // resolves the UUID itself, so author is ALWAYS the identifier — a UUID
    // author would be dropped as a non-member. A member without a contact
    // UUID is still selectable; the server just degrades it to plain text.
    selections.set(`${mem.identifier}\u0000${token}`, { author: mem.identifier, token });
    closeMenu();
    autosize();
    el.input.focus();
}

// Snapshot for one send: only selections whose token survived in the text (the
// server's scan stays authoritative), capped at the server's MAX_MENTIONS. The
// Map key is the composite `${author}\u0000${token}`, so two members who share
// a display name stay two entries.
function mentionsFor(text) {
    const out = [];
    for (const { author, token } of selections.values()) {
        if (!text.includes(token)) continue;
        out.push({ token, author });
        if (out.length >= MENTION_MAX_SEND) break;
    }
    return out;
}

// ---- voice recording (MediaRecorder needs a secure context) -----------------

let recorder = null;
let recTimer = null;

function extForMime(mime) {
    if (mime.includes('ogg')) return 'ogg';
    if (mime.includes('mp4') || mime.includes('aac')) return 'm4a';
    return 'webm';
}

async function toggleRecording() {
    if (recorder) {
        recorder.stop();
        return;
    }
    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (ex) {
        toast(`Microphone not available: ${ex.message}`);
        return;
    }
    const chunks = [];
    const cid = state.current;
    const started = Date.now();
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        clearInterval(recTimer);
        const mime = (recorder.mimeType || 'audio/webm').split(';')[0];
        recorder = null;
        el.rec.textContent = '🎤';
        el.rec.classList.remove('recording');
        el.recTime.hidden = true;
        if (Date.now() - started < 700) return;    // accidental tap
        const stamp = new Date().toISOString().replace(/[:.]/g, '-');
        const file = new File(chunks, `voice-${stamp}.${extForMime(mime)}`, { type: mime });
        try {
            await postMessage(cid, '', [file]);
        } catch (ex) {
            toast(`Sending voice message failed: ${ex.message}`);
        }
    };
    recorder.start();
    el.rec.textContent = '⏹';
    el.rec.classList.add('recording');
    el.recTime.hidden = false;
    const tick = () => {
        const s = Math.floor((Date.now() - started) / 1000);
        el.recTime.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    };
    tick();
    recTimer = setInterval(tick, 500);
}

// ---- setup -------------------------------------------------------------------

export function initChat(root) {
    el.root = root;
    el.list = h('div', { class: 'messages', onscroll: () => { if (el.list.scrollTop < 80) loadOlder(); } });
    el.typing = h('div', { class: 'typing', 'aria-live': 'polite' });
    el.pending = h('div', { class: 'pending', hidden: true });
    el.file = h('input', {
        type: 'file', multiple: true, hidden: true,
        onchange: () => { addFiles([...el.file.files]); el.file.value = ''; },
    });
    el.input = h('textarea', {
        rows: 1, placeholder: 'Message', 'aria-label': 'Message',
        oninput: () => { autosize(); updateMenu(); },
        onkeydown: (e) => {
            // Menu keys come first, but an IME owns the keys while composing
            // (otherwise Enter/Arrows would hijack the candidate list).
            if (menu && e.isComposing) return;
            if (menu) {
                if (e.key === 'ArrowDown') { e.preventDefault(); moveMenu(1); return; }
                if (e.key === 'ArrowUp') { e.preventDefault(); moveMenu(-1); return; }
                if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); chooseMention(menu.sel); return; }
                if (e.key === 'Escape') { e.preventDefault(); closeMenu(); return; }
            }
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
        },
        // Clicking a row blurs the textarea before its click fires: the mousedown
        // guard keeps the menu alive long enough to receive the click.
        onblur: () => { if (!menuPointerDown) closeMenu(); },
        onpaste: (e) => {
            const files = [...(e.clipboardData?.files || [])];
            if (files.length) { e.preventDefault(); addFiles(files); }
        },
    });
    el.send = h('button', { class: 'icon primary', title: 'Send', 'aria-label': 'Send', text: '➤', onclick: send });
    el.rec = h('button', { class: 'icon', title: 'Record voice message', 'aria-label': 'Record voice message', text: '🎤', onclick: toggleRecording });
    el.recTime = h('span', { class: 'rec-time', hidden: true });
    const canRecord = window.isSecureContext && navigator.mediaDevices && window.MediaRecorder;

    el.composer = h('div', { class: 'composer' },
        h('button', { class: 'icon', title: 'Attach files', 'aria-label': 'Attach files', text: '📎', onclick: () => el.file.click() }),
        el.file, el.input, canRecord ? [el.recTime, el.rec] : null, el.send);

    append(root, [
        el.list, el.typing, el.pending, el.composer,
    ]);

    root.addEventListener('dragover', (e) => {
        if ([...e.dataTransfer.types].includes('Files')) { e.preventDefault(); root.classList.add('dropping'); }
    });
    root.addEventListener('dragleave', (e) => { if (e.target === root || !root.contains(e.relatedTarget)) root.classList.remove('dropping'); });
    root.addEventListener('drop', (e) => {
        e.preventDefault();
        root.classList.remove('dropping');
        addFiles([...e.dataTransfer.files]);
    });
}
