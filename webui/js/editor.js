// Minimal text editor: textarea, Save (Ctrl+S) / Cancel, ETag conflict check.
import { api, q } from './api.js';
import { h, toast } from './dom.js';

export async function openEditor(root, path, cid, onClose) {
    let etag = null;
    let saved = '';
    const status = h('span', { class: 'status' });
    const area = h('textarea', { class: 'editor-text', spellcheck: 'false', disabled: true, 'aria-label': path });
    const conflict = h('div', { class: 'conflict', hidden: true });
    const box = h('div', { class: 'editor' },
        h('div', { class: 'editor-head' },
            h('span', { class: 'editor-path', text: `📝 ${path}` }), status,
            h('button', { class: 'small primary', text: 'Save', title: 'Save (Ctrl+S)', onclick: () => save() }),
            h('button', { class: 'small', text: 'Cancel', onclick: () => close() })),
        conflict, area);
    root.append(box);

    const dirty = () => area.value !== saved;
    const setStatus = () => { status.textContent = dirty() ? '● modified' : ''; };

    async function load() {
        const res = await api('GET', `/api/files/raw?${q({ path, cid })}`, { raw: true });
        etag = res.headers.get('ETag');
        saved = await res.text();
        area.value = saved;
        area.disabled = false;
        conflict.hidden = true;
        setStatus();
        area.focus();
    }

    async function save(force = false) {
        try {
            const headers = { 'Content-Type': 'text/plain; charset=utf-8' };
            if (etag && !force) headers['If-Match'] = etag;
            const res = await api('PUT', `/api/files/raw?${q({ path, cid })}`, { body: area.value, headers });
            etag = res.etag;
            saved = area.value;
            conflict.hidden = true;
            setStatus();
            toast('Saved', 'info');
        } catch (ex) {
            if (ex.status !== 409) return toast(`Saving failed: ${ex.message}`);
            conflict.replaceChildren(
                h('span', { text: 'The file was changed elsewhere since you opened it. ' }),
                h('button', { class: 'small', text: 'Reload (discard my edits)', onclick: () => load().catch((e) => toast(e.message)) }),
                h('button', { class: 'small danger', text: 'Overwrite', onclick: () => save(true) }));
            conflict.hidden = false;
        }
    }

    function close() {
        if (dirty() && !confirm('Discard unsaved changes?')) return;
        document.removeEventListener('keydown', onKey);
        box.remove();
        onClose();
    }

    function onKey(e) {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && box.isConnected) {
            e.preventDefault();
            save();
        }
    }

    area.addEventListener('input', setStatus);
    document.addEventListener('keydown', onKey);
    try {
        await load();
    } catch (ex) {
        toast(`Opening failed: ${ex.message}`);
        document.removeEventListener('keydown', onKey);
        box.remove();
        onClose();
    }
}
