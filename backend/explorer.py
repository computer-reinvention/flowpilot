"""Exploration agent — pre-explores a site to discover inputs and save reflex scripts.

Triggered on flow creation (if entry_url exists) or on-demand via
POST /api/flows/{flow_id}/explore. Uses a full browser-use agent with
custom tools to map the journey and save reflexes for repeatable stages.

The explorer builds a tree of stages as it navigates. Each stage node:
  {id, url, title, type, reflex_name?, note?, children[]}

type: "page" | "form" | "auth_wall" | "navigation" | "extraction" | "dynamic"
"""

import asyncio
import http.client
import json
import logging
import uuid

from browser_use import Agent, Browser, ActionResult, Tools

from . import db as _db
from .models import Flow
from .runner import _get_llm

logger = logging.getLogger(__name__)


def _build_exploration_prompt(flow: Flow) -> str:
    return f"""\
You are exploring {flow.entry_url} to map the journey for: "{flow.task}"

You have THREE jobs — do them IN ORDER as you navigate:

1. REPORT STAGES: As you move through the site, call report_stage() for each
   distinct page or interaction point. This builds a tree map of the journey.
   Call it BEFORE you save a reflex for that stage.

2. DISCOVER INPUTS: Identify what user-provided values this flow needs.
   Call declare_inputs() with the list of required inputs you find.

3. MAP REFLEXES: For repeatable stages, call save_reflex() with a JS script.
   The reflex gets attached to the most recent stage you reported.

STAGE TYPES:
- "page": A distinct page/view the user lands on
- "form": A form that needs to be filled
- "auth_wall": Login/OTP/CAPTCHA — requires human help
- "navigation": A click-through step (e.g., "click Book Now")
- "extraction": Reading data from the page (e.g., scraping a title)
- "dynamic": Content that changes per session (don't reflex this)

SAVE A REFLEX when:
- A form has fixed input fields (same selectors every time)
- Navigation follows a predictable path
- The logic can be expressed as JS
- Selecting options, parsing prices, reading text — JS problems

DO NOT save a reflex when:
- Page structure is unpredictable (A/B tests, random layouts)
- Mark these as "dynamic" stages instead

Use placeholder values when interacting (e.g., "test location" for addresses).
Write scripts with inputs.field_name so real values work at run time.
If you hit auth/login, report it as an "auth_wall" stage and stop exploring that branch.
"""


