import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createCodeMode } from './codemode.mjs';

test('real WASM composes tools and serializes browser mutations', async () => {
  let active = 0;
  const calls = [];
  const host = createCodeMode([{ name: 'click' }], async (_, args) => {
    assert.equal(active++, 0);
    await new Promise(resolve => setTimeout(resolve, 10));
    calls.push(args.id);
    active--;
    return args.id;
  });
  try {
    const result = await host.sandbox.execute('return await Promise.all([tools.click({id:1}), tools.click({id:2})]);');
    assert.equal(result.ok, true);
    assert.deepEqual(result.value, [1, 2]);
    assert.deepEqual(calls, [1, 2]);
  } finally { await host.sandbox.close(); }
});

test('host capabilities and human-help are unavailable inside timed code', async () => {
  const host = createCodeMode([{ name: 'human_help', human: true }], () => assert.fail());
  try {
    const result = await host.sandbox.execute('return [typeof process, typeof fetch];');
    assert.deepEqual(result.value, ['undefined', 'undefined']);
    const human = await host.sandbox.execute('return await tools.human_help({});');
    assert.equal(human.ok, false);
  } finally { await host.sandbox.close(); }
});

test('runaway programs time out and cancellation stops execution', async () => {
  const host = createCodeMode([], () => {}, { timeoutMs: 100 });
  try {
    const result = await host.sandbox.execute('while (true) {}');
    assert.equal(result.ok, false);
    assert.equal(result.error.kind, 'timeout');
    const controller = new AbortController();
    controller.abort();
    const cancelled = await host.sandbox.execute('return 1', { signal: controller.signal });
    assert.equal(cancelled.ok, false);
    assert.equal(cancelled.error.kind, 'aborted');
  } finally { await host.sandbox.close(); }
});
