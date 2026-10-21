import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import lockfile from 'proper-lockfile';

export const authPath = () => resolve(process.env.PI_AUTH_FILE || new URL('../.pi-auth/auth.json', import.meta.url).pathname);
export function credentialStore(path = authPath()) {
  async function load() {
    try { return JSON.parse(await readFile(path, 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return {}; throw error; }
  }
  async function modify(id, fn) {
    await mkdir(dirname(path), { recursive: true, mode: 0o700 });
    const release = await lockfile.lock(path, { realpath: false, stale: 60000,
      retries: { retries: 120, minTimeout: 100, maxTimeout: 500 } });
    try {
      const data = await load();
      const value = await fn(data[id]);
      if (value === undefined) delete data[id]; else data[id] = value;
      const temp = path + '.' + randomUUID();
      await writeFile(temp, JSON.stringify(data), { mode: 0o600 });
      await rename(temp, path);
      return value;
    } finally { await release(); }
  }
  return { read: async id => (await load())[id],
    list: async () => Object.entries(await load()).map(([providerId, value]) => ({ providerId, type: value.type })),
    modify, delete: id => modify(id, async () => undefined) };
}
