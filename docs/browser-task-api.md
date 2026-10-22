# Standalone browser-task HTTP API (v1)

Flowpilot owns authentication, task execution, browser profiles, script memory and conversational state. Any HTTP client can use it. No broker, Harp configuration, shared filesystem, browser-provider credential or required SDK is involved.

This guide targets the **frozen foundation contract**, `backend/task_contracts.py` and `tests/fixtures/browser_tasks/{exchanges,policy}.json`. The examples and owner UI are verified against synthetic HTTP fixtures, **not an assembled live executor**. Deployment and real-service acceptance belong to lanes005/006. Browser tasks are disabled/fail-closed unless the operator enables a complete composition; a 404/503 is not an invitation to fall back to legacy flow execution.

## Start in the dashboard

1. Sign up or log in with the existing account form. When `FLOWPILOT_SIGNUP_EMAILS` is set, only those emails can sign up (403 otherwise). Password fields clear after success/failure.
2. Select **API Keys → Create Key**. Plaintext appears once in a dismissible dialog. Copying is explicit; dismissing, navigating away or logging out drops that disclosure. Lists contain metadata only. Save the key in your own secret manager, not source control, task text or a URL.
3. Select **Browser tasks**. The default creates a **fresh isolated profile**. Explicitly choose an owned profile to reuse website login and memory; follow up in the displayed session only after its previous task is terminal.
4. Save the returned task ID to reattach after refresh. Task IDs, not secret responses or viewer links, are the recovery handle. The dashboard intentionally does not persist task prompts, private answers or help grants in browser storage. Its existing account JWT remains in the existing `flowpilot_token` convention.
5. Use the displayed question, explicit approve/decline buttons, or current-browser-pause assistance. The private viewer opens in a separate window because the frozen viewer forbids framing. Allow the popup and keep its address private. The app closes its managed viewer on pause changes, expiration, navigation and logout.

The old **Flows**, **Runs**, legacy **Profiles**, API-key and webhook surfaces remain available. Browser-task profiles use a separate owned API; a legacy profile name is not a browser-task profile ID.

## Authority and isolation

- `Authorization: Bearer <account-owned API key>` or the existing dashboard JWT authenticates every account endpoint. Never put a key in a URL, header debug dump, browser log, screenshot or task input.
- **Two API keys for one account have the same account authority.** Neither keys nor a client-supplied user/profile string create separate tenants. A multi-user client must use separate Flowpilot accounts/credentials for separate security principals.
- Fresh profiles within an account isolate website cookies/storage, script memory and conversation history. Explicit profile/session reuse restores the corresponding durable state. One active task per session and one writer per profile are allowed; there is no implicit queue behind a busy profile.
- Profiles belong to immutable server-resolved accounts. Foreign objects return `not_found`, not metadata about another owner. UI controls are not authorization boundaries; the service must enforce ownership on every operation.
- Logout/account switching unmounts account UI, fences old HTTP responses and closes managed help windows. A bearer viewer is a separate short-lived capability, **not** an account login. The frozen contract has no independent “revoke this help token on logout” endpoint. Server revocation occurs on remint, resume, cancellation, expiry and restart. Closing a window alone is not server revocation, and a deliberately copied link remains a secret until revoked/expired.

## Endpoints and shapes

Account operations retain existing routes and envelopes:

| Method/path | Body / response |
| --- | --- |
| `POST /api/auth/signup` | `{email,password,name}` → `{token,user}` |
| `POST /api/auth/login` | `{email,password}` → `{token,user}` |
| `GET /api/auth/me` | `{user}` |
| `POST /api/v1/api-keys` | `{name}` → metadata plus once-only `key` |
| `GET /api/v1/api-keys` | metadata array, never plaintext |
| `DELETE /api/v1/api-keys/{key_id}` | revoke immediately for future requests; existing handler returns `{status:"deleted"}` |

Do not cache authentication/key responses. The frontend sends `cache: no-store`; the backend/reverse proxy must also emit `Cache-Control: no-store` for auth, key and assistance responses (integration check required). Revoking a key prevents later polling, responses and mints; it **does not implicitly cancel** an already admitted task. Use another currently authorized credential to control it.

New task operations live under `T = /api/v1/browser-tasks`:

