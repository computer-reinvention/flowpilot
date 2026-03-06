# FlowPilot

**Explore once. Replay forever. Take over when it gets stuck.**

FlowPilot turns any website into a repeatable automation. You point it at a URL, it explores the site with an AI agent, and writes JavaScript scripts that replay the interaction — no LLM needed on repeat runs. When it hits something it can't handle (logins, CAPTCHAs), it hands you the browser. You do the thing, click resume, it continues.

Built on [browser-use](https://github.com/browser-use/browser-use). Self-hosted. Runs in Docker.

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
1. Create a flow     → give it a name, entry URL, and input fields
2. Explore           → AI agent walks the site, generates JS reflexes at each stage
3. Run               → reflexes replay as pure JavaScript via CDP (no LLM calls)
4. When stuck        → agent pauses, you take over the live browser, click resume
```

Warm runs are ~10x faster than vanilla browser-use because they skip the LLM entirely. The agent only kicks in as a fallback when cached scripts break.

### Three-tier execution

Every run follows this strategy:

1. **Fast path** — Execute cached JS reflexes in order. If one fails, try to repair it (LLM rewrites the script using the current DOM). Bail after 3 failures.
2. **Generate** — If no reflexes exist, navigate to the entry URL, snapshot the DOM, ask the LLM to write a script from scratch.
3. **Agent** — Full browser-use agent loop as last resort. Can save new reflexes for next time.

### Reflexes

A reflex is a JavaScript async arrow function stored per-domain:

```javascript
async (inputs, entryUrl) => {
  await waitFor('#search-input', 5000);
  await typeInto('#search-input', inputs.query);
  document.querySelector('#search-btn').click();
}
```

Executed directly via CDP `Runtime.evaluate`. Built-in helpers: `waitFor()`, `sleep()`, `typeInto()`, `fillInput()`, `acceptSuggestion()`.

Created automatically during exploration, or saved by the agent after solving a new interaction.

### Human-in-the-Loop

When the agent encounters something it can't automate — login walls, CAPTCHAs, 2FA, consent screens — it pauses and hands you the live browser at 1920x1080 via CDP screencast. You interact directly (mouse + keyboard relayed through WebSocket), then click resume. The agent picks up where you left off.

Categories: `auth` · `captcha` · `consent` · `verification` · `form_fill` · `confirmation` · `other`

---

## Quick start

### Prerequisites

- Docker and Docker Compose
- An LLM API key (Gemini, Claude, or OpenAI)

### 1. Configure

```bash
cp .env.example .env
# Edit .env — set LLM_PROVIDER and your API key
```

### 2. Start

```bash
chmod +x dev
./dev restart
```

Open [http://localhost:8080](http://localhost:8080).

### 3. Chrome extension (optional)

For pre-capturing sessions (cookies/localStorage) so the agent starts authenticated:

1. `chrome://extensions/` → Developer mode → Load unpacked → select `extension/`
2. Browse to any site → click extension icon → capture session

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

Requires Docker socket access. Can't run on serverless.

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

## Benchmarks

5 scenarios (form fills, login flows, validation, checkboxes, tables) on Gemini Flash:

| | Total time | LLM calls |
|---|---|---|
| Vanilla browser-use | ~274s | ~50 |
| FlowPilot (warm) | ~27s | 0 |

~10x speedup. See `scripts/BENCHMARK_RESULTS.md` for details.

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
