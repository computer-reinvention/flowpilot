"""Reusable helper for capturing browser auth state into the session DB."""

import json
import logging
import os
import tempfile
import uuid
from typing import Protocol

from .models import Session
from . import db as _db

logger = logging.getLogger(__name__)


class BrowserLike(Protocol):
    async def export_storage_state(self) -> dict: ...


def storage_state_to_file(storage_state: dict) -> str:
    """Write a storage_state dict to a temp file and return the path.

    browser-use's StorageStateWatchdog only loads from file paths, not dicts.
    Use this before passing storage_state to Browser() so cookies are injected.
    Caller is responsible for deleting the file when done.
    """
    fd, path = tempfile.mkstemp(suffix=".json")
    os.write(fd, json.dumps(storage_state).encode())
    os.close(fd)
    return path


async def capture_session(
    browser: BrowserLike,
    user_id: str,
    domain: str,
    profile_name: str = "",
    context: str = "unknown",
) -> int:
    """Export cookies + localStorage from browser and save into the session DB.

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

        # Try to capture localStorage from the current page.
        # export_storage_state() always returns origins=[], so we extract it manually.
        local_storage: dict = {}
        try:
            page = await browser.must_get_current_page()  # type: ignore[attr-defined]
            local_storage = await page.evaluate(
                "() => Object.fromEntries(Object.entries(localStorage))"
            )
            if not isinstance(local_storage, dict):
                local_storage = {}
        except Exception:
            pass

        session = Session(
            id=str(uuid.uuid4())[:12],
            user_id=user_id,
            domain=domain,
            cookies=json.dumps(cookies),
            local_storage=json.dumps(local_storage),
            profile_name=profile_name,
        )
        await _db.create_session(session)
        logger.info(
            "Captured %d cookies, %d localStorage keys [%s] for domain=%s profile=%s",
            len(cookies), len(local_storage), context, domain, profile_name,
        )
        return len(cookies)
    except Exception as e:
        logger.debug("Could not capture session [%s]: %s", context, e)
        return 0
