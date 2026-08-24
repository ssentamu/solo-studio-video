"""Durable authentication sessions and audit records for Solo Studio."""
from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path
from typing import Any

import job_store


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def initialize(path: str | Path | None = None) -> None:
    job_store.initialize(path)
    with job_store.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_digest TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL,
                last_seen_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                action TEXT NOT NULL,
                job_id TEXT,
                created_at REAL NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_events_job ON audit_events(job_id, created_at);
            """
        )
        connection.commit()


def create_session(path: str | Path | None, owner_id: str, ttl_seconds: int) -> str:
    initialize(path)
    token = secrets.token_urlsafe(32)
    now = time.time()
    with job_store.connect(path) as connection:
        connection.execute(
            "INSERT INTO auth_sessions(token_digest, owner_id, created_at, expires_at, last_seen_at) VALUES(?, ?, ?, ?, ?)",
            (_token_digest(token), owner_id, now, now + max(1, int(ttl_seconds)), now),
        )
        connection.commit()
    return token


def validate_session(path: str | Path | None, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    initialize(path)
    now = time.time()
    with job_store.connect(path) as connection:
        row = connection.execute(
            "SELECT owner_id, created_at, expires_at, revoked_at FROM auth_sessions WHERE token_digest=?",
            (_token_digest(token),),
        ).fetchone()
        if row is None or row["revoked_at"] is not None or row["expires_at"] <= now:
            return None
        connection.execute(
            "UPDATE auth_sessions SET last_seen_at=? WHERE token_digest=?",
            (now, _token_digest(token)),
        )
        connection.commit()
        return {"owner_id": row["owner_id"], "created_at": row["created_at"], "expires_at": row["expires_at"]}


def revoke_session(path: str | Path | None, token: str) -> None:
    if not token:
        return
    initialize(path)
    with job_store.connect(path) as connection:
        connection.execute(
            "UPDATE auth_sessions SET revoked_at=? WHERE token_digest=? AND revoked_at IS NULL",
            (time.time(), _token_digest(token)),
        )
        connection.commit()


def prune_sessions(path: str | Path | None) -> int:
    initialize(path)
    with job_store.connect(path) as connection:
        result = connection.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
            (time.time(),),
        )
        connection.commit()
        return result.rowcount


def audit(path: str | Path | None, owner_id: str, action: str, *, job_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    initialize(path)
    with job_store.connect(path) as connection:
        connection.execute(
            "INSERT INTO audit_events(owner_id, action, job_id, created_at, metadata_json) VALUES(?, ?, ?, ?, ?)",
            (owner_id[:128], action[:128], job_id, time.time(), job_store._json(metadata or {})),
        )
        connection.commit()
