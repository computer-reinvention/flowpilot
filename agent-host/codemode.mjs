import { CodemodeSandbox } from '@earendil-works/pi-codemode';
import { Type } from 'typebox';

// Serialize even nested Promise.all calls: one browser has one current page.
export function createCodeMode(definitions, invoke, { timeoutMs = 30000 } = {}) {
  let tail = Promise.resolve();
  const tools = definitions.filter(tool => !tool.human).map(tool => ({
    ...tool,
    execute(args, { signal }) {
      const next = tail.then(() => {
        signal?.throwIfAborted();
        return invoke(tool.name, args, signal);
      });
      tail = next.catch(() => {});
      return next;
    },
  }));
  const sandbox = new CodemodeSandbox({ tools, timeoutMs, memoryLimitBytes: 32 * 1024 * 1024 });
  return {
    sandbox,
    tool: {
      name: 'codemode', label: 'Browser program', executionMode: 'sequential',
      description: `Run bounded JavaScript composing browser tools. Only injected tools are available; no filesystem, network or process APIs. Return concise observations. Human help must be called separately. Available tool schemas: ${JSON.stringify(definitions.filter(t => !t.human))}`,
      parameters: Type.Object({ code: Type.String({ maxLength: 60000 }) }),
      async execute(id, { code }, signal) {
        const result = await sandbox.execute(code, { signal });
        const text = JSON.stringify(result.ok
          ? { value: result.value, output: result.output }
          : { error: result.error, output: result.output }).slice(0, 24000);
        return { content: [{ type: 'text', text }], details: { ok: result.ok } };
      },
    },
  };
}
