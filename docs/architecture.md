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
| `runner.py` | AI agent orchestration with browser-use |
| `cdp_proxy.py` | WebSocket proxy for live browser streaming + input relay |
| `sandbox.py` | Docker container lifecycle management |
| `auth.py` | JWT + API key authentication |
| `db.py` | SQLite CRUD layer (aiosqlite) |
| `webhooks.py` | Event delivery engine with HMAC signing |
| `profile.py` | Chrome profile save/restore from containers |
| `models.py` | Data classes (Flow, Run, Session) |
| `config.py` | Environment-based configuration |

### Frontend (React/Vite)

| Area | Purpose |
|------|---------|
| `BrowserCanvas` | Canvas rendering of CDP screencast + mouse/keyboard relay |
| `FlowRunner` | Split-pane run UI (browser left, steps right) |
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
    → Backend creates browser-use Agent with custom tools
    → Agent begins executing against entry_url
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

### Reflex System

```
Agent receives task
    → Calls search_reflexes(use_case) to find cached steps
    → If found: calls run_reflex(name) → follows step-by-step instructions
    → If not found: executes task normally using browser-use tools
    → After success: calls save_reflex() to cache steps for future runs
```

## Design Principles

1. **No Playwright layer** — browser-use's built-in tools are used directly. No separate executor.
2. **Reflexes are text, not code** — Step descriptions that the agent interprets, not executable scripts.
3. **CDP coexistence** — The CDP proxy attaches a flat session alongside browser-use's session. No conflicts.
4. **Async throughout** — FastAPI, aiosqlite, asyncio. No blocking calls.
5. **Container isolation** — No direct Docker socket access. docker-socket-proxy restricts API surface.
6. **Profile scoping** — `profile_id` on runs allows multiple identities per user.
