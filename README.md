# FlowPilot

**Explore once. Replay forever. Humans in the loop — programmatically.**

FlowPilot is a self-hosted platform for building browser automations that need humans. You define a flow, the system explores the site with an AI agent and compiles JavaScript scripts that replay the interaction with zero LLM calls. When the automation hits something it can't handle — auth walls, CAPTCHAs, consent screens — it pauses and exposes the live browser over CDP so your system (or a human) can take over and resume via API.

Everything is API-first: start runs, handle pauses, resume — all programmable. Webhook events fire on every state change with the live browser URL baked in. Trigger hooks give you URL endpoints that start runs from Slack, Zapier, cron, or anything that can POST.

Uses [browser-use](https://github.com/browser-use/browser-use) for browser connectivity, with selectable browser-use or Pi agent drivers. Runs in Docker.

## Modernized local setup

### Conversational test REPL (no dashboard)

```sh
./chat --user pankaj
```

This starts **full Chromium on an Xvfb virtual display in Docker**, using the
original sandbox image. It does not open a window on your desktop or attach to
your existing Chrome. Talk to the agent in the terminal:

```text
you> Go to Blinkit. Help me log in, then add two cartons of milk to my cart. Stop before checkout.
you> What is currently in my cart?
you> /stats
you> /scripts
you> /quit
```

For missing information (including OTP codes) the agent asks in the terminal and
continues using your answer. Confirmations also happen in chat. Enter `/browser`
instead of a requested value if you prefer to handle it yourself.
For direct intervention (CAPTCHA, device approval, or a sign-in you want to handle),
the agent sends a clickable assistance link. Open it to interact with the same
browser, then click **Resume agent** there. The link expires on resume or cancellation.
The page streams the browser through CDP and forwards pointer movement,
button-down/up, dragging, scrolling and direct keyboard input. Click the image
to control it; click outside to release keyboard focus. A text-entry box and
screenshot fallback are also available. This is browser-viewport remote control,
not an OS desktop/VNC server; it does not expose native system dialogs.
Links are local to this machine. Application deployments retain the existing
authenticated CDP proxy, webhook delivery and resume API.

Programmatic callers can pass `on_handoff=async_callback` to receive a `run.paused`
event with `assistance_url`, `browser_cdp_url`, `resume_url`, category and requested
fields. Return from that callback after delivery; the agent waits for resolution
via the link/API. `human_help` remains a legacy callback override. `/cdp` also
prints raw endpoints for developer access. Use `--user alice`
or `--user bob` for separate browser profiles, histories, and saved scripts.
The same user keeps cookies/local storage across restarts. Only one process can
open a given user's profile at a time.

For programmatic chat, pass `on_question=async_callback`. It receives
`{kind, message, fields}` and returns `{"values": {"field_name": "answer"}}` for
inputs or `{"confirmed": true/false}` for confirmation. Values collected this
way are available during the run but are not appended to persisted input history.

Copy `repl.example.json` to ignored `repl.local.json` for startup defaults, or
pass `--config FILE`. Alternatively set `FLOWPILOT_REPL_USER=pankaj` in `.env`
and launch `./chat` without arguments. Identity precedence is `--user`, config,
environment, then OS username. State is stored under ignored `.repl-state/<user>/`.
These are local identity namespaces, **not authentication against other people
with access to the same OS account**. The configured model subscription is shared;
website sessions and automation memory are separate. The chat now uses the same
Docker/Xvfb browser architecture as the application, with CDP published only to
localhost. Its Linux profile is saved atomically as `sandbox-profile.tar` under
the user directory after Chromium closes gracefully and restored before startup.
The previous native macOS profile remains in `browser/`; it is not overwritten
or copied across operating systems. Conversation and saved scripts are retained.

Commands: `/history`, `/stats`, `/scripts`, `/repeat`, `/cdp`, `/inputs {"key":"value"}`,
`/quit`. `/repeat` resubmits the previous ask using the current inputs and page;
the agent checks existing state before acting. Similar asks can reuse saved scripts
or repair failed ones. Each answer reports wall/agent/human time, model turns,
tool calls, script reuse and failures. This is explicit reusable automation, not
model training; reuse and speed improvements are measured rather than guaranteed.

One-shot and programmatic usage:

```sh
./chat --user pankaj --ask 'Visit https://example.com and summarize it'
```

```python
from dotenv import load_dotenv
from backend.repl import BrowserSession

load_dotenv()  # configured Codex/Pi environment
async with BrowserSession("pankaj") as session:
    result = await session.ask("Search this site for milk", inputs={"query": "milk"})
    print(result["answer"], result["metrics"])
    followup = await session.ask("Which result is cheapest?")
```

Prerequisites: running Docker, `docker build -f Dockerfile.sandbox -t flowpilot-sandbox .`,
`uv sync`, `npm ci --prefix agent-host`, and subscription sign-in below.
`--browser-mode native` retains the previous local headless test backend and needs
`uv run playwright install chromium`. The default `sandbox` mode needs no local browser.
Read-only
live trial: first request 32.39s/8 turns; follow-up with saved-script reuse
19.18s/5 turns. Cookies/local-storage isolation and restart persistence verified
by `uv run python -m scripts.smoke_repl_state`.

1. Copy `.env.example` to `.env`, set a strong `JWT_SECRET`, and add the key for
   your chosen `LLM_PROVIDER` (`gemini`, `claude`, or `openai`).
2. Run `./dev build`, then `docker compose up -d`.
3. Open http://localhost:8080. Local Compose binds the backend to loopback.

Set `AGENT_DRIVER=pi` to use Pi for exploration/fallback. Its code-mode tool runs
JavaScript in a bounded QuickJS/WASM VM with only injected browser capabilities;
browser calls are serialized and human-help calls remain outside timed batches.
The default remains `browser-use` pending live provider comparison. Docker includes
Node and Pi. Native development also needs Node >=22.19 and `npm ci` in `agent-host`.

### Codex subscription instead of an API key

Run `node agent-host/login.mjs` locally and complete the displayed OpenAI device
sign-in. Credentials are saved privately in ignored `.pi-auth/auth.json`, mounted
only into the backend. Pi refreshes tokens under a cross-process file lock.
Set `LLM_PROVIDER=codex`, `AGENT_DRIVER=pi`, and optionally `CODEX_MODEL` (or
`CODEX_EXPLORATION_MODEL`/`CODEX_FAST_MODEL`), then rebuild/recreate Compose.
Exploration, fallback, script generation and repair all use the subscription.
This does not authenticate the separate optional Jev service.

Model overrides use `GEMINI_`, `ANTHROPIC_`, or `OPENAI_` prefixes followed by
`MODEL`, `EXPLORATION_MODEL`, or `FAST_MODEL`. See `.env.example`. Cached complete
replay plans do not construct a model or require a provider key.

Optional Jev routing: set `JEV_ENABLED=true` and `TYPESAFE_API_KEY` to classify
failure observations through TypeSafe. Low-confidence or unavailable responses
fall back to the agent. Successful replay never invokes Jev.

Sandbox profiles are copied into stopped containers before Chromium starts; no
shared profile mount is exposed. Default Docker seccomp and no-new-privileges
are used. `SANDBOX_RUNTIME=runsc` requires a separately configured gVisor host;
it is not installed by this project. Chromium still uses `--no-sandbox`, so the
container boundary remains important.

Verification commands:

```sh
uv run pytest -q
npm --prefix agent-host test
npm --prefix frontend run build
docker compose exec -T backend uv run python < scripts/smoke_replay.py
```

The last command creates an isolated test database/container, checks real two-stage
CDP replay with pause/resume and no repeated first-stage effect, then cleans up.
Live provider tests require `uv run pytest --live` and configured credentials.
Research, decisions, current evidence and limitations: [modernization notes](docs/modernization.md).
Concurrent browser-worker sizing, measured limits and reproducible load harness:
[session capacity benchmark](docs/session-capacity.md).

### Historical benchmarks (Gemini Flash, 5 scenarios; not revalidated for this revision)

| Scenario | What it does | Baseline | Cold | Warm | Speedup |
|---|---|---|---|---|---|
| Simple form fill | 8 fields (text, radio, checkbox, autocomplete, textarea) + submit | 74.6s | 18.5s | **5.4s** | **13.7x** |
| Login + add to cart | Login, add product to cart, navigate to cart, verify item | 36.5s | 59.0s | **5.0s** | **7.3x** |
| Form with validation | 4 fields (name, email, 2 addresses) + submit, verify output | 35.7s | 17.1s | **3.8s** | **9.4x** |
| Checkboxes + tree | Expand 3 tree nodes, check 2 specific checkboxes, verify result | 66.6s | 34.9s | **7.5s** | **8.8x** |
| Modal + table + search | Open modal, fill 6-field form, submit, search table, verify row | 61.0s | 47.3s | **5.1s** | **11.9x** |
| **TOTAL** | | **274.5s** | | **26.9s** | **10.2x** |

Pass rate: 5/5 across all modes. Warm runs make zero LLM calls.

---

## How it works

```
1. Create a flow     → name, entry URL, input schema
2. Explore           → AI agent walks the site, generates JS reflexes per stage
3. Run               → reflexes replay as pure JS via CDP — no LLM
4. Pause             → agent can't proceed → pauses, fires webhook, exposes browser
5. Resume            → human or external system resolves it → POST /resume → continues
```

### Three-tier execution

1. **Fast path** — Execute every stage in a complete flow-scoped plan. Auto-repair on failure; an unrepaired prerequisite stops dependent replay and hands off current state.
2. **Generate** — No reflexes? Navigate to entry URL, snapshot DOM, LLM writes a script.
3. **Agent** — Selectable Pi or browser-use loop as fallback. Saves new reflexes for next time.

### Reflexes

JavaScript async arrow functions stored per-flow and domain, executed via bounded CDP `Runtime.evaluate`. Explicit tree references preserve legacy scripts:

```javascript
async (inputs, entryUrl) => {
  await waitFor('#search-input', 5000);
  await typeInto('#search-input', inputs.query);
  document.querySelector('#search-btn').click();
}
```

Helpers: `waitFor()`, `sleep()`, `typeInto()`, `fillInput()`, `acceptSuggestion()`.

Generated during exploration or saved by the agent after solving a new interaction.

### Human-in-the-Loop (HITL)

HITL is a first-class primitive, not an escape hatch. When a run pauses:

1. **Webhook fires** `run.paused` with `browser_cdp_url` (live browser WebSocket) and `resume_url` (POST to continue)
2. **Browser is accessible** — full mouse/keyboard control over CDP screencast at 1920x1080
3. **Resume via API** — `POST /api/runs/{id}/resume` or `POST /api/v1/runs/{id}/resume` with API key
4. **Agent continues** from the current browser state — cookies captured automatically on resume

This means you can build around it:
- Slack bot notifies on-call when auth is needed, they open the browser URL, log in, hit resume
- External CAPTCHA service gets the CDP URL, solves it, calls resume
- Cron job triggers a run via trigger hook, webhook alerts your system on pause, operator resolves it

**Pause categories**: `auth` · `captcha` · `consent` · `verification` · `form_fill` · `confirmation` · `other`

### Trigger hooks

URL endpoints that start runs without authentication — the token is the credential:

```bash
# Create a hook
POST /api/v1/flows/{flow_id}/hooks  →  { "token": "abc123", "url": "/api/v1/hooks/abc123" }

# Fire it from anywhere
curl -X POST https://your-instance/api/v1/hooks/abc123 \
  -d '{"inputs": {"query": "flights to tokyo"}}'
```

Wire these into Slack slash commands, Zapier, GitHub Actions, cron — anything that can POST.

### API keys

`fp_live_` prefixed keys for programmatic access. Create via API or UI, use on all `/api/v1/` endpoints. Bcrypt-hashed at rest.

---

## Quick start

### Prerequisites

- Docker and Docker Compose
- An LLM API key (Gemini, Claude, or OpenAI)

### 1. Configure

```bash
cp .env.example .env
# Set LLM_PROVIDER and your API key
```

### 2. Start

```bash
chmod +x dev
./dev restart
```

Open [http://localhost:8080](http://localhost:8080).

### 3. Chrome extension (optional)

Pre-capture sessions so the agent starts authenticated:

1. `chrome://extensions/` → Developer mode → Load unpacked → select `extension/`
2. Browse to any site → click extension icon → capture

---

## Development

```bash
# Backend (hot reload)
uv sync && uv run uvicorn backend.main:app --reload --port 8080

# Frontend (separate terminal, proxies to backend)
cd frontend && npm install && npm run dev
```

Dev helper: `./dev stop | build | build-fe | up | restart | logs | url`

---

## Deployment

Requires Docker socket access. Not serverless-compatible.

```bash
sudo ./deploy setup    # Docker + Caddy + HTTPS + build + start
sudo ./deploy restart  # rebuild + restart
sudo ./deploy logs     # follow logs
```

| Provider | Spec | ~Cost/mo |
|----------|------|----------|
| Hetzner CX32 | 4 vCPU, 8GB | $7 |
| DigitalOcean | 4GB Droplet | $24 |
| Oracle Cloud | Ampere A1 4-core | Free tier |

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `gemini` | `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | — | Gemini |
| `ANTHROPIC_API_KEY` | — | Claude |
| `OPENAI_API_KEY` | — | OpenAI |
| `JWT_SECRET` | (generated) | Must be stable in production |
| `FLOWPILOT_BASE_URL` | `http://localhost:8080` | Public URL (used in webhooks) |
| `FLOWPILOT_TRACE` | `0` | `1` to enable reflex execution tracing |

See [docs/configuration.md](docs/configuration.md) for all options including model overrides and file paths.

---

## Docs

| | |
|---|---|
| [Architecture](docs/architecture.md) | System design, data flow, design principles |
| [Agent & Reflexes](docs/agent.md) | Three-tier execution, tools, reflex format, explorer |
| [API Reference](docs/api-reference.md) | All HTTP + WebSocket endpoints |
| [Backend](docs/backend.md) | Module reference |
| [Frontend](docs/frontend.md) | Components, hooks, state |
| [Database](docs/database.md) | Schema, tables, relationships |
| [Infrastructure](docs/infrastructure.md) | Docker, networking, deployment |
| [Webhooks](docs/webhooks.md) | Events, HMAC signing, retries |
| [Configuration](docs/configuration.md) | All env vars |

---

## License

Private project.
