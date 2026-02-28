# uber-bookr

Self-hosted Uber booking automation with browser sandbox and profile persistence.

## Prerequisites

- Docker and Docker Compose
- An LLM API key (Gemini, Claude, or OpenAI)

## Quick Start

```bash
git clone https://github.com/youruser/uber-bookr.git
cd uber-bookr
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

1. **Connect** -- Click "Connect Uber" to launch a sandboxed Chromium browser. Log in to your Uber account through the embedded noVNC viewer. Your browser profile (cookies, session) is saved to disk.

2. **Book** -- Enter pickup and drop-off locations, hit "Book Ride". The backend spins up a fresh sandbox, restores your saved profile, and uses browser-use (an LLM-driven browser automation agent) to navigate Uber's web app and complete the booking.

3. **Watch** -- Real-time progress streams over WebSocket. Optionally expand the live browser view to watch the automation in action.

Sandbox containers are ephemeral -- created on demand via the Docker API and destroyed after each session.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | LLM backend: `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | -- | Gemini API key |
| `ANTHROPIC_API_KEY` | -- | Claude API key |
| `OPENAI_API_KEY` | -- | OpenAI API key |
| `VNC_PASSWORD` | `bookr` | Password for noVNC access |
| `PROFILES_DIR` | `/app/profiles` | Directory for saved browser profiles |
