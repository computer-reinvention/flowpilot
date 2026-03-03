"""Tests for session endpoints: capture with profile_name, list, delete, profile filtering."""

import httpx
import pytest


async def _create_flow(client: httpx.AsyncClient, headers: dict) -> dict:
    """Helper to create a flow for testing."""
    resp = await client.post("/api/flows", json={
        "name": "Session Test Flow",
        "domain": "example.com",
        "description": "A test flow",
        "entry_url": "https://example.com",
        "inputs": [],
    }, headers=headers)
    assert resp.status_code == 200
    return resp.json()


async def test_capture_session_default_profile(client: httpx.AsyncClient, auth_headers: dict):
    """Capture a session with default profile_name."""
    resp = await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "example.com",
        "cookies": [{"name": "sid", "value": "abc123", "domain": "example.com"}],
        "localStorage": {"key": "val"},
        "sessionStorage": {},
        "userAgent": "TestAgent/1.0",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "example.com"
    assert data["profile_name"] == "default"
    assert "id" in data
    assert "captured_at" in data


async def test_capture_session_custom_profile(client: httpx.AsyncClient, auth_headers: dict):
    """Capture a session with a custom profile_name."""
    resp = await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "example.com",
        "cookies": [{"name": "sid", "value": "xyz789", "domain": "example.com"}],
        "profile_name": "test-account-1",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["domain"] == "example.com"
    assert data["profile_name"] == "test-account-1"


async def test_list_sessions_includes_profile_name(client: httpx.AsyncClient, auth_headers: dict):
    """List sessions should include profile_name."""
    # Create two sessions with different profiles
    await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "site-a.com",
        "cookies": [{"name": "a", "value": "1"}],
        "profile_name": "default",
    })
    await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "site-b.com",
        "cookies": [{"name": "b", "value": "2"}],
        "profile_name": "staging-user",
    })

    resp = await client.get("/api/sessions", headers=auth_headers)
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) >= 2

    profiles = {s["domain"]: s.get("profile_name") for s in sessions}
    assert profiles["site-a.com"] == "default"
    assert profiles["site-b.com"] == "staging-user"


async def test_delete_session(client: httpx.AsyncClient, auth_headers: dict):
    """Delete a session by ID."""
    resp = await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "delete-me.com",
        "cookies": [{"name": "x", "value": "y"}],
    })
    session_id = resp.json()["id"]

    resp = await client.delete(f"/api/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # Verify it's gone
    resp = await client.get("/api/sessions", headers=auth_headers)
    domains = [s["domain"] for s in resp.json()]
    assert "delete-me.com" not in domains


async def test_delete_session_not_found(client: httpx.AsyncClient, auth_headers: dict):
    """Delete a non-existent session."""
    resp = await client.delete("/api/sessions/nonexistent", headers=auth_headers)
    assert resp.status_code == 404


async def test_capture_session_unauthenticated(client: httpx.AsyncClient):
    """Capture session without auth should return 401."""
    resp = await client.post("/api/sessions/capture", json={
        "domain": "example.com",
        "cookies": [],
    })
    assert resp.status_code == 401


async def test_multiple_profiles_same_domain(client: httpx.AsyncClient, auth_headers: dict):
    """Multiple sessions for the same domain with different profile names."""
    await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "multi.com",
        "cookies": [{"name": "a", "value": "1"}],
        "profile_name": "user-1",
    })
    await client.post("/api/sessions/capture", headers=auth_headers, json={
        "domain": "multi.com",
        "cookies": [{"name": "b", "value": "2"}],
        "profile_name": "user-2",
    })

    resp = await client.get("/api/sessions", headers=auth_headers)
    multi_sessions = [s for s in resp.json() if s["domain"] == "multi.com"]
    assert len(multi_sessions) == 2

    profile_names = {s["profile_name"] for s in multi_sessions}
    assert profile_names == {"user-1", "user-2"}
