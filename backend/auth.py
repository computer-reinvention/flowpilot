"""Authentication module — email/password auth with JWT tokens and SQLite storage."""

import asyncio
import os
import uuid
import logging
import secrets
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone, timedelta

import aiosqlite
import bcrypt
import jwt
from fastapi import Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse

from .config import DB_PATH
from . import db as _platform_db

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_hex(32)
    logger.warning("JWT_SECRET not set — using generated secret (tokens won't survive restarts)")

JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 7


def validate_hosted_secret(hosted: bool = False) -> None:
    """Hosted task mode must not silently use an ephemeral signing secret."""
    from .task_settings import TaskSettings
    if (hosted or TaskSettings.from_env().enabled) and len(os.environ.get('JWT_SECRET', '').encode()) < 32:
        raise RuntimeError('Hosted tasks require a configured JWT_SECRET of at least 32 bytes')


_rate_buckets = OrderedDict()


def rate_limit(key: str, limit: int) -> None:
    """Single-host bounded sliding window, keyed only by opaque account or peer IP."""
    timestamp = time.monotonic()
    bucket = _rate_buckets.setdefault(key, deque())
    _rate_buckets.move_to_end(key)
    while bucket and bucket[0] <= timestamp - 60:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, 'Request limit exceeded', headers={'Retry-After': '60'})
    bucket.append(timestamp)
    while len(_rate_buckets) > 10_000:
        _rate_buckets.popitem(last=False)


class SensitiveRoute(APIRoute):
    """Bound auth/key bodies before decoding and suppress validation input echoes."""
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            from .task_settings import TaskSettings
            if request.url.path in {'/api/auth/login', '/api/auth/signup'}:
                peer = request.client.host if request.client else 'unknown'
                rate_limit(str(DB_PATH) + ':login:' + peer, TaskSettings.from_env().login_requests_per_minute)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    raise HTTPException(422, 'Request exceeds authentication bounds')
            request._body = bytes(body)
            try:
                response = await original(request)
            except RequestValidationError:
                return JSONResponse({'detail': 'Invalid authentication request'}, status_code=422, headers={'Cache-Control': 'no-store'})
            response.headers['Cache-Control'] = 'no-store'
            return response
        async def protected(request):
            try:
                return await handle(request)
            except HTTPException as exc:
                return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                                    headers={**(exc.headers or {}), 'Cache-Control': 'no-store'})
            except Exception:
                logger.error('Authentication operation failed; exception details withheld')
                return JSONResponse({'detail': 'Authentication operation unavailable'}, status_code=500,
                                    headers={'Cache-Control': 'no-store'})
        return protected


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
    validate_hosted_secret()
    if not 6 <= len(password.encode()) <= 72 or not 1 <= len(email) <= 254 or not 1 <= len(name.strip()) <= 200:
        raise ValueError('Invalid account details')
    password_hash = await asyncio.to_thread(lambda: bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())
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

    logger.info('Created account id=%s', user_id)
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

    # Unknown accounts still perform a bcrypt check; no credential-bearing logs.
    if not 1 <= len(password.encode()) <= 72:
        return None
    encoded = row['password_hash'].encode() if row else _DUMMY_PASSWORD_HASH
    if not await asyncio.to_thread(bcrypt.checkpw, password.encode(), encoded) or not row:
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
    validate_hosted_secret()
    payload = {
        "user_id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRY_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> str | None:
    """Decode a JWT and return user_id, or None if invalid/expired."""
    try:
        validate_hosted_secret()
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], options={'require': ['exp', 'iat', 'user_id']})
        user_id = payload.get('user_id')
        return user_id if isinstance(user_id, str) and 1 <= len(user_id) <= 128 else None
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ---------------------------------------------------------------------------
# API Key helpers
# ---------------------------------------------------------------------------

_API_KEY_PREFIX_LEN = 12  # characters used for DB lookup
_DUMMY_PASSWORD_HASH = b'$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxR6cpvYOz.dCxyDtEDO0YTpRvW'


def generate_api_key() -> str:
    return f"fp_live_{secrets.token_hex(16)}"


def hash_api_key(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()


def verify_api_key(plaintext: str, key_hash: str) -> bool:
    if len(plaintext.encode()) > 72:
        return False
    try:
        return bcrypt.checkpw(plaintext.encode(), key_hash.encode())
    except ValueError:
        return False


async def _update_key_last_used(key_id: str) -> None:
    try:
        await _platform_db.update_api_key_last_used(key_id)
    except Exception:
        pass


async def _resolve_api_key(token: str) -> dict:
    """Look up an API key by prefix, verify hash, return user dict or raise 401."""
    prefix = token[:_API_KEY_PREFIX_LEN]
    if len(token) != 40 or any(c not in '0123456789abcdef' for c in token[8:]):
        raise HTTPException(401, 'Invalid credentials')
    # Prefixes are lookup hints, not unique identities. Check every candidate.
    async with aiosqlite.connect(str(_platform_db.DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute('SELECT id,user_id,key_hash FROM api_keys WHERE key_prefix=?', (prefix,))).fetchall()
    row = None
    for candidate in rows:
        if await asyncio.to_thread(verify_api_key, token, candidate['key_hash']):
            row = candidate
            break
    if row is None:
        raise HTTPException(401, 'Invalid credentials')
    user = await get_user_by_id(row["user_id"])
    if not user:
        raise HTTPException(401, 'Invalid credentials')
    asyncio.create_task(_update_key_last_used(row["id"]))
    return user


# ---------------------------------------------------------------------------
# Request authentication
# ---------------------------------------------------------------------------

async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: extract and validate JWT or API key from Authorization header or cookie.

    Returns user dict or raises 401.
    """
    token = None

    # Check Authorization header first
    auth_header = request.headers.get("authorization", "")
    if len(auth_header) > 4096:
        raise HTTPException(401, 'Invalid credentials')
    if auth_header and not auth_header.startswith('Bearer '):
        raise HTTPException(401, 'Invalid credentials')
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    # Fallback to cookie
    if not token:
        token = request.cookies.get("token")

    if token and len(token) > 4096:
        raise HTTPException(401, 'Invalid credentials')

    if not auth_header and token and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{request.url.netloc}'
        if origin != expected:
            raise HTTPException(401, 'Invalid credentials')

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # API key path
    if token.startswith("fp_live_"):
        return await _resolve_api_key(token)

    # JWT path
    user_id = decode_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user
