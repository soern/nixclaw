// Files view: the user's 1:1 workspace. Breadcrumbs, upload (picker or drag &
// drop) into the current folder, new folder, delete, download, 📝 editor.
import { api, q } from './api.js';
import { h, clear, toast, fmtSize, fmtTime } from './dom.js';
import { state } from './state.js';
import { openEditor } from './editor.js';
import { showOverlay } from './overlay.js';

let root = null;
let cwd = '';
let listEl = null;
let entries = null;   // current folder listing (new-file name check); null while loading
let lastCid = null;   // last channel the files view was shown for
let sortKey = sessionStorage.getItem('files-sort-key') || 'name';
let sortDir = sessionStorage.getItem('files-sort-dir') || 'asc';

const join = (dir, name) => (dir ? `${dir}/${name}` : name);
const rawUrl = (path, download) => `/api/files/raw?${q({ path, cid: state.current, download: download ? 1 : undefined })}`;

function setSort(key, dir) {
    if (sortKey === key && sortDir === dir) return;
    sortKey = key;
    sortDir = dir;
    sessionStorage.setItem('files-sort-key', sortKey);
    sessionStorage.setItem('files-sort-dir', sortDir);
    go(cwd);
}

function sortEntries(list) {
    const dir = sortDir === 'desc' ? -1 : 1;
    const cmp = {
        name: (a, b) => a.name.localeCompare(b.name),
        size: (a, b) => a.size - b.size,
        date: (a, b) => (a.mtime || 0) - (b.mtime || 0),
    }[sortKey] || ((a, b) => a.name.localeCompare(b.name));
    return [...list].sort((a, b) => {
        if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
        const d = cmp(a, b);
        return d !== 0 ? d * dir : a.name.localeCompare(b.name);
    });
}

function headerRow() {
    const arrow = (col, dir) => h('button', {
        class: `sort-arrow${sortKey === col && sortDir === dir ? ' active' : ''}`,
        title: `Sort by ${col} (${dir === 'asc' ? 'ascending' : 'descending'})`,
        text: dir === 'asc' ? '↑' : '↓',
        onclick: () => setSort(col, dir),
    });
    return h('li', { class: 'file-row header' },
        h('span', { class: 'name' }, 'Name',
            h('span', { class: 'sort-arrows' }, arrow('name', 'asc'), arrow('name', 'desc'))),
        h('span', { class: 'size' }, 'Size',
            h('span', { class: 'sort-arrows' }, arrow('size', 'asc'), arrow('size', 'desc'))),
        h('span', { class: 'mtime' }, 'Date',
            h('span', { class: 'sort-arrows' }, arrow('date', 'asc'), arrow('date', 'desc'))),
        h('span', { class: 'actions' }));
}

function isEditable(e) {
    const { text_extensions: exts = [], editor_max_kb: maxKb = 1024 } = state.me.limits;
    const dot = e.name.lastIndexOf('.');
    const ext = dot > 0 ? e.name.slice(dot).toLowerCase() : '';
    return e.type === 'file' && exts.includes(ext) && e.size <= maxKb * 1024;
}

function breadcrumbs() {
    const parts = cwd ? cwd.split('/') : [];
    const nav = h('nav', { class: 'crumbs' },
        h('button', { class: 'link', text: '🏠 Workspace', onclick: () => go('') }));
    parts.forEach((p, i) => {
        const path = parts.slice(0, i + 1).join('/');
        nav.append(h('span', { class: 'sep', text: '/' }),
            i === parts.length - 1 ? h('span', { text: p }) : h('button', { class: 'link', text: p, onclick: () => go(path) }));
    });
    return nav;
}

function row(e) {
    const path = join(cwd, e.name);
    const name = e.type === 'dir'
        ? h('button', { class: 'link name', text: `📁 ${e.name}`, onclick: () => go(path) })
        : h('button', { class: 'link name', text: `📄 ${e.name}`, onclick: () => showOverlay(rawUrl(path), { name: e.name }) });
    return h('li', { class: 'file-row' },
        name,
        h('span', { class: 'size', text: e.type === 'file' ? fmtSize(e.size) : '' }),
        h('span', { class: 'mtime', text: fmtTime(e.mtime, true) }),
        h('span', { class: 'actions' },
            isEditable(e) ? h('button', { class: 'icon small', title: 'Open in text editor', text: '📝', onclick: () => edit(path) }) : null,
            e.type === 'file' ? h('a', { class: 'icon small button', href: rawUrl(path, true), title: 'Download', text: '⬇' }) : null,
            h('button', { class: 'icon small', title: 'Delete', text: '🗑', onclick: () => remove(path, e) })));
}

