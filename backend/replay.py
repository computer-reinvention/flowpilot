"""Flow-bound replay planning and pause control shared by fast and agent paths."""

import asyncio
import json


class RunControl:
    """Implements the pause/resume interface expected by the existing run API."""

    def __init__(self, paused_ref: list[bool]):
        self.paused_ref = paused_ref
        self._ready = asyncio.Event()
        self._ready.set()
        self.agent = None

    def pause(self):
        self.paused_ref[0] = True
        self._ready.clear()
        if self.agent is not None:
            self.agent.pause()

    def resume(self):
        self.paused_ref[0] = False
        self._ready.set()
        if self.agent is not None:
            self.agent.resume()

    async def checkpoint(self):
        await self._ready.wait()


async def replay_plan(db, flow) -> tuple[list[dict], bool]:
    """Return only flow-owned or explicitly referenced legacy scripts.

    The boolean says whether the plan is complete enough for unattended replay.
    A flow without a tree can replay only its own whole-flow auto-generated script.
    Incomplete plans are supplied to the agent as candidates, never run blindly.
    """
    scoped = await db.list_reflexes_for_domain(flow.domain, flow_id=flow.id)
    by_name = {r["name"]: r for r in scoped}
    tree = json.loads(flow.exploration_tree or "[]")
    if not tree:
        whole = by_name.get("auto_generated")
        return ([whole], True) if whole else (scoped, False)

    ordered = []
    complete = True

    async def walk(nodes):
        nonlocal complete
        for node in nodes:
            name = node.get("reflex_name")
            if name:
                reflex = by_name.get(name)
                if reflex is None:
                    reflex = await db.get_reflex(flow.domain, name)
                if reflex is None:
                    complete = False
                else:
                    ordered.append(reflex)
            elif not node.get("children"):
                # A leaf stage without code still needs the agent to handle it.
                complete = False
            await walk(node.get("children", []))

    await walk(tree)
    return ordered, bool(ordered) and complete
