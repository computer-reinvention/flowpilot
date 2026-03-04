# Agent & Reflexes

## Agent Architecture

FlowPilot uses [browser-use](https://github.com/browser-use/browser-use) as its AI agent framework. The agent controls Chromium via CDP, using browser-use's built-in action toolkit (click, type, navigate, etc.) plus custom tools.

**No Playwright layer** — browser-use's native tools are used directly. There is no separate Playwright executor.

### Agent Configuration

- **Max steps**: 30 (hard limit per run)
- **LLM**: Configurable via `LLM_PROVIDER` (Gemini, Claude, or OpenAI)
- **System prompt**: Emphasizes reflexes-first strategy, efficiency, and minimal steps

### Execution Flow

```
1. Wait for CDP readiness (poll /json/version, up to 40s)
2. Load domain reflexes from DB
3. Connect browser-use Browser to CDP URL
4. Create Agent with custom tools + system prompt
5. Agent runs autonomously (up to 30 steps)
6. On completion: return result
```

## Custom Tools

Four custom tools are registered on the agent alongside browser-use's built-in actions.

### search_reflexes(use_case: str)

Search for cached step sequences relevant to the current task.

- Fetches all reflexes for the flow's domain from DB
- Ranks by relevance using word overlap against `use_case`
- Returns top matches with names, descriptions, and pause hints
- Agent should call this first before attempting any task

### run_reflex(name: str)

Retrieve and follow a cached reflex.

- Fetches the full reflex from DB by name
- Returns the script (step-by-step text instructions)
- Agent must follow the steps using browser-use's native tools (click, type, etc.)
- Steps are descriptions, not executable code

### save_reflex(name, description, steps, tags, pause_after_auth)

Cache a successful task as a reflex for future runs.

- Domain-scoped by the flow's domain
- Upserts (updates if name already exists for that domain)
- `steps`: text descriptions of what to do
- `pause_after_auth`: if true, the reflex will include a HITL pause hint
- Agent calls this after successfully completing a task

### request_human_help(reason, category)

Pause the run and request human intervention.

- Sets the run status to `paused_for_hitl`
- Broadcasts status update via WebSocket
- Fires `run.paused` webhook event
- Agent blocks until the human resumes the run
- Categories: `auth`, `captcha`, `consent`, `verification`, `form_fill`, `confirmation`, `other`

**HITL flow**:
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

Reflexes are domain-scoped cached step sequences that dramatically reduce step count on repeat visits to the same domain.

### Anatomy of a Reflex

```
Domain:      uber.com
Name:        book_ride
Description: Book an Uber ride with pickup and dropoff
Script:      1. Click the "Where to?" search bar
             2. Type the pickup location and select from dropdown
             3. Click "Choose destination"
             4. Type the dropoff location and select from dropdown
             5. Select "UberX" ride type
             6. Click "Request UberX"
Tags:        ["booking", "ride"]
Pause After: auth (indicates HITL may be needed for login)
```

### How Reflexes Work

1. **Discovery**: Agent calls `search_reflexes("book a ride")` at the start of a task
2. **Matching**: System ranks reflexes by word overlap with the use case
3. **Execution**: Agent calls `run_reflex("book_ride")` → receives step-by-step text
4. **Following**: Agent interprets each step and executes using browser-use tools
5. **Saving**: After completing a new task, agent calls `save_reflex()` to cache the steps

### Key Properties

- **Text, not code**: Steps are human-readable descriptions, not executable scripts
- **Domain-scoped**: Reflexes are specific to a domain (e.g., uber.com, amazon.com)
- **Unique by name**: `UNIQUE(domain, name)` constraint in the database
- **Updatable**: `save_reflex` upserts — existing reflexes get updated
- **Agent-interpreted**: The agent decides how to translate step descriptions into browser actions

### Agent Strategy

The system prompt instructs the agent to:
1. Always search for reflexes first
2. Follow reflex steps if a match is found
3. Use browser-use's native tools to execute each step
4. Save new reflexes after successfully completing novel tasks
5. Minimize total steps — accept autocomplete suggestions, don't overthink

## LLM Configuration

| Provider | Env Variable | Default Model |
|----------|-------------|---------------|
| Gemini | `GOOGLE_API_KEY` | `gemini-3-flash-preview` |
| Claude | `ANTHROPIC_API_KEY` | `claude-sonnet-4-20250514` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o` |

Set `LLM_PROVIDER` to `gemini`, `claude`, or `openai`. The corresponding API key env var must be set.