| Method/path | Request | Success |
| --- | --- | --- |
| `POST T` | `{task,inputs?,profile_id?,session_id?,idempotency_key}` | 202 `TaskAdmission` |
| `GET T/{task_id}` | optional `after_revision`, `wait_seconds` query parameters | 200 `TaskView` |
| `POST T/{task_id}/cancel` | no body | 202 current `TaskView` |
| `POST T/{task_id}/interactions/{interaction_id}/respond` | generation-bound response below | 202 `TaskView` |
| `POST T/{task_id}/assistance` | `{interaction_id,generation}` for current browser pause | 201 secret grant |
| `POST T/profiles` | `{label,idempotency_key}` | 201 profile |
| `GET T/profiles` | no body | 200 `{items,next_cursor?}` |
| `DELETE T/profiles/{profile_id}` | no body | 204 empty |

`TaskAdmission` contains `task_id`, `session_id`, `profile_id`, `revision`, and a canonical **relative** `status_url`. Prefer constructing the known owned status route rather than forwarding credentials to arbitrary returned URLs. The examples refuse redirects and non-loopback plaintext HTTP.

`TaskView` contains those IDs, `state`, monotonic `revision`, safe `progress`, timestamps, and optionally `interaction`, `result` or typed `error`. A result is `{answer,source_urls,metrics}`. Ordinary status/results never contain an assistance capability. States:

```
queued → running → waiting_for_input / waiting_for_confirmation / waiting_for_browser
                 → completed / failed / interrupted
nonterminal → cancelling → cancelled / interrupted
```

`cancelling` is intent, not proof of termination. Poll until a terminal state. Disconnecting, navigation, closing a help window and a polling deadline do **not** cancel execution.

Bounds: task 1–16,000 characters, up to 64 bounded named inputs / 32 KiB encoded input values, body 128 KiB, idempotency keys 8–128 characters from `[A-Za-z0-9_.:-]`. Long polling waits at most 25 seconds and requires `after_revision`. The UI uses serialized short polling. Profile list responses are bounded to 100 items; the frozen contract exposes `next_cursor` but **does not define a cursor request parameter**. The UI reports truncation; it does not invent pagination.

## Safe runnable clients

Files under `examples/browser_tasks/` use only Python standard library, Node built-ins (Node 22+), or curl/bash/Python. They import no Flowpilot internals. Run from the repository root or copy the two client files elsewhere; they need only a configured base URL/key. `mock_http.mjs` is a synthetic fixture service, **not** a production backend, state machine or isolation/security proof.

```sh
node examples/browser_tasks/mock_http.mjs
# In another terminal, set the printed loopback origin:
export FLOWPILOT_BASE_URL=http://127.0.0.1:PORT
# Read a real key without putting it into shell history (bash):
read -r -s -p 'Flowpilot key: ' FLOWPILOT_API_KEY; echo
export FLOWPILOT_API_KEY
# Against the fake only, any explicitly synthetic input works.
```

Do not enable shell tracing, curl `-v/--trace`, HTTP debug logging, terminal recording or telemetry around credentials. Avoid passing secrets as process arguments. Environment variables are convenient examples, not a substitute for an appropriately protected secret store.

### Login, signup, and once-only creation without disclosure

The Python CLI can bootstrap a **temporary** key: prompt for account email/password, log in, create a key held in memory, perform one command, then revoke it using the dashboard JWT. No plaintext key/JWT is printed. Add `--signup` to create an account first (account creation is not undone). Example task JSON contains no credentials:

```sh
python3 examples/browser_tasks/http_client.py start --bootstrap <<'JSON'
{"task":"Read the synthetic catalogue","idempotency_key":"admission-owner-001"}
JSON
```

Bootstrap prompts use hidden terminal input independently of JSON stdin and require an interactive terminal. For noninteractive automation, the JS CLI uses secret environment values `FLOWPILOT_EMAIL` and `FLOWPILOT_PASSWORD`; inject them from your secret manager, not inline assignments in a recorded shell:

```sh
node examples/browser_tasks/http_client.mjs <<'JSON'
{"bootstrap":true,"command":"start","body":{"task":"Read the synthetic catalogue","idempotency_key":"bootstrap-owner-001"}}
JSON
```

Programmatic account/key creation, Python (the key never leaves process memory):

