# Database Schema

FlowPilot uses SQLite via aiosqlite for non-blocking database access. The database file is located at `$PROFILES_DIR/platform.db`.

All CRUD operations are in `backend/db.py`. Tables are created on startup via `init_db()`.

## Tables

### users

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT,
    created_at TEXT NOT NULL
);
```

### flows

```sql
CREATE TABLE flows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    description TEXT NOT NULL,
    entry_url TEXT,
    inputs TEXT DEFAULT '[]',       -- JSON array of input definitions
    icon TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

The `inputs` field is a JSON array defining the flow's input parameters. Each input has a `name`, `type` (text, select, textarea), and optional `options` for select types.

### runs

```sql
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    inputs TEXT DEFAULT '{}',       -- JSON dict of input values
    sandbox_id TEXT,
    steps TEXT DEFAULT '[]',        -- JSON array of {status, message, timestamp}
    current_step TEXT,
    result TEXT,                    -- JSON result on completion
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    profile_id TEXT DEFAULT '',     -- Scopes sessions per entity
    FOREIGN KEY (flow_id) REFERENCES flows(id)
);
```

**Status values**: `pending`, `connecting`, `running`, `paused_for_hitl`, `completed`, `failed`, `cancelled`

**Active statuses**: `pending`, `connecting`, `running`, `paused_for_hitl`

**Terminal statuses**: `completed`, `failed`, `cancelled`

The `profile_id` field defaults to `user_id` and allows multiple identities per user (e.g., test accounts on the same domain).

### sessions

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    cookies TEXT DEFAULT '[]',          -- JSON array
    local_storage TEXT DEFAULT '{}',    -- JSON dict
    session_storage TEXT DEFAULT '{}',  -- JSON dict
    user_agent TEXT,
    profile_name TEXT DEFAULT 'default',
    captured_at TEXT NOT NULL
);
```

Sessions are captured via the Chrome extension and matched by `(user_id, domain)` when starting runs.

### reflexes

```sql
CREATE TABLE reflexes (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    script TEXT NOT NULL,          -- Step-by-step text instructions (not code)
    pause_after TEXT,              -- NULL or "auth"
    tags TEXT DEFAULT '[]',        -- JSON array
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(domain, name)
);
```

Reflexes are domain-scoped cached step sequences. The `script` field contains human-readable step descriptions that the AI agent interprets and follows using browser-use tools.

### api_keys

```sql
CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,   -- bcrypt hash
    key_prefix TEXT NOT NULL,        -- first 12 chars for fast lookup
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

API keys use the format `fp_live_{hex32}` (40 chars). The plaintext key is returned once on creation and never stored. Lookup is done by prefix, then verified against the bcrypt hash.

### webhooks

```sql
CREATE TABLE webhooks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,            -- HMAC-SHA256 signing secret
    events TEXT DEFAULT '[]',        -- JSON array; empty = all events
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

### webhook_deliveries

```sql
CREATE TABLE webhook_deliveries (
    id TEXT PRIMARY KEY,
    webhook_id TEXT NOT NULL,
    run_id TEXT,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,            -- JSON
    attempt INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',    -- pending | delivered | failed
    response_status INTEGER,
    response_body TEXT,
    delivered_at TEXT,
    FOREIGN KEY (webhook_id) REFERENCES webhooks(id)
);
```

### sandboxes

```sql
CREATE TABLE sandboxes (
    id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    cdp_url TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    created_at TEXT NOT NULL
);
```

Tracks active Docker containers. Used on startup to reconcile in-memory state with persistent storage and clean up orphaned containers.

## Relationships

```
users ──┬── flows (created_by)
        ├── runs (user_id)
        ├── sessions (user_id)
        ├── api_keys (user_id)
        ├── webhooks (user_id)
        └── sandboxes (user_id)

flows ──── runs (flow_id)

webhooks ──── webhook_deliveries (webhook_id)
```
