# Backend

The backend is a FastAPI application that manages flows, runs, sandboxes, authentication, and real-time communication.

**Stack**: Python 3.12, FastAPI, aiosqlite, browser-use, Docker SDK

**Entry point**: `backend/main.py`
**Default port**: 8080

## Module Reference

### `main.py` — Application & Routes

The FastAPI app with all route definitions, WebSocket handlers, and lifecycle management.

**Key globals**:

- `_ws_connections: dict[run_id, list[WebSocket]]` — Active WebSocket clients per run
- `_run_tasks: dict[run_id, asyncio.Task]` — Background run execution tasks
- `_run_agents: dict[run_id, tuple]` — Active agent references `(agent, browser, flow, paused_ref)` for HITL resume

**Lifespan**:

- On startup: init DB, reconcile sandboxes from DB, clean orphan containers, seed default flows
- On shutdown: stop all sandboxes, cancel running tasks

**Key internal functions**:

- `_start_run_impl(flow_id, inputs, user_id, profile_id)` — Shared logic for starting runs (used by both `/api/runs` and `/api/v1/runs`)
- `_execute_run(run_id, flow, inputs, user_id, profile_id)` — Background task: starts sandbox, waits for CDP, runs agent, handles cleanup
- `_broadcast_run_progress(run_id, data)` — Fan out WebSocket messages to all connected clients for a run

**Profile saves on teardown**: Profile is saved before sandbox teardown on every path — manual stop, cancel exploration, restart exploration, cancel run.

### `runner.py` — Agent Execution Engine

Orchestrates the three-tier execution strategy: cached JS reflexes → auto-generation → full agent.

**Class**: `FlowRunner`

**Main method**:

```python
async def run_flow(
    flow, inputs, user_id, sandbox_id, cdp_url, run_id,
    progress_callback, storage_state, agent_ready_callback
) -> dict
```

**Execution sequence**:

1. Poll CDP endpoint until ready (up to 40s)
2. Load domain reflexes from DB (ordered by exploration tree, then DB order)
3. **Fast path**: execute cached JS reflexes via CDP `Runtime.evaluate`; auto-repair on failure; bail after 3 cumulative failures
4. **Generate path**: if no reflexes exist, navigate to entry URL, dump DOM, ask LLM to write JS
5. **Slow path**: full browser-use agent loop with custom tools, `max_steps=30`
6. Return result

**Key internal functions**:

