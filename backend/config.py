"""Shared configuration constants."""

import os
from pathlib import Path

PROFILES_DIR = Path(os.environ.get("PROFILES_DIR", "/app/profiles"))
