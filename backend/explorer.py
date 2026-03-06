"""Exploration agent — pre-explores a site to discover inputs and save reflex scripts.

Triggered on flow creation (if entry_url exists) or on-demand via
POST /api/flows/{flow_id}/explore. Uses a full browser-use agent with
custom tools to map the journey and save reflexes for repeatable stages.

Context management strategy:
  - The main agent loop is kept lean: short tool descriptions, small screenshots,
    low max_history_items.
  - Reflex generation is offloaded to a separate fast-LLM call so the agent
    never sees bulky JS in its conversation history.
  - Reflex testing executes scripts and returns only a pass/fail summary.
"""

import asyncio
import http.client
import json
import logging
import os
import re
import uuid

from browser_use import Agent, Browser, ActionResult, Tools

from . import db as _db
from .models import Flow
from .runner import _get_exploration_llm, _get_fast_llm
from .trace import trace

logger = logging.getLogger(__name__)

# ── Reflex generation prompt (runs on fast LLM, outside agent context) ──────

_REFLEX_GEN_PROMPT = """\
Write a JavaScript reflex script for the following browser interaction.

CONTEXT:
- Page URL: {url}
- Stage: {stage_title}
- Description: {description}

OUTPUT FORMAT:
Your response must be a single async arrow function that page.evaluate() can call:

  async (inputs, entryUrl) => {{
      // your code here
  }}

The function receives:
- inputs: object with user-provided values (inputs.pickup_location, inputs.date, etc.)
- entryUrl: the flow's entry URL string

USEFUL PATTERNS (define inside the function if you need them):

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const waitFor = (sel, timeout = 5000) => new Promise((resolve, reject) => {{
    const el = document.querySelector(sel);
    if (el) return resolve(el);
    const obs = new MutationObserver(() => {{
        const el = document.querySelector(sel);
        if (el) {{ obs.disconnect(); resolve(el); }}
    }});
    obs.observe(document.body, {{childList: true, subtree: true}});
    setTimeout(() => {{ obs.disconnect(); reject(new Error('timeout: ' + sel)); }}, timeout);
}});

const typeInto = async (el, text, delay = 30) => {{
    el.focus(); el.value = '';
    for (const ch of text) {{
        el.value += ch;
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        await sleep(delay);
    }}
    el.dispatchEvent(new Event('change', {{bubbles: true}}));
}};

RULES:
- Output ONLY the arrow function. No markdown fences, no explanation.
- Use inputs.field_name for user-specific values
- Keep it short and robust — prefer waitFor() over hardcoded delays
- For autocomplete/suggestion fields: type into the field, wait for suggestion
  elements to appear, then click the first matching suggestion
"""


def _build_exploration_prompt(flow: Flow) -> str:
    return f"""\
Explore {flow.entry_url} to map the journey for: "{flow.task}"

Follow the EXACT order implied by the task description. If the task mentions
auth/login first, handle auth first.

AGENT LOOP — repeat for every stage:

1. ARRIVE at a page → call report_stage().
2. SEARCH — call search_reflexes(query) to check if a reflex already exists.
   If found: call test_reflex(name). If it passes, navigate forward. Done.
3. PROBE — interact minimally to discover dynamic elements: type into a field
   to see what autocomplete/suggestions appear, trigger a dropdown to see its
   options. Note the exact CSS selectors you observe in the DOM.
4. DECLARE INPUTS — call declare_inputs() for any fields that need user values
   (text inputs, dropdowns, card selectors, toggles — anything the user must
   provide). Each input needs name, label, type.
5. WRITE REFLEX — call generate_and_save_reflex() with a precise description
   including the selectors you discovered and the complete stage interaction.
6. TEST — call test_reflex(name). If it fails, read the error and call
   generate_and_save_reflex() again with a corrected description. Repeat once.
7. NAVIGATE forward to the next stage.

REFLEX SCOPE — one reflex covers the entire stage end-to-end:
  BAD:  "Click the first autocomplete suggestion"
  GOOD: "Fill pickup input[placeholder*='pickup'] with inputs.pickup_location,
         wait for ul.suggestions li to appear, click first item,
         repeat for dropoff, then click button[type=submit]"

STAGE TYPES: page, form, auth_wall, navigation, extraction, dynamic

INPUTS — declare ALL user-controlled values including:
- Text fields (locations, names, dates)
- Dropdowns and select elements
- Card/tile selectors (e.g. ride type: UberX, Comfort, Black)
- Toggles and radio groups

SKIP reflexes for:
- Auth walls — call request_human_help() instead
- Truly unpredictable layouts — mark as "dynamic"

For fields needing real values during probe/test, call request_input_values().
Before any final submit/confirm/book, call request_confirmation().
If you hit auth/login, report as "auth_wall" and call request_human_help().
"""


