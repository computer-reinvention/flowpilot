import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { credentialStore } from './auth.mjs';

test('credential refresh is serialized and stored privately', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'flowpilot-auth-'));
  const path = join(dir, 'auth.json');
  try {
    const stores = [credentialStore(path), credentialStore(path)];
    await Promise.all(stores.map(s => s.modify('test', async current => {
      await new Promise(resolve => setTimeout(resolve, 10));
      return { type: 'oauth', count: (current?.count || 0) + 1 };
    })));
    assert.equal((await stores[0].read('test')).count, 2);
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.deepEqual(await stores[0].list(), [{providerId:'test',type:'oauth'}]);
  } finally { await rm(dir, {recursive:true, force:true}); }
});
