# Configuration

All configuration is via environment variables. Set them in `docker-compose.yml` or your shell environment.

## Required

| Variable | Description |
|----------|-------------|
| `LLM_PROVIDER` | LLM backend: `gemini`, `claude`, or `openai` |
| `GOOGLE_API_KEY` | Required when `LLM_PROVIDER=gemini` |
| `ANTHROPIC_API_KEY` | Required when `LLM_PROVIDER=claude` |
| `OPENAI_API_KEY` | Required when `LLM_PROVIDER=openai` |

## Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET` | Auto-generated | Secret for signing JWTs. Must be stable in production. |
| `FLOWPILOT_BASE_URL` | `http://localhost:8080` | Public-facing URL. Used in webhook payloads and API responses. |
| `PROFILES_DIR` | `/app/profiles` | Directory for SQLite DB and Chrome profiles. |
| `PROFILES_HOST_DIR` | Same as `PROFILES_DIR` | Host-side path for Docker volume mounts. |
| `DOCKER_HOST` | Auto-detected | Docker daemon URL (e.g., `tcp://docker-proxy:2375`). |
| `SANDBOX_NETWORK` | `flowpilot_sandbox-net` | Docker network for sandbox containers. |
| `ALLOWED_ORIGINS` | `http://localhost:8080,http://localhost:3000` | CORS allowed origins (comma-separated). |

## LLM Models

| Variable | Default |
|----------|---------|
| `GEMINI_MODEL` | `gemini-3-flash-preview` |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` |
| `OPENAI_MODEL` | `gpt-4o` |

## File Paths

| Path | Description |
|------|-------------|
| `$PROFILES_DIR/platform.db` | SQLite database |
| `$PROFILES_DIR/{user_id}/{profile_name}/` | Saved Chrome profiles |

## Docker Compose Environment

In `docker-compose.yml`, the backend service receives:

```yaml
environment:
  - DOCKER_HOST=tcp://docker-proxy:2375
  - LLM_PROVIDER=gemini
  - GOOGLE_API_KEY=${GOOGLE_API_KEY}
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
  - OPENAI_API_KEY=${OPENAI_API_KEY}
  - JWT_SECRET=${JWT_SECRET}
  - FLOWPILOT_BASE_URL=${FLOWPILOT_BASE_URL:-http://localhost:8080}
  - PROFILES_DIR=/app/profiles
  - PROFILES_HOST_DIR=${PROFILES_HOST_DIR:-./profiles}
  - ALLOWED_ORIGINS=${ALLOWED_ORIGINS:-http://localhost:8080,http://localhost:3000}
```

The `docker-proxy` service restricts Docker socket access to only container and network operations (`CONTAINERS=1`, `POST=1`, `NETWORKS=1`).
