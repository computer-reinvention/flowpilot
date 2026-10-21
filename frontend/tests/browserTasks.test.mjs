import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { BrowserTaskClient, TASKS, viewerPath, pauseKey } from '../src/lib/browserTasks.ts';
const { cases } = JSON.parse(await readFile(new URL('../../tests/fixtures/browser_tasks/exchanges.json', import.meta.url)));
const fixture = name => cases.find(item => item.name === name);
test('frozen fixture HTTP operations and no-store authorization', async () => {
  const pending = ['admit', 'question', 'answer', 'confirmation', 'decline', 'browser_pause', 'mint_assistance', 'cancel', 'cancelled', 'list_profiles', 'delete_profile'];
  const client = new BrowserTaskClient('synthetic-account-token', async (path, options) => {
    const item = fixture(pending.shift());
    assert.equal(path, item.path); assert.equal(options.method, item.method);
    assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error');
    assert.equal(options.headers.Authorization, 'Bearer synthetic-account-token');
    if (options.body) assert.deepEqual(JSON.parse(options.body), item.request);
    return new Response(item.status === 204 ? null : JSON.stringify(item.response), { status: item.status });
  });
  await client.start(fixture('admit').request);
  await client.respond(await client.status('task_demo'), fixture('answer').request);
  await client.respond(await client.status('task_demo'), fixture('decline').request);
  const grant = await client.assistance(await client.status('task_demo'));
  assert.equal(viewerPath(grant), fixture('mint_assistance').response.assistance_url);
  await client.cancel('task_demo'); await client.status('task_demo');
  await client.request(`${TASKS}/profiles`); await client.request(`${TASKS}/profiles/profile_demo`, 'DELETE');
  assert.equal(pending.length, 0);
});
test('account switch fences late results and future old-account requests', async () => {
  let finish;
  const old = new BrowserTaskClient('account-A', () => new Promise(resolve => { finish = resolve; }));
  const request = old.status('task_demo'); old.dispose();
  finish(Response.json(fixture('question').response));
  await assert.rejects(request, { name: 'AbortError' });
  await assert.rejects(old.status('task_demo'), { name: 'AbortError' });
  const next = new BrowserTaskClient('account-B', async (_path, opts) => {
    assert.equal(opts.headers.Authorization, 'Bearer account-B'); return Response.json(fixture('foreign_task').response, { status: 404 });
  });
  await assert.rejects(next.status('task_demo'), /not_found/);
});
test('generation identity, expired decisions, foreign viewer paths fail closed', async () => {
  const task = fixture('question').response;
  assert.notEqual(pauseKey(task), pauseKey({ ...task, interaction: { ...task.interaction, generation: 2 } }));
  const client = new BrowserTaskClient('test', async () => { throw Error('must not send'); });
  assert.throws(() => client.respond({ ...task, interaction: { ...task.interaction, expires_at: '2000-01-01T00:00:00Z' } }, {}), /expired/);
  assert.throws(() => viewerPath({ assistance_url: 'https://evil.test/token' }), /Invalid/);
  await assert.rejects(client.request('https://evil.test'), /Invalid/);
});
test('typed errors never echo secret response bodies', async () => {
  const client = new BrowserTaskClient('test', async () => Response.json({ error: { code: 'stale_generation', message: 'secret-value' } }, { status: 409 }));
  await assert.rejects(client.status('task_demo'), error => error.code === 'stale_generation' && !error.message.includes('secret-value'));
});
