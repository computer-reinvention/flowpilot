"""Authentication module — email/password auth with JWT tokens and SQLite storage."""

import os
import uuid
import logging
import secrets
from datetime import datetime, timezone, timedelta

import aiosqlite
import bcrypt
import jwt
from fastapi import Request, HTTPException

from .config import DB_PATH

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    logger.warning("JWT_SECRET not set — using generated secret (tokens won't survive restarts)")

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7


async def init_db() -> None:
    """Create the users table if it doesn't exist."""
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
        await db.commit()
    logger.info("Auth DB initialized at %s", DB_PATH)


async def create_user(email: str, password: str, name: str) -> dict:
    """Create a new user. Returns user dict (without password_hash)."""
    user_id = str(uuid.uuid4())[:12]
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    created_at = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(str(DB_PATH)) as db:
        try:
            await db.execute(
                "INSERT INTO users (id, email, password_hash, name, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email.lower().strip(), password_hash, name, created_at),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise ValueError("Email already registered")

    logger.info("Created user '%s' (id=%s)", name, user_id)
    return {"id": user_id, "email": email.lower().strip(), "name": name, "created_at": created_at}


async def authenticate(email: str, password: str) -> dict | None:
    """Verify email+password. Returns user dict or None."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, email, password_hash, name, created_at FROM users WHERE email = ?",
            (email.lower().strip(),),
        )
        row = await cursor.fetchone()

    if not row:
        return None

    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return None

    return {"id": row["id"], "email": row["email"], "name": row["name"], "created_at": row["created_at"]}


async def get_user_by_id(user_id: str) -> dict | None:
    """Look up a user by ID."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, email, name, created_at FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"], "created_at": row["created_at"]}


def create_token(user_id: str) -> str:
    """Create a JWT with user_id claim, expires in JWT_EXPIRY_DAYS."""
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> str | None:
    """Decode a JWT and return user_id, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload.get("user_id")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: extract and validate JWT from Authorization header or cookie.

    Returns user dict or raises 401.
    """
    token = None

    # Check Authorization header first
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    # Fallback to cookie
    if not token:
        token = request.cookies.get("token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user
