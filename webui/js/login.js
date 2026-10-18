// Login: number -> code (sent via Signal) + endpoint name.
import { api } from './api.js';
import { $ } from './dom.js';

function guessDeviceName() {
    const ua = navigator.userAgent;
    const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox'
        : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser';
    const os = /Android/.test(ua) ? 'Android' : /iPhone|iPad/.test(ua) ? 'iOS'
        : /Windows/.test(ua) ? 'Windows' : /Mac OS/.test(ua) ? 'macOS' : /Linux/.test(ua) ? 'Linux' : '';
    return os ? `${browser} on ${os}` : browser;
}

export function showLogin(onSuccess) {
    const numberForm = $('#login-number');
    const codeForm = $('#login-code');
    const err = $('#login-error');
    let number = '';

    const fail = (msg) => { err.textContent = msg; err.hidden = !msg; };
    const busy = (form, on) => form.querySelectorAll('button, input').forEach((el) => { el.disabled = on; });
    const step = (codeStep) => {
        numberForm.hidden = codeStep;
        codeForm.hidden = !codeStep;
        fail('');
        (codeStep ? $('#login-code-input') : $('#login-number-input')).focus();
    };

    numberForm.onsubmit = async (e) => {
        e.preventDefault();
        number = $('#login-number-input').value.replace(/[\s()-]/g, '');
        busy(numberForm, true);
        try {
            await api('POST', '/api/auth/request', { json: { number } });
            $('#login-code-hint').textContent =
                `If ${number} is allowed to use NixClaw, a code was sent to it via Signal.`;
            if (!$('#login-endpoint-input').value) $('#login-endpoint-input').value = guessDeviceName();
            step(true);
        } catch (ex) {
            fail(ex.status === 429 ? 'Too many requests, please wait a while.' : ex.message);
        } finally {
            busy(numberForm, false);
        }
    };

    codeForm.onsubmit = async (e) => {
        e.preventDefault();
        busy(codeForm, true);
        try {
            await api('POST', '/api/auth/verify', {
                json: {
                    number,
                    code: $('#login-code-input').value.trim(),
                    endpoint_name: $('#login-endpoint-input').value.trim(),
                },
            });
            $('#login-code-input').value = '';
            $('#login').hidden = true;
            onSuccess();
        } catch (ex) {
            fail(ex.status === 429 ? 'Too many attempts, please wait a while.' : ex.message);
        } finally {
            busy(codeForm, false);
        }
    };

    $('#login-back').onclick = () => step(false);

    $('#app').hidden = true;
    $('#login').hidden = false;
    step(false);
}
