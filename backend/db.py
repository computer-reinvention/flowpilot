"""Database layer — SQLite via aiosqlite for flows, runs, sessions."""

import json
import logging
from datetime import datetime, timezone

import aiosqlite

from .config import DB_PATH
from .models import Flow, Run, Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

async def init_db():
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
                captured_at TEXT NOT NULL
            )
        """)
        await db.commit()
    logger.info("Platform DB initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------

async def create_flow(flow: Flow) -> Flow:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO flows (id, name, domain, description, entry_url, inputs, icon, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (flow.id, flow.name, flow.domain, flow.description, flow.entry_url,
             flow.inputs, flow.icon, flow.created_by, flow.created_at),
        )
        await db.commit()
    return flow


async def get_flow(flow_id: str) -> Flow | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM flows WHERE id = ?", (flow_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return Flow(**dict(row))


async def list_flows() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM flows ORDER BY created_at DESC")
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
               steps, current_step, result, error, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run.id, run.flow_id, run.user_id, run.status, run.inputs, run.sandbox_id,
             run.steps, run.current_step, run.result, run.error, run.created_at, run.completed_at),
        )
        await db.commit()
    return run


async def get_run(run_id: str) -> Run | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
    if not row:
        return None
    return Run(**dict(row))


async def list_runs(user_id: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC", (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_run(run_id: str, updates: dict) -> None:
    allowed = {"status", "sandbox_id", "steps", "current_step", "result", "error", "completed_at"}
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
               session_storage, user_agent, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session.id, session.user_id, session.domain, session.cookies,
             session.local_storage, session.session_storage, session.user_agent,
             session.captured_at),
        )
        await db.commit()
    return session


async def list_sessions(user_id: str) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, user_id, domain, captured_at FROM sessions WHERE user_id = ? ORDER BY captured_at DESC",
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


async def get_session_for_domain(user_id: str, domain: str) -> Session | None:
    """Get the most recent session for a user+domain combo."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
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
# Seed default flows
# ---------------------------------------------------------------------------

async def seed_default_flows():
    """Seed the default Uber flow if no flows exist."""
    flows = await list_flows()
    if flows:
        return
    import uuid
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
