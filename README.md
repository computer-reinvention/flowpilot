# FlowPilot

**Give the AI a task. Watch it browse. Take over when it gets stuck.**

FlowPilot is a self-hosted platform for AI browser automation. You define flows in plain English, the agent executes them in isolated Chromium containers, and you watch live — stepping in only when the bot hits something it can't handle (logins, CAPTCHAs).

Uses [browser-use](https://github.com/browser-use/browser-use) for LLM-driven browsing and [cdp-use](https://github.com/browser-use/cdp-use) for the live browser stream.

---

## What it does

```
You write: "Book a ride from my office to the airport for tomorrow 9am"
          ↓
FlowPilot: spins up a fresh browser → restores your Uber session →
           navigates + fills the form → hits "request ride"
          ↓
You watch the whole thing happen in real time.
If it gets stuck → you take over → it picks back up.
```

**Key capabilities:**

- **Live browser view** — CDP screencast at native 1920×1080. No VNC lag, pixel-accurate, ~1-frame latency.
- **Human-in-the-Loop (HITL)** — When the agent hits a login wall or CAPTCHA it can't solve, it pauses and hands control to you. You log in, click Resume, it continues.
- **Session capture** — Chrome extension grabs cookies and localStorage from any site. Injected before each run so the agent starts authenticated.
- **Reflexes** — Cached step sequences the agent builds as it works. Next run on the same site it fires the cached steps instead of re-learning — faster and more reliable.
- **Webhook events** — `run.started`, `run.paused`, `run.completed`, `run.failed`, `run.cancelled`. The `run.paused` event includes the live browser URL so external systems can take over.
- **API keys** — `fp_live_` prefixed keys for programmatic access. Trigger runs from your own code.

---

## How to run it

### Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- Node.js 20+ and npm
- Docker and Docker Compose
- An LLM API key (Gemini, Claude, or OpenAI)

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env` — at minimum set your LLM provider and key:

```bash
LLM_PROVIDER=gemini           # gemini | claude | openai
GOOGLE_API_KEY=your-key-here

JWT_SECRET=some-random-secret  # tokens won't survive restarts without this
```

### 2. Start

```bash
chmod +x dev
./dev restart    # builds images, starts containers, follows logs
```

Open [http://localhost:8080](http://localhost:8080).

That's it. The frontend, backend, and Docker socket proxy all start together.

### 3. Install the Chrome extension (optional, for session capture)

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. **Load unpacked** → select the `extension/` directory
4. Browse to any site, click the extension icon, capture your session

---

## Development

Run backend and frontend separately for hot reload:

```bash
# Backend
uv sync
uv run playwright install chromium && uv run playwright install-deps
uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8080

# Frontend (separate terminal)
cd frontend && npm install && npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

### Dev commands

```
./dev stop       Stop all containers
./dev build      Rebuild sandbox + backend images
./dev build-fe   Rebuild frontend only
./dev up         Start containers
./dev restart    Full cycle: stop → build → up → logs
./dev logs       Follow logs
./dev url        Print the app URL
```

---

## Architecture

```
┌─────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
│  React UI   │─────>│  FastAPI Backend      │─────>│  Docker Socket Proxy │
│  (Vite)     │ REST │  (port 8080)          │      └──────────┬───────────┘
│  port 3000* │<─────│                       │                 │
└─────────────┘  WS  │  - Auth (JWT + API    │      ┌──────────▼───────────┐
                      │    keys)              │      │  Sandbox Container   │
┌─────────────┐      │  - Flows CRUD         │      │  - Chromium 1920×1080│
│  Chrome     │─────>│  - Runs engine        │      │  - CDP (port 9222)   │
│  Extension  │ POST │  - Reflexes (SQLite)  │      │  - Xvfb + SwiftShader│
└─────────────┘      │  - Sessions           │      │  - browser-use agent │
                      │  - Webhooks           │◄─────│                      │
                      │  - CDP proxy (WS)     │  CDP └──────────────────────┘
                      └──────────────────────┘
```

`*` Port 3000 in dev (Vite proxies `/api` and `/ws` to :8080), port 8080 in production.

The CDP proxy at `/ws/browser/{sandbox_id}` connects to Chromium alongside browser-use's Playwright session — both coexist without conflict via a flat `Target.attachToTarget` session.

---

## API Reference

### Auth
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/auth/signup` | Create account |
| `POST` | `/api/auth/login` | Login → JWT |
| `GET` | `/api/auth/me` | Current user |

### Flows
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/flows` | List flows |
| `POST` | `/api/flows` | Create flow |
| `PUT` | `/api/flows/{id}` | Update flow |
| `DELETE` | `/api/flows/{id}` | Delete flow |

### Runs
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/runs` | Start a run → `run_id`, `sandbox_id` |
| `GET` | `/api/runs` | List runs |
| `GET` | `/api/runs/{id}` | Run status, steps, result |
| `POST` | `/api/runs/{id}/cancel` | Cancel |
| `POST` | `/api/runs/{id}/resume` | Resume after HITL |

### Sessions & Profiles
| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/sessions/capture` | Capture session (from extension) |
| `GET` | `/api/sessions` | List sessions |
| `DELETE` | `/api/sessions/{id}` | Delete session |
| `POST` | `/api/profile/save` | Save Chrome profile from sandbox |

### API Keys & Webhooks
| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/api-keys` | List API keys |
| `POST` | `/api/v1/api-keys` | Create key (`fp_live_…`) |
| `DELETE` | `/api/v1/api-keys/{id}` | Revoke key |
| `GET` | `/api/v1/webhooks` | List webhooks |
| `POST` | `/api/v1/webhooks` | Register webhook |
| `DELETE` | `/api/v1/webhooks/{id}` | Delete webhook |

### WebSockets
| Endpoint | Description |
|----------|-------------|
| `WS /ws/runs/{run_id}` | Real-time run progress (status, steps) |
| `WS /ws/browser/{sandbox_id}?token=` | Live browser stream + mouse/keyboard input |

### Webhook payload (run.paused)

```json
{
  "event": "run.paused",
  "run_id": "abc123",
  "category": "auth",
  "browser_cdp_url": "ws://...",
  "resume_url": "http://localhost:8080/api/runs/abc123/resume"
}
```

HITL categories: `auth` · `captcha` · `consent` · `verification` · `form_fill` · `confirmation` · `other`

Webhook requests are signed with `X-FlowPilot-Signature: sha256=<hmac>`. Retried at 2s, 10s, 60s on failure.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `gemini` | `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | — | Required for Gemini |
| `ANTHROPIC_API_KEY` | — | Required for Claude |
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | |
| `OPENAI_MODEL` | `gpt-4o` | |
| `JWT_SECRET` | (generated) | Set this — tokens won't survive restarts otherwise |
| `FLOWPILOT_BASE_URL` | `http://localhost:8080` | Used in webhook `resume_url` |
| `PROFILES_DIR` | `/app/profiles` | SQLite DB + Chrome profiles location |
| `ALLOWED_ORIGINS` | `http://localhost:8080,…` | CORS |
| `DOCKER_HOST` | (auto) | Docker daemon URL |
| `SANDBOX_NETWORK` | `flowpilot_sandbox-net` | Docker network for sandbox containers |

---

## Security

- **Sandbox isolation** — Each run: its own container, 1GB RAM, 100k CPU quota, 256 PIDs, 256MB shm
- **No raw Docker socket** — Backend talks through docker-socket-proxy (containers + networks only)
- **CDP not exposed** — Port 9222 is internal to the Docker network only
- **CDP proxy auth** — `/ws/browser/{id}` requires a JWT via `?token=`
- **Per-user limit** — Max 2 concurrent sandboxes per user
- **API key hashing** — Keys are bcrypt-hashed at rest; raw value shown once on creation

---

## Hosting

Requires Docker socket access (can't run on serverless).

| Provider | Spec | ~Cost/mo | Notes |
|----------|------|----------|-------|
| **Hetzner Cloud** | CX32 (4 vCPU, 8GB) | $7 | Best value |
| **DigitalOcean** | Basic 4GB Droplet | $24 | Easy setup |
| **Oracle Cloud** | Ampere A1 (4 core, 24GB) | Free tier | ARM, generous free allowance |

The `./deploy` script handles first-time server setup end-to-end:

```bash
sudo ./deploy setup   # installs Docker + Caddy, creates .env, builds + starts everything
```

It prompts for your domain and LLM key, generates a JWT secret, configures Caddy for HTTPS, and starts the stack. After that:

```
sudo ./deploy restart   # rebuild + restart
sudo ./deploy status    # container + Caddy status
sudo ./deploy logs      # follow logs (q to stop)
```

**Not supported**: Vercel, Netlify, Lambda, Fly.io — no Docker socket or privileged container support.

**Backup**: SQLite at `$PROFILES_DIR/platform.db` + the `profiles/` directory.

---

## Tests

```bash
uv run pytest tests/ -v
```

---

## License

Private project.
