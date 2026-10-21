#!/usr/bin/env node
// Node 22+ built-ins only. No Flowpilot SDK or filesystem coupling.
import { pathToFileURL } from 'node:url';

export const PREFIX = '/api/v1/browser-tasks';
export const terminal = state => ['completed', 'failed', 'cancelled', 'interrupted'].includes(state);
export function identifier(value) {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(value ?? '')) throw Error('Invalid object ID');
  return value;
}
export class Client {
  constructor(baseURL, token) {
    const url = new URL(baseURL);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') throw Error('Use a service origin without credentials/path/query');
    if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw Error('Use HTTPS except for loopback mocks');
    this.baseURL = url.origin; this.token = token;
  }
  async request(method, path, body) {
    if (!path.startsWith('/api/') || path.includes('..') || path.includes(':') || path.includes('#')) throw Error('Use a relative API path');
    let response;
    try {
      response = await fetch(this.baseURL + path, {
        method, redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(35000),
        headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch { throw Error('Transport outcome unknown. Retry identical admission/key or reattach by task ID; never automatically replay website effects'); }
    if (!response.ok) throw Error(`HTTP ${response.status}; refresh current owned status before deciding again`);
    if (response.status === 204) return null;
    const text = await response.text();
    if (text.length > 262144) throw Error('Response exceeds example client limit');
    try { return JSON.parse(text); } catch { throw Error('Invalid JSON response'); }
  }
  start(payload) {
    if (!payload.idempotency_key) throw Error('Supply a stable admission idempotency_key before sending');
    return this.request('POST', PREFIX, payload);
  }
  status(id) { return this.request('GET', `${PREFIX}/${identifier(id)}`); }
  async poll(id, attempts = 20, interval = 1000) {
    for (let n = 0; n < attempts; n++) {
      const view = await this.status(id);
      if (terminal(view.state) || view.interaction) return view;
      await new Promise(resolve => setTimeout(resolve, interval));
    }
    throw Error('Polling budget exhausted; task is not cancelled. Reattach by task ID');
  }
  respond(id, pause, values) {
    return this.request('POST', `${PREFIX}/${identifier(id)}/interactions/${identifier(pause.interaction_id)}/respond`, {
      ...values, generation: pause.generation, kind: pause.kind,
    });
  }
  async assistance(id, pause) {
    const grant = await this.request('POST', `${PREFIX}/${identifier(id)}/assistance`, { interaction_id: pause.interaction_id, generation: pause.generation });
    if (!/^\/api\/v1\/browser-tasks\/help\/[A-Za-z0-9_-]{32,128}$/.test(grant.assistance_url) || grant.interaction_id !== pause.interaction_id || grant.generation !== pause.generation) throw Error('Private viewer does not match the current pause/path');
    return grant;
  }
}
export function safeOutput(value) {
  if (Array.isArray(value)) return value.map(safeOutput);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).filter(([key]) => !['key', 'token', 'assistance_url', 'values'].includes(key)).map(([key, item]) => [key, safeOutput(item)]));
  return value;
}

// JSON operation requests come from stdin, never argv; credentials come from env.
// For interactive private answers, use the Python CLI or call respond() in memory.
async function main() {
  const client = new Client(process.env.FLOWPILOT_BASE_URL ?? 'http://127.0.0.1:8080', process.env.FLOWPILOT_API_KEY ?? '');
  let input = ''; for await (const chunk of process.stdin) input += chunk;
  const operation = JSON.parse(input);
  let dashboard, createdKey;
  try {
    if (operation.bootstrap) {
      const auth = await client.request('POST', operation.signup ? '/api/auth/signup' : '/api/auth/login', {
        email: process.env.FLOWPILOT_EMAIL, password: process.env.FLOWPILOT_PASSWORD, ...(operation.signup ? { name: operation.name } : {}),
      });
      dashboard = new Client(client.baseURL, auth.token);
      const created = await dashboard.request('POST', '/api/v1/api-keys', { name: 'Temporary standalone HTTP example' });
      createdKey = created.id; client.token = created.key;
    } else if (!client.token) throw Error('Set FLOWPILOT_API_KEY via secret input');
    let output;
    const id = operation.id;
    switch (operation.command) {
      case 'start': output = await client.start(operation.body); break;
      case 'followup': output = await client.start({ ...operation.body, session_id: identifier(id) }); break;
      case 'status': output = await client.status(id); break;
      case 'poll': output = await client.poll(id); break;
      case 'cancel': output = await client.request('POST', `${PREFIX}/${identifier(id)}/cancel`); break;
      case 'profiles': output = await client.request('GET', `${PREFIX}/profiles`); break;
      case 'profile-create': output = await client.request('POST', `${PREFIX}/profiles`, operation.body); break;
      case 'profile-delete': output = await client.request('DELETE', `${PREFIX}/profiles/${identifier(id)}`); break;
      case 'respond': output = await client.respond(id, operation.interaction, operation.body); operation.body = null; break;
      case 'help': {
        const view = await client.status(id);
        if (view.interaction?.kind !== 'browser') throw Error('Help requires a current browser pause');
        output = await client.assistance(id, view.interaction);
        if (operation.private_link) {
          if (!process.stdout.isTTY) throw Error('Refusing bearer viewer link disclosure to logs/pipes');
          console.log(client.baseURL + output.assistance_url);
        }
        break;
      }
      case 'revoke': output = await client.request('DELETE', `/api/v1/api-keys/${identifier(id)}`); break;
      default: throw Error('Unknown command');
    }
    console.log(JSON.stringify(safeOutput(output), null, 2));
  } finally {
    if (createdKey) await dashboard.request('DELETE', `/api/v1/api-keys/${identifier(createdKey)}`);
    client.token = ''; input = '';
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => { console.error(error instanceof SyntaxError ? 'Invalid input JSON' : error.message); process.exitCode = 1; });
}