async def explore_flow(
    flow: Flow,
    sandbox_id: str,
    cdp_url: str,
    storage_state: dict | None = None,
) -> None:
    """Run the exploration agent against a flow's entry_url."""
    await _db.update_flow(flow.id, {"exploration_status": "exploring"})

    # Wait for CDP
    _cdp_hp = cdp_url.split("//")[1].split("/")[0]
    _cdp_h, _cdp_p = _cdp_hp.rsplit(":", 1)
    _cdp_info = None
    for _attempt in range(20):
        try:
            _conn = http.client.HTTPConnection(_cdp_h, int(_cdp_p), timeout=3)
            _conn.request("GET", "/json/version", headers={"Host": "localhost"})
            _resp = _conn.getresponse()
            if _resp.status == 200:
                _cdp_info = json.loads(_resp.read())
                break
        except (OSError, http.client.HTTPException):
            pass
        if _attempt < 19:
            await asyncio.sleep(2)
    else:
        raise RuntimeError(f"CDP not reachable at {cdp_url} after 40s")

    _ws_path = "/" + "/".join(_cdp_info["webSocketDebuggerUrl"].split("/")[3:])
    cdp_url = f"ws://{_cdp_h}:{_cdp_p}{_ws_path}"

    browser: Browser | None = None
    notes: list[str] = []
    reflexes_saved = 0

    # Tree state — flat list of stage nodes, each with a parent_id
    stages: list[dict] = []
    current_stage_id: list[str | None] = [None]  # mutable ref for closures

    def _build_tree() -> list[dict]:
        """Convert flat stages list into nested tree."""
        by_id = {s["id"]: {**s, "children": []} for s in stages}
        roots: list[dict] = []
        for s in stages:
            node = by_id[s["id"]]
            parent = s.get("parent_id")
            if parent and parent in by_id:
                by_id[parent]["children"].append(node)
            else:
                roots.append(node)
        # Strip parent_id from output
        def _clean(node: dict) -> dict:
            return {k: v for k, v in node.items() if k != "parent_id"}
        def _clean_tree(nodes: list[dict]) -> list[dict]:
            result = []
            for n in nodes:
                cleaned = _clean(n)
                cleaned["children"] = _clean_tree(n.get("children", []))
                result.append(cleaned)
            return result
        return _clean_tree(roots)

    async def _persist_tree() -> None:
        """Save current tree to DB."""
        tree = _build_tree()
        await _db.update_flow(flow.id, {"exploration_tree": json.dumps(tree)})

    try:
        browser = Browser(
            cdp_url=cdp_url,
            storage_state=storage_state,
            headers={"Host": "localhost"},
            wait_between_actions=2.0,
            wait_for_network_idle_page_load_time=1.0,
        )

        llm = _get_llm()
        tools = Tools()

        # ----------------------------------------------------------
        # report_stage: build the exploration tree
        # ----------------------------------------------------------
        @tools.action(
            description=(
                "Report a stage in the exploration journey. Call this as you navigate "
                "to build a tree map. Each stage becomes a node in the journey tree.\n"
                "Parameters:\n"
                "  title: Short description (e.g., 'Homepage', 'Booking form', 'Login page')\n"
                "  url: Current page URL\n"
                "  type: One of: page, form, auth_wall, navigation, extraction, dynamic\n"
                "  note: Optional observation about this stage\n"
                "  is_child: If true, this stage is a sub-step of the previous stage. "
                "If false (default), it's a sibling (next step in the flow)."
            )
        )
        async def report_stage(
            title: str,
            url: str,
            type: str = "page",
            note: str | None = None,
            is_child: bool = False,
        ) -> ActionResult:
            stage_id = str(uuid.uuid4())[:8]
            parent_id = current_stage_id[0] if is_child else None
            # If not a child, find the parent of the current stage to make this a sibling
            if not is_child and current_stage_id[0]:
                for s in stages:
                    if s["id"] == current_stage_id[0]:
                        parent_id = s.get("parent_id")
                        break

            stage = {
                "id": stage_id,
                "parent_id": parent_id,
                "title": title,
                "url": url,
                "type": type,
                "note": note,
                "reflex_name": None,
            }
            stages.append(stage)
            current_stage_id[0] = stage_id
            await _persist_tree()
            return ActionResult(
                extracted_content=f"Stage '{title}' recorded (id={stage_id})."
            )

        # ----------------------------------------------------------
        # save_reflex: save a JS script, attach to current stage
        # ----------------------------------------------------------
        @tools.action(
            description=(
                "Save a JavaScript reflex script for a repeatable interaction. "
                "The script gets attached to the most recently reported stage.\n"
                "The script runs as an async function body receiving (inputs, entryUrl) "
                "plus helpers: waitFor(sel, timeout), sleep(ms), typeInto(el, text, delay), "
                "fillInput(el, value), acceptSuggestion(sel, timeout), "
                "typeAndSelect(el, text, opts?).\n"
                "Use inputs.field_name for user values. Name: snake_case."
            )
        )
        async def save_reflex(
            name: str,
            description: str,
            script: str,
            tags: list[str] | None = None,
            pause_after_auth: bool = False,
        ) -> ActionResult:
            nonlocal reflexes_saved
            pause_after = "auth" if pause_after_auth else None
            await _db.upsert_reflex(
                domain=flow.domain,
                name=name,
                description=description,
                script=script,
                pause_after=pause_after,
                tags=tags or ["explored"],
            )
            reflexes_saved += 1
            # Attach to current stage
            if current_stage_id[0]:
                for s in stages:
                    if s["id"] == current_stage_id[0]:
                        s["reflex_name"] = name
                        break
            await _persist_tree()
            return ActionResult(
                extracted_content=f"Reflex '{flow.domain}/{name}' saved and attached to current stage."
            )

        # ----------------------------------------------------------
        # declare_inputs: agent reports what inputs this flow needs
        # ----------------------------------------------------------
        @tools.action(
            description=(
                "Declare the inputs this flow requires. Each item: "
                "{name, label, type, placeholder?, required?}. "
                "type: text, textarea, number, select, date, time. "
                "Can be called multiple times — later calls replace earlier ones."
            )
        )
        async def declare_inputs(inputs: list[dict]) -> ActionResult:
            await _db.update_flow(flow.id, {"inputs": json.dumps(inputs)})
            names = [i.get("name", "?") for i in inputs]
            return ActionResult(
                extracted_content=f"Inputs declared: {', '.join(names)}"
            )

        # ----------------------------------------------------------
        # request_human_help: note auth walls during exploration
        # ----------------------------------------------------------
        @tools.action(
            description=(
                "Note an auth wall encountered during exploration. "
                "ONLY for: login forms, CAPTCHAs, OTP/SMS, consent dialogs."
            )
        )
        async def request_human_help(reason: str, category: str = "other") -> ActionResult:
            notes.append(f"AUTH WALL: [{category}] {reason}")
            return ActionResult(
                extracted_content="Noted auth wall. Report it as an auth_wall stage, then stop exploring that branch."
            )

        # ----------------------------------------------------------
        # add_notes: record observations
        # ----------------------------------------------------------
        @tools.action(
            description="Record an observation about the site for developers to review."
        )
        async def add_notes(note: str) -> ActionResult:
            notes.append(note)
            return ActionResult(extracted_content="Note recorded.")

        task_prompt = _build_exploration_prompt(flow)

        agent = Agent(
            task=task_prompt,
            llm=llm,
            browser=browser,
            tools=tools,
            max_actions_per_step=10,
            extend_system_message=(
                "You are an EXPLORER, not a runner. Your job is to map the site and "
                "save reflexes. Use placeholder values. Don't try to complete the actual task.\n"
                "ALWAYS call report_stage() BEFORE save_reflex() so the reflex gets "
                "attached to the right stage in the tree."
            ),
        )

        await agent.run(max_steps=15)

        # Final persist
        status = "ready" if reflexes_saved > 0 else "failed"
        error = None if reflexes_saved > 0 else "No reflexes saved — site may need manual exploration"
        exploration_notes = "\n".join(notes) if notes else None
        await _db.update_flow(flow.id, {
            "exploration_status": status,
            "exploration_error": error,
            "exploration_notes": exploration_notes,
            "exploration_tree": json.dumps(_build_tree()),
        })
        logger.info(
            "Exploration of flow %s complete: status=%s, reflexes=%d, stages=%d",
            flow.id, status, reflexes_saved, len(stages),
        )

    except Exception as e:
        exploration_notes = "\n".join(notes) if notes else None
        await _db.update_flow(flow.id, {
            "exploration_status": "failed",
            "exploration_error": str(e),
            "exploration_notes": exploration_notes,
            "exploration_tree": json.dumps(_build_tree()),
        })
        raise
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
