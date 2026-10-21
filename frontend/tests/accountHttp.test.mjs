import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiFetch, getToken, setToken, clearToken } from '../src/lib/api.ts';

test('account HTTP aborts stale responses, never clears a switched account, handles 204', async () => {
  const originalFetch = globalThis.fetch;
  const originalWindow = globalThis.window;
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const stored = new Map();
  globalThis.window = new EventTarget();
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: k => stored.get(k) ?? null, setItem: (k, v) => stored.set(k, v), removeItem: k => stored.delete(k) } });
  try {
    setToken('account-A');
    let release;
    globalThis.fetch = async (_path, options) => {
      assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error');
      assert.equal(options.headers.Authorization, 'Bearer account-A');
      return { ok: false, status: 401, statusText: 'Unauthorized', json: () => new Promise(resolve => { release = resolve; }) };
    };
    const request = apiFetch('/api/auth/me');
    await new Promise(resolve => setImmediate(resolve));
    setToken('account-B'); release({ detail: 'Unauthorized' });
    await assert.rejects(request, { name: 'AbortError' }); assert.equal(getToken(), 'account-B');
    globalThis.fetch = async (_path, options) => { assert.equal(options.headers.Authorization, 'Bearer account-B'); return new Response(null, { status: 204 }); };
    assert.equal(await apiFetch('/api/v1/api-keys/key_demo', { method: 'DELETE' }), undefined);
    clearToken(); assert.equal(getToken(), null);
  } finally {
    globalThis.fetch = originalFetch; globalThis.window = originalWindow;
    if (originalStorage === undefined) delete globalThis.localStorage; else Object.defineProperty(globalThis, 'localStorage', originalStorage);
  }
});