- `_execute_reflex(page, script, inputs, entry_url)` — Run JS via CDP `Runtime.evaluate` (bypasses browser-use's `page.evaluate()` validation)
- `_repair_reflex(script, error, dom, flow)` — Single LLM call to fix a broken script using DOM context (up to 2 attempts)
- `_generate_reflex(page, flow, inputs)` — Generate a new reflex from scratch by navigating + dumping DOM
- `_get_llm()`, `_get_fast_llm()`, `_get_exploration_llm()` — LLM provider factory functions

**Lazy DB import**: Uses `_get_db()` helper to avoid importing `aiosqlite`/`sqlite3` at module level.

**Browser teardown**: Uses `browser.stop()` (not `.close()`) — `BrowserSession` in browser-use 0.12+ uses `.stop()`.

See [Agent & Reflexes](agent.md) for details on tools and reflex format.

### `explorer.py` — Flow Exploration Engine

Pre-explores a website to discover inputs, map the user journey as a stage tree, and auto-generate JavaScript reflex scripts.

**Class**: `FlowExplorer`

**Main method**:

```python
async def explore(
    flow, user_id, sandbox_id, cdp_url, run_id,
    progress_callback, storage_state, on_session_capture
) -> dict
```

**What it does**:

1. Connects browser-use agent to the flow's `entry_url`
2. Agent navigates the site, reporting stages (page, form, auth_wall, navigation, extraction, dynamic)
3. At each significant stage, generates a JavaScript reflex via a fast LLM call
4. Tests each generated reflex on the live page
5. Builds a hierarchical stage tree persisted as `exploration_tree` in the DB
6. Auto-extracts inputs from reflex scripts via regex (`inputs.\w+`)

**Agent config**: `max_history_items=6`, `max_actions_per_step=3`, `use_vision=True`, `llm_screenshot_size=(768,576)`, `max_steps=30`

### `cdp_proxy.py` — CDP WebSocket Proxy

Streams browser frames to the frontend and relays input events back.

**Handler**: `async def handle(websocket, sandbox_id, sandbox_mgr, token)`

**Protocol**:

1. Authenticate via JWT in query param
2. Connect to Chromium's CDP WebSocket
3. Attach flat session via `Target.attachToTarget`
4. Start screencast (`Page.startScreencast`, JPEG quality 80)
5. Forward frames as `{type: "frame", data: base64}` to frontend
6. Receive input events from frontend, dispatch as CDP commands
7. Track tab changes via `Target.targetCreated` events

**Graceful teardown**: Catches `ConnectionClosedError` silently on screencast ack, input relay, and outer connection — no "Task exception was never retrieved" spam on sandbox shutdown.

**Message types** (backend → frontend):

- `{type: "frame", data: "base64-jpeg"}` — Browser frame
- `{type: "nav", url: "..."}` — Navigation event
- `{type: "loading", loading: true|false}` — Page load state

**Message types** (frontend → backend):

- `{type: "mouse", eventType, x, y, button, clickCount, modifiers}` — Mouse events
- `{type: "key", eventType, key, code, text, modifiers, keyCode}` — Keyboard events
- `{type: "navigate", action: "back|forward|reload|goto", url?}` — Browser navigation

### `sandbox.py` — Container Management

Manages Docker containers that run isolated Chromium instances.

**Class**: `SandboxManager`

**Key methods**:

- `start(profile_path, user_id)` — Create and start a sandbox container
- `stop(sandbox_id)` — Stop and remove container
- `get_cdp_url(sandbox_id)` — Return internal CDP WebSocket URL
- `is_running(sandbox_id)` — Check container status
- `reconcile_from_db(db_rows)` — Sync in-memory state with DB on startup
- `cleanup_orphans()` — Remove stale containers
- `stop_all()` — Graceful shutdown

**Container spec**:

- Image: `flowpilot-sandbox`
- Name: `flowpilot-{sandbox_id}`
- Network: `flowpilot_sandbox-net`
- Limits: 1GB RAM, 100k CPU quota, 256 PIDs, 256MB shm
- CDP URL: `ws://flowpilot-{sandbox_id}:9223`

### `auth.py` — Authentication

Dual authentication: JWT tokens for UI sessions, API keys for programmatic access.

**JWT auth**:

- `create_token(user_id)` → JWT with 7-day expiry
- `decode_token(token)` → user_id

**API key auth**:

- Keys prefixed `fp_live_` (40 chars total)
- Stored as bcrypt hash, looked up by 12-char prefix
- `generate_api_key()` → plaintext key (shown once)
- `hash_api_key(plaintext)` → bcrypt hash
- `verify_api_key(plaintext, hash)` → bool

**FastAPI dependency**: `get_current_user(request)` — Resolves Bearer token as JWT or API key.

### `webhooks.py` — Event Delivery

Delivers webhook events with HMAC-SHA256 signatures and retry logic.

See [Webhooks](webhooks.md) for full documentation.

### `db.py` — Database Layer

Async SQLite CRUD for all entities.

See [Database Schema](database.md) for table definitions.

### `profile.py` — Profile Management

Save and restore Chromium profiles from sandbox containers.

**Class**: `ProfileManager`

**Methods**:

- `save_profile(sandbox_id, profile_name, user_id)` — Extract `/tmp/chrome-profile` from container as tar.gz
- `restore_profile(user_id, profile_name, sandbox_id)` — Copy profile back into container
- `has_profile(user_id, profile_name)` — Check if profile exists
- `get_profile_info(user_id, profile_name)` — Metadata (size, last modified)

**Reliability**: Includes retry mechanism (2 attempts), container running check, and archive non-empty verification. Uses Docker SDK's `container.put_archive()` instead of shelling out to `docker cp`.

### `trace.py` — Tracing (Debug)

Optional tracing module for debugging reflex execution.

- Gated by `FLOWPILOT_TRACE` env var (default off, `=1` to enable)
- Writes traces to `FLOWPILOT_TRACES_DIR` (default `/tmp/flowpilot-traces/`)
- Captures: full scripts, prompts, DOM context, error messages, failure counts
- Instrumented functions: `_execute_reflex`, `_repair_reflex`, `_generate_reflex`, fast path attempts, explorer generate/test

### `models.py` — Data Classes

```python
@dataclass
class Flow:
    id, name, domain, description, entry_url, inputs, icon, created_by, created_at

@dataclass
class Run:
    id, flow_id, user_id, status, inputs, sandbox_id, steps, current_step,
    result, error, created_at, completed_at, profile_id

@dataclass
class Session:
    id, user_id, domain, cookies, local_storage, session_storage,
    user_agent, profile_name, captured_at
```

**Run statuses**: `pending` → `connecting` → `running` → `completed` | `failed` | `cancelled` | `paused_for_hitl`

### `config.py` — Configuration

See [Configuration](configuration.md) for all environment variables.
