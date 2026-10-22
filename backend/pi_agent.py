"""Pi agent subprocess with capability-limited tool dispatch and cancellation."""

import asyncio
import json
import os
from pathlib import Path

from .llm import DEFAULT_MODELS, KEYS, PREFIXES

HOST = Path(__file__).resolve().parent.parent / "agent-host" / "host.mjs"


async def run_pi(task, system, tools, dispatch, role="agent", checkpoint=None, on_event=None, effort=None):
    provider = (os.getenv("LLM_PROVIDER") or "gemini").lower()
    provider = "claude" if provider == "anthropic" else provider
    subscription = provider == "codex"
    if provider not in KEYS and not subscription:
        raise ValueError(f"Unsupported provider: {provider}")
    key = "" if subscription else os.getenv(KEYS[provider], "")
    if not key and not subscription:
        raise ValueError(f"{KEYS[provider]} is required for Pi")
    prefix = "CODEX" if subscription else PREFIXES[provider]
    suffix = "MODEL" if role == "agent" else f"{role.upper()}_MODEL"
    model = os.getenv(f"{prefix}_{suffix}") or (
        os.getenv(f"{prefix}_MODEL") if role == "exploration" else None
    ) or ("gpt-6.1-sol" if subscription else DEFAULT_MODELS[provider][role])
    # Child gets only runtime basics and the selected provider credential.
    env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "NODE_EXTRA_CA_CERTS") if name in os.environ}
    env["FLOWPILOT_MODEL_KEY"] = key
    if os.getenv("PI_AUTH_FILE"):
        env["PI_AUTH_FILE"] = os.environ["PI_AUTH_FILE"]
    proc = await asyncio.create_subprocess_exec(
        "node", str(HOST), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env, limit=1024 * 1024,
    )
    pending = {}
    allowed = {tool["name"] for tool in tools}
    lock = asyncio.Lock()

    async def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def call(message):
        try:
            async with lock:
                if checkpoint:
                    await checkpoint()
                if message["name"] not in allowed:
                    raise ValueError("Unknown tool")
                result = await dispatch(message["name"], message.get("args", {}))
            await send({"type": "tool_result", "id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await send({"type": "tool_result", "id": message["id"], "error": str(exc)[:4000]})

    try:
        await send({"type": "start", "provider": {"gemini": "google", "claude": "anthropic", "openai": "openai", "codex": "openai-codex"}[provider],
                    "model": model, "system": system, "task": task, "tools": tools, "maxTurns": 50,
                    # Effort per model turn (low|medium|high); unset uses the provider default (high).
                    **({"thinkingLevel": level} if (level := effort or os.getenv("FLOWPILOT_AGENT_EFFORT")) else {})})
        while line := await proc.stdout.readline():
            message = json.loads(line)
            if on_event and message["type"] in ("tool", "metric"):
                on_event({"type": message["type"], "name": message.get("name")})
            if message["type"] == "tool":
                pending[message["id"]] = asyncio.create_task(call(message))
            elif message["type"] == "cancel_tool":
                if message["id"] in pending:
                    pending[message["id"]].cancel()
            elif message["type"] == "error":
                raise RuntimeError(f"Pi: {message['error']}")
            elif message["type"] == "result":
                return message["text"]
        raise RuntimeError("Pi exited without a result")
    finally:
        for future in pending.values():
            future.cancel()
        await asyncio.gather(*pending.values(), return_exceptions=True)
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
