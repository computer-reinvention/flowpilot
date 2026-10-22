"""Tests for auth endpoints: signup, login, me, invalid credentials."""

import httpx
import pytest


async def test_signup_success(client: httpx.AsyncClient):
    resp = await client.post("/api/auth/signup", json={
        "email": "new@example.com",
        "password": "secret123",
        "name": "New User",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["user"]["email"] == "new@example.com"
    assert data["user"]["name"] == "New User"
    assert "id" in data["user"]


async def test_signup_duplicate_email(client: httpx.AsyncClient):
    payload = {"email": "dup@example.com", "password": "secret123", "name": "Dup"}
    resp1 = await client.post("/api/auth/signup", json=payload)
    assert resp1.status_code == 200

    resp2 = await client.post("/api/auth/signup", json=payload)
    assert resp2.status_code == 409


async def test_signup_short_password(client: httpx.AsyncClient):
    resp = await client.post("/api/auth/signup", json={
        "email": "short@example.com",
        "password": "abc",
        "name": "Short",
    })
    assert resp.status_code == 400


async def test_login_success(client: httpx.AsyncClient):
    await client.post("/api/auth/signup", json={
        "email": "login@example.com",
        "password": "mypassword",
        "name": "Login User",
    })
    resp = await client.post("/api/auth/login", json={
        "email": "login@example.com",
        "password": "mypassword",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["user"]["email"] == "login@example.com"


async def test_login_invalid_password(client: httpx.AsyncClient):
    await client.post("/api/auth/signup", json={
        "email": "badpw@example.com",
        "password": "correct_password",
        "name": "BadPW",
    })
    resp = await client.post("/api/auth/login", json={
        "email": "badpw@example.com",
        "password": "wrong_password",
    })
    assert resp.status_code == 401


async def test_login_nonexistent_email(client: httpx.AsyncClient):
    resp = await client.post("/api/auth/login", json={
        "email": "nobody@example.com",
        "password": "whatever",
    })
    assert resp.status_code == 401


async def test_me_authenticated(client: httpx.AsyncClient, auth_headers: dict):
    resp = await client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["email"] == "test@example.com"
    assert data["user"]["name"] == "Test User"


async def test_me_no_token(client: httpx.AsyncClient):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_signup_allowlist_admits_only_listed_emails(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setenv("FLOWPILOT_SIGNUP_EMAILS", "owner@example.com, Second@Example.com")
    blocked = await client.post("/api/auth/signup", json={"email": "stranger@example.com", "password": "secret123", "name": "S"})
    assert blocked.status_code == 403
    for email in ("Owner@example.com", "second@example.com"):
        allowed = await client.post("/api/auth/signup", json={"email": email, "password": "secret123", "name": "O"})
        assert allowed.status_code == 200
