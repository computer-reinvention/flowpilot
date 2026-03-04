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
import re
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
   Call declare_inputs() ONLY after you know the exact field names/labels.
   Each input MUST have a "name" (e.g. "pickup_location"). Do NOT call
   declare_inputs with empty objects or placeholders — wait until you can
   see the actual form fields.

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

When you encounter form fields needing user-specific values (addresses, names,
dates, etc.), call request_input_values() with the field details. The user will
provide real values. Do NOT use placeholder/dummy values for these fields.
Write scripts with inputs.field_name so real values work at run time.

Before clicking any final submit/confirm/book/order button, call
request_confirmation() with a summary of what will happen. The user will
approve or decline. Do NOT click submit buttons without confirmation.

If you hit auth/login, report it as an "auth_wall" stage and call request_human_help().
The user will log in for you in the live browser, then exploration resumes.
"""


async def explore_flow(
    flow: Flow,
    sandbox_id: str,
    cdp_url: str,
    storage_state: dict | None = None,
    agent_ready_callback=None,
    on_status_change=None,
) -> None:
    """Run the exploration agent against a flow's entry_url."""

    async def _update_and_notify(updates: dict) -> None:
        """Update flow in DB and notify WebSocket subscribers."""
        await _db.update_flow(flow.id, updates)
        if on_status_change:
            await on_status_change(flow.id)

    await _update_and_notify({"exploration_status": "exploring"})

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
        """Save current tree to DB and notify subscribers."""
        tree = _build_tree()
        await _update_and_notify({"exploration_tree": json.dumps(tree)})

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
            # Deduplicate: if same URL + type already exists, reuse it
            for s in stages:
                if s["url"] == url and s["type"] == type:
                    current_stage_id[0] = s["id"]
                    if note:
                        s["note"] = note  # update note if provided
                    return ActionResult(
                        extracted_content=(
                            f"Stage already recorded for {url} (type={type}, id={s['id']}). "
                            "Reusing existing stage. Navigate to a NEW page before reporting another stage."
                        )
                    )

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
                "Declare the inputs this flow requires. Each item MUST have: "
                "{name: string, label: string, type: string, placeholder?, required?}. "
                "name, label, and type are REQUIRED — do NOT pass empty objects. "
                "type: text, textarea, number, select, date, time. "
                "Call this ONLY when you can see the actual form fields. "
                "Can be called multiple times — later calls replace earlier ones."
            )
        )
        async def declare_inputs(inputs: list[dict]) -> ActionResult:
            # Validate: each input must have at least name and label
            valid = []
            for inp in inputs:
                name = inp.get("name", "").strip()
                itype = inp.get("type", "").strip()
                if not name or not itype:
                    continue
                valid.append({
                    "name": name,
                    "label": inp.get("label", name).strip() or name,
                    "type": itype,
                    "placeholder": inp.get("placeholder", ""),
                    "required": inp.get("required", True),
                })
            if not valid:
                return ActionResult(
                    extracted_content="No valid inputs provided. Each must have at least a 'name' field."
                )
            await _db.update_flow(flow.id, {"inputs": json.dumps(valid)})
            names = [i["name"] for i in valid]
            return ActionResult(
                extracted_content=f"Inputs declared: {', '.join(names)}"
            )

        # ----------------------------------------------------------
        # request_human_help: pause for HITL (same as runs)
        # ----------------------------------------------------------
        _paused = [False]
        _agent_ref: list = []
        _hitl_response: list = [None]  # shared ref: resume endpoint stores user response here

        async def _wait_for_resume(poll_interval: float = 0.5, timeout: float = 300) -> dict | None:
            """Block until _paused is set back to False by the resume endpoint."""
            elapsed = 0.0
            while _paused[0] and elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
            response = _hitl_response[0]
            _hitl_response[0] = None
            return response

        @tools.action(
            description=(
                "Request human help. The browser will be shown to the user so they "
                "can log in, solve CAPTCHAs, etc. Exploration pauses until they resume. "
                "ONLY for: login forms, CAPTCHAs, OTP/SMS, consent dialogs."
            )
        )
        async def request_human_help(reason: str, category: str = "other") -> ActionResult:
            _paused[0] = True
            notes.append(f"AUTH WALL: [{category}] {reason}")
            await _update_and_notify({
                "exploration_status": "paused_for_hitl",
            })
            if _agent_ref:
                _agent_ref[0].pause()
            # Block until the user finishes and clicks resume
            await _wait_for_resume()
            return ActionResult(
                extracted_content="Human assisted. Continue exploring from current page state."
            )

        # ----------------------------------------------------------
        # request_input_values: pause for user-provided form values
        # ----------------------------------------------------------
        @tools.action(
            description=(
                "Pause exploration and ask the user for real form values. "
                "Call this when you encounter form fields that need user-specific data "
                "(addresses, names, dates, payment info, etc.).\n"
                "Parameters:\n"
                "  fields: list of objects, each MUST have:\n"
                '    - "name": snake_case identifier (e.g. "pickup_location")\n'
                '    - "label": human-readable label (e.g. "Pickup Location")\n'
                '    - "description": optional hint (e.g. "Street address for pickup")\n'
                '    - "type": "text" (default) or "select" for dropdowns\n'
                '    - "options": list of strings (required when type is "select")\n'
                "Returns a dict mapping field names to user-provided values."
            )
        )
        async def request_input_values(fields: list[dict]) -> ActionResult:
            _paused[0] = True
            # Validate/normalize fields before saving
            validated = []
            for f in fields:
                name = (f.get("name") or f.get("field_name") or f.get("id") or "").strip()
                label = (f.get("label") or f.get("title") or name).strip()
                if not name:
                    continue
                field_type = (f.get("type") or "text").strip()
                if field_type not in ("text", "select"):
                    field_type = "text"
                options = f.get("options") or []
                if not isinstance(options, list):
                    options = []
                entry: dict = {
                    "name": name,
                    "label": label or name,
                    "description": (f.get("description") or f.get("hint") or "").strip() or None,
                    "type": field_type,
                }
                if field_type == "select" and options:
                    entry["options"] = [str(o) for o in options]
                validated.append(entry)
            if not validated:
                return ActionResult(
                    extracted_content="No valid fields provided. Each must have at least a 'name' and 'label'."
                )
            await _update_and_notify({
                "exploration_status": "paused_for_hitl",
                "exploration_input_request": json.dumps(validated),
            })
            if _agent_ref:
                _agent_ref[0].pause()
            # Block until the resume endpoint sets _paused back to False
            response = await _wait_for_resume()
            if response and isinstance(response, dict):
                values = response.get("values", {})
                return ActionResult(
                    extracted_content=(
                        f"User provided values: {json.dumps(values)}. "
                        "For EACH field: 1) click the input, 2) type the value, "
                        "3) wait 2 seconds for a suggestion/autocomplete dropdown to appear, "
                        "4) click the first suggestion in the list. "
                        "Do this one field at a time."
                    )
                )
            return ActionResult(
                extracted_content="User resumed without providing values. Continue with best effort."
            )

        # ----------------------------------------------------------
        # request_confirmation: pause before final submit
        # ----------------------------------------------------------
        @tools.action(
            description=(
                "Pause exploration and ask the user for confirmation before clicking "
                "a final submit/confirm/book/order button. Describe what will happen.\n"
                "Parameters:\n"
                "  summary: What the action will do (e.g., 'Book ride from X to Y for $15').\n"
                "Returns whether the user approved or declined."
            )
        )
        async def request_confirmation(summary: str) -> ActionResult:
            _paused[0] = True
            await _update_and_notify({
                "exploration_status": "paused_for_hitl",
                "exploration_confirmation_request": summary,
            })
            if _agent_ref:
                _agent_ref[0].pause()
            # Block until the resume endpoint sets _paused back to False
            response = await _wait_for_resume()
            if response and isinstance(response, dict) and not response.get("confirmed", True):
                return ActionResult(
                    extracted_content="User declined. Stop this action and do not proceed."
                )
            return ActionResult(
                extracted_content="User approved. Proceed with the action."
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
            use_vision="auto",
            llm_screenshot_size=(1024, 768),  # downscale before sending to LLM
            extend_system_message=(
                "You are an EXPLORER, not a runner. Your job is to map the site and "
                "save reflexes. Don't try to complete the actual task.\n\n"
                "CRITICAL RULES:\n"
                "1. ALWAYS call report_stage() BEFORE save_reflex() so the reflex gets "
                "attached to the right stage in the tree.\n"
                "2. ALWAYS call declare_inputs() when you see form fields that need "
                "user-provided values. This is MANDATORY — without it, the flow has no "
                "inputs and reflexes using inputs.X will fail at runtime.\n"
                "3. NAVIGATE FORWARD. Don't stay on the same page. After mapping a stage "
                "and saving its reflex, click through to the next page/view.\n"
                "4. For form fields needing real values, call request_input_values() to "
                "get actual values from the user, THEN fill them in.\n"
                "5. Before any final submit/confirm/book button, call request_confirmation().\n"
                "6. Do NOT report the same page twice — move forward in the journey."
            ),
        )

        _agent_ref.append(agent)

        if agent_ready_callback:
            agent_ready_callback(agent, browser, _paused, sandbox_id, _hitl_response)

        await agent.run(max_steps=15)

        # Final persist — auto-extract inputs from reflex scripts if agent forgot
        current_flow = await _db.get_flow(flow.id)
        declared_inputs = json.loads(current_flow.inputs) if current_flow else []
        if not declared_inputs and reflexes_saved > 0:
            # Scan all reflexes for inputs.X references
            all_reflexes = await _db.list_reflexes_for_domain(flow.domain)
            discovered: dict[str, dict] = {}
            for r in all_reflexes:
                for match in re.finditer(r"inputs\.(\w+)", r.get("script", "") if isinstance(r, dict) else getattr(r, "script", "")):
                    name = match.group(1)
                    if name not in discovered:
                        discovered[name] = {
                            "name": name,
                            "label": name.replace("_", " ").title(),
                            "type": "text",
                            "placeholder": "",
                            "required": True,
                        }
            if discovered:
                auto_inputs = list(discovered.values())
                await _db.update_flow(flow.id, {"inputs": json.dumps(auto_inputs)})
                notes.append(f"Auto-extracted {len(auto_inputs)} inputs from reflex scripts: {', '.join(discovered.keys())}")
                logger.info("Auto-extracted inputs for flow %s: %s", flow.id, list(discovered.keys()))

        status = "ready" if reflexes_saved > 0 else "failed"
        error = None if reflexes_saved > 0 else "No reflexes saved — site may need manual exploration"
        exploration_notes = "\n".join(notes) if notes else None
        await _update_and_notify({
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
        await _update_and_notify({
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
