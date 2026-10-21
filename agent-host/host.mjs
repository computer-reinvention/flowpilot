// Newline-delimited JSON transport. stdout is reserved for protocol messages.
import readline from 'node:readline';
import { Agent } from '@earendil-works/pi-agent-core';
import { createModels } from '@earendil-works/pi-ai';
import { anthropicProvider } from '@earendil-works/pi-ai/providers/anthropic';
import { googleProvider } from '@earendil-works/pi-ai/providers/google';
import { openaiProvider } from '@earendil-works/pi-ai/providers/openai';
import { openaiCodexProvider } from '@earendil-works/pi-ai/providers/openai-codex';
import { credentialStore } from './auth.mjs';
import { createCodeMode } from './codemode.mjs';

const send = message => process.stdout.write(JSON.stringify(message) + '\n');
const pending = new Map();
let sequence = 0;
let running = false;
function invoke(name, args, signal) {
  return new Promise((resolve, reject) => {
    const id = String(++sequence);
    const abort = () => {
      pending.delete(id);
      send({ type: 'cancel_tool', id });
      reject(new Error('Tool cancelled'));
    };
    if (signal?.aborted) return reject(new Error('Tool cancelled'));
    signal?.addEventListener('abort', abort, { once: true });
    pending.set(id, {
      settle(message) {
        signal?.removeEventListener('abort', abort);
        message.error ? reject(new Error(message.error)) : resolve(message.result);
      },
    });
    send({ type: 'tool', id, name, args });
  });
}

async function run(config) {
  const models = createModels({ credentials: credentialStore() });
  const providers = { anthropic: anthropicProvider, google: googleProvider, openai: openaiProvider, 'openai-codex': openaiCodexProvider };
  if (!providers[config.provider]) throw new Error('Unsupported provider');
  models.setProvider(providers[config.provider]());
  const model = models.getModel(config.provider, config.model);
  if (!model) throw new Error(`Pi model catalog does not contain ${config.provider}/${config.model}`);
  const code = createCodeMode(config.tools, invoke);
  const direct = config.tools.map(def => ({
    name: def.name, label: def.name, description: def.description,
    parameters: def.inputSchema, executionMode: 'sequential',
    async execute(id, args, signal) {
      const value = await invoke(def.name, args, signal);
      return { content: [{ type: 'text', text: JSON.stringify(value).slice(0, 24000) }], details: {} };
    },
  }));
  let turns = 0;
  const agent = new Agent({
    initialState: { model, systemPrompt: config.system, tools: [...direct, code.tool] },
    streamFn: models.streamSimple.bind(models), toolExecution: 'sequential',
    getApiKey: () => process.env.FLOWPILOT_MODEL_KEY || undefined,
    finishTurn: () => ++turns >= (config.maxTurns || 50) ? { action: 'end' } : undefined,
  });
  agent.subscribe(event => {
    if (event.type === 'turn_start') send({ type: 'metric', name: 'model_turn' });
  });
  try {
    await agent.prompt(config.task);
    const last = agent.state.messages.filter(m => m.role === 'assistant').at(-1);
    if (!last || ['error', 'aborted'].includes(last.stopReason)) {
      throw new Error(last?.errorMessage || 'Agent did not produce a result');
    }
    if (turns >= (config.maxTurns || 50)) throw new Error('Agent turn limit reached');
    send({ type: 'result', text: last.content.filter(c => c.type === 'text').map(c => c.text).join('\n') });
  } finally { await code.sandbox.close(); }
}

const lines = readline.createInterface({ input: process.stdin });
lines.on('line', line => {
  try {
    const message = JSON.parse(line);
    if (message.type === 'tool_result') {
      const request = pending.get(message.id);
      pending.delete(message.id);
      request?.settle(message);
    } else if (message.type === 'start' && !running) {
      running = true;
      run(message).catch(error => send({ type: 'error', error: error.message }))
        .finally(() => { lines.close(); process.stdin.destroy(); });
    }
  } catch (error) { send({ type: 'error', error: error.message }); }
});
lines.on('close', () => { if (pending.size) process.exitCode = 1; });
