"""Generic flow runner using browser-use — replaces the hardcoded Uber booking agent."""

import asyncio
import json
import os
import logging
from enum import Enum
from typing import Callable, Awaitable
from datetime import datetime, timezone

from browser_use import Agent, Browser, ActionResult

from .models import Flow, Run

logger = logging.getLogger(__name__)


class _LLMWithProvider:
    """Proxy that adds .provider/.model_name attrs browser-use requires.

    browser-use accesses llm.provider and llm.model_name, and its token tracker
    monkey-patches llm.ainvoke. LangChain LLMs are Pydantic models with
    extra="ignore", so we can't set arbitrary attrs on them. This proxy stores
    overrides locally and delegates everything else to the real LLM.
    """

    def __init__(self, llm: object, provider: str):
        object.__setattr__(self, "_llm", llm)
        model = getattr(llm, "model", None) or getattr(llm, "model_name", "unknown")
        object.__setattr__(self, "_overrides", {
            "provider": provider,
            "model_name": model,
        })

    def __getattr__(self, name: str):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_llm"), name)

    def __setattr__(self, name: str, value: object):
        if name in ("_llm", "_overrides"):
            object.__setattr__(self, name, value)
        else:
            object.__getattribute__(self, "_overrides")[name] = value


class RunStatus(str, Enum):
    PENDING = "pending"
    CONNECTING = "connecting"
    RUNNING = "running"
    PAUSED_FOR_AUTH = "paused_for_auth"
    COMPLETED = "completed"
    FAILED = "failed"


def _get_llm():
    """Create the LLM instance based on LLM_PROVIDER env var."""
    provider = os.environ.get("LLM_PROVIDER", "gemini").lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GOOGLE_API_KEY environment variable is required when LLM_PROVIDER=gemini")
        llm = ChatGoogleGenerativeAI(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            google_api_key=api_key,
        )
        return _LLMWithProvider(llm, "google")
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


def _build_agent_prompt(flow: Flow, inputs: dict) -> str:
    """Build the agent task prompt from flow description + user inputs."""
    parts = []

    if flow.entry_url:
        parts.append(f"Go to {flow.entry_url}.")

    parts.append(flow.description)

    if inputs:
        parts.append("\nUser-provided inputs:")
        for key, value in inputs.items():
            parts.append(f"- {key}: {value}")

    return " ".join(parts)


class FlowRunner:
    """Runs any flow definition through browser-use inside a sandbox."""

    async def run_flow(
        self,
        flow: Flow,
        inputs: dict,
        user_id: str,
        sandbox_id: str,
        cdp_url: str,
        run_id: str,
        progress_callback: Callable[[RunStatus, str], Awaitable[None]] | None = None,
        storage_state: dict | None = None,
        agent_ready_callback: Callable | None = None,
    ) -> dict:
        """Execute a flow and return the result."""

        async def _update(status: RunStatus, message: str):
            if progress_callback:
                await progress_callback(status, message)
            logger.info("Run %s: [%s] %s", run_id, status.value, message)

        try:
            await _update(RunStatus.CONNECTING, "Waiting for sandbox browser...")

            # Wait for CDP to be reachable (sandbox needs time to start Chromium + socat)
            import socket as _socket
            _cdp_hp = cdp_url.split("//")[1].split("/")[0]
            _cdp_h, _cdp_p = _cdp_hp.rsplit(":", 1)
            for _attempt in range(20):
                try:
                    _s = _socket.create_connection((_cdp_h, int(_cdp_p)), timeout=2)
                    _s.close()
                    break
                except (OSError, ConnectionRefusedError):
                    pass
                if _attempt < 19:
                    await asyncio.sleep(2)
                    await _update(RunStatus.CONNECTING, f"Waiting for sandbox browser (attempt {_attempt + 2}/20)...")
            else:
                raise RuntimeError(f"CDP not reachable at {cdp_url} after 40s")

            # Resolve the full CDP WebSocket URL from /json/version
            # Chromium returns ws://localhost/devtools/browser/<GUID> — we need to
            # rewrite the host:port to match the socat-forwarded address.
            import http.client as _http
            _cdp_conn = _http.HTTPConnection(_cdp_h, int(_cdp_p), timeout=5)
            _cdp_conn.request("GET", "/json/version", headers={"Host": "localhost"})
            _cdp_resp = _cdp_conn.getresponse()
            _cdp_info = json.loads(_cdp_resp.read())
            _ws_debugger_url = _cdp_info["webSocketDebuggerUrl"]
            # Extract path (/devtools/browser/GUID) and rebuild with correct host:port
            _ws_path = "/" + "/".join(_ws_debugger_url.split("/")[3:])
            cdp_url = f"ws://{_cdp_h}:{_cdp_p}{_ws_path}"

            await _update(RunStatus.CONNECTING, "Connecting to sandbox browser via CDP")

            # Chromium rejects CDP WebSocket upgrades with non-localhost Host headers.
            # Pass Host: localhost so the connection succeeds across containers.
            browser = Browser(cdp_url=cdp_url, storage_state=storage_state, headers={"Host": "localhost"})

            if storage_state:
                cookie_count = len(storage_state.get("cookies", []))
                await _update(RunStatus.CONNECTING, f"Injected {cookie_count} cookies from saved session")

            await _update(RunStatus.RUNNING, "Connected. Launching flow agent...")

            llm = _get_llm()
            task = _build_agent_prompt(flow, inputs)

            step_count = 0

            def _on_step(state_summary, agent_output, step_num):
                nonlocal step_count
                step_count += 1
                try:
                    state = getattr(agent_output, "current_state", None)
                    goal = getattr(state, "next_goal", None) or getattr(state, "next_step", None) or "processing"
                    msg = f"Step {step_num}: {goal}"
                except Exception:
                    msg = f"Step {step_num}: processing"
                asyncio.create_task(_update(RunStatus.RUNNING, msg))

            # Build HITL tool: request_human_login
            from browser_use import Controller
            controller = Controller()

            @controller.action(
                description=(
                    "Request human help to log in. Call this when you encounter a login page, "
                    "auth wall, or captcha that you cannot bypass. The human will log in via "
                    "the browser and then resume you."
                ),
            )
            async def request_human_login(reason: str) -> ActionResult:
                await _update(RunStatus.PAUSED_FOR_AUTH, f"Login needed: {reason}")
                agent.pause()
                return ActionResult(
                    extracted_content="Human has logged in. You can now continue with the authenticated session.",
                )

            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                register_new_step_callback=_on_step,
                controller=controller,
            )

            if agent_ready_callback:
                await agent_ready_callback(agent, browser, flow)

            await _update(RunStatus.RUNNING, f"Agent running flow '{flow.name}'")

            result = await asyncio.wait_for(agent.run(), timeout=300)

            await _update(RunStatus.COMPLETED, "Flow completed successfully")

            return {
                "raw_result": str(result),
                "flow_name": flow.name,
                "inputs": inputs,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }

        except Exception as e:
            error_msg = f"Flow failed: {str(e)}"
            logger.error("Run %s failed: %s", run_id, e, exc_info=True)
            await _update(RunStatus.FAILED, error_msg)
            raise

        finally:
            try:
                if "browser" in locals():
                    await browser.close()
            except Exception:
                pass
