# Architecture Overview

## System Design

FlowPilot is a self-hosted browser automation platform. An AI agent (browser-use) executes user-defined flows inside isolated Docker containers running Chromium. Users watch the browser live via CDP screencast and can intervene when the agent requests human help (HITL).

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (React)                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Flow UI  │  │ Run UI   │  │ Settings │  │ BrowserCanvas │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│       │              │             │                │           │
│       └──────────────┴─────────────┴────────────────┘           │
│                    HTTP + WebSocket                              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────┴──────────────────────────────────────┐
│                     Backend (FastAPI)                            │
│  ┌──────┐  ┌────────┐  ┌───────────┐  ┌──────────┐  ┌───────┐ │
│  │ Auth │  │ Runner │  │ CDP Proxy │  │ Webhooks │  │  DB   │ │
│  └──────┘  └───┬────┘  └─────┬─────┘  └──────────┘  └───────┘ │
│           ┌────┴────┐        │                                  │
│           │Explorer │        │                                  │
│           └────┬────┘        │                                  │
│                │              │                                  │
└────────────────┼──────────────┼──────────────────────────────────┘
                 │              │
       ┌─────────┴──────────────┴─────────┐
       │      Docker Network              │
       │  ┌───────────────────────────┐   │
       │  │  Sandbox Container        │   │
       │  │  ┌─────────┐  ┌───────┐  │   │
       │  │  │Chromium  │  │ socat │  │   │
       │  │  │ (CDP)    │──│ proxy │  │   │
       │  │  └─────────┘  └───────┘  │   │
       │  └───────────────────────────┘   │
       └──────────────────────────────────┘
```

## Core Components

### Backend (Python/FastAPI)

| Module | Purpose |
|--------|---------|
| `main.py` | API routes, WebSocket handlers, app lifecycle |
| `runner.py` | Three-tier execution: cached JS → auto-generate → agent loop |
| `explorer.py` | Flow exploration: stage tree discovery + reflex generation |
| `cdp_proxy.py` | WebSocket proxy for live browser streaming + input relay |
| `sandbox.py` | Docker container lifecycle management |
| `auth.py` | JWT + API key authentication |
| `db.py` | SQLite CRUD layer (aiosqlite) |
| `webhooks.py` | Event delivery engine with HMAC signing |
| `profile.py` | Chrome profile save/restore (Docker SDK) |
| `trace.py` | Optional reflex execution tracing |
| `models.py` | Data classes (Flow, Run, Session) |
| `config.py` | Environment-based configuration |

### Frontend (React/Vite)

| Area | Purpose |
|------|---------|
| `BrowserCanvas` | Canvas rendering of CDP screencast + mouse/keyboard relay |
| `FlowRunner` | Split-pane run UI (browser left, steps right) |
| `FlowDetail` | Flow detail view with persistent context-aware side panel |
| `FlowGrid` / `FlowForm` | Flow browsing and execution |
| `Settings` | API key and webhook management |
| Hooks (`useRun`, `useFlows`, etc.) | State management via custom React hooks |

### Sandbox (Docker)

Each run gets an isolated container with:
- Chromium with remote debugging (CDP on port 9222)
- Xvfb virtual display
- socat forwarding CDP to port 9223
- Resource limits (1GB RAM, 256 PIDs, 256MB shm)

## Key Data Flow

### Starting a Run

```
User clicks "Run" in UI
    → POST /api/runs {flow_id, inputs}
    → Backend creates Run record (status: pending)
    → Backend starts sandbox container
    → Backend waits for CDP readiness (polls /json/version)
    → Fast path: execute cached JS reflexes via CDP Runtime.evaluate
    → If no reflexes: generate one from live page DOM via LLM
    → If all else fails: create browser-use Agent with custom tools
    → Progress updates via WS /ws/runs/{run_id}
    → Browser frames via WS /ws/browser/{sandbox_id}
```

### Human-in-the-Loop (HITL)

```
Agent encounters blocker (login wall, CAPTCHA, etc.)
    → Agent calls request_human_help(reason, category)
    → Run status → paused_for_hitl
    → Frontend shows HITL banner + browser notification
    → User interacts with browser directly via canvas
    → User clicks "Resume"
    → POST /api/runs/{id}/resume
    → Agent continues from current browser state
```

### Flow Exploration

```
User clicks "Explore" on a flow
    → Backend starts sandbox, connects explorer agent
    → Agent navigates entry_url, discovers stages
    → At each stage: generates JS reflex via fast LLM
    → Tests each reflex on the live page
    → Builds stage tree + saves reflexes to DB
    → Auto-extracts input declarations from scripts
```

### Reflex Execution (Fast Path)

```
Run starts → load domain reflexes from DB
    → For each reflex (ordered by exploration tree):
        Execute JS via CDP Runtime.evaluate
        ↓ success → continue to next reflex
        ↓ failure → repair via LLM (up to 2 attempts)
        ↓ still failing → increment failure count
        ↓ 3 cumulative failures → bail to full agent
```

## Design Principles

1. **Three-tier execution** — Cached JS first, auto-generated JS second, full agent last. Warm runs use zero LLM calls.
2. **Reflexes are JavaScript** — Complete async arrow functions executed via CDP `Runtime.evaluate`. Not text descriptions.
3. **No Playwright layer** — browser-use's built-in tools are used directly. No separate executor.
4. **CDP coexistence** — The CDP proxy attaches a flat session alongside browser-use's session. No conflicts.
5. **Async throughout** — FastAPI, aiosqlite, asyncio. No blocking calls.
6. **Container isolation** — No direct Docker socket access. docker-socket-proxy restricts API surface.
7. **Profile scoping** — `profile_id` on runs allows multiple identities per user.
8. **Browser-use defaults** — No custom wait times, vision settings, or history limits in the runner.
