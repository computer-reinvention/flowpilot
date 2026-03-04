# Webhooks

FlowPilot fires webhook events for run lifecycle changes. Webhooks enable external systems to react to automation events — for example, sending a Slack notification when a run needs human intervention.

## Events

| Event | Fired When |
|-------|-----------|
| `run.started` | A run begins execution |
| `run.paused` | Agent requests human help (HITL) |
| `run.completed` | Run finishes successfully |
| `run.failed` | Run encounters an error |
| `run.cancelled` | Run is cancelled by the user |

## Registration

```
POST /api/v1/webhooks
Authorization: Bearer {token}

{
  "url": "https://example.com/webhook",
  "events": ["run.paused", "run.completed"]
}
```

**Response** includes a `secret` field (shown once). Use this to verify HMAC signatures.

An empty `events` array subscribes to all events.

## Payload Format

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
  "timestamp": "2026-03-04T10:20:30Z"
}
```

The `category` field is extracted from the message prefix `[category]` and is one of: `auth`, `captcha`, `consent`, `verification`, `form_fill`, `confirmation`, `other`.

The `browser_cdp_url` and `resume_url` fields are included in `run.paused` events, enabling external systems to view the browser and resume the run programmatically.

## HMAC Verification

Every delivery is signed with HMAC-SHA256 using the webhook secret.

**Headers sent with each delivery**:
- `X-FlowPilot-Signature: sha256={hmac-hex}` — HMAC-SHA256 of the request body
- `X-FlowPilot-Event: {event}` — Event name
- `X-FlowPilot-Delivery: {delivery_id}` — Unique delivery ID

**Verification example** (Python):
```python
import hmac
import hashlib

def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

## Retry Strategy

Failed deliveries are retried with exponential backoff:

| Attempt | Delay |
|---------|-------|
| 1 | Immediate |
| 2 | 2 seconds |
| 3 | 10 seconds |
| 4 | 60 seconds |

After 4 failed attempts, the delivery is marked as permanently failed.

A delivery is considered successful if the server responds with a 2xx status code.

## Delivery Records

All deliveries are logged in the `webhook_deliveries` table with:
- Attempt count
- Status (`pending`, `delivered`, `failed`)
- Response status code and body
- Timestamp

## Testing

Send a test delivery to verify your endpoint:

```
POST /api/v1/webhooks/{id}/test
Authorization: Bearer {token}
```

This sends a test payload to the webhook URL and returns the delivery result.

## Implementation Details

- `fire_event()` is non-blocking — it spawns async delivery tasks and returns immediately
- Deliveries run in background asyncio tasks
- Each webhook gets its own retry chain
- The webhook engine is in `backend/webhooks.py`
