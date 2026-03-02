"""Tests for flows CRUD endpoints (all authenticated)."""

import httpx
import pytest


async def _create_flow(client: httpx.AsyncClient, headers: dict, **overrides) -> dict:
    """Helper to create a flow and return the response JSON."""
    payload = {
        "name": "Test Flow",
        "domain": "example.com",
        "description": "A test flow",
        "entry_url": "https://example.com",
        "inputs": [{"key": "query", "label": "Search query", "type": "text"}],
        "icon": "🔍",
    }
    payload.update(overrides)
    resp = await client.post("/api/flows", json=payload, headers=headers)
    assert resp.status_code == 200
    return resp.json()


async def test_create_flow(client: httpx.AsyncClient, auth_headers: dict):
    data = await _create_flow(client, auth_headers)
    assert data["name"] == "Test Flow"
    assert data["domain"] == "example.com"
    assert data["description"] == "A test flow"
    assert data["entry_url"] == "https://example.com"
    assert len(data["inputs"]) == 1
    assert data["icon"] == "🔍"
    assert "id" in data


async def test_list_flows(client: httpx.AsyncClient, auth_headers: dict):
    await _create_flow(client, auth_headers, name="Flow A")
    await _create_flow(client, auth_headers, name="Flow B")

    resp = await client.get("/api/flows")
    assert resp.status_code == 200
    flows = resp.json()
    names = [f["name"] for f in flows]
    assert "Flow A" in names
    assert "Flow B" in names


async def test_get_flow(client: httpx.AsyncClient, auth_headers: dict):
    created = await _create_flow(client, auth_headers)
    flow_id = created["id"]

    resp = await client.get(f"/api/flows/{flow_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == flow_id
    assert data["name"] == "Test Flow"


async def test_get_flow_not_found(client: httpx.AsyncClient):
    resp = await client.get("/api/flows/nonexistent")
    assert resp.status_code == 404


async def test_update_flow(client: httpx.AsyncClient, auth_headers: dict):
    created = await _create_flow(client, auth_headers)
    flow_id = created["id"]

    resp = await client.put(
        f"/api/flows/{flow_id}",
        json={"name": "Updated Flow", "description": "Updated desc"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Flow"
    assert data["description"] == "Updated desc"
    assert data["domain"] == "example.com"  # unchanged


async def test_delete_flow(client: httpx.AsyncClient, auth_headers: dict):
    created = await _create_flow(client, auth_headers)
    flow_id = created["id"]

    resp = await client.delete(f"/api/flows/{flow_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    resp = await client.get(f"/api/flows/{flow_id}")
    assert resp.status_code == 404


async def test_delete_flow_not_found(client: httpx.AsyncClient, auth_headers: dict):
    resp = await client.delete("/api/flows/nonexistent", headers=auth_headers)
    assert resp.status_code == 404


async def test_create_flow_unauthenticated(client: httpx.AsyncClient):
    resp = await client.post("/api/flows", json={
        "name": "No Auth",
        "domain": "example.com",
        "description": "Should fail",
    })
    assert resp.status_code == 401
