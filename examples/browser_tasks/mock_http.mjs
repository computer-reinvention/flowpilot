#!/usr/bin/env node
// Local synthetic fixture server ONLY: not a backend implementation or security oracle.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const { cases } = JSON.parse(await readFile(new URL('../../tests/fixtures/browser_tasks/exchanges.json', import.meta.url)));
export async function createMock() {
  const calls = [];
  const receipts = new Map();
  const server = createServer(async (req, res) => {
    let text = ''; for await (const bytes of req) text += bytes;
    const body = text ? JSON.parse(text) : null;
    const path = new URL(req.url, 'http://localhost').pathname;
    // Store no bearer credentials or private interaction values in mock history.
    calls.push({ method: req.method, path, ...(body?.idempotency_key ? { idempotency_key: body.idempotency_key } : {}) });
    const send = (status, data) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(data === undefined ? undefined : JSON.stringify(data)); };
    if (path === '/api/auth/login' || path === '/api/auth/signup') return send(200, { token: 'synthetic_dashboard_token', user: { id: 'account_demo', email: 'owner@example.test', name: 'Synthetic owner' } });
    if (path === '/api/v1/api-keys' && req.method === 'POST') return send(200, { id: 'key_demo', name: body.name, key: 'synthetic_api_key_not_real', key_prefix: 'synthetic', created_at: '2030-01-01T00:00:00Z' });
    if (path === '/api/v1/api-keys/key_demo' && req.method === 'DELETE') return send(200, { status: 'deleted' });
    let item;
    if (path === '/api/v1/browser-tasks' && req.method === 'POST') {
      const prior = receipts.get(body.idempotency_key);
      const normalized = JSON.stringify(body);
      if (prior && prior !== normalized) item = cases.find(c => c.name === 'conflicting_retry');
      else { receipts.set(body.idempotency_key, normalized); item = cases.find(c => c.name === 'admit'); }
    } else if (path === '/api/v1/browser-tasks/task_demo' && req.method === 'GET') item = cases.find(c => c.name === 'question');
    else item = cases.find(c => c.path === path && c.method === req.method && c.name !== 'duplicate_admit');
    if (!item) return send(404, { error: { code: 'not_found', message: 'No synthetic fixture', retryable: false } });
    send(item.status, item.status === 204 ? undefined : item.response);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { server, calls, baseURL: `http://127.0.0.1:${server.address().port}` };
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const mock = await createMock();
  console.log(`Synthetic fixture service: ${mock.baseURL}`);
  console.log('This fake is not an auth/executor implementation; it never visits websites. Ctrl-C to stop.');
}
