"""Smoke test: instantiate the LLM and run a trivial browser-use agent (no sandbox needed)."""

import asyncio
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Ensure the project root is on the path so we can import backend.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


def test_get_llm():
    """_get_llm() should return an object with a .provider property."""
    from backend.runner import _get_llm
    llm = _get_llm()
    assert hasattr(llm, "provider"), f"{type(llm).__name__} has no 'provider' attribute"
    logger.info("LLM created: %s (provider=%s, model=%s)", type(llm).__name__, llm.provider, llm.model)


async def _run_agent():
    """Run a minimal browser-use agent with a real LLM (no browser needed)."""
    from browser_use import Agent
    from backend.runner import _get_llm

    llm = _get_llm()
    logger.info("Using LLM: %s (provider=%s)", type(llm).__name__, llm.provider)

    agent = Agent(
        task="What is 2 + 2? Reply with just the number.",
        llm=llm,
    )
    result = await agent.run()
    logger.info("Agent result: %s", result)
    return result


def test_agent_run():
    """Run the agent and verify it completes without error."""
    result = asyncio.run(_run_agent())
    assert result is not None, "Agent returned None"


if __name__ == "__main__":
    print("=== Step 1: Test _get_llm() ===")
    test_get_llm()
    print("PASS\n")

    print("=== Step 2: Test agent run ===")
    test_agent_run()
    print("PASS\n")

    print("All tests passed.")
