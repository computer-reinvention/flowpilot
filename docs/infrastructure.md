# Sandbox & Infrastructure

## Docker Architecture

FlowPilot uses Docker containers as isolated browser sandboxes. Each run gets its own container with Chromium, a virtual display, and CDP access.

### Images

#### `flowpilot-sandbox` (Dockerfile.sandbox)

The sandbox image runs Chromium in an isolated environment.

**Base**: `python:3.12-slim`

**System packages**: chromium, Xvfb, socat, fonts (CJK, emoji, etc.)

**Services** (managed by supervisord):
- **Xvfb** — Virtual display on `:1`
- **Chromium** — Launches with `--remote-debugging-port=9222` (localhost only)
- **socat** — Forwards `0.0.0.0:9223` → `127.0.0.1:9222` (makes CDP accessible on the Docker network)

**User**: `sandboxuser` (non-root)

**Ports**:
- 9222 — Direct Chromium CDP (localhost only)
- 9223 — socat-forwarded CDP (network accessible)

**Health check**: `curl http://localhost:9222/json/version`

#### `flowpilot-backend` (Dockerfile.backend)

Multi-stage build:
1. **Frontend stage**: Node 20, builds React app to `dist/`
2. **Backend stage**: Python 3.12, installs deps via uv, copies frontend dist
3. Runs `uvicorn backend.main:app` on port 8080

### docker-compose.yml

```yaml
services:
  docker-proxy:
    image: tecnativa/docker-socket-proxy
    volumes: [/var/run/docker.sock:/var/run/docker.sock]
    environment:
      CONTAINERS: 1
      POST: 1
      NETWORKS: 1

  backend:
    build: . (Dockerfile.backend)
    ports: ["127.0.0.1:8080:8080"]
    volumes: ["./profiles:/app/profiles"]
    environment:
      DOCKER_HOST: tcp://docker-proxy:2375
      LLM_PROVIDER: gemini
      # ... other env vars
    depends_on: [docker-proxy]
```

**Network**: `flowpilot_sandbox-net` — All services and dynamically created sandbox containers join this network.

## Networking

```
                Internet
                   │
              ┌────┴────┐
              │  Caddy   │  (HTTPS termination, production only)
              └────┬────┘
                   │ :8080
              ┌────┴────┐
              │ Backend  │
              └────┬────┘
                   │ Docker network (flowpilot_sandbox-net)
         ┌────────┼────────┐
         │        │        │
    ┌────┴───┐ ┌──┴───┐ ┌──┴──────────┐
    │docker- │ │sand- │ │ sandbox-N   │
    │proxy   │ │box-1 │ │             │
    └────────┘ └──────┘ └─────────────┘
```

- **Backend** connects to sandbox CDP via Docker DNS: `ws://flowpilot-{sandbox_id}:9223`
- **docker-socket-proxy** restricts Docker API access (only container + network operations)
- No ports are published from sandbox containers — CDP is only reachable via the Docker network

## Container Lifecycle

### Start

1. `SandboxManager.start(profile_path, user_id)` called
2. Random 12-char `sandbox_id` generated
3. `docker run flowpilot-sandbox` as `flowpilot-{sandbox_id}` on `flowpilot_sandbox-net`
4. If profile exists: `docker cp` Chrome profile into container at `/tmp/chrome-profile`
5. Container record saved to DB
6. Backend polls `{cdp_url}/json/version` until Chromium is ready (up to 40s)

### Stop

1. `SandboxManager.stop(sandbox_id)` called
2. Container stopped and removed
3. In-memory tracking cleaned up
4. DB record deleted

### Reconciliation (on backend startup)

1. Load sandbox records from DB
2. Check each container's actual status via Docker API
3. Remove records for dead containers
4. Clean up any orphan `flowpilot-*` containers not in the DB

## Resource Limits

Each sandbox container is constrained:

| Resource | Limit |
|----------|-------|
| Memory | 1 GB |
| CPU quota | 100,000 (1 core) |
| PIDs | 256 |
| Shared memory | 256 MB |

## Production Deployment

### `deploy` Script

A bash script for server setup and management.

**Commands**:
- `sudo ./deploy setup` — First-time setup: install Docker, Caddy, generate JWT secret, configure HTTPS, build and start
- `sudo ./deploy restart` — Rebuild images and restart services
- `sudo ./deploy status` — Show service status
- `sudo ./deploy logs` — Follow compose logs

### `dev` Script

Local development helper.

**Commands**:
- `./dev stop` — Kill all containers, clean networks
- `./dev build-fe` — Build frontend (`npm run build`)
- `./dev build` — Docker build sandbox + backend images
- `./dev up` — Start compose stack
- `./dev restart` — Full rebuild cycle: stop → build-fe → build → up → logs
- `./dev logs` — Follow compose logs
- `./dev url` — Print app URL

### Local Development

**Full stack**:
```bash
./dev restart
```

**Backend only**:
```bash
uv sync
uv run uvicorn backend.main:app --reload --port 8080
```

**Frontend only** (proxies API/WS to backend):
```bash
cd frontend
npm install && npm run dev  # Vite on port 3000
```
