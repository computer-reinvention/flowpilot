"""Additive, reapplicable task schema; legacy platform tables are untouched."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_schema (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO bt_schema VALUES (1);
CREATE TABLE IF NOT EXISTS bt_profiles (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, label TEXT NOT NULL,
 created TEXT NOT NULL, recovery INTEGER NOT NULL DEFAULT 0,
 deleting INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bt_sessions (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, profile TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bt_tasks (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, session TEXT NOT NULL,
 profile TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
 generation INTEGER NOT NULL, claimed INTEGER NOT NULL DEFAULT 0,
 reserved INTEGER NOT NULL DEFAULT 1, view TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS bt_session_active ON bt_tasks(session)
 WHERE state NOT IN ('completed','failed','cancelled','interrupted');
CREATE UNIQUE INDEX IF NOT EXISTS bt_profile_writer ON bt_tasks(profile) WHERE reserved=1;
CREATE INDEX IF NOT EXISTS bt_account_capacity ON bt_tasks(owner,reserved);
CREATE TABLE IF NOT EXISTS bt_idempotency (
 owner TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, receipt TEXT NOT NULL,
 PRIMARY KEY(owner,operation,key)
);
CREATE TABLE IF NOT EXISTS bt_interactions (
 task TEXT NOT NULL, id TEXT NOT NULL, generation INTEGER NOT NULL,
 descriptor TEXT NOT NULL, response_key TEXT, digest TEXT, confirmed INTEGER,
 receipt TEXT, PRIMARY KEY(task,id), UNIQUE(task,generation)
);
CREATE TABLE IF NOT EXISTS bt_capabilities (
 digest TEXT PRIMARY KEY, task TEXT NOT NULL, binding TEXT NOT NULL
);
"""
