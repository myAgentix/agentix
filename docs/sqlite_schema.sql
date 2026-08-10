-- Agentix kernel — SQLite schema (reference mirror)
--
-- SOURCE OF TRUTH: src/agentix/storage/sqlite_store.py (`_SCHEMA_STATEMENTS`) — created in code.
-- Drift-checked mirror (tests/unit/test_schema_drift.py); regenerate when the code schema
-- changes. Schema version: 12. App-specific tables (the migration app's
-- `diagnoses` / `applied_memory_rules`) live in ludo.storage.ludo_sqlite, not here.

CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        total_input_tokens INTEGER NOT NULL DEFAULT 0,
        total_output_tokens INTEGER NOT NULL DEFAULT 0,
        total_cost_usd REAL NOT NULL DEFAULT 0.0,
        checkpoint TEXT,
        -- App-specific session scope as a JSON object. The kernel treats it
        -- opaquely; apps read their own keys (the migration app stores
        -- source_version / target_version / target_models here). '{}' = no
        -- app scope (auto-discovery / spike / probe). NOT NULL with a default
        -- so the column read is always safe.
        app_meta TEXT NOT NULL DEFAULT '{}',
        -- Human-touchpoint classification for this session — the autonomy
        -- product metric. Counts of non-'none' values per account show whether
        -- the autonomous loop is converging. Written at session close.
        intervention_type TEXT NOT NULL DEFAULT 'none',
        -- Honest session outcome label, derived from session-end verification
        -- rather than the agent's terminal message. NULL until computed at
        -- session close; rows that predate it stay NULL (unjudged).
        outcome TEXT,
        -- Control-plane binding: the control-plane-assigned run id this
        -- Session executes. NULL for local runs.
        control_plane_id TEXT,
        -- A2A delegation link: the Session that spawned this one (self-ref).
        -- NULL for top-level runs.
        parent_session_id TEXT REFERENCES sessions(id),
        -- Session lease (I7): ISO expiry the owning worker holds until (renewed
        -- each turn); a reaper fails 'running' rows past it. NULL = unleased.
        lease_expires_at TEXT,
        -- Worker id holding the lease. NULL = unleased.
        leased_by TEXT
    );
CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions (customer_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions (status);
CREATE INDEX IF NOT EXISTS idx_sessions_control_plane ON sessions (control_plane_id) WHERE control_plane_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        turn_index INTEGER NOT NULL,
        role TEXT NOT NULL,
        content_ref TEXT,
        content_inline TEXT,
        tool_name TEXT,
        tool_ok INTEGER,
        latency_ms INTEGER,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0.0,
        created_at TEXT NOT NULL
    );
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns (session_id, turn_index);
CREATE TABLE IF NOT EXISTS safety_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        turn_id INTEGER REFERENCES turns(id) ON DELETE SET NULL,
        tool_name TEXT,
        tool_input TEXT,
        type TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    );
CREATE INDEX IF NOT EXISTS idx_safety_events_session ON safety_events (session_id, type);
CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        turn_id INTEGER REFERENCES turns(id) ON DELETE SET NULL,
        model TEXT,
        external_id TEXT,
        error_class TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL
    );
CREATE INDEX IF NOT EXISTS idx_errors_session ON errors (session_id);
CREATE TABLE IF NOT EXISTS target_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        ok INTEGER NOT NULL,
        error_class TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL
    );
CREATE INDEX IF NOT EXISTS idx_target_health_url_time ON target_health (url, created_at);
CREATE TABLE IF NOT EXISTS tool_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        tool_name TEXT NOT NULL,
        percent REAL,
        message TEXT,
        created_at TEXT NOT NULL
    );
CREATE INDEX IF NOT EXISTS idx_tool_progress_session ON tool_progress (session_id, created_at);
-- One row per billed LLM call. `provider_id`/`location` are per-call data-residency
-- facts reported by the gateway and can differ between two calls to the same model.
-- `credits` is the gateway's billed amount in its own unit; `cost_usd` is that
-- converted with the operator's credit rate, or a local estimate (`cost_source`).
CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        provider_id TEXT,
        location TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        credits REAL,
        cost_usd REAL NOT NULL DEFAULT 0.0,
        cost_source TEXT NOT NULL,
        energy_kwh REAL,
        carbon_g_co2 REAL,
        system_fingerprint TEXT,
        finish_reason TEXT,
        created_at TEXT NOT NULL
    );
CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_location ON llm_calls (location, created_at);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
        session_id,
        tool_name,
        content_inline,
        content='turns',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
        INSERT INTO turns_fts (rowid, session_id, tool_name, content_inline)
        VALUES (new.id, new.session_id, COALESCE(new.tool_name, ''), COALESCE(new.content_inline, ''));
    END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
        INSERT INTO turns_fts (turns_fts, rowid, session_id, tool_name, content_inline)
        VALUES ('delete', old.id, old.session_id, COALESCE(old.tool_name, ''), COALESCE(old.content_inline, ''));
    END;
CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
        INSERT INTO turns_fts (turns_fts, rowid, session_id, tool_name, content_inline)
        VALUES ('delete', old.id, old.session_id, COALESCE(old.tool_name, ''), COALESCE(old.content_inline, ''));
        INSERT INTO turns_fts (rowid, session_id, tool_name, content_inline)
        VALUES (new.id, new.session_id, COALESCE(new.tool_name, ''), COALESCE(new.content_inline, ''));
    END;
