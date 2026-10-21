import { createModels } from '@earendil-works/pi-ai';
import { openaiCodexProvider } from '@earendil-works/pi-ai/providers/openai-codex';
import { credentialStore, authPath } from './auth.mjs';

const models = createModels({ credentials: credentialStore() });
models.setProvider(openaiCodexProvider());
await models.login('openai-codex', 'oauth', {
  signal: AbortSignal.timeout(15 * 60 * 1000),
  prompt: async p => {
    if (p.type === 'select') {
      const method = p.options.find(o => o.id === 'device_code');
      if (method) return method.id;
    }
    throw new Error('Interactive login required: ' + p.message);
  },
  notify: e => {
    if (e.type === 'device_code') console.log(JSON.stringify({ url: e.verificationUri, code: e.userCode }));
    else if (e.type === 'auth_url') console.log(JSON.stringify({ url: e.url }));
    else if (e.message) console.log(e.message);
  },
});
console.log('Subscription login saved to ' + authPath());