async def explore_flow(
    flow: Flow,
    sandbox_id: str,
    cdp_url: str,
    storage_state: dict | None = None,
    agent_ready_callback=None,
    on_status_change=None,
    # Injected session-capture callback: async (browser) -> None
    # Lets callers (main.py) persist cookies without explorer knowing the user context.
    on_session_capture=None,
) -> None:
    """Run the exploration agent against a flow's entry_url."""

    async def _update_and_notify(updates: dict) -> None:
        await _db.update_flow(flow.id, updates)
        if on_status_change:
            await on_status_change(flow.id)

    await _update_and_notify({"exploration_status": "exploring"})

    # ── Wait for CDP ────────────────────────────────────────────────────
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
    _ss_tmp_path: str | None = None
    notes: list[str] = []
    reflexes_saved = 0

    # ── Stage tree state ────────────────────────────────────────────────
    stages: list[dict] = []
    current_stage_id: list[str | None] = [None]

    def _build_tree() -> list[dict]:
        by_id = {s["id"]: {**s, "children": []} for s in stages}
        roots: list[dict] = []
        for s in stages:
            node = by_id[s["id"]]
            parent = s.get("parent_id")
            if parent and parent in by_id:
                by_id[parent]["children"].append(node)
            else:
                roots.append(node)

        def _clean_tree(nodes: list[dict]) -> list[dict]:
            return [
                {
                    **{k: v for k, v in n.items() if k != "parent_id"},
                    "children": _clean_tree(n.get("children", [])),
                }
                for n in nodes
            ]

        return _clean_tree(roots)

    async def _persist_tree() -> None:
        tree = _build_tree()
        await _update_and_notify({"exploration_tree": json.dumps(tree)})

    try:
        # browser-use's StorageStateWatchdog only loads from file paths, not dicts.
        # Write the storage_state dict to a temp file so cookies are actually injected.
        ss_arg = None
        if storage_state:
            from .session_utils import storage_state_to_file
            _ss_tmp_path = storage_state_to_file(storage_state)
            ss_arg = _ss_tmp_path

        browser = Browser(
            cdp_url=cdp_url,
            storage_state=ss_arg,
            headers={"Host": "localhost"},
        )

        llm = _get_exploration_llm()
        fast_llm = _get_fast_llm()
        tools = Tools()

        # ── report_stage ────────────────────────────────────────────────
        @tools.action(
            description=(
                "Report a stage in the journey. "
                "Args: title, url, type (page|form|auth_wall|navigation|extraction|dynamic), "
                "note (optional), is_child (bool, default false)."
            )
        )
        async def report_stage(
            title: str,
            url: str,
            type: str = "page",
            note: str | None = None,
            is_child: bool = False,
        ) -> ActionResult:
            for s in stages:
                if (s["url"] == url and s["type"] == type) or (s["title"] == title and s["type"] == type):
                    current_stage_id[0] = s["id"]
                    if note:
                        s["note"] = note
                    return ActionResult(
                        extracted_content=f"Stage exists for '{title}' (id={s['id']}). Move to next page."
                    )

            stage_id = str(uuid.uuid4())[:8]
            parent_id = current_stage_id[0] if is_child else None
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
            return ActionResult(extracted_content=f"Stage '{title}' recorded (id={stage_id}).")

        # ── generate_and_save_reflex (offloaded to fast LLM) ───────────
        @tools.action(
            description=(
                "Generate and save a JS reflex for the ENTIRE current stage — not a single action. "
                "The reflex must cover ALL interactions in the stage from start to finish. "
                "Example: a booking form reflex fills pickup, fills dropoff, selects autocomplete "
                "suggestions for both, picks ride type, all in one script. "
                "Call this ONCE per stage after you have fully explored it. "
                "YOU provide a plain-English description; a separate model writes the JS. "
                "Args: name (snake_case), description (full stage flow to automate), "
                "tags (optional list), pause_after_auth (bool, default false)."
            )
        )
        async def generate_and_save_reflex(
            name: str,
            description: str,
            tags: list[str] | None = None,
            pause_after_auth: bool = False,
        ) -> ActionResult:
            nonlocal reflexes_saved

            # Get current stage info for context
            stage_title = "unknown"
            stage_url = flow.entry_url
            if current_stage_id[0]:
                for s in stages:
                    if s["id"] == current_stage_id[0]:
                        stage_title = s["title"]
                        stage_url = s["url"]
                        break

            # Generate the script using the fast LLM (outside agent context)
            gen_prompt = _REFLEX_GEN_PROMPT.format(
                url=stage_url,
                stage_title=stage_title,
                description=description,
            )
            try:
                from browser_use.llm.messages import UserMessage
                response = await fast_llm.ainvoke([UserMessage(content=gen_prompt)])
                script = response.completion.strip()
                # Strip markdown fences if the model wrapped them
                if script.startswith("```"):
                    script = "\n".join(script.split("\n")[1:])
                if script.endswith("```"):
                    script = "\n".join(script.split("\n")[:-1])
                script = script.strip()
            except Exception as e:
                logger.warning("Reflex generation failed for '%s': %s", name, e)
                return ActionResult(
                    extracted_content=f"Reflex generation failed: {e}. Move on."
                )

            trace("explorer.generate_reflex", name=name, description=description, prompt=gen_prompt, script=script)

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

            if current_stage_id[0]:
                for s in stages:
                    if s["id"] == current_stage_id[0]:
                        s["reflex_name"] = name
                        break
            await _persist_tree()
            return ActionResult(
                extracted_content=f"Reflex '{name}' generated and saved. Move to next stage."
            )

        # ── search_reflexes ──────────────────────────────────────────────
        @tools.action(
            description=(
                "Search saved reflexes for this domain by keyword. "
                "Matches against reflex name and description. "
                "Call this before exploring a stage to check if a reflex already "
                "covers it — if one exists, call test_reflex(name) instead of "
                "re-exploring. Args: query (keyword or phrase to search for)."
            )
        )
        async def search_reflexes(query: str) -> ActionResult:
            reflexes = await _db.search_reflexes(flow.domain, query)
            if not reflexes:
                return ActionResult(extracted_content=f"No reflexes found for '{query}' on {flow.domain}. Proceed with exploration and save a reflex after completing this stage.")
            lines = [f"Reflexes matching '{query}' on {flow.domain}:"]
            for r in reflexes:
                lines.append(f"  - {r['name']}: {r['description']}")
            return ActionResult(extracted_content="\n".join(lines))

        # ── test_reflex (execute + return summary) ──────────────────────
        @tools.action(
            description=(
                "Test a saved reflex by executing it on the current page. "
                "Returns pass/fail with error details. Args: name (the reflex name)."
            )
        )
        async def test_reflex(name: str) -> ActionResult:
            reflex = await _db.get_reflex(flow.domain, name)
            if not reflex:
                return ActionResult(extracted_content=f"Reflex '{name}' not found.")

            script = reflex.get("script", "") if isinstance(reflex, dict) else getattr(reflex, "script", "")
            if not script:
                return ActionResult(extracted_content=f"Reflex '{name}' has no script.")

            # Build sample inputs from declared flow inputs so the reflex
            # runs with real-looking values rather than empty dict.
            # Empty inputs cause inputs.field === undefined which silently
            # produces wrong behavior (e.g. navigating to /tag/undefined/).
            current_flow = await _db.get_flow(flow.id)
            declared = json.loads(current_flow.inputs or "[]") if current_flow else []
            test_inputs = {}
            for inp in declared:
                iname = inp.get("name", "")
                itype = inp.get("type", "text")
                if itype == "text":
                    test_inputs[iname] = inp.get("placeholder") or iname.replace("_", " ")
                elif itype == "number":
                    test_inputs[iname] = "1"
                elif itype == "date":
                    test_inputs[iname] = "2026-01-01"
                else:
                    test_inputs[iname] = iname

            trace("explorer.test_reflex.call", name=name, script=script, test_inputs=test_inputs, entry_url=flow.entry_url)
            try:
                page = await browser.must_get_current_page()
                result = await page.evaluate(script, test_inputs, flow.entry_url)
                trace("explorer.test_reflex.ok", name=name, result=str(result)[:200])
                return ActionResult(
                    extracted_content=f"Reflex '{name}' PASSED with inputs {test_inputs}. Result: {str(result)[:200]}"
                )
            except Exception as e:
                error_msg = str(e)[:300]
                trace("explorer.test_reflex.fail", name=name, error=error_msg)
                return ActionResult(
                    extracted_content=f"Reflex '{name}' FAILED with inputs {test_inputs}: {error_msg}"
                )

        # ── declare_inputs ──────────────────────────────────────────────
        @tools.action(
            description=(
                "Declare inputs this flow needs. Pass a JSON string array. "
                'Example: [{"name":"pickup_location","label":"Pickup Location","type":"text"},'
                '{"name":"dropoff_location","label":"Dropoff Location","type":"text"}]. '
                "Each object MUST have name, label, type. "
                "type: text|textarea|number|select|date|time. "
                "Call ONLY when you can see actual form fields."
            )
        )
        async def declare_inputs(inputs_json: str) -> ActionResult:
            try:
                inputs = json.loads(inputs_json)
            except (json.JSONDecodeError, TypeError):
                return ActionResult(
                    extracted_content="Invalid JSON. Pass a JSON array string like: "
                    '[{"name":"pickup","label":"Pickup","type":"text"}]'
                )
            if not isinstance(inputs, list):
                inputs = [inputs]
            valid = []
            for inp in inputs:
                if not isinstance(inp, dict):
                    continue
                name = inp.get("name", "").strip()
                itype = inp.get("type", "").strip()
                if not name or not itype:
                    continue
                valid.append(
                    {
                        "name": name,
                        "label": inp.get("label", name).strip() or name,
                        "type": itype,
                        "placeholder": inp.get("placeholder", ""),
                        "required": inp.get("required", True),
                    }
                )
            if not valid:
                return ActionResult(
                    extracted_content="No valid inputs parsed. Each object needs 'name', 'label', 'type'. "
                    'Example: [{"name":"email","label":"Email","type":"text"}]'
                )
            # Merge with existing inputs (preserve creation-time inputs)
            current_flow = await _db.get_flow(flow.id)
            existing_raw = json.loads(current_flow.inputs or "[]") if current_flow else []
            existing_names = {i["name"] for i in existing_raw}
            merged = list(existing_raw)
            for inp in valid:
                if inp["name"] not in existing_names:
                    merged.append(inp)
            await _db.update_flow(flow.id, {"inputs": json.dumps(merged)})
            names = [i["name"] for i in merged]
            return ActionResult(extracted_content=f"Inputs declared: {', '.join(names)}")

        # ── HITL tools ──────────────────────────────────────────────────
        _paused = [False]
        _agent_ref: list = []
        _hitl_response: list = [None]

        async def _wait_for_resume(poll_interval: float = 0.5, timeout: float = 300) -> dict | None:
            elapsed = 0.0
            while _paused[0] and elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
            response = _hitl_response[0]
            _hitl_response[0] = None
            return response

        @tools.action(
            description=(
                "Pause for human help (login, CAPTCHA, OTP). "
                "Args: reason, category (auth|captcha|consent|verification|other)."
            )
        )
        async def request_human_help(reason: str, category: str = "other") -> ActionResult:
            _paused[0] = True
            notes.append(f"AUTH WALL: [{category}] {reason}")
            await _update_and_notify({"exploration_status": "paused_for_hitl"})
            if _agent_ref:
                _agent_ref[0].pause()
            await _wait_for_resume()
            return ActionResult(extracted_content="Human assisted. Continue from current page.")

        @tools.action(
            description=(
                "Ask user for real form values (addresses, names, dates). "
                "Pass a JSON string array of fields. "
                'Example: [{"name":"pickup_location","label":"Pickup Location"},'
                '{"name":"dropoff_location","label":"Dropoff Location"}]. '
                "Each object MUST have name and label. Optional: description, type (text|select), options (for select). "
                "Returns dict of name→value."
            )
        )
        async def request_input_values(fields_json: str) -> ActionResult:
            try:
                fields = json.loads(fields_json)
            except (json.JSONDecodeError, TypeError):
                return ActionResult(
                    extracted_content="Invalid JSON. Pass a JSON array string like: "
                    '[{"name":"pickup","label":"Pickup Location"}]'
                )
            if not isinstance(fields, list):
                fields = [fields]
            _paused[0] = True
            validated = []
            for f in fields:
                if not isinstance(f, dict):
                    continue
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
                _paused[0] = False
                return ActionResult(
                    extracted_content="No valid fields parsed. Each needs 'name' and 'label'. "
                    'Example: [{"name":"pickup","label":"Pickup Location"}]'
                )
            await _update_and_notify(
                {
                    "exploration_status": "paused_for_hitl",
                    "exploration_input_request": json.dumps(validated),
                }
            )
            if _agent_ref:
                _agent_ref[0].pause()
            response = await _wait_for_resume()
            if response and isinstance(response, dict):
                values = response.get("values", {})
                return ActionResult(
                    extracted_content=(
                        f"User values: {json.dumps(values)}. "
                        "Fill each field: click → type → wait 2s for suggestions → click first suggestion."
                    )
                )
            return ActionResult(extracted_content="User resumed without values. Continue best effort.")

        @tools.action(
            description=(
                "Ask user to confirm before a final submit/book/order action. "
                "Args: summary (what will happen)."
            )
        )
        async def request_confirmation(summary: str) -> ActionResult:
            _paused[0] = True
            await _update_and_notify(
                {
                    "exploration_status": "paused_for_hitl",
                    "exploration_confirmation_request": summary,
                }
            )
            if _agent_ref:
                _agent_ref[0].pause()
            response = await _wait_for_resume()
            if response and isinstance(response, dict) and not response.get("confirmed", True):
                return ActionResult(extracted_content="User declined. Do NOT proceed.")
            return ActionResult(extracted_content="User approved. Proceed.")

        @tools.action(description="Record an observation for developers.")
        async def add_notes(note: str) -> ActionResult:
            notes.append(note)
            return ActionResult(extracted_content="Noted.")

        # ── Build agent ─────────────────────────────────────────────────
        task_prompt = _build_exploration_prompt(flow)

        agent = Agent(
            task=task_prompt,
            llm=llm,
            browser=browser,
            tools=tools,
            max_history_items=6,
            max_actions_per_step=3,
            use_vision=True,
            llm_screenshot_size=(768, 576),
            page_extraction_llm=fast_llm,
            extend_system_message=(
                "You are an EXPLORER. Map the site and generate reflexes. "
                "Do NOT try to complete the actual task.\n\n"
                "RULES:\n"
                "1. Follow the task description ORDER. Auth first if task says so.\n"
                "2. call search_reflexes(query) before probing any stage. "
                "   If a match exists, test_reflex(name) and move on — do NOT re-explore.\n"
                "   If nothing found, that is expected on first exploration — proceed.\n"
                "3. Call report_stage() BEFORE generate_and_save_reflex().\n"
                "4. PROBE minimally — interact only enough to discover selectors for "
                "   dynamic elements (autocomplete lists, dropdown options, card tiles).\n"
                "5. Call declare_inputs() for every user-controlled field you find.\n"
                "6. ONE reflex per stage covering the ENTIRE stage end-to-end.\n"
                "7. NAVIGATE FORWARD after each stage.\n"
                "8. For fields needing real values to probe, call request_input_values().\n"
                "9. Before final submit/confirm/book, call request_confirmation().\n"
                "10. Keep your reasoning SHORT. Do not repeat observations."
            ),
        )

        _agent_ref.append(agent)

        if agent_ready_callback:
            agent_ready_callback(agent, browser, _paused, sandbox_id, _hitl_response)

        await agent.run(max_steps=30)

        # ── Post-run: auto-extract inputs from reflexes if agent forgot ─
        current_flow = await _db.get_flow(flow.id)
        declared_inputs = json.loads(current_flow.inputs) if current_flow else []
        if not declared_inputs and reflexes_saved > 0:
            all_reflexes = await _db.list_reflexes_for_domain(flow.domain)
            discovered: dict[str, dict] = {}
            for r in all_reflexes:
                script_text = (
                    r.get("script", "") if isinstance(r, dict) else getattr(r, "script", "")
                )
                for match in re.finditer(r"inputs\.(\w+)", script_text):
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
                notes.append(
                    f"Auto-extracted {len(auto_inputs)} inputs from reflexes: {', '.join(discovered.keys())}"
                )
                logger.info("Auto-extracted inputs for flow %s: %s", flow.id, list(discovered.keys()))

        status = "ready" if reflexes_saved > 0 else "failed"
        error = None if reflexes_saved > 0 else "No reflexes saved — site may need manual exploration"
        exploration_notes = "\n".join(notes) if notes else None
        await _update_and_notify(
            {
                "exploration_status": status,
                "exploration_error": error,
                "exploration_notes": exploration_notes,
                "exploration_tree": json.dumps(_build_tree()),
            }
        )
        logger.info(
            "Exploration of flow %s complete: status=%s, reflexes=%d, stages=%d",
            flow.id, status, reflexes_saved, len(stages),
        )

    except Exception as e:
        exploration_notes = "\n".join(notes) if notes else None
        await _update_and_notify(
            {
                "exploration_status": "failed",
                "exploration_error": str(e),
                "exploration_notes": exploration_notes,
                "exploration_tree": json.dumps(_build_tree()),
            }
        )
        raise
    finally:
        if browser is not None:
            if on_session_capture:
                try:
                    await on_session_capture(browser)
                except Exception as e:
                    logger.debug("Session capture callback failed: %s", e)
            try:
                await browser.close()
            except Exception:
                pass
        if _ss_tmp_path and os.path.exists(_ss_tmp_path):
            try:
                os.unlink(_ss_tmp_path)
            except Exception:
                pass