async function go(path) {
    const cid = state.current;
    cwd = path;
    entries = null;
    clear(root);
    const file = h('input', {
        type: 'file', multiple: true, hidden: true,
        onchange: () => { upload([...file.files]); file.value = ''; },
    });
    listEl = h('ul', { class: 'file-list' }, h('li', { class: 'empty', text: 'Loading…' }));
    root.append(
        h('div', { class: 'files-head' }, breadcrumbs(),
            h('span', { class: 'toolbar' },
                h('button', { class: 'small', text: '⬆ Upload', onclick: () => file.click() }), file,
                h('button', { class: 'small', text: '+ File', onclick: newFile }),
                h('button', { class: 'small', text: '+ Folder', onclick: mkdir }),
                h('button', { class: 'small', title: 'Reload', text: '⟳', onclick: () => go(cwd) }))),
        listEl,
        h('p', { class: 'hint', text: 'Drop files here to upload them into this folder.' }));
    try {
        const res = await api('GET', `/api/files/list?${q({ path, cid })}`);
        if (cid !== state.current || path !== cwd) return;
        entries = res.entries;
        clear(listEl);
        listEl.append(headerRow());
        if (cwd) listEl.append(h('li', { class: 'file-row' }, h('button', { class: 'link name', text: '⬑ ..', onclick: () => go(cwd.split('/').slice(0, -1).join('/')) })));
        if (!res.entries.length) listEl.append(h('li', { class: 'empty', text: 'This folder is empty.' }));
        sortEntries(res.entries).forEach((e) => listEl.append(row(e)));
    } catch (ex) {
        clear(listEl).append(h('li', { class: 'empty error', text: ex.message }));
    }
}

async function upload(files) {
    if (!files.length) return;
    const max = state.me.limits.max_upload_mb * 1024 * 1024;
    const total = files.reduce((n, f) => n + f.size, 0);
    if (total > max) return toast(`Upload is larger than ${state.me.limits.max_upload_mb} MB`);
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.name);
    try {
        const res = await api('POST', `/api/files/upload?${q({ dir: cwd, cid: state.current })}`, { body: fd });
        toast(`Uploaded ${res.names.join(', ')}`, 'info');
    } catch (ex) {
        toast(`Upload failed: ${ex.message}`);
    }
    go(cwd);
}

async function newFile() {
    const name = (prompt('New file name (e.g. notes.md)') || '').trim();
    if (!name) return;
    if (name.includes('/') || name.startsWith('.')) return toast('Use a plain file name (no "/" and not starting with ".").');
    // PUT creates *or overwrites*, so never let it touch an existing entry.
    if (!entries) return toast('The folder is still loading, try again.');
    if (entries.some((e) => e.name === name)) return toast(`"${name}" already exists in this folder.`);
    const path = join(cwd, name);
    try {
        await api('PUT', `/api/files/raw?${q({ path, cid: state.current })}`, { body: '', headers: { 'Content-Type': 'text/plain; charset=utf-8', 'If-None-Match': '*' } });
    } catch (ex) {
        return toast(`Creating the file failed: ${ex.message}`);
    }
    if (isEditable({ name, type: 'file', size: 0 })) edit(path);
    else go(cwd);
}

async function mkdir() {
    const name = prompt('New folder name (a/b creates nested folders)');
    if (!name || !name.trim()) return;
    try {
        await api('POST', `/api/files/mkdir?${q({ path: join(cwd, name.trim().replace(/^\/+|\/+$/g, '')), cid: state.current })}`);
    } catch (ex) {
        toast(`Creating the folder failed: ${ex.message}`);
    }
    go(cwd);
}

async function remove(path, e) {
    const what = e.type === 'dir' ? `the folder "${e.name}" and everything in it` : `"${e.name}"`;
    if (!confirm(`Delete ${what}? This cannot be undone.`)) return;
    try {
        await api('DELETE', `/api/files?${q({ path, cid: state.current })}`);
    } catch (ex) {
        toast(`Delete failed: ${ex.message}`);
    }
    go(cwd);
}

function edit(path) {
    clear(root);
    openEditor(root, path, state.current, () => go(cwd));
}

export function showFiles() {
    if (state.current !== lastCid) {
        lastCid = state.current;
        cwd = '';
    }
    if (!root.querySelector('.editor')) go(cwd);
}

export function initFiles(el) {
    root = el;
    root.addEventListener('dragover', (e) => {
        if (!root.querySelector('.editor') && [...e.dataTransfer.types].includes('Files')) {
            e.preventDefault();
            root.classList.add('dropping');
        }
    });
    root.addEventListener('dragleave', (e) => { if (!root.contains(e.relatedTarget)) root.classList.remove('dropping'); });
    root.addEventListener('drop', (e) => {
        if (root.querySelector('.editor')) return;
        e.preventDefault();
        root.classList.remove('dropping');
        upload([...e.dataTransfer.files]);
    });
}
