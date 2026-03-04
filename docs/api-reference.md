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

## Public API (v1)

These endpoints accept API key authentication (`fp_live_` prefix).

### POST /api/v1/runs

Same as `POST /api/runs`. Supports API key auth.

### GET /api/v1/runs/{id}

Same as `GET /api/runs/{id}`. Supports API key auth.

### POST /api/v1/runs/{id}/resume

Same as `POST /api/runs/{id}/resume`. Supports API key auth.

## Sandbox

### POST /api/sandbox/start

Start a sandbox container.

**Auth**: Required
**Body**: `{ "profile_name": "optional" }`
**Response**: `{ "sandbox_id": "...", "cdp_url": "..." }`

### POST /api/sandbox/stop

Stop a sandbox container.

**Auth**: Required
**Body**: `{ "sandbox_id": "..." }`

## Sessions

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

## Profiles

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
