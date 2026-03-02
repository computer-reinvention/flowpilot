# FlowPilot

AI-powered browser automation platform with sandboxed execution.

## Prerequisites

- Docker and Docker Compose
- An LLM API key (Gemini, Claude, or OpenAI)

## Quick Start

```bash
git clone https://github.com/youruser/flowpilot.git
cd flowpilot
cp .env.example .env
# Edit .env and set your API key
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080).

## Architecture

```
┌──────────────┐     ┌──────────────┐
│   Frontend   │────>│   Backend    │
│  (port 8080) │     │  (FastAPI)   │
└──────────────┘     └──────┬───────┘
                            │ Docker API
                     ┌──────▼───────┐
                     │   Sandbox    │
                     │  Chromium +  │
                     │  noVNC +     │
                     │  browser-use │
                     └──────────────┘
```

## How It Works

1. **Define Flows** -- Create automation flows for any website. Each flow describes what the browser agent should do, with configurable inputs.

2. **Capture Sessions** -- Use the Chrome extension to capture browser sessions (cookies, localStorage) from any domain. These are injected into sandboxed runs so the agent inherits your logged-in state.

3. **Run** -- Execute a flow with your inputs. The backend spins up a sandboxed Chromium container, restores your session, and uses browser-use (an LLM-driven browser automation agent) to complete the task.

4. **Watch** -- Real-time progress streams over WebSocket. Optionally expand the live browser view to watch the automation in action.

Sandbox containers are ephemeral -- created on demand via the Docker API and destroyed after each run.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | LLM backend: `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | -- | Gemini API key |
| `ANTHROPIC_API_KEY` | -- | Claude API key |
| `OPENAI_API_KEY` | -- | OpenAI API key |
| `VNC_PASSWORD` | `changeme` | Password for noVNC access |
| `PROFILES_DIR` | `/app/profiles` | Directory for saved browser profiles |
| `JWT_SECRET` | (generated) | Secret for JWT token signing |
