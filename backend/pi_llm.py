"""Structured generation/repair through the same subscription as the Pi driver."""

import json
from types import SimpleNamespace

from .pi_agent import run_pi


class PiSubscriptionLLM:
    def __init__(self, role):
        self.role = role

    async def ainvoke(self, messages, output_format):
        value = None

        async def dispatch(name, args):
            nonlocal value
            value = output_format.model_validate(args)
            return {"accepted": True}

        system = "\n".join(str(m.content) for m in messages if m.role == "system")
        task = "\n".join(str(m.content) for m in messages if m.role != "system")
        await run_pi(task, system + "\nReturn the structured answer by calling submit_output exactly once.",
                     [{"name": "submit_output", "description": "Return the requested structured output.",
                       "inputSchema": output_format.model_json_schema()}], dispatch, role=self.role)
        if value is None:
            raise ValueError("Pi ended without a valid structured output")
        return SimpleNamespace(completion=value)
