# Frontend

The frontend is a React single-page application providing the UI for managing flows, watching runs, and interacting with the browser during HITL pauses.

**Stack**: React 19, Vite, TypeScript, Tailwind CSS 4, shadcn/ui (Radix UI)

**Entry point**: `frontend/src/main.tsx`
**Dev port**: 3000 (proxies to backend on 8080)
**Production**: Built to `frontend/dist/`, served by the backend

## Application Structure

### `App.tsx` — Main Container

Manages top-level state and routing:

- **Auth state**: Login/signup views via `useAuth()`
- **Navigation**: `activeSection` switches between flows, runs, sessions, settings
- **Run tracking**: `activeRunId` drives the FlowRunner view
- **Auto-reconnect**: On load, reconnects to first active run if any exist

**View logic**:
- Not authenticated → `LoginForm` / `SignupForm`
- Authenticated → Sidebar + content area
- Active run selected → `FlowRunner` (browser canvas + steps panel)

### `lib/api.ts` — HTTP Client

```typescript
function getToken(): string | null    // from localStorage
function setToken(token: string): void
function clearToken(): void

async function apiFetch<T>(path, options): Promise<T>
// Adds Authorization header, throws ApiError on non-2xx
```

### `lib/types.ts` — Type Definitions

```typescript
interface User { id, email, name }
interface Flow { id, name, domain, description, inputs, entry_url, icon, created_by, created_at }
interface Run  { id, flow_id, user_id, status, inputs, sandbox_id, steps, ... }
interface RunStep { status, message, timestamp }
interface ApiKey  { id, user_id, name, key_prefix, created_at, last_used_at, key? }
interface Webhook { id, user_id, url, events, enabled, created_at, secret? }
interface Session { id, user_id, domain, profile_name, captured_at }

type RunStatus = "pending" | "connecting" | "starting" | "running"
              | "paused_for_hitl" | "completed" | "failed" | "cancelled"
```

## Hooks

### `useAuth()`

```typescript
{
  user: User | null,
  loading: boolean,
  error: string | null,
  login(email, password): Promise<void>,
  signup(email, password, name): Promise<void>,
  logout(): void
}
```

Token stored in localStorage for persistence across page reloads.

### `useRun()` — Single Run State

```typescript
{
  runId: string | null,
  sandboxId: string | null,
  status: string,
  steps: RunStep[],
  currentStep: string | null,
  error: string | null,

  startRun(flowId, inputs, profileId?): Promise<string | null>,
  reconnectToRun(runId): Promise<void>,
  resumeRun(): Promise<void>,
  cancelRun(): Promise<void>,
  reset(): void
}
```

Maintains a WebSocket connection to `/ws/runs/{run_id}` for real-time progress. On `paused_for_hitl`, sends a browser notification and flashes the page title. Auto-reconnects on disconnect; closes on terminal status.

### `useRuns()` — Run History

```typescript
{
  runs: Run[],
  activeRuns: Run[],  // status in active set
  loading: boolean,
  error: string | null,
  refetch(): Promise<void>
}
```

### `useFlows()` — Flow CRUD

```typescript
{
  flows: Flow[],
  loading: boolean,
  error: string | null,
  createFlow(data): Promise<void>,
  updateFlow(id, data): Promise<void>,
  deleteFlow(id): Promise<void>,
  refetch(): Promise<void>
}
```

### `useSessions()` — Session Management

```typescript
{
  sessions: Session[],
  loading: boolean,
  deleteSession(sessionId): Promise<void>,
  refetch(): Promise<void>
}
```

## Components

### `BrowserCanvas`

The core component for live browser viewing and interaction.

**Props**:
```typescript
{
  sandboxId: string | null,
  isPaused?: boolean,   // highlight border when HITL
  className?: string
}
```

**How it works**:
1. Opens WebSocket to `/ws/browser/{sandbox_id}?token={jwt}`
2. Receives base64 JPEG frames → converts to `ImageBitmap` → draws on `<canvas>`
3. Captures mouse/keyboard events → sends as JSON to WebSocket
4. Displays URL bar from `Page.frameNavigated` events
5. Browser toolbar with back/forward/refresh buttons and loading indicator

**Canvas dimensions**: 1920x1080 (matches browser-use viewport, 1:1 coordinate mapping)

**Reconnect**: Exponential backoff (1s → 2s → 4s → 8s cap)

**Input mapping**:
- Mouse: `mousemove`, `mousedown`, `mouseup` → CDP `Input.dispatchMouseEvent`
- Keyboard: `keydown`, `keyup` → CDP `Input.dispatchKeyEvent`
- Modifier bits: `alt=1, ctrl=2, meta=4, shift=8`

### `FlowRunner`

Split-pane run interface using `ResizablePanel` (60/40 split).

- **Left pane**: `BrowserCanvas` (live browser stream)
- **Right pane**: Steps list, status badge, HITL banner, action buttons

**HITL banner**: Parses `[category]` from the pause reason and displays it prominently.

**Action buttons**:
- Resume (visible when paused_for_hitl)
- Cancel (visible when not in terminal state)
- Back/Hide

### `FlowGrid`

Card grid displaying available flows. Each card shows icon, name, domain, description. Click to select a flow and open `FlowForm`.

### `FlowForm`

Renders dynamic input fields based on flow definition. Supports text, select, textarea input types. Submit triggers run creation.

### `CreateFlowForm`

Form for creating new flows with name, domain, description, entry URL, and input definitions.

### `RunHistory`

Table of past runs with status badges, flow name, timestamps. Click a run to reconnect and view it.

### `SessionList`

List of captured browser sessions (domain, profile name, timestamp) with delete buttons.

### `SettingsPanel`

Tabbed settings page with:
- **API Keys tab** (`ApiKeySettings`): Create/list/revoke API keys. Raw key shown once on creation.
- **Webhooks tab** (`WebhookSettings`): Create/list/delete webhooks. Test delivery button.

### Layout Components

- `AppShell` — Wrapper around sidebar + content area
- `AppSidebar` — Navigation links, active runs badge with spinner, user menu

## Build Configuration

`vite.config.ts`:
- Plugins: React, Tailwind CSS
- Path alias: `@` → `./src`
- Dev proxy: `/api` → `http://localhost:8080`, `/ws` → `ws://localhost:8080`
- Build output: `frontend/dist/`
