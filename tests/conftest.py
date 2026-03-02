"""Shared test configuration — set env vars before any backend imports."""

import os
import tempfile

# Point PROFILES_DIR to a temp dir so backend modules don't try to create /app/profiles
_test_profiles = tempfile.mkdtemp(prefix="flowpilot-test-profiles-")
os.environ["PROFILES_DIR"] = _test_profiles
