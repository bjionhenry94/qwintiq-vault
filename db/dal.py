"""Data access layer — one surface, two drivers.

DATABASE_URL set  -> Postgres (Supabase, prod).
DATABASE_URL unset -> local SQLite file (dev), same tables, uuids minted in Python.

Everything the vault persists goes through here: the Keyring (consultants + keys),
OAuth artifacts (clients, codes, tokens), admins, and framework state (icebreaker
setups / partner routines). No other module talks SQL.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PG_URL = os.environ.get("DATABASE_URL", "").strip()
_SQLITE_PATH = os.environ.get("VAULT_SQLITE", str(Path(__file__).parent / "vault.dev.sqlite3"))
_lock = threading.Lock()
_conn = None


def is_prod() -> bool:
    """A real deploy. Render sets RENDER=true; we also honour an explicit VAULT_ENV=prod."""
    return bool(os.environ.get("RENDER") or os.environ.get("VAULT_ENV", "").lower() == "prod")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def connect():
    global _conn
    if _conn is not None:
        return _conn
    with _lock:  # double-checked: only one connection is ever created
        if _conn is not None:
            return _conn
        if _PG_URL:
            import psycopg  # lazy: prod-only dependency

            _conn = psycopg.connect(_PG_URL, autocommit=True)
        elif is_prod():
            # A blank DATABASE_URL in prod would silently fall back to ephemeral SQLite and
            # wipe every consultant/key/admin on the next redeploy. Refuse loudly instead.
            raise RuntimeError("DATABASE_URL is required in production (no ephemeral SQLite fallback).")
        else:
            _conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("pragma journal_mode=wal")
    return _conn


def q(sql: str, params: tuple = (), fetch: str | None = None):
    """Run one statement. sqlite uses ?, postgres uses %s — write ? here, we translate.
    Constraint: no query may contain a literal '%' (psycopg treats it as the placeholder
    sigil); the assert makes a violation fail loudly in dev, not silently only in prod."""
    conn = connect()
    if _PG_URL:
        assert "%" not in sql, "literal '%' in a query breaks the psycopg placeholder translation"
        sql = sql.replace("?", "%s")
    with _lock:
        cur = conn.execute(sql, params)
        if fetch == "one":
            row = cur.fetchone()
            return dict(row) if row is not None and not _PG_URL else (
                dict(zip([d[0] for d in cur.description], row)) if row is not None else None
            )
        if fetch == "all":
            rows = cur.fetchall()
            if _PG_URL:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return [dict(r) for r in rows]
        if not _PG_URL:
            conn.commit()
        return None


_TABLES = """
create table if not exists consultants (
    id text primary key, email text unique not null, full_name text,
    password_hash text, must_change_password integer not null default 1,
    status text not null default 'active', created_at text not null, revoked_at text);
create table if not exists consultant_keys (
    id text primary key,
    consultant_id text not null references consultants(id) on delete cascade,
    label text,
    active integer not null default 1, created_at text not null, revoked_at text);
create index if not exists idx_keys_consultant on consultant_keys(consultant_id);
create table if not exists admins (
    id text primary key, email text unique not null, full_name text,
    password_hash text not null, created_at text not null);
create table if not exists oauth_clients (
    client_id text primary key, name text, redirect_uris text not null, created_at text not null);
create table if not exists oauth_codes (
    code text primary key, client_id text not null, redirect_uri text not null,
    code_challenge text not null, consultant_id text not null, expires_at text not null);
create table if not exists access_tokens (
    token_hash text primary key, consultant_id text not null, key_id text not null,
    created_at text not null, expires_at text not null, revoked_at text);
create index if not exists idx_tokens_consultant on access_tokens(consultant_id);
create table if not exists framework_state (
    id text primary key, consultant_id text, kind text not null, name text not null,
    config text not null, updated_at text not null, unique (consultant_id, kind, name));
create table if not exists extraction_log (
    id text primary key, consultant_id text, tool text not null, reason text not null,
    input_excerpt text, created_at text not null);
