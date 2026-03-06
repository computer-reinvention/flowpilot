# API Reference

Base URL: `http://localhost:8080` (configurable via `FLOWPILOT_BASE_URL`)

Authentication: `Authorization: Bearer {jwt_or_api_key}` header or `token` cookie.

API keys use the `fp_live_` prefix and work on all `/api/v1/` endpoints.

## Auth

### POST /api/auth/signup

Create a new user account.

**Body**: `{ "email": "...", "password": "...", "name": "..." }`
**Response**: `{ "token": "jwt...", "user": { "id", "email", "name" } }`

### POST /api/auth/login

Authenticate and receive a JWT.

**Body**: `{ "email": "...", "password": "..." }`
**Response**: `{ "token": "jwt...", "user": { "id", "email", "name" } }`

### GET /api/auth/me

Get the current authenticated user.

**Auth**: Required
**Response**: `{ "id", "email", "name" }`

## Flows

### POST /api/flows

Create a new flow.

**Auth**: Required
**Body**:
```json
{
  "name": "Book Uber",
  "domain": "uber.com",
  "description": "Book an Uber ride",
  "entry_url": "https://m.uber.com",
  "inputs": [
    { "name": "pickup", "type": "text", "label": "Pickup Location" },
    { "name": "dropoff", "type": "text", "label": "Drop-off Location" }
  ]
}
```

### GET /api/flows

List all flows. No auth required.

### GET /api/flows/{id}

Get a single flow. No auth required.

### PUT /api/flows/{id}

Update a flow. Auth required.

### DELETE /api/flows/{id}

Delete a flow. Auth required.

## Exploration

### POST /api/flows/{flow_id}/explore

Start or restart exploration for a flow. Spins up a sandbox, runs the explorer agent, discovers stages, and generates JS reflexes.

**Auth**: Required
**Body** (optional): `{ "profile_name": "..." }`
**Response**: `{ "status": "exploring" }`

If exploration is already complete, re-exploring overwrites existing stages and reflexes.

### POST /api/flows/{flow_id}/explore/cancel

Cancel an in-progress exploration. Saves the browser profile before teardown.

**Auth**: Required

### POST /api/flows/{flow_id}/explore/resume

Resume exploration after a HITL pause (e.g., after completing manual auth).

**Auth**: Required

### POST /api/flows/{flow_id}/auth/done

Signal that manual auth is complete. Captures cookies from the auth sandbox, tears it down, and starts exploration.

**Auth**: Required

### WS /ws/flows/{flow_id}

Real-time exploration progress stream. Receives status updates, stage discoveries, HITL requests.

## Runs

### POST /api/runs

Start a new run.

**Auth**: Required
**Body**:
```json
{
  "flow_id": "...",
  "inputs": { "pickup": "Home", "dropoff": "Office" },
  "profile_id": "optional-profile-id"
}
```
**Response**: `{ "run_id": "...", "sandbox_id": "..." }`

### GET /api/runs

List the current user's runs.

**Auth**: Required

### GET /api/runs/{id}

Get a single run with full details.

**Auth**: Required

### POST /api/runs/{id}/cancel

Cancel a running or pending run.

**Auth**: Required

### POST /api/runs/{id}/resume

Resume a run paused for HITL. Captures current browser cookies before resuming the agent.

**Auth**: Required

### POST /api/runs/{id}/pause

Manually pause a run (fallback mechanism).

**Auth**: Required

## Sandbox

### POST /api/sandbox/start

Start a sandbox container (standalone, not tied to a run).

**Auth**: Required
**Body**: `{ "profile_name": "optional" }`
**Response**: `{ "sandbox_id": "...", "cdp_url": "..." }`

### POST /api/sandbox/stop

Stop a sandbox container.

**Auth**: Required
**Body**: `{ "sandbox_id": "..." }`

## Sessions & Profiles

### POST /api/sessions/capture

Capture a browser session from the Chrome extension.

**Auth**: Required
**Body**:
```json
{
  "domain": "example.com",
  "cookies": [...],
  "local_storage": {...},
  "session_storage": {...},
  "user_agent": "...",
  "profile_name": "default"
}
```

### GET /api/sessions

List the current user's captured sessions.

**Auth**: Required

### DELETE /api/sessions/{id}

Delete a captured session.

**Auth**: Required

### GET /api/profiles

List saved Chrome profiles for the current user.

**Auth**: Required

### DELETE /api/profiles/{profile_name}

Delete a saved Chrome profile.

**Auth**: Required

### POST /api/profile/save

Save a Chrome profile from a running sandbox.

**Auth**: Required
**Body**: `{ "sandbox_id": "...", "profile_name": "..." }`

### GET /api/profile/status

Get profile metadata.

**Auth**: Required
**Query**: `?profile_name=...`

