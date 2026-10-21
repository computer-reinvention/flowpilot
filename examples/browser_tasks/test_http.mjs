import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { Client, PREFIX, safeOutput } from './http_client.mjs';
import { createMock } from './mock_http.mjs';

function run(executable, args, env, input) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, args, { env: { ...process.env, ...env }, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; }); child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', reject); child.on('exit', code => code === 0 ? resolve(stdout) : reject(Error(`Example exited ${code}: ${stderr}`)));
    child.stdin.end(input);
  });
}
test('standard JS/Python/curl clients run against a local fixture HTTP service', async () => {
  const { server, baseURL, calls } = await createMock();
  try {
    const client = new Client(baseURL, 'synthetic-key');
    const auth = await client.request('POST', '/api/auth/login', { email: 'owner@example.test', password: 'synthetic' });
    const dashboard = new Client(baseURL, auth.token);
    const key = await dashboard.request('POST', '/api/v1/api-keys', { name: 'Example client' });
    assert.equal(key.id, 'key_demo');
    const payload = { task: 'Read the synthetic catalogue', idempotency_key: 'standalone-test-001' };
    const admission = await client.start(payload);
    assert.deepEqual(await client.start(payload), admission);
    await assert.rejects(client.start({ ...payload, task: 'Changed payload' }), /409/);
    const task = await client.poll(admission.task_id, 2, 0);
    assert.equal(task.state, 'waiting_for_input');
    await client.start({ ...payload, idempotency_key: 'followup-test-001', session_id: admission.session_id });
    await client.start({ ...payload, idempotency_key: 'profile-test-001', profile_id: admission.profile_id });
    await client.respond(task.task_id, task.interaction, { idempotency_key: 'answer-test-001', values: { date: '2030-01-02' } });
    await client.respond(task.task_id, { interaction_id: 'confirmation_demo', generation: 2, kind: 'confirmation' }, { idempotency_key: 'decline-test-001', confirmed: false });
    const help = await client.assistance(task.task_id, { interaction_id: 'browser_demo', generation: 3 });
    assert.ok(help.assistance_url); assert.ok(!JSON.stringify(safeOutput(help)).includes('synthetic_capability'));
    await client.request('POST', `${PREFIX}/${task.task_id}/cancel`);
    await client.request('POST', `${PREFIX}/profiles`, { label: 'Synthetic profile', idempotency_key: 'profile-create-001' });
    await client.request('GET', `${PREFIX}/profiles`);
    await client.request('DELETE', `${PREFIX}/profiles/profile_demo`);
    await dashboard.request('DELETE', '/api/v1/api-keys/key_demo');
    const env = { FLOWPILOT_BASE_URL: baseURL, FLOWPILOT_API_KEY: 'synthetic-key' };
    const cwd = new URL('.', import.meta.url).pathname;
    const python = process.env.PYTHON ?? 'python3';
    const output = await run(python, [cwd + 'http_client.py', 'start'], env, JSON.stringify(payload));
    assert.equal(JSON.parse(output).task_id, admission.task_id);
    const poll = await run(python, [cwd + 'http_client.py', 'poll', 'task_demo'], env, '');
    assert.equal(JSON.parse(poll).state, 'waiting_for_input');
    const js = await run(process.execPath, [cwd + 'http_client.mjs'], env, JSON.stringify({ command: 'start', body: payload }));
    assert.equal(JSON.parse(js).task_id, admission.task_id);
    const bootstrap = await run(process.execPath, [cwd + 'http_client.mjs'], { ...env, FLOWPILOT_EMAIL: 'owner@example.test', FLOWPILOT_PASSWORD: 'synthetic' }, JSON.stringify({ bootstrap: true, command: 'start', body: { ...payload, idempotency_key: 'bootstrap-test-001' } }));
    assert.ok(!bootstrap.includes('synthetic_api_key')); assert.ok(!bootstrap.includes('synthetic_dashboard_token'));
    const curl = await run('bash', [cwd + 'curl_http.sh', 'POST', PREFIX], env, JSON.stringify(payload));
    assert.equal(JSON.parse(curl).task_id, admission.task_id);
    const revoked = await run('bash', [cwd + 'curl_http.sh', 'DELETE', PREFIX + '/profiles/profile_demo'], env, '');
    assert.equal(revoked, '');
    assert.ok(calls.length > 15);
  } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});
test('reject unsafe origins and redact credential-bearing output', () => {
  assert.throws(() => new Client('http://example.test', 'test'), /HTTPS/);
  assert.throws(() => new Client('https://user:password@example.test', 'test'), /origin/);
  assert.deepEqual(safeOutput({ key: 'secret', token: 'secret', assistance_url: 'secret', values: { otp: 'secret' }, id: 'safe' }), { id: 'safe' });
});
