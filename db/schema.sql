-- Qwintiq Vault — Postgres schema (reference / optional manual migration).
-- The app SELF-BOOTSTRAPS these tables on first boot via dal.init_db() (same DDL, run
-- against whatever DATABASE_URL points at — Render Postgres or Supabase), so this file is
-- normally NOT needed. Kept as documentation and as an optional manual migration if you
-- prefer to pre-create the schema. Mirrors db/dal.py's _TABLES exactly (FK + indexes incl.).

create table if not exists consultants (
    id text primary key,
    email text unique not null,
    full_name text,
    password_hash text,
    must_change_password integer not null default 1,
    status text not null default 'active',        -- 'active' | 'revoked'
    created_at text not null,
    revoked_at text
);

create table if not exists consultant_keys (
    id text primary key,
    consultant_id text not null references consultants(id) on delete cascade,
    label text,
    active integer not null default 1,
    created_at text not null,
    revoked_at text
);
create index if not exists idx_keys_consultant on consultant_keys(consultant_id);

create table if not exists admins (
    id text primary key,
    email text unique not null,
    full_name text,
    password_hash text not null,
    created_at text not null
);

create table if not exists oauth_clients (
    client_id text primary key,
    name text,
    redirect_uris text not null,                  -- JSON array
    created_at text not null
);

create table if not exists oauth_codes (
    code text primary key,
    client_id text not null,
    redirect_uri text not null,
    code_challenge text not null,
    consultant_id text not null,
    expires_at text not null
);

create table if not exists access_tokens (
    token_hash text primary key,                  -- sha256; raw token never stored
    consultant_id text not null,
    key_id text not null,
    created_at text not null,
    expires_at text not null,
    revoked_at text
);
create index if not exists idx_tokens_consultant on access_tokens(consultant_id);

create table if not exists framework_state (
    id text primary key,
    consultant_id text,                           -- null = shared/global
    kind text not null,                           -- 'icebreaker_setup' | 'partner_routine'
    name text not null,
    config text not null,                         -- JSON
    updated_at text not null,
    unique (consultant_id, kind, name)
);

create table if not exists extraction_log (
    id text primary key,
    consultant_id text,
    tool text not null,
    reason text not null,                         -- 'meta_guard' | 'model_flagged' | 'leak_filter'
    input_excerpt text,
    created_at text not null
);
