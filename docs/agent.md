# Agent & Reflexes

## Agent Architecture

FlowPilot uses [browser-use](https://github.com/browser-use/browser-use) as its AI agent framework. The agent controls Chromium via CDP, using browser-use's built-in action toolkit (click, type, navigate, etc.) plus custom tools.

**No Playwright layer** — browser-use's native tools are used directly. There is no separate Playwright executor.

### Agent Configuration

- **Max steps**: 30 (hard limit per run)
- **LLM**: Configurable via `LLM_PROVIDER` (Gemini, Claude, or OpenAI)
- **System prompt**: Emphasizes reflexes-first strategy, efficiency, and minimal steps
- All browser-use defaults are preserved (no custom wait times, vision settings, or history limits)

### Three-Tier Execution

Runs follow a tiered strategy — cached scripts first, then auto-generation, then full agent:

```
1. Fast Path  → Execute cached JS reflexes sequentially
                 ↓ (on failure: auto-repair up to 2 attempts)
                 ↓ (after 3 cumulative failures: bail)
2. Generate   → Navigate to entry_url, dump DOM, ask LLM to write JS
                 ↓ (one repair pass if initial script fails)
3. Slow Path  → Full browser-use agent loop with custom tools (max 30 steps)
```

The fast path executes reflexes ordered by the exploration tree (if one exists), then by DB insertion order.

## Custom Tools

### Runner Tools

Four custom tools are registered on the agent in the slow path:

#### run_reflex(name: str)
Retrieve and execute a cached JavaScript reflex.
- Fetches the full reflex from DB by name
- Executes the JS via CDP `Runtime.evaluate`

#### save_reflex(name, description, script, tags, pause_after_auth)
Cache a successful task as a JavaScript reflex for future runs.
- Domain-scoped by the flow's domain
- Upserts (updates if name already exists for that domain)
- `script`: complete `async (inputs, entryUrl) => { ... }` arrow function
- Agent calls this after successfully completing a novel task

#### request_human_help(reason, category)
Pause the run and request human intervention.
- Sets the run status to `paused_for_hitl`
- Broadcasts status update via WebSocket
- Fires `run.paused` webhook event
- Agent blocks until the human resumes the run
- Categories: `auth`, `captcha`, `consent`, `verification`, `form_fill`, `confirmation`, `other`

#### search_reflexes(use_case: str)
Search for cached reflexes relevant to the current task.
- Fetches all reflexes for the flow's domain from DB
- Ranks by relevance using word overlap against `use_case`
- Returns top matches with names, descriptions, and pause hints

### Explorer Tools

The explorer agent (used during flow exploration) has a richer tool set:

- `report_stage(title, url, type, note, is_child)` — Record a stage in the journey tree
- `generate_and_save_reflex(name, description, tags, pause_after_auth)` — Generate JS reflex via fast LLM from current page DOM
- `search_reflexes(query)` — Keyword search existing reflexes for the domain
- `test_reflex(name)` — Execute a reflex on the current page, return pass/fail
- `declare_inputs(inputs_json)` — Declare user-controlled inputs for the flow
- `request_human_help(reason, category)` — HITL pause for auth/captcha/etc.
- `request_input_values(fields_json)` — Ask user for real form values during exploration
- `request_confirmation(summary)` — Confirm before final submit
- `add_notes(note)` — Developer notes

### HITL Flow

```
Agent calls request_human_help("[auth] Login required", "auth")
    → paused_ref[0] = True
    → Progress callback fires (status: PAUSED_FOR_HITL)
    → WebSocket broadcast to connected clients
    → Webhook fires run.paused event
    → Agent blocks in a polling loop (checks paused_ref[0] every 500ms)

Human interacts with browser via BrowserCanvas
    → Mouse/keyboard events relayed through CDP proxy

Human clicks "Resume" in UI
    → POST /api/runs/{id}/resume
    → Cookies captured from current browser state
    → paused_ref[0] = False
    → agent.resume() called
    → Agent continues from current page
```

## Reflex System

Reflexes are domain-scoped cached **JavaScript scripts** that execute common tasks without any LLM calls — pure JS replay.

### Anatomy of a Reflex

```
Domain:      uber.com
Name:        book_ride
Description: Book an Uber ride with pickup and dropoff
Script:      async (inputs, entryUrl) => {
               const pickup = inputs.pickup || "Home";
               await typeInto('[data-testid="pickup"]', pickup);
               ...
             }
Tags:        ["booking", "ride"]
Pause After: auth (indicates HITL may be needed for login)
```

### Script Format

Reflex scripts are **complete async arrow functions** stored and executed as-is:

```javascript
async (inputs, entryUrl) => {
  // inputs: object with user-provided values
  // entryUrl: the flow's entry URL

  // Built-in helpers available:
  // waitFor(selector, timeoutMs) — wait for element
  // sleep(ms) — delay
  // typeInto(selector, text) — clear + type
  // fillInput(selector, text) — set value + dispatch events
  // acceptSuggestion(selector) — click autocomplete suggestion

  const name = inputs.name || "John Doe";
  await waitFor('#full-name', 5000);
  await typeInto('#full-name', name);
  // ...
}
```

### Execution via CDP

Scripts are executed directly via Chrome DevTools Protocol `Runtime.evaluate` — **not** through browser-use's `page.evaluate()`. This bypasses browser-use's validation (which requires scripts starting with `(`) and gives direct access to the page context.

Navigation-caused exceptions (e.g., form submit triggers page load) are detected as success if the URL changed.

### Repair and Generation

- **Repair**: When a cached reflex fails, the runner dumps the current DOM and asks the LLM to fix the script. Up to 2 repair attempts per reflex.
- **Generation**: If no reflexes exist for a domain, the runner navigates to the entry URL, dumps the DOM, and asks the LLM to write a new script from scratch. Gets one repair pass if the initial attempt fails.
- **3-strike fallthrough**: After 3 cumulative reflex failures (across all reflexes in the fast path), the runner abandons scripts and falls through to the full agent.

### How Reflexes Are Created

1. **During exploration**: The explorer agent calls `generate_and_save_reflex()` which uses a fast LLM to write JS from the current page DOM
2. **During runs**: The agent in slow path can call `save_reflex()` to cache a working script
3. **Auto-extraction**: After exploration, inputs are auto-extracted from reflex scripts via regex (`inputs.\w+`) if the agent forgot to `declare_inputs()`

### Key Properties

- **JavaScript, not text**: Scripts are executable async arrow functions with helpers
- **Domain-scoped**: Reflexes are specific to a domain (e.g., uber.com, amazon.com)
- **Unique by name**: `UNIQUE(domain, name)` constraint in the database
- **Updatable**: `save_reflex` upserts — existing reflexes get updated
- **Zero LLM calls on warm runs**: Cached reflexes execute as pure JS — no agent involvement

## LLM Configuration

| Provider | Env Variable | Default Model |
|----------|-------------|---------------|
| Gemini | `GOOGLE_API_KEY` | `gemini-3-flash-preview` |
| Claude | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o` |

Set `LLM_PROVIDER` to `gemini`, `claude`, or `openai`. The corresponding API key env var must be set.

### Multi-Model Support

The runner uses up to three LLM tiers:

| Tier | Purpose | Gemini Default | Env Override |
|------|---------|----------------|-------------|
| Primary | Agent loop, exploration | `gemini-3-flash-preview` | `GEMINI_MODEL` |
| Exploration | Explorer page extraction | `gemini-2.5-flash-preview-05-20` | `GEMINI_EXPLORATION_MODEL` |
| Fast | Reflex generation/repair | `gemini-2.5-flash-preview-05-20` | `GEMINI_FAST_MODEL` |

Anthropic and OpenAI support `*_FAST_MODEL` overrides similarly.
