"""Reusable helper for capturing browser auth state into the session DB."""

import json
import logging
import uuid
from typing import Protocol

from .models import Session
from . import db as _db

logger = logging.getLogger(__name__)


class BrowserLike(Protocol):
    async def export_storage_state(self) -> dict: ...


async def capture_session(
    browser: BrowserLike,
    user_id: str,
    domain: str,
    profile_name: str = "",
    context: str = "unknown",
) -> int:
    """Export cookies from browser and upsert into the session DB.

    Args:
        browser: Any browser-use Browser/BrowserSession instance.
        user_id: The user who owns this session.
        domain: The domain to scope the session to.
        profile_name: Optional profile scope (defaults to "").
        context: Log-friendly label for where this was called from.

    Returns:
        Number of cookies captured (0 if none or on error).
    """
    try:
        storage_state = await browser.export_storage_state()
        cookies = storage_state.get("cookies", [])
        if not cookies:
            return 0
        session = Session(
            id=str(uuid.uuid4())[:12],
            user_id=user_id,
            domain=domain,
            cookies=json.dumps(cookies),
            profile_name=profile_name,
        )
        await _db.create_session(session)
        logger.info(
            "Captured %d cookies [%s] for domain=%s profile=%s",
            len(cookies), context, domain, profile_name,
        )
        return len(cookies)
    except Exception as e:
        logger.debug("Could not capture cookies [%s]: %s", context, e)
        return 0
