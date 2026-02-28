"""Uber booking automation using browser-use."""

import json
import os
import logging
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Awaitable
from datetime import datetime, timezone

from browser_use import Agent, Browser, BrowserConfig

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))


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


def _get_llm():
    """Create the LLM instance based on LLM_PROVIDER env var.

    Supported providers:
      - gemini (default): uses GOOGLE_API_KEY
      - claude / anthropic: uses ANTHROPIC_API_KEY
      - openai: uses OPENAI_API_KEY
    """
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            google_api_key=os.environ.get("GOOGLE_API_KEY"),
        )
    elif provider in ("claude", "anthropic"):
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
    elif provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            openai_api_key=os.environ.get("OPENAI_API_KEY"),
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
        profile_name: str = "default",
    ) -> dict:
        """Run the full Uber booking flow.

        Args:
            pickup: Pickup address or location name.
            dropoff: Dropoff address or location name.
            ride_type: Uber ride type (e.g. UberGo, Premier, Auto).
            sandbox_id: The sandbox container ID for context.
            cdp_url: Chrome DevTools Protocol WebSocket URL to connect to.
            booking_id: Unique booking ID for tracking state.
            progress_callback: Async callback(status, message) for streaming progress.
            profile_name: Profile to load cookies/storage from.

        Returns:
            Dict with booking result details.
        """
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

            # Connect to the sandbox browser via Chrome DevTools Protocol
            browser = Browser(
                config=BrowserConfig(
                    cdp_url=cdp_url,
                )
            )

            # Inject cookies from imported session if available
            cookies_path = PROFILES_DIR / profile_name / "cookies.json"
            session_path = PROFILES_DIR / profile_name / "session.json"

            if cookies_path.exists():
                try:
                    cookies = json.loads(cookies_path.read_text())
                    if cookies:
                        context = await browser.browser.contexts[0] if hasattr(browser, 'browser') else None
                        # browser-use exposes the playwright browser; inject cookies via context
                        # We need to get a context and page after browser init
                        pw_browser = await browser.get_playwright_browser()
                        if pw_browser and pw_browser.contexts:
                            ctx = pw_browser.contexts[0]
                            await ctx.add_cookies(cookies)
                            await _update(BookingStatus.NAVIGATING, f"Injected {len(cookies)} cookies from saved session")

                            # Inject localStorage and sessionStorage if available
                            if session_path.exists():
                                session_data = json.loads(session_path.read_text())
                                local_storage = session_data.get("localStorage", {})
                                session_storage = session_data.get("sessionStorage", {})

                                if local_storage or session_storage:
                                    # Navigate to uber.com first so we can set storage on the right origin
                                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                                    await page.goto("https://m.uber.com", wait_until="domcontentloaded", timeout=30000)

                                    if local_storage:
                                        await page.evaluate("""(items) => {
                                            for (const [key, value] of Object.entries(items)) {
                                                try { localStorage.setItem(key, value); } catch(e) {}
                                            }
                                        }""", local_storage)

                                    if session_storage:
                                        await page.evaluate("""(items) => {
                                            for (const [key, value] of Object.entries(items)) {
                                                try { sessionStorage.setItem(key, value); } catch(e) {}
                                            }
                                        }""", session_storage)

                                    await _update(BookingStatus.NAVIGATING, "Injected localStorage/sessionStorage")
                        else:
                            logger.warning("Could not access browser contexts for cookie injection")
                except Exception as e:
                    logger.warning("Failed to inject session data: %s", e)
                    await _update(BookingStatus.NAVIGATING, "Cookie injection failed, continuing without session")

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

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
            )

            await _update(BookingStatus.ENTERING_PICKUP, f"Agent running: entering pickup '{pickup}'")

            # Run the browser-use agent — it handles all the intermediate steps
            result = await agent.run()

            await _update(BookingStatus.COMPLETED, "Booking completed successfully")

            state.result = {
                "raw_result": str(result),
                "pickup": pickup,
                "dropoff": dropoff,
                "ride_type": ride_type,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

            return state.result

        except Exception as e:
            error_msg = f"Booking failed: {str(e)}"
            logger.error("Booking %s failed: %s", booking_id, e, exc_info=True)
            await _update(BookingStatus.FAILED, error_msg)
            state.error = error_msg
            raise

        finally:
            # Try to close the browser connection gracefully
            try:
                if "browser" in locals():
                    await browser.close()
            except Exception:
                pass