```python
import getpass, os
from http_client import Client
base = os.environ["FLOWPILOT_BASE_URL"]
auth = Client(base, "").request("POST", "/api/auth/login", {
    "email": input("Email: "), "password": getpass.getpass("Password: ")})
dashboard = Client(base, auth["token"])
created = dashboard.request("POST", "/api/v1/api-keys", {"name": "My HTTP client"})
client = Client(base, created.pop("key"))
# Keep created["id"] for explicit revocation. Never print auth/created/client.token.
```

The JavaScript `Client` exposes the same `request`, `start`, `status`, `poll`, `respond`, `assistance` methods. Bootstrap automatically revokes its temporary key even when polling/admission fails; an admitted task may still be running and requires another authorized key to reattach. Revocation errors are surfaced rather than silently ignored.

### Admission, fresh isolation, follow-up and explicit profile reuse

**Choose a stable admission key before the first HTTP attempt**. These examples intentionally do not auto-generate a replacement after an uncertain outcome.

```sh
python3 examples/browser_tasks/http_client.py start <<'JSON'
{"task":"Read the synthetic catalogue","inputs":{"query":"milk"},"idempotency_key":"admission-owner-002"}
JSON

# Use the actual returned task/session/profile IDs, not an account name.
python3 examples/browser_tasks/http_client.py status task_demo
python3 examples/browser_tasks/http_client.py poll task_demo
python3 examples/browser_tasks/http_client.py followup session_demo <<'JSON'
{"task":"Summarize the previous result","idempotency_key":"followup-owner-001"}
JSON

python3 examples/browser_tasks/http_client.py profile-create <<'JSON'
{"label":"Separate private browsing","idempotency_key":"profile-owner-001"}
JSON
python3 examples/browser_tasks/http_client.py profiles
python3 examples/browser_tasks/http_client.py start <<'JSON'
{"task":"Inspect the current page without submitting anything","profile_id":"profile_demo","idempotency_key":"reuse-owner-001"}
JSON
```

Omitting **both** session/profile creates a new private profile every time. To create two independent profiles in one account, admit two tasks with different admission keys and no reuse fields, or create two profiles explicitly. Reusing the **same** admission key/payload returns the **same** admission, not a second isolated browser. Separate accounts must use their own credentials; never pass an owner string to impersonate another principal.

JS CLI equivalent:

```sh
node examples/browser_tasks/http_client.mjs <<'JSON'
{"command":"start","body":{"task":"Read the synthetic catalogue","idempotency_key":"admission-js-001"}}
JSON
node examples/browser_tasks/http_client.mjs <<'JSON'
{"command":"followup","id":"session_demo","body":{"task":"Summarize the previous result","idempotency_key":"followup-js-001"}}
JSON
```

The bounded curl helper passes Authorization through a pipe (not process arguments), reads POST JSON from stdin, refuses redirects, and strips credentials/capabilities from output:

```sh
bash examples/browser_tasks/curl_http.sh POST /api/v1/browser-tasks <<'JSON'
{"task":"Read the synthetic catalogue","idempotency_key":"admission-curl-001"}
JSON
bash examples/browser_tasks/curl_http.sh GET /api/v1/browser-tasks/task_demo
bash examples/browser_tasks/curl_http.sh POST /api/v1/browser-tasks/task_demo/cancel </dev/null
bash examples/browser_tasks/curl_http.sh DELETE /api/v1/api-keys/key_demo
```

The curl helper may call existing auth/key endpoints with JSON stdin too, but deliberately **discards** returned plaintext keys/JWTs. Use the dashboard or in-memory bootstrap for a usable new key; do not redirect an auth response into ordinary logs/files. Never put the header directly into a copied curl command or shell history.

### Questions, confirmations, and current-pause help

A pending interaction has `interaction_id`, `generation`, `kind`, `message`, `expires_at`, and either typed `fields` or a confirmation `action_id`. Bind the response to **the exact returned task/interaction/generation**, with a separate stable response idempotency key:

```json
{"kind":"input","generation":1,"idempotency_key":"response-owner-001","values":{"date":"2030-01-02"}}
```

For secret fields, obtain the value privately and hold it only in memory. Do not put it in sample files, argv, task text, history or telemetry. Python `respond task_demo` prompts without echo for all input values and clears its response dictionary after sending; it asks for explicit `approve`/`decline` or `resolved`. Values are not restored after a process restart. Runtime strings cannot promise cryptographic memory zeroization.

```sh
python3 examples/browser_tasks/http_client.py respond task_demo
```

Confirmation responses are exactly one of:

