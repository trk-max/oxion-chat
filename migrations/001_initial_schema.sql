CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    created_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION,
    workspace_type TEXT NOT NULL DEFAULT 'chat',
    provider_id TEXT,
    project_root TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    conv_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tokens INTEGER DEFAULT 0,
    created_at DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key_enc TEXT,
    default_model TEXT,
    is_active SMALLINT DEFAULT 0,
    created_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS provider_daily_usage (
    provider_id TEXT PRIMARY KEY,
    token_budget INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    utc_date TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_edits (
    id BIGSERIAL PRIMARY KEY,
    conv_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    project_root TEXT NOT NULL,
    old_content TEXT,
    new_content TEXT NOT NULL,
    applied_at DOUBLE PRECISION NOT NULL,
    undone_at DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_workspace_edits_conv
    ON workspace_edits(conv_id, id DESC);
