// Overlay viewer: full-screen modal for images, video, PDF, and text files.
// Files that don't match any category are downloaded instead.

import { h } from './dom.js';

const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];
const VIDEO_EXT = ['mp4', 'webm', 'ogv', 'mov', 'mkv'];
const TEXT_EXT = ['txt', 'md', 'csv', 'log', 'json', 'yml', 'yaml', 'js', 'css',
    'html', 'py', 'c', 'cpp', 'h', 'sh', 'ini', 'conf', 'cfg', 'toml'];

let overlayEl = null;

function ext(name) {
    const dot = name.lastIndexOf('.');
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

function detectType(name, contentType) {
    const ct = (contentType || '').split(';')[0].trim().toLowerCase();
    const e = ext(name || '');
    if (ct.startsWith('image/') || IMAGE_EXT.includes(e)) return 'image';
    if (ct.startsWith('video/') || VIDEO_EXT.includes(e)) return 'video';
    if (ct === 'application/pdf' || e === 'pdf') return 'pdf';
    if (ct.startsWith('text/') || TEXT_EXT.includes(e)) return 'text';
    return null;
}

function closeOverlay() {
    if (!overlayEl) return;
    // Stop any playing media
    const video = overlayEl.querySelector('video');
    if (video) video.pause();
    const iframe = overlayEl.querySelector('iframe');
    if (iframe) iframe.src = 'about:blank';
    document.removeEventListener('keydown', _onKey);
    overlayEl.remove();
    overlayEl = null;
}

function _onKey(e) {
    if (e.key === 'Escape') closeOverlay();
}

/**
 * Show a full-screen overlay for the given file.
 * @param {string} url - URL to the file (raw endpoint)
 * @param {object} opts
 * @param {string} opts.name - file name (for title and download)
 * @param {string} [opts.contentType] - MIME type (optional, used for detection)
 */
export async function showOverlay(url, { name, contentType } = {}) {
    const type = detectType(name, contentType);

    // If we can't display it, just download.
    if (!type) {
        const a = document.createElement('a');
        a.href = url;
        a.download = name || '';
        a.click();
        return;
    }

    closeOverlay();

    const backdrop = h('div', {
        class: 'overlay-backdrop',
        onclick: (e) => { if (e.target === backdrop) closeOverlay(); },
    });

    const header = h('div', { class: 'overlay-header' },
        h('span', { class: 'overlay-title', text: name || 'Preview' }),
        h('div', { class: 'overlay-actions' },
            h('a', {
                class: 'icon small button', title: 'Download', text: '⬇',
                href: url, download: name || '',
            }),
            h('button', { class: 'icon small', title: 'Close', text: '✕', onclick: closeOverlay }),
        ),
    );

    const content = h('div', { class: 'overlay-content' });
    backdrop.append(header, content);
    document.body.appendChild(backdrop);
    overlayEl = backdrop;
    document.addEventListener('keydown', _onKey);

    if (type === 'image') {
        content.append(h('img', { src: url, alt: name || '' }));
    } else if (type === 'video') {
        content.append(h('video', { src: url, controls: true, autoplay: true }));
    } else if (type === 'pdf') {
        content.append(h('iframe', { src: url, title: name || 'PDF' }));
    } else if (type === 'text') {
        const pre = h('pre', { class: 'overlay-text', text: 'Loading…' });
        content.append(pre);
        try {
            const res = await fetch(url);   // same-origin: cookies sent automatically
            const text = await res.text();
            pre.textContent = text;
        } catch {
            pre.textContent = '(failed to load)';
        }
    }
}