"""


def init_db() -> None:
    for stmt in _TABLES.strip().split(";"):
        if stmt.strip():
            q(stmt)
    _seed()
    purge_expired()


def _seed() -> None:
    """Idempotent: the control-panel admin + the migrated global setups/routines.

    In prod the admin is NEVER seeded from a source-visible default — ADMIN_EMAIL and
    ADMIN_PASSWORD must both be supplied, or we refuse to boot. The convenience default
    exists only for local dev (SQLite)."""
    prod = bool(_PG_URL) or is_prod()
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip()
    admin_pw = os.environ.get("ADMIN_PASSWORD", "")
    if prod and (not admin_email or not admin_pw):
        raise RuntimeError(
            "Refusing to boot: set ADMIN_EMAIL and ADMIN_PASSWORD (no default admin in production).")
    admin_email = admin_email or "admin@qwintiq.local"
    admin_pw = admin_pw or "qwintiq-admin-dev"
    if not q("select id from admins where email = ?", (admin_email,), fetch="one"):
        from auth.passwords import hash_password

        q(
            "insert into admins (id, email, full_name, password_hash, created_at) values (?,?,?,?,?)",
            (uid(), admin_email, os.environ.get("ADMIN_NAME", "Qwintiq Admin"), hash_password(admin_pw), _now()),
        )
    seeds = Path(__file__).parent / "seed_state"
    if seeds.is_dir():
        for f in sorted(seeds.glob("*.json")):
            data = json.loads(f.read_text())
            kind = "icebreaker_setup" if "setup_id" in data else "partner_routine"
            name = data.get("setup_name") or data.get("routine_name") or f.stem
            if not q(
                "select id from framework_state where consultant_id is null and kind = ? and name = ?",
                (kind, name), fetch="one",
            ):
                q(
                    "insert into framework_state (id, consultant_id, kind, name, config, updated_at)"
                    " values (?,?,?,?,?,?)",
                    (uid(), None, kind, name, json.dumps(data), _now()),
                )


# ---------- Keyring ----------

def add_consultant(email: str, full_name: str, password_hash: str) -> dict:
    cid = uid()
    q(
        "insert into consultants (id, email, full_name, password_hash, must_change_password,"
        " status, created_at) values (?,?,?,?,1,'active',?)",
        (cid, email.lower().strip(), full_name.strip(), password_hash, _now()),
    )
    return {"id": cid, "email": email.lower().strip(), "full_name": full_name.strip()}


def remove_consultant(consultant_id: str) -> None:
    q("update consultants set status='revoked', revoked_at=? where id=?", (_now(), consultant_id))
    q("update consultant_keys set active=0, revoked_at=? where consultant_id=?", (_now(), consultant_id))
    q("update access_tokens set revoked_at=? where consultant_id=? and revoked_at is null",
      (_now(), consultant_id))


def list_consultants() -> list[dict]:
    rows = q("select * from consultants order by created_at", fetch="all") or []
    for r in rows:
        k = q(
            "select id, active from consultant_keys where consultant_id=? order by created_at desc limit 1",
            (r["id"],), fetch="one",
        )
        r["key_active"] = bool(k and k["active"])
    return rows


def get_consultant(consultant_id: str) -> dict | None:
    return q("select * from consultants where id=?", (consultant_id,), fetch="one")


def get_consultant_by_email(email: str) -> dict | None:
    return q("select * from consultants where email=?", (email.lower().strip(),), fetch="one")


def issue_key(consultant_id: str, label: str = "") -> str:
    q("update consultant_keys set active=0, revoked_at=? where consultant_id=? and active=1",
      (_now(), consultant_id))
    kid = uid()
    q("insert into consultant_keys (id, consultant_id, label, active, created_at) values (?,?,?,1,?)",
      (kid, consultant_id, label, _now()))
    return kid


def revoke_key(consultant_id: str) -> None:
    q("update consultant_keys set active=0, revoked_at=? where consultant_id=? and active=1",
      (_now(), consultant_id))
    q("update access_tokens set revoked_at=? where consultant_id=? and revoked_at is null",
      (_now(), consultant_id))


def active_key(consultant_id: str) -> dict | None:
    return q(
        "select * from consultant_keys where consultant_id=? and active=1 order by created_at desc limit 1",
        (consultant_id,), fetch="one",
    )


def set_password(consultant_id: str, password_hash: str) -> None:
    """Consultant sets their own password on first sign-in; clears the must-change flag so
    the one-time temp password can never be reused."""
    q("update consultants set password_hash=?, must_change_password=0 where id=?",
      (password_hash, consultant_id))


# ---------- OAuth ----------

def save_client(client_id: str, name: str, redirect_uris: list[str]) -> None:
    q("insert into oauth_clients (client_id, name, redirect_uris, created_at) values (?,?,?,?)",
      (client_id, name, json.dumps(redirect_uris), _now()))


def get_client(client_id: str) -> dict | None:
    row = q("select * from oauth_clients where client_id=?", (client_id,), fetch="one")
    if row:
        row["redirect_uris"] = json.loads(row["redirect_uris"])
    return row


def save_code(code: str, client_id: str, redirect_uri: str, challenge: str, consultant_id: str) -> None:
    exp = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    q("insert into oauth_codes (code, client_id, redirect_uri, code_challenge, consultant_id, expires_at)"
      " values (?,?,?,?,?,?)", (code, client_id, redirect_uri, challenge, consultant_id, exp))


def take_code(code: str) -> dict | None:
    row = q("select * from oauth_codes where code=?", (code,), fetch="one")
    if row:
        q("delete from oauth_codes where code=?", (code,))
        if row["expires_at"] > _now():
            return row
    return None


def save_token(token_hash: str, consultant_id: str, key_id: str, days: int = 30) -> None:
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    q("insert into access_tokens (token_hash, consultant_id, key_id, created_at, expires_at)"
      " values (?,?,?,?,?)", (token_hash, consultant_id, key_id, _now(), exp))


def check_token(token_hash: str) -> dict | None:
    """The per-call check. Valid only while: token unexpired+unrevoked AND consultant active
    AND that key row is still active. Revoke the key -> next call dies. This is the Wall."""
    t = q("select * from access_tokens where token_hash=?", (token_hash,), fetch="one")
    if not t or t["revoked_at"] or t["expires_at"] <= _now():
        return None
    c = get_consultant(t["consultant_id"])
    if not c or c["status"] != "active":
        return None
    k = q("select * from consultant_keys where id=? and active=1", (t["key_id"],), fetch="one")
    if not k:
        return None
    return {"consultant": c, "key": k}


# ---------- Framework state ----------

def state_save(consultant_id: str | None, kind: str, name: str, config: dict) -> None:
    existing = q(
        "select id from framework_state where consultant_id is ? and kind=? and name=?"
        if not _PG_URL else
        "select id from framework_state where consultant_id is not distinct from ? and kind=? and name=?",
        (consultant_id, kind, name), fetch="one",
    )
    if existing:
        q("update framework_state set config=?, updated_at=? where id=?",
          (json.dumps(config), _now(), existing["id"]))
    else:
        q("insert into framework_state (id, consultant_id, kind, name, config, updated_at)"
          " values (?,?,?,?,?,?)", (uid(), consultant_id, kind, name, json.dumps(config), _now()))


def state_get(consultant_id: str | None, kind: str, name: str) -> dict | None:
    row = q(
        "select * from framework_state where kind=? and name=? and (consultant_id is ?"
        " or consultant_id is null) order by (consultant_id is null) limit 1"
        if not _PG_URL else
        "select * from framework_state where kind=? and name=? and (consultant_id is not distinct"
        " from ? or consultant_id is null) order by (consultant_id is null) limit 1",
        (kind, name, consultant_id), fetch="one",
    )
    if row:
        row["config"] = json.loads(row["config"])
    return row


def state_list(consultant_id: str | None, kind: str) -> list[dict]:
    rows = q(
        "select id, consultant_id, kind, name, updated_at from framework_state where kind=?"
        " and (consultant_id is ? or consultant_id is null) order by name"
        if not _PG_URL else
        "select id, consultant_id, kind, name, updated_at from framework_state where kind=?"
        " and (consultant_id is not distinct from ? or consultant_id is null) order by name",
        (kind, consultant_id), fetch="all",
    )
    return rows or []


def log_extraction(consultant_id: str | None, tool: str, reason: str, excerpt: str) -> None:
    q("insert into extraction_log (id, consultant_id, tool, reason, input_excerpt, created_at)"
      " values (?,?,?,?,?,?)", (uid(), consultant_id, tool, reason, excerpt[:400], _now()))


def purge_expired(keep_log_days: int = 30) -> None:
    """Housekeeping so tables don't grow forever: drop expired OAuth codes, expired/long-
    revoked access tokens, and age out the extraction log. Cheap; runs at boot."""
    now = _now()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_log_days)).isoformat()
    q("delete from oauth_codes where expires_at <= ?", (now,))
    q("delete from access_tokens where expires_at <= ?", (now,))
    q("delete from access_tokens where revoked_at is not null and revoked_at <= ?", (cutoff,))
    q("delete from extraction_log where created_at <= ?", (cutoff,))
