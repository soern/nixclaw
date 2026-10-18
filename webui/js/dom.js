// Tiny DOM helpers. Never uses innerHTML: all data goes through textContent /
// attributes, so message and file content can't inject markup (strict CSP).

export function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
        else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden') el[k] = v;
        else el.setAttribute(k, v === true ? '' : String(v));
    }
    append(el, children);
    return el;
}

export function append(el, children) {
    for (const c of children.flat(Infinity)) {
        if (c === null || c === undefined || c === false) continue;
        el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
}

export function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
    return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

export function toast(msg, kind = 'error') {
    let box = $('#toasts');
    if (!box) box = document.body.appendChild(h('div', { id: 'toasts' }));
    const t = box.appendChild(h('div', { class: `toast ${kind}`, role: 'status', text: msg }));
    setTimeout(() => t.remove(), kind === 'error' ? 6000 : 3000);
}

export function fmtSize(n) {
    if (n == null) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtTime(ms, withDate = false) {
    if (!ms) return '';
    const d = new Date(ms);
    const sameDay = d.toDateString() === new Date().toDateString();
    const opts = { hour: '2-digit', minute: '2-digit' };
    if (withDate || !sameDay) Object.assign(opts, { year: 'numeric', month: 'short', day: 'numeric' });
    return d.toLocaleString(undefined, opts);
}

// crypto.randomUUID() needs a secure context; the web UI also runs over plain HTTP.
export function uuid() {
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const x = [...b].map((v) => v.toString(16).padStart(2, '0')).join('');
    return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}
