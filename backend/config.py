"""Shared configuration constants."""

import os
from pathlib import Path

PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))
DB_PATH = PROFILES_DIR / "platform.db"
BASE_URL = os.environ.get("FLOWPILOT_BASE_URL", "http://localhost:8080")
