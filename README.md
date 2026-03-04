# FlowPilot

AI-powered browser automation platform. Define flows for any website, capture authenticated sessions via Chrome extension, and execute them in sandboxed Chromium containers with a live interactive browser view.

Uses [browser-use](https://github.com/browser-use/browser-use) for LLM-driven browser automation and [cdp-use](https://github.com/browser-use/cdp-use) for the live browser canvas.

## How It Works

1. **Define Flows** — Create automation flows for any website. Each flow has a name, target domain, natural language description, and configurable input fields (text, select, textarea, date, etc.).

2. **Capture Sessions** — Install the Chrome extension to capture browser sessions (cookies, localStorage) from any domain. These are injected into sandbox runs so the agent starts authenticated.

3. **Run** — Execute a flow with your inputs. The backend spins up an ephemeral sandboxed Chromium container, restores your session, and the LLM agent navigates the site. Reflexes (cached Playwright snippets built from previous runs) let the agent skip re-learning steps it has already solved.

4. **Watch & Interact** — A live canvas streams the browser via CDP screencast at native viewport resolution. During HITL pauses (login walls, captchas) you take over directly — full mouse and keyboard interaction with the running browser.

5. **HITL (Human-in-the-Loop)** — When the agent hits an auth wall it can't bypass, it pauses and notifies you. You log in directly in the live canvas, click Resume, and the agent continues with the authenticated session automatically saved.

## Architecture

```
┌─────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
│  React UI   │─────>│  FastAPI Backend      │─────>│  Docker Socket Proxy │
│  (Vite)     │ REST │  (port 8080)          │      └──────────┬───────────┘
│  port 3000* │<─────│                       │                 │
└─────────────┘  WS  │  - Auth (JWT)         │      ┌──────────▼───────────┐
                      │  - Flows CRUD         │      │  Sandbox Container   │
┌─────────────┐      │  - Runs engine        │      │  - Chromium 1920×1080│
│  Chrome     │─────>│  - Reflexes (SQLite)  │      │  - CDP (port 9222)   │
│  Extension  │ POST │  - Sessions           │      │  - socat forwarder   │
└─────────────┘      │  - Profiles           │      │  - Xvfb + SwiftShader│
                      │  - CDP proxy (WS)     │◄─────│  - browser-use agent │
                      └──────────────────────┘  CDP  └──────────────────────┘
```

The CDP proxy (`/ws/browser/{sandbox_id}`) connects to Chromium via a flat `Target.attachToTarget` session that coexists with browser-use's Playwright session — both can use the browser simultaneously without conflict.

`*` port 3000 in dev (Vite proxies `/api` and `/ws` to :8080), port 8080 in production (FastAPI serves the built frontend as static files).

## Features

- **Reflexes** — The agent builds a library of reusable Playwright snippets scoped by domain (e.g. `uber.com/fill_booking_form`). On subsequent runs it fires reflexes for steps it recognises, skipping slow LLM-driven interaction. When a reflex breaks it handles the step manually and updates the cached version.
- **Live browser canvas** — CDP screencast at native 1920×1080 viewport. No VNC, no iframe — direct pixel-accurate stream with ~1-frame latency.
- **Interactive HITL** — Full mouse (click, drag, scroll) and keyboard input forwarded to the remote browser via `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent`.
- **Session capture** — Chrome extension captures cookies and localStorage; injected before each run so the agent starts logged in.
- **Profile persistence** — Save the full Chrome profile out of a sandbox and restore it on future runs for longer-lived auth state.

## Prerequisites

- **Python 3.12+** with [uv](https://docs.astral.sh/uv/)
- **Node.js 20+** and npm
- **Docker** and Docker Compose
- **An LLM API key** — Google Gemini, Anthropic Claude, or OpenAI

## Setup

### 1. Environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# LLM provider: gemini, claude, or openai
LLM_PROVIDER=gemini
GOOGLE_API_KEY=your-key-here

# Model override (optional — defaults shown)
# GEMINI_MODEL=gemini-3-flash-preview      # or gemini-3.1-pro-preview
# ANTHROPIC_MODEL=claude-sonnet-4-20250514
# OPENAI_MODEL=gpt-4o

# Token signing (auto-generated if unset, but tokens won't survive restarts)
JWT_SECRET=some-random-secret
```

### 2. Dev utility

```bash
chmod +x dev
./dev          # shows available commands
```

```
Commands:
  stop       Stop all containers (force-kills stuck ones too)
  build-fe   Rebuild the frontend only
  build      Rebuild sandbox + backend Docker images
  up         Start all containers
  restart    Full cycle: stop → build-fe → build → up → logs
  logs       Follow logs (press q to stop)
  url        Print the app URL
```

### 3. First run

```bash
./dev restart
```

Open [http://localhost:8080](http://localhost:8080).

### 4. Local backend development

```bash
uv sync
uv run playwright install chromium && uv run playwright install-deps
uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8080
```

Frontend dev server (with hot reload):

```bash
cd frontend && npm install && npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

### 5. Chrome Extension

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. **Load unpacked** → select the `extension/` directory
4. Navigate to any website, click the extension icon, capture your session

## API Reference

### Auth
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/signup` | — | Create account |
| POST | `/api/auth/login` | — | Login → JWT token |
| GET | `/api/auth/me` | ✓ | Current user |

### Flows
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/flows` | — | List all flows |
| GET | `/api/flows/{id}` | — | Get flow |
| POST | `/api/flows` | ✓ | Create flow |
| PUT | `/api/flows/{id}` | ✓ | Update flow |
| DELETE | `/api/flows/{id}` | ✓ | Delete flow |

### Runs
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/runs` | ✓ | Start a run → `run_id`, `sandbox_id` |
| GET | `/api/runs` | ✓ | List runs |
| GET | `/api/runs/{id}` | ✓ | Run details (status, steps, result) |
| POST | `/api/runs/{id}/cancel` | ✓ | Cancel run |
| POST | `/api/runs/{id}/pause` | ✓ | Manual pause |
| POST | `/api/runs/{id}/resume` | ✓ | Resume after HITL login |

### Sessions & Profiles
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/sessions/capture` | ✓ | Capture session (from extension) |
| GET | `/api/sessions` | ✓ | List sessions |
| DELETE | `/api/sessions/{id}` | ✓ | Delete session |
| POST | `/api/profile/save` | ✓ | Save Chrome profile from sandbox |
| GET | `/api/profile/status` | ✓ | Profile status |

### WebSockets
| Endpoint | Description |
|----------|-------------|
| `WS /ws/runs/{run_id}` | Real-time run progress (status, steps) |
| `WS /ws/browser/{sandbox_id}?token=` | Live CDP browser canvas stream + input |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `gemini` | `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | — | Required if `LLM_PROVIDER=gemini` |
| `ANTHROPIC_API_KEY` | — | Required if `LLM_PROVIDER=claude` |
| `OPENAI_API_KEY` | — | Required if `LLM_PROVIDER=openai` |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | Gemini model |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Claude model |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model |
| `JWT_SECRET` | (generated) | JWT signing secret |
| `PROFILES_DIR` | `/app/profiles` | Profile + DB storage directory |
| `ALLOWED_ORIGINS` | `http://localhost:8080,...` | CORS origins |
| `DOCKER_HOST` | (auto) | Docker daemon URL |
| `SANDBOX_NETWORK` | `flowpilot_sandbox-net` | Docker network for sandboxes |

## Security

- **Sandbox isolation** — Each run gets its own container with resource limits (1GB RAM, 100k CPU quota, 256 PIDs, 256MB shared memory)
- **No raw Docker socket** — Backend talks through docker-socket-proxy (containers + networks API only)
- **CDP not exposed to host** — Port 9222 is internal-only; the socat forwarder (9223) is also internal to the Docker network
- **CDP proxy authentication** — `/ws/browser/{id}` requires a JWT via `?token=` query param
- **Per-user sandbox limit** — Max 2 concurrent sandboxes per user
- **JWT auth** — All mutation endpoints require authentication
- **Profile path validation** — `^[a-zA-Z0-9_-]+$` prevents path traversal

## Hosting

FlowPilot requires Docker socket access for sandbox containers.

| Provider | Spec | ~Cost/mo | Notes |
|----------|------|----------|-------|
| **Hetzner Cloud** | CX32 (4 vCPU, 8GB) | $7 | Best value |
| **DigitalOcean** | Basic 4GB Droplet | $24 | Simple setup |
| **Oracle Cloud** | Ampere A1 (4 core, 24GB) | Free tier | ARM, generous allowance |

Setup: install Docker, clone repo, `./dev restart`. Use Caddy or nginx for HTTPS.

**Not recommended**: Vercel, Netlify, Lambda, Fly.io — no Docker socket or privileged container support.

**Storage**: SQLite DB at `$PROFILES_DIR/platform.db`. Back up that file and the `profiles/` directory.

## Running Tests

```bash
uv run pytest tests/ -v
```

## License

Private project.
