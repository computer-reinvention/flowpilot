# FlowPilot

AI-powered browser automation platform. Define automation flows for any website, capture authenticated sessions via Chrome extension, and execute them in sandboxed Chromium containers with live browser viewing.

Uses [browser-use](https://github.com/browser-use/browser-use) — an LLM-driven browser automation agent — to translate natural language task descriptions into browser actions.

## How It Works

1. **Define Flows** — Create automation flows for any website. Each flow has a name, target domain, natural language description of what the agent should do, and configurable input fields (text, select, textarea, etc.).

2. **Capture Sessions** — Install the Chrome extension to capture browser sessions (cookies, localStorage, sessionStorage) from any domain. These are injected into sandbox runs so the agent inherits your logged-in state.

3. **Run** — Execute a flow with your inputs. The backend spins up a sandboxed Chromium container via Docker, restores your captured session, and the LLM agent navigates the site to complete the task.

4. **Watch** — Real-time progress streams over WebSocket. Expand the live noVNC browser view to watch the automation in action.

Sandbox containers are ephemeral — created on demand via the Docker API and destroyed after each run.

## Architecture

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────────┐
│  React UI   │─────>│  FastAPI Backend  │─────>│  Docker Socket      │
│  (Vite)     │ REST │  (port 8080)      │      │  Proxy (limited)    │
│  port 3000* │<─────│                   │      └─────────┬───────────┘
└─────────────┘  WS  │  - Auth (JWT)     │                │
                      │  - Flows CRUD     │      ┌─────────▼───────────┐
┌─────────────┐      │  - Runs engine    │      │  Sandbox Container  │
│  Chrome     │─────>│  - Sessions       │      │  - Chromium + CDP   │
│  Extension  │ POST │  - Profiles       │      │  - Xvfb + VNC       │
└─────────────┘      └──────────────────┘      │  - noVNC (web view) │
                                                │  - browser-use      │
                                                └─────────────────────┘
* port 3000 in dev (Vite proxy to 8080), port 8080 in production (static files served by FastAPI)
```

## Prerequisites

- **Python 3.12+**
- **Node.js 20+** and npm
- **Docker** and Docker Compose (for sandboxed browser execution)
- **An LLM API key** — one of: Google Gemini, Anthropic Claude, or OpenAI

## Setup

### 1. Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# Choose your LLM provider: gemini, claude, or openai
LLM_PROVIDER=gemini

# Set the API key for your chosen provider
GOOGLE_API_KEY=your-key-here        # if LLM_PROVIDER=gemini
ANTHROPIC_API_KEY=your-key-here     # if LLM_PROVIDER=claude
OPENAI_API_KEY=your-key-here        # if LLM_PROVIDER=openai

# Optional
VNC_PASSWORD=changeme               # Password for noVNC browser view
JWT_SECRET=some-random-secret       # Token signing (auto-generated if unset, but won't survive restarts)
PROFILES_DIR=./profiles             # Where browser profiles are stored (default: /app/profiles in Docker)
```

### 2. Backend (Local Development)

```bash
# Install Python dependencies
pip install -r backend/requirements.txt

# Install Playwright browser (needed for browser-use)
playwright install chromium
playwright install-deps chromium

# Start the backend (port 8080)
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8080
```

### 3. Frontend (Local Development)

```bash
cd frontend
npm install
npm run dev     # Starts Vite dev server on port 3000, proxies /api and /ws to :8080
```

Open [http://localhost:3000](http://localhost:3000).

### 4. Docker (Production)

```bash
# Build sandbox image first
docker build -t flowpilot-sandbox -f Dockerfile.sandbox .

# Start everything
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080). The backend serves the built frontend as static files.

### 5. Chrome Extension

1. Open `chrome://extensions/` in Chrome
2. Enable "Developer mode" (top right)
3. Click "Load unpacked" and select the `extension/` directory
4. Navigate to any website, click the extension icon, and capture your session

Captured sessions are sent to the backend and automatically used when running flows for that domain.

## API Endpoints

### Auth
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/signup` | No | Create account (email, password, name) |
| POST | `/api/auth/login` | No | Login, returns JWT token |
| GET | `/api/auth/me` | Yes | Current user info |

### Flows
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/flows` | No | List all flows |
| GET | `/api/flows/{id}` | No | Get flow details |
| POST | `/api/flows` | Yes | Create flow (name, domain, description, inputs, entry_url) |
| PUT | `/api/flows/{id}` | Yes | Update flow |
| DELETE | `/api/flows/{id}` | Yes | Delete flow |

### Runs
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/runs` | Yes | Start a run (flow_id, inputs) → returns run_id + noVNC URL |
| GET | `/api/runs` | Yes | List user's runs |
| GET | `/api/runs/{id}` | Yes | Get run details (status, steps, result) |
| POST | `/api/runs/{id}/cancel` | Yes | Cancel a running run |

### Sessions
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/sessions/capture` | Yes | Capture browser session (from extension) |
| GET | `/api/sessions` | Yes | List captured sessions |
| DELETE | `/api/sessions/{id}` | Yes | Delete a session |

### Sandbox
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/sandbox/start` | Yes | Start a sandbox container (noVNC + CDP) |
| POST | `/api/sandbox/stop` | Yes | Stop a sandbox container |

### Profiles
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/profile/save` | Yes | Save browser profile from sandbox |
| GET | `/api/profile/status` | Yes | Check profile status |

### Other
| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/health` | No | Health check |
| WS | `/ws/runs/{run_id}` | No | Real-time run progress stream |

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `gemini` | LLM backend: `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | — | Gemini API key (required if provider=gemini) |
| `ANTHROPIC_API_KEY` | — | Claude API key (required if provider=claude) |
| `OPENAI_API_KEY` | — | OpenAI API key (required if provider=openai) |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model name |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Claude model name |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `VNC_PASSWORD` | `changeme` | Password for noVNC browser view |
| `JWT_SECRET` | (generated) | Secret for JWT token signing |
| `PROFILES_DIR` | `/app/profiles` | Browser profile storage directory |
| `ALLOWED_ORIGINS` | `http://localhost:8080,http://localhost:3000` | CORS allowed origins |
| `DOCKER_HOST` | — | Docker daemon URL (auto-detected if unset) |

## Security

- **Sandbox isolation**: Each run gets its own Docker container with resource limits (1GB RAM, 256 PIDs, 256MB shared memory)
- **No raw Docker socket**: Backend communicates via docker-socket-proxy with limited API access (containers + networks only)
- **CDP not exposed**: Chrome DevTools Protocol port (9222) is only accessible on the internal Docker network, never published to the host
- **Per-user sandbox limit**: Maximum 2 concurrent sandboxes per user
- **Per-sandbox VNC password**: Each sandbox gets a unique random VNC password
- **JWT auth**: All mutation endpoints require authentication
- **Profile path validation**: Profile names are validated against `^[a-zA-Z0-9_-]+$` to prevent path traversal
- **Tar extraction safety**: Archive extraction checks for path traversal (`..` and absolute paths)

## Hosting

FlowPilot requires Docker-in-Docker (or Docker socket access) for sandbox containers. Recommended options for self-hosting:

### VPS / Dedicated Server (Recommended)

Best fit for a personal tool with Docker requirements.

| Provider | Spec | ~Cost/mo | Notes |
|----------|------|----------|-------|
| **Hetzner Cloud** | CX32 (4 vCPU, 8GB) | $7 | Best value, EU/US datacenters |
| **DigitalOcean** | Basic Droplet (4GB) | $24 | Simple setup, good docs |
| **Linode (Akamai)** | Shared 4GB | $24 | Reliable, good network |
| **Oracle Cloud** | Ampere A1 (4 core, 24GB) | Free tier | ARM — Docker works, generous free allowance |

Setup: install Docker, clone repo, `docker compose up -d`. Use Caddy or nginx for HTTPS.

### Railway / Render

Can work but need Docker builder support. Railway supports Dockerfiles natively. Render supports Docker but may have limitations with Docker-in-Docker for sandbox containers.

### Not Recommended

- **Vercel/Netlify**: No Docker, no long-running processes
- **AWS Lambda / Cloud Functions**: No Docker socket, no persistent containers
- **Fly.io**: Possible but Docker-in-Docker requires privileged containers which Fly doesn't support by default

### Storage

SQLite database stored at `PROFILES_DIR/platform.db`. Back up this file and the `profiles/` directory for data persistence across redeployments.

## Running Tests

```bash
pip install pytest pytest-asyncio httpx
cd /home/cmux/flowpilot
python -m pytest tests/ -v
```

## License

Private project.
