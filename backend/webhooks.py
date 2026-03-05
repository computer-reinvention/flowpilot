"""Webhook delivery engine — fire-and-forget with retries."""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone

import httpx

from . import db as _db

logger = logging.getLogger(__name__)

# Store references to delivery tasks so the GC doesn't kill them mid-flight.
_pending_tasks: set[asyncio.Task] = set()

VALID_EVENTS = frozenset({
    "run.started",
    "run.paused",       # HITL — includes category + browser_cdp_url
    "run.completed",
    "run.failed",
    "run.cancelled",
})

HITL_CATEGORIES = frozenset({
    "auth", "captcha", "consent", "verification", "form_fill", "confirmation", "other"
})

RETRY_DELAYS = [2, 10, 60]  # seconds between attempts


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


def sign_payload(secret: str, body: bytes) -> str:
    """Return 'sha256=<hmac-sha256-hex>' signature."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def fire_event(user_id: str, event: str, run_id: str | None, payload: dict) -> None:
    """Dispatch an event to all matching webhooks for a user (non-blocking)."""
    if event not in VALID_EVENTS:
        logger.warning("Unknown webhook event '%s' — skipping", event)
        return

    webhooks = await _db.get_webhooks_for_event(user_id, event)
    if not webhooks:
        return

    payload_str = json.dumps(payload, separators=(",", ":"))

    for wh in webhooks:
        delivery_id = await _db.create_webhook_delivery(
            webhook_id=wh["id"],
            run_id=run_id,
            event=event,
            payload=payload_str,
        )
        task = asyncio.create_task(
            _deliver_with_retries(
                delivery_id=delivery_id,
                webhook_id=wh["id"],
                url=wh["url"],
                secret=wh["secret"],
                event=event,
                payload_str=payload_str,
            )
        )
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)


async def _deliver_with_retries(
    delivery_id: str,
    webhook_id: str,
    url: str,
    secret: str,
    event: str,
    payload_str: str,
) -> None:
    body = payload_str.encode()
    signature = sign_payload(secret, body)

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-FlowPilot-Signature": signature,
                        "X-FlowPilot-Event": event,
                        "X-FlowPilot-Delivery": delivery_id,
                    },
                )
            success = 200 <= resp.status_code < 300
            await _db.update_webhook_delivery(delivery_id, {
                "attempt": attempt + 1,
                "status": "delivered" if success else "failed",
                "response_status": resp.status_code,
                "response_body": resp.text[:500],
                "delivered_at": datetime.now(timezone.utc).isoformat() if success else None,
            })
            if success:
                logger.info("Webhook %s delivery %s succeeded (attempt %d)", webhook_id, delivery_id, attempt + 1)
                return
            logger.warning(
                "Webhook %s delivery %s got %d (attempt %d)", webhook_id, delivery_id, resp.status_code, attempt + 1
            )
        except Exception as exc:
            await _db.update_webhook_delivery(delivery_id, {
                "attempt": attempt + 1,
                "status": "failed",
                "response_body": str(exc)[:500],
            })
            logger.warning("Webhook %s delivery %s error: %s (attempt %d)", webhook_id, delivery_id, exc, attempt + 1)

        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])

    logger.error("Webhook %s delivery %s permanently failed after %d attempts", webhook_id, delivery_id, len(RETRY_DELAYS) + 1)
