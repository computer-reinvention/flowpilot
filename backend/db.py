"""Database layer — SQLite via aiosqlite for flows, runs, sessions, reflexes."""

import json
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from .config import DB_PATH
from .models import Flow, Run, Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS flows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                domain TEXT NOT NULL,
                description TEXT NOT NULL,
                entry_url TEXT,
                inputs TEXT NOT NULL DEFAULT '[]',
                icon TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                flow_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                inputs TEXT NOT NULL DEFAULT '{}',
                sandbox_id TEXT,
                steps TEXT NOT NULL DEFAULT '[]',
                current_step TEXT,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (flow_id) REFERENCES flows(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                domain TEXT NOT NULL,
                cookies TEXT NOT NULL DEFAULT '[]',
                local_storage TEXT NOT NULL DEFAULT '{}',
                session_storage TEXT NOT NULL DEFAULT '{}',
                user_agent TEXT,
                profile_name TEXT NOT NULL DEFAULT 'default',
                captured_at TEXT NOT NULL
            )
        """)
        # Reflexes: domain-scoped JavaScript scripts the agent builds over time.
        # Named within a domain (e.g., domain="uber.com", name="fill_booking_form").
        # The `script` column holds raw JS executed via page.evaluate() — zero LLM calls.
        # The agent saves them via save_reflex() and executes via run_reflex().
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reflexes (
                id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                script TEXT NOT NULL,
                pause_after TEXT,
                tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(domain, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                url TEXT NOT NULL,
                secret TEXT NOT NULL,
                events TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id TEXT PRIMARY KEY,
                webhook_id TEXT NOT NULL,
                run_id TEXT,
                event TEXT NOT NULL,
                payload TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                response_status INTEGER,
                response_body TEXT,
                delivered_at TEXT,
                FOREIGN KEY (webhook_id) REFERENCES webhooks(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trigger_hooks (
                id TEXT PRIMARY KEY,
                flow_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                FOREIGN KEY (flow_id) REFERENCES flows(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sandboxes (
                id TEXT PRIMARY KEY,
                container_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                cdp_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                created_at TEXT NOT NULL
            )
        """)
        # Migrations: add columns that may be missing in existing DBs
        for migration in [
            "ALTER TABLE sessions ADD COLUMN profile_name TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE runs ADD COLUMN profile_id TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                await db.execute(migration)
            except aiosqlite.OperationalError:
                pass  # column already exists
        await db.commit()
    logger.info("Platform DB initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

async def create_flow(flow: Flow) -> Flow:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO flows
               (id, name, domain, description, entry_url, inputs, icon, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (flow.id, flow.name, flow.domain, flow.description, flow.entry_url,
             flow.inputs, flow.icon, flow.created_by, flow.created_at),
        )
        await db.commit()
    return flow


_FLOW_COLS = "id, name, domain, description, entry_url, inputs, icon, created_by, created_at"


async def get_flow(flow_id: str) -> Flow | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"SELECT {_FLOW_COLS} FROM flows WHERE id = ?", (flow_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return Flow(**dict(row))


async def list_flows() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"SELECT {_FLOW_COLS} FROM flows ORDER BY created_at DESC")
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_flow(flow_id: str, updates: dict) -> Flow | None:
    allowed = {"name", "domain", "description", "entry_url", "inputs", "icon"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return await get_flow(flow_id)
    sets = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [flow_id]
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(f"UPDATE flows SET {sets} WHERE id = ?", vals)
        await db.commit()
    return await get_flow(flow_id)


async def delete_flow(flow_id: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM flows WHERE id = ?", (flow_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

async def create_run(run: Run) -> Run:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO runs (id, flow_id, user_id, status, inputs, sandbox_id,
               steps, current_step, result, error, created_at, completed_at, profile_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run.id, run.flow_id, run.user_id, run.status, run.inputs, run.sandbox_id,
             run.steps, run.current_step, run.result, run.error, run.created_at, run.completed_at,
             run.profile_id),
        )
        await db.commit()
    return run


_RUN_COLS = "id, flow_id, user_id, status, inputs, sandbox_id, steps, current_step, result, error, created_at, completed_at, profile_id"


async def get_run(run_id: str) -> Run | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"SELECT {_RUN_COLS} FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return Run(**dict(row))


async def list_runs(user_id: str, flow_id: str | None = None) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if flow_id:
            cursor = await db.execute(
                f"SELECT {_RUN_COLS} FROM runs WHERE user_id = ? AND flow_id = ? ORDER BY created_at DESC",
                (user_id, flow_id),
            )
        else:
            cursor = await db.execute(
                f"SELECT {_RUN_COLS} FROM runs WHERE user_id = ? ORDER BY created_at DESC", (user_id,),
            )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_run(run_id: str, updates: dict) -> None:
    allowed = {"status", "sandbox_id", "steps", "current_step", "result", "error", "completed_at", "profile_id"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return
    sets = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [run_id]
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(f"UPDATE runs SET {sets} WHERE id = ?", vals)
        await db.commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def create_session(session: Session) -> Session:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO sessions (id, user_id, domain, cookies, local_storage,
               session_storage, user_agent, profile_name, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session.id, session.user_id, session.domain, session.cookies,
             session.local_storage, session.session_storage, session.user_agent,
             session.profile_name, session.captured_at),
        )
        await db.commit()
    return session


async def list_sessions(user_id: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, domain, profile_name, captured_at FROM sessions WHERE user_id = ? ORDER BY captured_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_session(session_id: str) -> Session | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return Session(**dict(row))


async def get_session_for_domain(user_id: str, domain: str, profile_name: str | None = None) -> Session | None:
    """Get the most recent session for a user+domain combo, optionally filtered by profile."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if profile_name:
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND domain = ? AND profile_name = ? ORDER BY captured_at DESC LIMIT 1",
                (user_id, domain, profile_name),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND domain = ? ORDER BY captured_at DESC LIMIT 1",
                (user_id, domain),
            )
        row = await cursor.fetchone()
    if not row:
        return None
    return Session(**dict(row))


async def delete_session(session_id: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Reflexes
# ---------------------------------------------------------------------------

async def upsert_reflex(
    domain: str,
    name: str,
    description: str,
    script: str,
    pause_after: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Insert or replace a reflex. Returns the stored row as a dict."""
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    tags_json = json.dumps(tags or [])
    reflex_id = str(uuid.uuid4())[:12]
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        # Try to get existing id so we don't churn it on updates
        cursor = await db.execute(
            "SELECT id FROM reflexes WHERE domain = ? AND name = ?", (domain, name)
        )
        existing = await cursor.fetchone()
        if existing:
            reflex_id = existing["id"]
            await db.execute(
                """UPDATE reflexes
                   SET description=?, script=?, pause_after=?, tags=?, updated_at=?
                   WHERE domain=? AND name=?""",
                (description, script, pause_after, tags_json, now, domain, name),
            )
        else:
            await db.execute(
                """INSERT INTO reflexes (id, domain, name, description, script, pause_after, tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (reflex_id, domain, name, description, script, pause_after, tags_json, now, now),
            )
        await db.commit()
        cursor = await db.execute("SELECT * FROM reflexes WHERE id = ?", (reflex_id,))
        row = await cursor.fetchone()
    result = dict(row)
    result["tags"] = json.loads(result.get("tags", "[]"))
    return result


async def get_reflex(domain: str, name: str) -> dict | None:
    """Get a single reflex by domain + name."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM reflexes WHERE domain = ? AND name = ?", (domain, name)
        )
        row = await cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    result["tags"] = json.loads(result.get("tags", "[]"))
    return result


async def list_reflexes_for_domain(domain: str) -> list[dict]:
    """Return all reflexes for a domain, ordered by creation time."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM reflexes WHERE domain = ? ORDER BY created_at ASC", (domain,)
        )
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["tags"] = json.loads(r.get("tags", "[]"))
        result.append(r)
    return result




# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

async def create_api_key(user_id: str, name: str, key_hash: str, key_prefix: str) -> dict:
    key_id = str(uuid.uuid4())[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO api_keys (id, user_id, name, key_hash, key_prefix, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key_id, user_id, name, key_hash, key_prefix, created_at),
        )
        await db.commit()
    return {"id": key_id, "user_id": user_id, "name": name, "key_prefix": key_prefix, "created_at": created_at, "last_used_at": None}


async def list_api_keys(user_id: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, name, key_prefix, created_at, last_used_at FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_api_key_by_prefix(key_prefix: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, name, key_hash, key_prefix, created_at, last_used_at FROM api_keys WHERE key_prefix = ?",
            (key_prefix,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def update_api_key_last_used(key_id: str) -> None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), key_id),
        )
        await db.commit()


async def delete_api_key(key_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

async def create_webhook(user_id: str, url: str, secret: str, events: list[str]) -> dict:
    webhook_id = str(uuid.uuid4())[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    events_json = json.dumps(events)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO webhooks (id, user_id, url, secret, events, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (webhook_id, user_id, url, secret, events_json, created_at),
        )
        await db.commit()
    return {"id": webhook_id, "user_id": user_id, "url": url, "events": events, "enabled": True, "created_at": created_at}


async def list_webhooks(user_id: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, url, events, enabled, created_at FROM webhooks WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["events"] = json.loads(r.get("events", "[]"))
        r["enabled"] = bool(r["enabled"])
        result.append(r)
    return result


async def get_webhooks_for_event(user_id: str, event: str) -> list[dict]:
    """Return enabled webhooks subscribed to the given event (empty events list = all events)."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, url, secret, events FROM webhooks WHERE user_id = ? AND enabled = 1",
            (user_id,),
        )
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        r = dict(row)
        subscribed = json.loads(r.get("events", "[]"))
        if not subscribed or event in subscribed:
            result.append({"id": r["id"], "url": r["url"], "secret": r["secret"]})
    return result


async def get_webhook(webhook_id: str, user_id: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, url, secret, events, enabled, created_at FROM webhooks WHERE id = ? AND user_id = ?",
            (webhook_id, user_id),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    r = dict(row)
    r["events"] = json.loads(r.get("events", "[]"))
    r["enabled"] = bool(r["enabled"])
    return r


async def delete_webhook(webhook_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM webhooks WHERE id = ? AND user_id = ?", (webhook_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Webhook deliveries
# ---------------------------------------------------------------------------

async def create_webhook_delivery(webhook_id: str, run_id: str | None, event: str, payload: str) -> str:
    delivery_id = str(uuid.uuid4())[:12]
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO webhook_deliveries (id, webhook_id, run_id, event, payload) VALUES (?, ?, ?, ?, ?)",
            (delivery_id, webhook_id, run_id, event, payload),
        )
        await db.commit()
    return delivery_id


async def update_webhook_delivery(delivery_id: str, updates: dict) -> None:
    allowed = {"attempt", "status", "response_status", "response_body", "delivered_at"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return
    sets = ", ".join(f"{k} = ?" for k in filtered)
    vals = list(filtered.values()) + [delivery_id]
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(f"UPDATE webhook_deliveries SET {sets} WHERE id = ?", vals)
        await db.commit()


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------

async def create_sandbox(sandbox_id: str, container_id: str, user_id: str, cdp_url: str) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO sandboxes (id, container_id, user_id, cdp_url, created_at) VALUES (?, ?, ?, ?, ?)",
            (sandbox_id, container_id, user_id, cdp_url, created_at),
        )
        await db.commit()


async def delete_sandbox(sandbox_id: str) -> None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM sandboxes WHERE id = ?", (sandbox_id,))
        await db.commit()


async def list_active_sandboxes() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sandboxes WHERE status = 'running'")
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_sandbox(sandbox_id: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,))
        row = await cursor.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Trigger Hooks
# ---------------------------------------------------------------------------

async def create_trigger_hook(flow_id: str, user_id: str, name: str, token: str) -> dict:
    hook_id = str(uuid.uuid4())[:12]
    created_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT INTO trigger_hooks (id, flow_id, user_id, name, token, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (hook_id, flow_id, user_id, name, token, created_at),
        )
        await db.commit()
    return {"id": hook_id, "flow_id": flow_id, "user_id": user_id, "name": name, "token": token, "created_at": created_at, "last_used_at": None}


async def list_trigger_hooks(flow_id: str, user_id: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, flow_id, user_id, name, token, created_at, last_used_at FROM trigger_hooks WHERE flow_id = ? AND user_id = ? ORDER BY created_at DESC",
            (flow_id, user_id),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_trigger_hook_by_token(token: str) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, flow_id, user_id, name, token, created_at, last_used_at FROM trigger_hooks WHERE token = ?",
            (token,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_trigger_hook(hook_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM trigger_hooks WHERE id = ? AND user_id = ?", (hook_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def touch_trigger_hook(hook_id: str) -> None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE trigger_hooks SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), hook_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Seed default flows
# ---------------------------------------------------------------------------

async def seed_default_flows() -> None:
    """Seed the default Uber flow if no flows exist."""
    flows = await list_flows()
    if flows:
        return
    uber_flow = Flow(
        id=str(uuid.uuid4())[:12],
        name="Uber Ride",
        domain="uber.com",
        description=(
            "Book an Uber ride on m.uber.com. Navigate to the site, enter the pickup "
            "and dropoff locations provided by the user, select their requested ride type, "
            "and confirm the booking. Wait for confirmation and return the driver name, "
            "car details, ETA, and fare estimate."
        ),
        entry_url="https://m.uber.com",
        inputs=json.dumps([
            {"name": "pickup", "label": "Pickup location", "type": "text", "placeholder": "e.g. Koramangala 5th Block"},
            {"name": "dropoff", "label": "Drop-off location", "type": "text", "placeholder": "e.g. Indiranagar Metro"},
            {"name": "ride_type", "label": "Ride type", "type": "select", "options": ["UberGo", "Premier", "Auto", "UberXL"]},
        ]),
        icon="🚗",
        created_by="system",
    )
    await create_flow(uber_flow)
    logger.info("Seeded default Uber flow (id=%s)", uber_flow.id)
