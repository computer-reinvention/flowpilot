"""Uber booking automation using browser-use."""

import asyncio
import json
import os
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Awaitable
from datetime import datetime, timezone

from browser_use import Agent, Browser

from .config import PROFILES_DIR

logger = logging.getLogger(__name__)


class BookingStatus(str, Enum):
    STARTING = "starting"
    CONNECTING = "connecting"
    NAVIGATING = "navigating"
    ENTERING_PICKUP = "entering_pickup"
    ENTERING_DROPOFF = "entering_dropoff"
    SELECTING_RIDE = "selecting_ride"
    CONFIRMING = "confirming"
    COMPLETED = "completed"
    FAILED = "failed"


BOOKING_STEPS = [
    BookingStatus.STARTING,
    BookingStatus.CONNECTING,
    BookingStatus.NAVIGATING,
    BookingStatus.ENTERING_PICKUP,
    BookingStatus.ENTERING_DROPOFF,
    BookingStatus.SELECTING_RIDE,
    BookingStatus.CONFIRMING,
    BookingStatus.COMPLETED,
]


@dataclass
class BookingState:
    booking_id: str
    status: BookingStatus = BookingStatus.STARTING
    steps_completed: list[str] = field(default_factory=list)
    current_step: str = "starting"
    error: str | None = None
    result: dict | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# In-memory booking state store
_bookings: dict[str, BookingState] = {}


def get_booking(booking_id: str) -> BookingState | None:
    """Retrieve a booking by ID."""
    return _bookings.get(booking_id)


async def _cleanup_booking(booking_id: str, delay: int = 3600):
    """Remove a completed/failed booking from memory after a delay."""
    await asyncio.sleep(delay)
    _bookings.pop(booking_id, None)


def _get_llm():
    """Create the LLM instance based on LLM_PROVIDER env var."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is required when LLM_PROVIDER=gemini")
        return ChatGoogleGenerativeAI(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            google_api_key=api_key,
        )
    elif provider in ("claude", "anthropic"):
        from langchain_anthropic import ChatAnthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required when LLM_PROVIDER=claude")
        return ChatAnthropic(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            anthropic_api_key=api_key,
        )
    elif provider == "openai":
        from langchain_openai import ChatOpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required when LLM_PROVIDER=openai")
        return ChatOpenAI(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_api_key=api_key,
        )
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use gemini, claude, or openai.")


class UberBookingAgent:
    """Automates Uber ride booking via browser-use inside a sandbox."""

    async def book_ride(
        self,
        pickup: str,
        dropoff: str,
        ride_type: str,
        sandbox_id: str,
        cdp_url: str,
        booking_id: str,
        progress_callback: Callable[[BookingStatus, str], Awaitable[None]] | None = None,
        user_id: str = "",
        profile_name: str = "default",
    ) -> dict:
        """Run the full Uber booking flow."""
        state = BookingState(booking_id=booking_id)
        _bookings[booking_id] = state

        async def _update(status: BookingStatus, message: str):
            state.status = status
            state.current_step = message
            if status != BookingStatus.FAILED:
                state.steps_completed.append(message)
            if progress_callback:
                await progress_callback(status, message)
            logger.info("Booking %s: [%s] %s", booking_id, status.value, message)

        try:
            await _update(BookingStatus.CONNECTING, "Connecting to sandbox browser via CDP")

            # Build storage_state from saved session — paths include user_id
            storage_state = None
            profile_base = PROFILES_DIR / user_id / profile_name if user_id else PROFILES_DIR / profile_name
            cookies_path = profile_base / "cookies.json"
            session_path = profile_base / "session.json"

            if cookies_path.exists():
                cookies = json.loads(cookies_path.read_text())
                storage_state = {"cookies": cookies, "origins": []}
                if session_path.exists():
                    session_data = json.loads(session_path.read_text())
                    local_storage = session_data.get("localStorage", {})
                    if local_storage:
                        storage_state["origins"].append({
                            "origin": "https://m.uber.com",
                            "localStorage": [{"name": k, "value": v} for k, v in local_storage.items()],
                        })

            browser = Browser(cdp_url=cdp_url, storage_state=storage_state)

            if storage_state:
                await _update(BookingStatus.NAVIGATING, f'Injected {len(storage_state.get("cookies", []))} cookies from saved session')

            await _update(BookingStatus.NAVIGATING, "Connected. Launching booking agent...")

            llm = _get_llm()

            task = (
                f"Go to https://m.uber.com. "
                f"Enter pickup location: {pickup}. "
                f"Enter dropoff location: {dropoff}. "
                f"Select the ride type: {ride_type}. "
                f"Click the confirm/request button to book the ride. "
                f"Wait for the booking confirmation screen. "
                f"Return the ride details including driver name, car details, ETA, and fare if visible."
            )

            # Step callback for intermediate progress statuses
            step_count = 0

            def _on_step(state_summary, agent_output, step_num):
                nonlocal step_count
                step_count += 1
                msg = f'Step {step_num}: {agent_output.current_state.next_step if hasattr(agent_output, "current_state") else "processing"}'
                if step_count <= 2:
                    asyncio.create_task(_update(BookingStatus.NAVIGATING, msg))
                elif step_count <= 4:
                    asyncio.create_task(_update(BookingStatus.ENTERING_PICKUP, msg))
                elif step_count <= 6:
                    asyncio.create_task(_update(BookingStatus.ENTERING_DROPOFF, msg))
                elif step_count <= 8:
                    asyncio.create_task(_update(BookingStatus.SELECTING_RIDE, msg))
                else:
                    asyncio.create_task(_update(BookingStatus.CONFIRMING, msg))

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                register_new_step_callback=_on_step,
            )

            await _update(BookingStatus.ENTERING_PICKUP, f"Agent running: entering pickup '{pickup}'")

            result = await asyncio.wait_for(agent.run(), timeout=300)

            await _update(BookingStatus.COMPLETED, "Booking completed successfully")

            state.result = {
                "raw_result": str(result),
                "pickup": pickup,
                "dropoff": dropoff,
                "ride_type": ride_type,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

            asyncio.create_task(_cleanup_booking(booking_id))
            return state.result

        except Exception as e:
            error_msg = f"Booking failed: {str(e)}"
            logger.error("Booking %s failed: %s", booking_id, e, exc_info=True)
            await _update(BookingStatus.FAILED, error_msg)
            state.error = error_msg
            asyncio.create_task(_cleanup_booking(booking_id))
            raise

        finally:
            try:
                if "browser" in locals():
                    await browser.close()
            except Exception:
                pass