## API Keys

### POST /api/v1/api-keys

Create a new API key. The plaintext key is returned only in this response.

**Auth**: Required
**Body**: `{ "name": "My Key" }`
**Response**: `{ "id", "name", "key_prefix", "key": "fp_live_...", "created_at" }`

### GET /api/v1/api-keys

List the current user's API keys (without plaintext values).

**Auth**: Required

### DELETE /api/v1/api-keys/{id}

Revoke an API key.

**Auth**: Required

## Webhooks

### POST /api/v1/webhooks

Register a webhook endpoint.

**Auth**: Required
**Body**:
```json
{
  "url": "https://example.com/webhook",
  "events": ["run.paused", "run.completed"]
}
```
**Response**: Includes `secret` (shown once, used for HMAC verification).

### GET /api/v1/webhooks

List the current user's webhooks.

**Auth**: Required

### DELETE /api/v1/webhooks/{id}

Delete a webhook.

**Auth**: Required

### POST /api/v1/webhooks/{id}/test

Send a test webhook delivery.

**Auth**: Required

## Trigger Hooks

URL-based triggers that start a run without authentication. Useful for integrations (Slack slash commands, Zapier, cron jobs).

### POST /api/v1/flows/{flow_id}/hooks

Create a trigger hook for a flow. Returns a unique token URL.

**Auth**: Required
**Body**: `{ "name": "Slack trigger" }`
**Response**: `{ "id", "name", "token", "url": "/api/v1/hooks/{token}", ... }`

### GET /api/v1/flows/{flow_id}/hooks

List trigger hooks for a flow.

**Auth**: Required

### DELETE /api/v1/flows/{flow_id}/hooks/{hook_id}

Delete a trigger hook.

**Auth**: Required

### POST /api/v1/hooks/{token}

Execute a trigger hook (starts a run). **No auth required** — the token is the credential.

**Body**: `{ "inputs": { "key": "value" }, "profile_id": "optional" }`
**Response**: `{ "run_id": "...", "sandbox_id": "..." }`

## Public API (v1)

These endpoints accept API key authentication (`fp_live_` prefix).

### POST /api/v1/runs

Same as `POST /api/runs`. Supports API key auth.

### GET /api/v1/runs/{id}

Same as `GET /api/runs/{id}`. Supports API key auth.

### POST /api/v1/runs/{id}/resume

Same as `POST /api/runs/{id}/resume`. Supports API key auth.

## WebSockets

### WS /ws/runs/{run_id}

Real-time run progress stream.

**Messages** (server → client):
```json
{ "status": "running", "message": "Navigating to page", "timestamp": "..." }
{ "status": "paused_for_hitl", "message": "[auth] Login required" }
{ "status": "completed", "result": {...} }
```

### WS /ws/browser/{sandbox_id}?token={jwt}

CDP proxy for live browser streaming and input relay.

**Auth**: JWT in query parameter

**Server → Client**:
```json
{ "type": "frame", "data": "base64-jpeg" }
{ "type": "nav", "url": "https://..." }
{ "type": "loading", "loading": true }
```

**Client → Server**:
```json
{ "type": "mouse", "eventType": "mousePressed", "x": 100, "y": 200, "button": "left", "clickCount": 1, "modifiers": 0 }
{ "type": "key", "eventType": "keyDown", "key": "a", "code": "KeyA", "text": "a", "modifiers": 0, "keyCode": 65 }
{ "type": "navigate", "action": "back" }
{ "type": "navigate", "action": "goto", "url": "https://example.com" }
```

### WS /ws/flows/{flow_id}

Real-time exploration and flow status stream.

**Messages** (server → client):
```json
{ "type": "exploration_status", "status": "exploring", "stages": [...] }
{ "type": "hitl_request", "reason": "Login required", "category": "auth" }
{ "type": "exploration_complete", "stages": [...], "reflexes": [...] }
```

## Webhook Payloads

### run.paused

```json
{
  "event": "run.paused",
  "run_id": "abc123",
  "status": "paused_for_hitl",
  "message": "[auth] Login required",
  "profile_id": "user123",
  "browser_cdp_url": "ws://localhost:8080/ws/browser/sandbox123",
  "resume_url": "/api/v1/runs/abc123/resume",
  "category": "auth",
  "timestamp": "2026-03-06T10:20:30Z"
}
```

HITL categories: `auth` · `captcha` · `consent` · `verification` · `form_fill` · `confirmation` · `other`

### Other events

`run.started`, `run.completed`, `run.failed`, `run.cancelled` — same structure minus `browser_cdp_url`, `resume_url`, `category`.

Webhook requests are signed with `X-FlowPilot-Signature: sha256=<hmac>`. Retried at 2s, 10s, 60s on failure. See [webhooks.md](webhooks.md) for details.