```json
{"kind":"confirmation","generation":2,"idempotency_key":"decision-owner-001","confirmed":false}
```

`true` explicitly authorizes only the proposed action; `false` does not. A decline stops the agent immediately (no further model turns or browser actions) and the task ends `completed` with an answer that starts `Not booked: you declined this proposal` followed by the proposal text, so "find options, then ask me" requests still return the options. `browser` responses use `browser_resolved:true`, only after the owner resolved that pause. A conflict/stale generation/expiry requires fetching status and reviewing the **new** interaction, never silently rebinding the old answer to it. The examples do not automatically retry private responses; after a lost response inspect status first. They do not claim every arbitrary website action can be classified or made exactly-once.

Help is minted only for `waiting_for_browser`:

```sh
python3 examples/browser_tasks/http_client.py help task_demo
# Explicit bearer-link disclosure only to an unrecorded interactive terminal:
python3 examples/browser_tasks/http_client.py help task_demo --private-link
```

`POST T/{task}/assistance` sends `{interaction_id,generation}`. Its response has `assistance_url`, `expires_at`, and the bound interaction/generation. The URL is a relative `T/help/{opaque_token}` path with **no API key**. Deliver it privately only when explicitly requested; ordinary output strips it. Never persist it in task history or browser storage. Same-origin input/resume/WebSocket requests must satisfy the frozen Origin policy. Viewer 410 means expired/revoked; refresh owner status and explicitly remint only for the current browser pause. The dashboard's same-origin top-level viewer uses lane003's viewer implementation, not a second assistance transport.

### Cancellation, revocation and profile deletion

```sh
python3 examples/browser_tasks/http_client.py cancel task_demo
python3 examples/browser_tasks/http_client.py poll task_demo
python3 examples/browser_tasks/http_client.py profile-delete profile_demo
python3 examples/browser_tasks/http_client.py revoke key_demo
```

Deletion removes profile archive, script memory, history and session references; a leased/busy profile returns conflict. Do not retry by deleting another profile. `recovery_required` means saved state needs operator recovery, not that a new profile silently substitutes for it.

## Lost responses, restarts and error handling

- Admission is 202 only after durable admission. If the connection times out, resend the **identical payload and idempotency key**. A changed payload under the same key conflicts. Do not automatically create a new key or task to “retry” an uncertain website effect.
- Once a task ID is known, reattach/poll it using the currently authorized credential. A client timeout/disconnect does not cancel it. The example poll budget is 20 iterations, stopping at a terminal state or pending interaction; HTTP attempts time out after 35 seconds. Polling budget exhaustion is not a failed/cancelled task.
- Long-poll `GET T/{id}?after_revision=3&wait_seconds=25` is available to custom clients. Waiting with no revision or a future revision is invalid.
- A restart interrupts active execution, revokes pauses/viewers and fences old owners. Restore durable profile/context only through a new explicit follow-up. A suspended Python stack is not resumable. **Interrupted/unknown effects are not automatically replayed**; inspect website state before authorizing more actions.
- Task errors use `{error:{code,message,retryable,retry_after_seconds?}}`. Authentication failure is 401; foreign/not-found 404; invalid input 422; conflict/stale generation/profile recovery/interruption 409; capacity 429; unavailable/shutdown 503; provider failure 502; expired assistance 410. Retryable HTTP failures still mean retry the same operation/key, never blindly redo website work. Observe `Retry-After` (bounded 1–300 seconds). The examples do not auto-retry.
- Existing account endpoints retain their legacy `{detail:...}` error convention; do not assume they use task envelopes. Frontend task errors show sanitized codes, not raw provider messages/request bodies. Account requests and key responses use no-store and are fenced when the account token changes.

## Offline verification

```sh
npm --prefix frontend run build
node --test frontend/tests/*.test.mjs
node --test examples/browser_tasks/test_http.mjs
```

Frontend HTTP tests use frozen fixtures. The real-DOM test launches local headless Chrome with an isolated temporary profile and serves only built UI plus a synthetic HTTP mock; set `CHROME_BIN` if Chrome is not at the macOS default path. No dependency/lockfile edits are needed. The client checks start a loopback mock and exercise JS, Python and curl subprocesses (`PYTHON` can point to the repository venv). Mocks prove request shapes/client behavior, not server ownership enforcement, provider execution, persistence, caching headers, remote viewer Origin security, or actual website effects. Those remain assembled-service acceptance checks for lane006.
