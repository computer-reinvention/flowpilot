# FlowPilot

**Explore once. Replay forever. Humans in the loop — programmatically.**

FlowPilot is a self-hosted platform for building browser automations that need humans. You define a flow, the system explores the site with an AI agent and compiles JavaScript scripts that replay the interaction with zero LLM calls. When the automation hits something it can't handle — auth walls, CAPTCHAs, consent screens — it pauses and exposes the live browser over CDP so your system (or a human) can take over and resume via API.

Everything is API-first: start runs, handle pauses, resume — all programmable. Webhook events fire on every state change with the live browser URL baked in. Trigger hooks give you URL endpoints that start runs from Slack, Zapier, cron, or anything that can POST.

Built on [browser-use](https://github.com/browser-use/browser-use). Runs in Docker.

### Benchmarks (Gemini Flash, 5 scenarios)

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

1. **Fast path** — Execute cached JS reflexes in order. Auto-repair on failure (LLM rewrites script using current DOM). Bail after 3 failures.
2. **Generate** — No reflexes? Navigate to entry URL, snapshot DOM, LLM writes a script.
3. **Agent** — Full browser-use agent loop as last resort. Saves new reflexes for next time.

### Reflexes

JavaScript async arrow functions stored per-domain, executed via CDP `Runtime.evaluate`:

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
