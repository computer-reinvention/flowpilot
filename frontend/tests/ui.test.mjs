// Dependency-free real DOM checks. Build first. Requires a local Chrome executable.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, extname } from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const { cases } = JSON.parse(await readFile(new URL('../../tests/fixtures/browser_tasks/exchanges.json', import.meta.url)));
const fixture = name => structuredClone(cases.find(c => c.name === name).response);
test('account/key/profile/task DOM experience against local frozen HTTP fixtures', { timeout: 90000 }, async () => {
  let account = 'a', key = null, revokeFails = true, listFails = false, stale = false, profileBusy = true, shortGrant = false;
  let task = fixture('question'), lastStart, lastResponse, admissions = 0, uncertain = true, uncertainStart;
  const profiles = [];
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    let body = ''; for await (const chunk of req) body += chunk;
    const data = body ? JSON.parse(body) : {};
    const send = (status, value) => { res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(value === undefined ? undefined : JSON.stringify(value)); };
    if (path === '/api/auth/login' || path === '/api/auth/signup') {
      if (data.password === 'wrong') return send(401, { detail: 'Invalid email or password' });
      account = data.email.startsWith('b') ? 'b' : 'a';
      return send(200, { token: `mock-account-${account}`, user: { id: account, name: `Account ${account}`, email: data.email } });
    }
    if (path === '/api/auth/me') return send(200, { user: { id: account, name: `Account ${account}`, email: `${account}@example.test` } });
    if (path === '/api/v1/api-keys') {
      if (req.method === 'POST') { key = { id: 'key_demo', name: data.name, user_id: account, key_prefix: 'synthetic', created_at: '2030-01-01T00:00:00Z' }; return send(200, { ...key, key: 'synthetic_once_only_key_not_real' }); }
      if (listFails) return send(503, { detail: 'Key list temporarily unavailable' });
      return send(200, key ? [key] : []);
    }
    if (path === '/api/v1/api-keys/key_demo') { if (revokeFails) return send(503, {}); key = null; return send(200, { status: 'deleted' }); }
    if (path === '/api/v1/browser-tasks/profiles') {
      if (req.method === 'POST') { profiles.push({ ...fixture('create_profile'), profile_id: `profile_${profiles.length}`, label: data.label }); return send(201, profiles.at(-1)); }
      return send(200, { items: account === 'a' ? profiles : [] });
    }
    if (path === '/api/v1/browser-tasks/profiles/profile_0' && req.method === 'DELETE') {
      if (profileBusy) return send(409, { error: { code: 'conflict', message: 'Profile leased', retryable: false } });
      profiles.splice(profiles.findIndex(p => p.profile_id === 'profile_0'), 1); return send(204);
    }
    if (path === '/api/v1/browser-tasks') {
      admissions++; lastStart = data;
      if (uncertain) { uncertain = false; uncertainStart = data; return send(503, { error: { code: 'unavailable', message: 'Unavailable', retryable: true } }); }
      task = fixture('question'); return send(202, fixture('admit'));
    }
    if (path === '/api/v1/browser-tasks/task_demo') return account === 'a' ? send(200, task) : send(404, fixture('foreign_task'));
    if (path.endsWith('/respond')) {
      lastResponse = data;
      if (stale) return send(409, { error: { code: 'stale_generation', message: 'No longer active' } });
      task = fixture(data.kind === 'input' ? 'confirmation' : data.kind === 'confirmation' ? 'browser_pause' : 'completed');
      task.task_id = 'task_demo'; task.session_id = 'session_demo'; task.profile_id = 'profile_demo'; task.revision = data.generation + 10;
      return send(202, task);
    }
    if (path.endsWith('/assistance')) return send(201, { ...fixture('mint_assistance'), ...(shortGrant ? { expires_at: new Date(Date.now() + 200).toISOString() } : {}) });
    if (path.endsWith('/cancel')) { task = fixture('cancel'); return send(202, task); }
    if (path.startsWith('/api/v1/browser-tasks/help/')) { res.writeHead(200, { 'Content-Type': 'text/html', 'Content-Security-Policy': "frame-ancestors 'none'" }); return res.end('<h1>Synthetic private viewer</h1>'); }
    if (path.startsWith('/api/')) return send(200, []);
    try {
      const name = path === '/' ? 'index.html' : path.slice(1);
      if (name.includes('..')) return send(404, {});
      const bytes = await readFile(new URL(`../dist/${name}`, import.meta.url));
      res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(name)] ?? 'application/octet-stream' }); res.end(bytes);
    } catch { send(404, {}); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const temp = await mkdtemp(join(tmpdir(), 'flowpilot-lane004-ui-'));
  const chrome = spawn(process.env.CHROME_BIN ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', ['--headless=new', '--window-size=1440,1000', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', `--user-data-dir=${temp}`, 'about:blank'], { stdio: 'ignore' });
  let ws;
  try {
    let port;
    for (let i = 0; i < 100; i++) { try { port = (await readFile(join(temp, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; break; } catch { await delay(50); } }
    assert.ok(port, 'Chrome started');
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    ws = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    let sequence = 0; const responses = new Map();
    ws.onmessage = event => { const message = JSON.parse(event.data); if (message.id) { responses.get(message.id)?.(message); responses.delete(message.id); } };
    const cdp = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; responses.set(id, msg => msg.error ? reject(Error(JSON.stringify(msg.error))) : resolve(msg.result)); ws.send(JSON.stringify({ id, method, params })); });
    const evaluate = async expression => { const result = await cdp('Runtime.evaluate', { expression, userGesture: true, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text); return result.result.value; };
    const wait = async predicate => { for (let i = 0; i < 160; i++) { if (await evaluate(predicate)) return; await delay(50); } throw Error(`Timed out: ${predicate}; alerts: ${await evaluate('JSON.stringify([...document.querySelectorAll("[role=alert]")].map(e => e.textContent))')}; profiles: ${profiles.length}`); };
    const text = () => evaluate('document.body.innerText');
    const contains = value => wait(`document.body.innerText.includes(${JSON.stringify(value)})`);
    const click = async label => { await wait(`[...document.querySelectorAll('button')].some(b => b.textContent.trim() === ${JSON.stringify(label)} && !b.disabled)`); return evaluate(`(() => { const b = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === ${JSON.stringify(label)} && !b.disabled); b.click(); })()`); };
    const fill = (selector, value) => evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) throw Error('Input missing'); Object.getOwnPropertyDescriptor(e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : e.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value').set.call(e, ${JSON.stringify(value)}); e.dispatchEvent(new Event(e.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); })()`);
    await cdp('Page.navigate', { url: base });
    await contains('Log in');
    await fill('#login-email', 'a@example.test'); await fill('#login-password', 'wrong'); await click('Log in');
    await contains('Invalid email or password');
    assert.equal(await evaluate('document.querySelector("#login-password").value'), '');
    await click('Sign up'); await fill('#signup-name', 'Synthetic owner'); await fill('#signup-email', 'a@example.test'); await fill('#signup-password', 'synthetic-password'); await click('Sign up');
    await contains('Start a task / follow-up');
    await click('API Keys'); await contains('No API keys yet');
    await click('Create Key'); await fill('#key-name', 'Test client'); await click('Create'); await contains('synthetic_once_only_key_not_real');
    assert.ok(!(await evaluate('JSON.stringify(localStorage)')).includes('synthetic_once_only'));
    await click('Done'); assert.ok(!(await text()).includes('synthetic_once_only'));
    await evaluate(`document.querySelector('button[aria-label="Revoke Test client"]').click()`); await click('Delete'); await contains('Revocation failed');
    revokeFails = false;
    await evaluate(`document.querySelector('button[aria-label="Revoke Test client"]').click()`); await click('Delete'); await contains('No API keys yet');
    listFails = true; await click('Refresh API keys'); await contains('Key list temporarily unavailable');
    listFails = false; await click('Refresh API keys'); await contains('No API keys yet');
    await click('Browser tasks');
    await fill('input[aria-label="New profile label"]', 'Isolated one'); await click('Create isolated profile'); await contains('profile_0');
    await fill('input[aria-label="New profile label"]', 'Isolated two'); await click('Create isolated profile'); await contains('profile_1');
    await evaluate('window.confirm = () => true'); await click('Delete'); await contains('Profile is busy or changed');
    profileBusy = false; await click('Delete'); await wait('!document.body.innerText.includes("Isolated one")');
    await fill('textarea', 'Read the synthetic catalogue'); await click('Start task'); await contains('Admission may have succeeded');
    await click('Retry same admission'); await contains('Choose a date'); assert.deepEqual(lastStart, uncertainStart);
    assert.equal(lastStart.profile_id, undefined); assert.equal(lastStart.session_id, undefined); assert.ok(lastStart.idempotency_key);
    task.interaction.fields[0].kind = 'secret'; await click('Refresh status / profiles');
    await fill('section form input', 'synthetic-private-value'); await click('Send answer privately'); await contains('Submit the synthetic form?');
    assert.equal(lastResponse.values.date, 'synthetic-private-value'); assert.ok(!(await text()).includes('synthetic-private-value'));
    await click('Decline'); await contains('Resolve the synthetic sign-in'); assert.equal(lastResponse.confirmed, false);
    shortGrant = true; await click('Prepare private browser assistance'); await contains('Open private viewer window');
    await wait('![...document.querySelectorAll("button")].some(b => b.textContent === "Open private viewer window")');
    shortGrant = false;
    await click('Prepare private browser assistance'); await contains('Open private viewer window');
    assert.ok(!(await text()).includes('synthetic_capability')); await click('Open private viewer window');
    await delay(100);
    assert.equal((await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).filter(t => t.url.includes('/help/')).length, 1);
    await click('I resolved this browser pause — resume'); await contains('The synthetic catalogue has two entries'); await delay(100);
    assert.equal((await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).filter(t => t.url.includes('/help/')).length, 0);
    await fill('select', 'session'); await fill('textarea', 'A safe follow-up'); await click('Start task'); await contains('Choose a date'); assert.equal(lastStart.session_id, 'session_demo');
    stale = true; await fill('section form input', 'not-retained'); await click('Send answer privately'); await contains('stale_generation');
    assert.equal(await evaluate('document.querySelector("section form input").value'), ''); stale = false;
    await click('Cancel task'); await contains('Cancellation requested'); task = fixture('cancelled'); await click('Refresh status / profiles'); await contains('cancelled');
    await fill('select', 'profile'); await fill('select[required]', 'profile_1'); await fill('textarea', 'Read in explicitly reused profile'); await click('Start task'); await contains('Choose a date'); assert.equal(lastStart.profile_id, 'profile_1');
    task.interaction = { ...task.interaction, expires_at: new Date(Date.now() + 300).toISOString() }; task.revision++;
    await click('Refresh status / profiles'); await fill('section form input', 'expires-unsubmitted');
    await contains('This pause expired'); assert.equal(await evaluate('document.querySelector("section form input").value'), '');
    await click('Flows'); await contains('Create'); // Existing dashboard remains reachable.
    await click('Browser tasks');
    await evaluate(`document.querySelector('button[aria-label="Log out"]').click()`); await contains('Log in');
    assert.ok(!(await text()).includes('task_demo')); assert.equal(await evaluate('localStorage.getItem("flowpilot_token")'), null);
    listFails = false; await fill('#login-email', 'b@example.test'); await fill('#login-password', 'synthetic-password'); await click('Log in'); await contains('Start a task / follow-up');
    assert.ok(!(await text()).includes('Isolated two'));
    await fill('form.flex input', 'task_demo'); await click('Reattach'); await contains('not_found');
    assert.ok(!(await text()).includes('Choose a date')); assert.equal(admissions, 4);
  } finally {
    ws?.close(); chrome.kill(); await new Promise(resolve => chrome.once('exit', resolve));
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
    await rm(temp, { recursive: true, force: true });
  }
});
