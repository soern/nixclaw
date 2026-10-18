// fetch wrapper for the NixClaw web API. The session cookie travels
// automatically; mutating requests carry the X-NixClaw CSRF header.

export class ApiError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}

const MUTATING = new Set(['POST', 'PUT', 'DELETE', 'PATCH']);

// opts: json (object body), body (FormData/string), headers, raw (return Response)
export async function api(method, path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    let body = opts.body;
    if (opts.json !== undefined) {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(opts.json);
    }
    if (MUTATING.has(method)) headers['X-NixClaw'] = '1';
    const res = await fetch(path, { method, headers, body, credentials: 'same-origin' });
    if (!res.ok) {
        let msg = `${res.status} ${res.statusText}`;
        try {
            const data = await res.json();
            if (data && data.error) msg = data.error;
        } catch { /* non-JSON error body */ }
        if (res.status === 401 && !path.startsWith('/api/auth/')) {
            window.dispatchEvent(new CustomEvent('nixclaw:unauthorized'));
        }
        throw new ApiError(res.status, msg);
    }
    if (opts.raw) return res;
    const type = res.headers.get('Content-Type') || '';
    return type.includes('application/json') ? res.json() : res.text();
}

export const q = (params) => {
    const u = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) u.set(k, v);
    return u.toString();
};

export const attachmentUrl = (cid, name) =>
    `/api/channels/${cid}/attachments/${encodeURIComponent(name)}`;
