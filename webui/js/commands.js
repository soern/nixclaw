// /status and /deauth typed into the composer: answered locally from
// /api/endpoints, shown as an ephemeral line, never sent to the agent.
import { api } from './api.js';
import { fmtTime } from './dom.js';

export const isCommand = (text) => /^\/(status|deauth)(\s|$)/i.test(text.trim());

function describe(e, i) {
    const parts = [`${i + 1}. ${e.name}${e.current ? ' (this device)' : ''}`,
        `created ${fmtTime(e.created_at, true)}`,
        `last used ${e.last_used_at ? fmtTime(e.last_used_at, true) : 'never'}`,
        e.expires_at ? `expires ${fmtTime(e.expires_at, true)}` : 'never expires',
        `${e.connections} connection${e.connections === 1 ? '' : 's'}`];
    return parts.join(' · ');
}

export async function runCommand(text) {
    const [cmd, ...rest] = text.trim().split(/\s+/);
    const arg = rest.join(' ');
    try {
        const { endpoints } = await api('GET', '/api/endpoints');
        if (cmd.toLowerCase() === '/status') {
            if (!endpoints.length) return ['No authorized web endpoints.'];
            return ['Authorized web endpoints:', ...endpoints.map(describe)];
        }
        if (!arg) return ['Usage: /deauth <number from /status | name>'];
        const target = /^\d+$/.test(arg)
            ? endpoints[Number(arg) - 1]
            : endpoints.find((e) => e.name.toLowerCase() === arg.toLowerCase());
        if (!target) return [`No endpoint "${arg}". Use /status to list them.`];
        await api('DELETE', `/api/endpoints/${target.id}`);
        return [`Deauthorized "${target.name}".${target.current ? ' This device is now logged out.' : ''}`];
    } catch (ex) {
        return [`Command failed: ${ex.message}`];
    }
}
