"""Operational state in SQLite (WAL + FTS5) — the kernel store.

Holds everything *operational* — sessions, per-turn traces, safety events,
errors — behind a relational schema the dashboard and resumability code
depend on. This is the **app-agnostic** base: the ``sessions`` row carries only
generic fields (id, ``customer_id``, status, token/cost totals, checkpoint) plus
an ``app_meta`` JSON blob the app fills with its own scope (e.g. the migration
app's source/target version + target models). Apps subclass this store to add
their own tables (see ``ludo.storage.ludo_sqlite.LudoSqliteStore`` which adds the
``diagnoses`` + ``applied_memory_rules`` tables and their queries).

``safety_events.type`` is an open string. The kernel emits a few generic types
(``dry_run_block``, ``per_batch_verify_fail``, …); apps pass their own types
(e.g. the migration app's ``xmlid_rollback``) as plain strings.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import aiosqlite
import structlog

if TYPE_CHECKING:
    from agentix.drivers.relational import RelationalDriver

log = structlog.get_logger(__name__)

SessionStatus = Literal["running", "paused", "completed", "failed"]
TurnRole = Literal["user", "assistant", "tool"]
# Per-session human-touchpoint classification — the autonomy product
# metric. ``none`` is the goal: a clean run with zero operator involvement.
# Every other value is one human touchpoint the autonomous loop did not close.
InterventionType = Literal["none", "aborted", "novel", "stuck", "partial"]
# ``safety_events.type`` is an open string (app-extensible). These are the
# generic types the kernel SafetyGate emits; apps add their own.
SafetyType = str
KERNEL_SAFETY_TYPES: frozenset[str] = frozenset(
    {
        "dry_run_block",
        "pre_flight_abort",
        "per_batch_verify_fail",
        "session_end_verify_fail",
        "memory_lock_timeout",
        "provider_failover",  # ChatFailoverChain fell over to a fallback
    }
)

# Current live schema version — bump when a migration adds or alters a
# table, and append a migration block in ``_apply_migrations``.
# Databases with no schema_version row get initialised to
# _SCHEMA_VERSION: they are already at the latest shape because
# CREATE TABLE IF NOT EXISTS took care of it.
#
# v12 = the kernel/app split: the Odoo columns
# (source_version/target_version/target_models) collapse into the generic
# ``app_meta`` JSON, and the ``diagnoses`` + ``applied_memory_rules`` tables
# move to the app subclass (``ludo.storage.ludo_sqlite``). ``customer_id`` is
# kept as the generic opaque tenant id. App-owned historical steps
# (v3/v7/v9/v10/v11) run in ``_migrate_app``.
#
# v13 = session-binding foundation: two generic, nullable link columns.
# ``control_plane_id`` binds the agent-side Session back to the control-plane
# Migration id (the gateway's ``ludo_session_id``) so a resumable event stream
# and observability can correlate the two halves without a side mapping.
# ``parent_session_id`` names the spawning Session for A2A delegation
# (self-referential FK); NULL for top-level runs. Both are kernel-generic —
# an app that uses neither simply leaves them NULL.
#
# v14 = session lease + orphan reaping (isolation.md I7): ``lease_expires_at``
# (ISO) + ``leased_by`` (worker id). A worker claims the lease when it starts a
# run and renews it each turn; a reaper transitions ``running`` sessions whose
# lease has expired to ``failed`` (their worker died). NULL lease = unleased
# (single-flight / local runs) — the reaper ignores those.
#
# v15 = terminology: ``safety_events.kind`` renamed to ``type`` (say "type",
# not "kind"). SQLite RENAME COLUMN rewrites the index definition in place.
_SCHEMA_VERSION = 15

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
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
        -- opaquely; apps read their own keys. '{}' = no app scope
        -- (auto-discovery / spike / probe). NOT NULL with a default so the
        -- column read is always safe.
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
        -- Session executes. Lets the control plane project a resumable event
        -- stream and correlate observability without keeping a separate
        -- mapping. NULL for runs with no control plane (local CLI).
        control_plane_id TEXT,
        -- A2A delegation link: the Session that spawned this one. NULL for
        -- top-level runs. Self-referential so a delegated child can be walked
        -- back to its parent. Enforcement of crossing rules lives above the DB.
        parent_session_id TEXT REFERENCES sessions(id),
        -- Session lease (isolation.md I7): ISO timestamp until which the owning
        -- worker holds this run. Renewed each turn; a reaper fails 'running'
        -- rows past this. NULL = unleased (single-flight / local); reaper skips.
        lease_expires_at TEXT,
        -- Worker id (hostname/pid) currently holding the lease. NULL = unleased.
        leased_by TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_customer ON sessions (customer_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions (status)",
    # NOTE: the idx_sessions_control_plane index is created AFTER migrations run
    # (see initialize()), not here — control_plane_id is a v13-added column and
    # is absent when this block runs against a pre-v13 DB.
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns (session_id, turn_index)",
    """
    CREATE TABLE IF NOT EXISTS safety_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        turn_id INTEGER REFERENCES turns(id) ON DELETE SET NULL,
        tool_name TEXT,
        tool_input TEXT,
        type TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_safety_events_session ON safety_events (session_id, type)",
    """
    CREATE TABLE IF NOT EXISTS errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        turn_id INTEGER REFERENCES turns(id) ON DELETE SET NULL,
        model TEXT,
        external_id TEXT,
        error_class TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_errors_session ON errors (session_id)",
    # Target-health log — populated by the `omg migrate` pre-flight
    # ping. A circuit breaker reads back the most recent rows per URL
    # to decide whether to let a new session start.
    """
    CREATE TABLE IF NOT EXISTS target_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT NOT NULL,
        ok INTEGER NOT NULL,
        error_class TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_target_health_url_time ON target_health (url, created_at)",
    # v9: tool progress events. Tools call ctx.progress(...) to write
    # rows for in-flight visibility. percent is 0.0..1.0 or NULL.
    """
    CREATE TABLE IF NOT EXISTS tool_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        tool_name TEXT NOT NULL,
        percent REAL,
        message TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_progress_session ON tool_progress (session_id, created_at)",
    # One row per billed LLM call: where it was served and what it cost.
    #
    # `provider_id` / `location` are per-call data-residency facts reported by
    # the gateway (they can differ between two calls to the SAME model), so they
    # cannot be inferred once from config — durable rows are the audit evidence.
    # `credits` is the gateway's own billed amount in ITS unit; `cost_usd` is
    # that converted with the operator's credit rate, or the local estimate when
    # no rate is configured. Both are kept so a rate change can be re-derived.
    # No ALTER migration is needed: initialize() runs every CREATE IF NOT EXISTS.
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls (session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_location ON llm_calls (location, created_at)",
    # FTS5 virtual table mirroring the searchable columns on `turns`. Kept in
    # sync via AFTER INSERT / UPDATE / DELETE triggers below.
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
        session_id,
        tool_name,
        content_inline,
        content='turns',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
        INSERT INTO turns_fts (rowid, session_id, tool_name, content_inline)
        VALUES (new.id, new.session_id, COALESCE(new.tool_name, ''), COALESCE(new.content_inline, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
        INSERT INTO turns_fts (turns_fts, rowid, session_id, tool_name, content_inline)
        VALUES ('delete', old.id, old.session_id, COALESCE(old.tool_name, ''), COALESCE(old.content_inline, ''));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
        INSERT INTO turns_fts (turns_fts, rowid, session_id, tool_name, content_inline)
        VALUES ('delete', old.id, old.session_id, COALESCE(old.tool_name, ''), COALESCE(old.content_inline, ''));
        INSERT INTO turns_fts (rowid, session_id, tool_name, content_inline)
        VALUES (new.id, new.session_id, COALESCE(new.tool_name, ''), COALESCE(new.content_inline, ''));
    END
    """,
)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


class SqliteStore:
    """Async operational-state store backed by a single SQLite file.

    Opens the database on ``initialize()``, enables WAL + a busy timeout +
    foreign keys, and runs the schema DDL idempotently. Keeps a single
    long-lived connection; ``aiosqlite`` serialises individual statements on
    its worker thread.

    Concurrency: the worker is single-flight (one run at a time), so there is
    no concurrent access to serialise and **no in-process lock**. A logical
    ``execute``+``commit`` is therefore atomic only under that single-writer
    discipline — running sessions concurrently in one process (``gather``)
    would need a per-task connection or a transaction lock, because
    ``commit()`` flushes *all* pending writes on the shared connection
    (agentix#39 / isolation.md I2, deferred). Cross-process safety (a 2nd
    worker on the same WAL file) is covered by ``busy_timeout``: a concurrent
    writer waits rather than failing immediately with ``SQLITE_BUSY``.
    """

    def __init__(self, path: str | Path | None = None, *, driver: RelationalDriver | None = None) -> None:
        if driver is None:
            if path is None:
                raise TypeError("SqliteStore needs a path or a RelationalDriver")
            # Lazy import: keeps storage importable without the drivers
            # package unless actually constructed from a path.
            from agentix.drivers.adapters.intrinsic.sqlite import SqliteRelationalDriver

            driver = SqliteRelationalDriver(path)
        self._driver = driver
        self.path = Path(path) if path is not None else Path(getattr(driver, "path", ""))

    @property
    def driver(self) -> RelationalDriver:
        """The relational transport underneath — exposed for registry wiring."""
        return self._driver

    @property
    def _db(self) -> aiosqlite.Connection | None:
        """Back-compat escape hatch: the raw connection when (and only when)
        the transport is the SQLite adapter — seam-#10 subclasses use it for
        cursor-level migration steps (knowingly sqlite-dialect). None before
        ``initialize()`` or on a non-SQLite transport."""
        try:
            return getattr(self._driver, "raw", None)
        except RuntimeError:  # adapter present but not connected yet
            return None

    # ─────────────────────────────── lifecycle ─────────────────────────────

    async def initialize(self) -> None:
        drv = self._driver
        await drv.connect()
        # Base (kernel) DDL first, then any app-specific tables the subclass adds.
        for stmt in (*_SCHEMA_STATEMENTS, *self._extra_schema_statements()):
            await drv.execute(stmt)
        await self._apply_migrations()
        # Indexes on migration-added columns must be created AFTER migrations —
        # the column is absent when _SCHEMA_STATEMENTS runs against a pre-v13 DB.
        # control_plane_id arrives in v13; index it once it is guaranteed present.
        await drv.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_control_plane "
            "ON sessions (control_plane_id) WHERE control_plane_id IS NOT NULL"
        )
        await drv.commit()
        log.info("sqlite.initialized", path=str(self.path), schema_version=_SCHEMA_VERSION)

    def _extra_schema_statements(self) -> tuple[str, ...]:
        """App-specific CREATE TABLE / INDEX DDL. Base kernel adds none.

        Apps override to append their own tables (e.g. the migration app's
        ``diagnoses`` + ``applied_memory_rules``). Runs right after the kernel
        DDL, before migrations."""
        return ()

    async def _migrate_app(self, current: int) -> None:
        """App-specific migration steps for an existing DB. Base is a no-op.

        Runs inside ``_apply_migrations`` after the generic session-column
        migrations, before the ``schema_version`` bump. ``current`` is the
        DB's schema version before this run. Apps override to migrate their
        own tables (guard every step by column/table existence so it is a
        safe no-op on a fresh DB)."""
        return

    async def _apply_migrations(self) -> None:
        """Bump ``schema_version`` and apply any pending migrations.

        Migrations run in order, inside the same transaction as
        ``initialize()``. ``CREATE TABLE IF NOT EXISTS`` above defines
        the shape for FRESH databases; this block ALTERs existing tables.
        Every step is guarded by column/table existence so it no-ops on a
        fresh DB (which starts at ``current`` 0 and still runs the blocks).
        """
        drv = self._driver
        row = await drv.query_one("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        current = int(row["v"]) if row else 0
        if current >= _SCHEMA_VERSION:
            return

        # v5 → v6: sessions.intervention_type — the human-touchpoint metric.
        if current < 6:
            existing = await self._session_columns()
            if "intervention_type" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN intervention_type TEXT NOT NULL DEFAULT 'none'")

        # v6 → v7: sessions.outcome — the honest session-end outcome label.
        if current < 7:
            existing = await self._session_columns()
            if "outcome" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN outcome TEXT")

        # v11 → v12: the kernel/app split. Add the generic ``app_meta`` column;
        # the app backfills it from the legacy source_version/target_version/
        # target_models columns in ``_migrate_app`` (those columns stay,
        # vestigial, on old DBs — the kernel simply stops reading them).
        if current < 12:
            existing = await self._session_columns()
            if "app_meta" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN app_meta TEXT NOT NULL DEFAULT '{}'")

        # v12 → v13: session-binding columns. Both nullable, no default needed —
        # existing rows read NULL (no control-plane link, top-level run).
        # SQLite can't add a column with an inline REFERENCES via ALTER, so the
        # parent_session_id FK is only declared on the fresh-DB CREATE above;
        # on migrated DBs it is a plain nullable TEXT (the app enforces linkage).
        if current < 13:
            existing = await self._session_columns()
            if "control_plane_id" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN control_plane_id TEXT")
            if "parent_session_id" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")

        # v13 → v14: session lease columns (I7). Both nullable; existing rows read
        # NULL (unleased) so the reaper ignores them until a worker claims a lease.
        if current < 14:
            existing = await self._session_columns()
            if "lease_expires_at" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN lease_expires_at TEXT")
            if "leased_by" not in existing:
                await drv.execute("ALTER TABLE sessions ADD COLUMN leased_by TEXT")

        # v14 → v15: safety_events.kind → type. Guarded by column presence so
        # it no-ops on fresh DBs (created with ``type`` already).
        if current < 15:
            cols = await self._table_columns("safety_events")
            if "kind" in cols and "type" not in cols:
                await drv.execute("ALTER TABLE safety_events RENAME COLUMN kind TO type")

        # App-owned table migrations (diagnoses / applied_memory_rules, and the
        # app_meta backfill from legacy Odoo columns) — no-op in the base.
        await self._migrate_app(current)

        await drv.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))

    async def _table_columns(self, table: str) -> set[str]:
        """Current column names on ``table``."""
        cols = await self._driver.query(f"PRAGMA table_info({table})")
        return {str(r["name"]) for r in cols}

    async def _session_columns(self) -> set[str]:
        """Current column names on the ``sessions`` table."""
        return await self._table_columns("sessions")

    async def close(self) -> None:
        await self._driver.aclose()

    async def __aenter__(self) -> SqliteStore:
        await self.initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _conn(self) -> aiosqlite.Connection:
        """Sqlite-dialect escape hatch (see ``_db``); kernel methods use the
        driver verbs instead."""
        db = self._db
        if db is None:
            raise RuntimeError("SqliteStore.initialize() not called (or non-SQLite transport)")
        return db

    # ─────────────────────────────── sessions ──────────────────────────────

    async def create_session(
        self,
        *,
        session_id: str,
        customer_id: str,
        status: SessionStatus = "running",
        app_meta: dict[str, Any] | None = None,
        control_plane_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        """Insert a new session row.

        ``customer_id`` is the opaque per-tenant id (no PII). ``app_meta`` is an
        app-specific scope object persisted as JSON in ``sessions.app_meta`` —
        the kernel treats it opaquely; the migration app stores its
        source/target version + target models there. Defaults to ``{}``
        (sessions without app scope: auto-discovery probes, spikes, etc.).

        ``control_plane_id`` binds this Session to the control-plane Migration id
        (the gateway's ``ludo_session_id``); NULL for local/no-control-plane
        runs. ``parent_session_id`` names the spawning Session for A2A
        delegation; NULL for top-level runs.
        """
        drv = self._driver
        app_meta_json = json.dumps(app_meta or {}, ensure_ascii=False, default=str)
        await drv.execute(
            """
            INSERT INTO sessions (
                id, customer_id, status, started_at,
                total_input_tokens, total_output_tokens, total_cost_usd, app_meta,
                control_plane_id, parent_session_id
            )
            VALUES (?, ?, ?, ?, 0, 0, 0.0, ?, ?, ?)
            """,
            (session_id, customer_id, status, _now(), app_meta_json, control_plane_id, parent_session_id),
        )
        await drv.commit()

    async def update_session(
        self,
        session_id: str,
        *,
        status: SessionStatus | None = None,
        checkpoint: str | None = None,
        input_tokens_delta: int = 0,
        output_tokens_delta: int = 0,
        cost_usd_delta: float = 0.0,
        mark_ended: bool = False,
    ) -> None:
        drv = self._driver
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if checkpoint is not None:
            sets.append("checkpoint = ?")
            params.append(checkpoint)
        if input_tokens_delta:
            sets.append("total_input_tokens = total_input_tokens + ?")
            params.append(input_tokens_delta)
        if output_tokens_delta:
            sets.append("total_output_tokens = total_output_tokens + ?")
            params.append(output_tokens_delta)
        if cost_usd_delta:
            sets.append("total_cost_usd = total_cost_usd + ?")
            params.append(cost_usd_delta)
        if mark_ended:
            sets.append("ended_at = ?")
            params.append(_now())
        if not sets:
            return
        params.append(session_id)
        await drv.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
        await drv.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        drv = self._driver
        row = await drv.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return row

    async def get_session_by_control_plane_id(self, control_plane_id: str) -> dict[str, Any] | None:
        """The most recent Session bound to ``control_plane_id`` (the gateway
        Migration id), or None. Powers resume-on-redelivery: a job carrying a
        known control-plane id maps back to the agent Session it already started.
        Returns the newest by ``started_at`` — the live one when several share
        the id (e.g. the compose path's per-model sessions)."""
        drv = self._driver
        row = await drv.query_one(
            "SELECT * FROM sessions WHERE control_plane_id = ? ORDER BY started_at DESC LIMIT 1",
            (control_plane_id,),
        )
        return row

    # ─────────────────────────── session lease (I7) ────────────────────────

    async def claim_session_lease(self, session_id: str, *, leased_by: str, ttl_seconds: float) -> None:
        """Take/refresh the lease on a session: set ``leased_by`` + push
        ``lease_expires_at`` to now + ttl. Called when a worker begins or resumes
        a run so a reaper can tell a live run from an orphaned one (I7)."""
        drv = self._driver
        expires = (datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        await drv.execute(
            "UPDATE sessions SET leased_by = ?, lease_expires_at = ? WHERE id = ?",
            (leased_by, expires, session_id),
        )
        await drv.commit()

    async def renew_session_lease(self, session_id: str, *, ttl_seconds: float) -> None:
        """Heartbeat: extend ``lease_expires_at`` to now + ttl, keeping the
        current owner. The worker calls this each turn so a long but live run is
        never reaped."""
        drv = self._driver
        expires = (datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        await drv.execute(
            "UPDATE sessions SET lease_expires_at = ? WHERE id = ?",
            (expires, session_id),
        )
        await drv.commit()

    async def reap_expired_sessions(self) -> list[str]:
        """Transition ``running`` sessions whose lease has expired to ``failed``
        (their worker died) and return the reaped ids. Unleased rows
        (``lease_expires_at IS NULL``) are ignored — single-flight / local runs
        that opt out of leasing. Safe to run periodically from any worker."""
        drv = self._driver
        now = _now()
        rows = await drv.query(
            "SELECT id FROM sessions WHERE status = 'running' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (now,),
        )
        ids = [str(r["id"]) for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            await drv.execute(
                f"UPDATE sessions SET status = 'failed', ended_at = ? WHERE id IN ({placeholders})",
                (now, *ids),
            )
            await drv.commit()
        return ids

    async def list_sessions(
        self,
        *,
        customer_id: str | None = None,
        status: SessionStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        drv = self._driver
        clauses: list[str] = []
        params: list[Any] = []
        if customer_id is not None:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        sql = f"SELECT * FROM sessions {where} ORDER BY started_at DESC LIMIT ?"
        return await drv.query(sql, params)

    async def set_intervention_type(self, session_id: str, intervention_type: InterventionType) -> None:
        """Classify a session's human-touchpoint outcome. Called once at
        session close by agent_runner. ``none`` means the autonomous
        loop closed without an operator; any other value is one human
        touchpoint and counts against the autonomy zero-intervention bar."""
        drv = self._driver
        await drv.execute(
            "UPDATE sessions SET intervention_type = ? WHERE id = ?",
            (intervention_type, session_id),
        )
        await drv.commit()

    async def set_outcome(self, session_id: str, outcome: str) -> None:
        """Record a session's honest outcome — aborted | incomplete |
        migrated — computed from session-end verification. Called once at
        session close by agent_runner, after the verify results are in."""
        drv = self._driver
        await drv.execute(
            "UPDATE sessions SET outcome = ? WHERE id = ?",
            (outcome, session_id),
        )
        await drv.commit()

    async def intervention_summary(self, *, customer_id: str | None = None) -> dict[str, int]:
        """Count sessions per intervention_type — the autonomy product
        metric. Returns every InterventionType key (zero-filled) so a
        consumer can render the full breakdown without missing-key
        guards. Filter by ``customer_id`` for a single tenant's trend;
        omit it for the cross-tenant view.

        The convergence signal: ``none`` rising as a fraction of the
        total across successive accounts means the loop is learning.
        """
        drv = self._driver
        clauses: list[str] = []
        params: list[Any] = []
        if customer_id is not None:
            clauses.append("customer_id = ?")
            params.append(customer_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT intervention_type, COUNT(*) AS n FROM sessions {where} GROUP BY intervention_type"
        rows = await drv.query(sql, params)
        summary: dict[str, int] = {
            "none": 0,
            "aborted": 0,
            "novel": 0,
            "stuck": 0,
            "partial": 0,
        }
        for row in rows:
            key = str(row["intervention_type"] or "none")
            if key in summary:
                summary[key] += int(row["n"] or 0)
            else:  # forward-compat: an unknown value still counts somewhere
                summary[key] = int(row["n"] or 0)
        return summary

    # ──────────────────────────────── turns ────────────────────────────────

    async def append_turn(
        self,
        *,
        session_id: str,
        turn_index: int,
        role: TurnRole,
        content_inline: str | None = None,
        content_ref: str | None = None,
        tool_name: str | None = None,
        tool_ok: bool | None = None,
        latency_ms: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> int:
        drv = self._driver
        cur = await drv.execute(
            """
            INSERT INTO turns (
                session_id, turn_index, role,
                content_ref, content_inline,
                tool_name, tool_ok, latency_ms,
                input_tokens, output_tokens, cost_usd, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn_index,
                role,
                content_ref,
                content_inline,
                tool_name,
                int(tool_ok) if tool_ok is not None else None,
                latency_ms,
                input_tokens,
                output_tokens,
                cost_usd,
                _now(),
            ),
        )
        await drv.commit()
        assert cur.lastrowid is not None  # SQLite always assigns a rowid
        return int(cur.lastrowid)

    async def append_tool_progress(
        self,
        *,
        session_id: str,
        tool_name: str,
        percent: float | None = None,
        message: str | None = None,
    ) -> None:
        """Record a progress tick. Best-effort — never raises."""
        try:
            drv = self._driver
            await drv.execute(
                "INSERT INTO tool_progress (session_id, tool_name, percent, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, tool_name, percent, message, _now()),
            )
            await drv.commit()
        except Exception as exc:
            log.warning(
                "sqlite.tool_progress_failed",
                session_id=session_id,
                tool=tool_name,
                error=type(exc).__name__,
                message=str(exc)[:300],
            )

    async def append_llm_call(
        self,
        *,
        session_id: str,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        cost_usd: float = 0.0,
        cost_source: str = "estimated",
        credits: float | None = None,
        provider_id: str | None = None,
        location: str | None = None,
        energy_kwh: float | None = None,
        carbon_g_co2: float | None = None,
        system_fingerprint: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        """Record one billed LLM call. Best-effort — never raises.

        ``cost_source`` distinguishes a gateway-reported amount from a locally
        estimated one, so a spend report can say which rows are authoritative.
        Failure to write must not break the model round-trip, matching
        :meth:`append_tool_progress`; the loud log is how an operator notices.
        """
        try:
            drv = self._driver
            await drv.execute(
                "INSERT INTO llm_calls (session_id, provider, model, provider_id, location, "
                "input_tokens, output_tokens, cached_tokens, credits, cost_usd, cost_source, "
                "energy_kwh, carbon_g_co2, system_fingerprint, finish_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    provider,
                    model,
                    provider_id,
                    location,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    credits,
                    cost_usd,
                    cost_source,
                    energy_kwh,
                    carbon_g_co2,
                    system_fingerprint,
                    finish_reason,
                    _now(),
                ),
            )
            await drv.commit()
        except Exception as exc:
            log.warning(
                "sqlite.llm_call_failed",
                session_id=session_id,
                provider=provider,
                model=model,
                error=type(exc).__name__,
                message=str(exc)[:300],
            )

    async def search_turns(
        self,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """FTS5 full-text search over turns.

        The caller's query is wrapped as an FTS5 *phrase* so that punctuation
        like ``-``, ``:``, ``.`` stays literal rather than being interpreted
        as FTS5 operators (``-`` would otherwise mean NOT).

        ``session_id`` is an optional filter applied post-FTS.
        """
        drv = self._driver
        phrase = '"' + query.replace('"', '""') + '"'
        clauses = ["turns_fts MATCH ?"]
        params: list[Any] = [phrase]
        if session_id is not None:
            clauses.append("t.session_id = ?")
            params.append(session_id)
        params.append(limit)
        sql = f"""
            SELECT t.*, bm25(turns_fts) AS rank
            FROM turns_fts
            JOIN turns t ON t.id = turns_fts.rowid
            WHERE {" AND ".join(clauses)}
            ORDER BY rank
            LIMIT ?
        """
        return await drv.query(sql, params)

    # ────────────────────────────── safety + errors ────────────────────────

    async def append_safety_event(
        self,
        *,
        session_id: str,
        type: SafetyType,
        turn_id: int | None = None,
        tool_name: str | None = None,
        tool_input: str | None = None,
        detail: str | None = None,
    ) -> int:
        drv = self._driver
        cur = await drv.execute(
            """
            INSERT INTO safety_events (
                session_id, turn_id, tool_name, tool_input, type, detail, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, turn_id, tool_name, tool_input, type, detail, _now()),
        )
        await drv.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def append_error(
        self,
        *,
        session_id: str,
        error_class: str,
        error_message: str,
        turn_id: int | None = None,
        model: str | None = None,
        external_id: str | None = None,
    ) -> int:
        drv = self._driver
        cur = await drv.execute(
            """
            INSERT INTO errors (
                session_id, turn_id, model, external_id, error_class, error_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, turn_id, model, external_id, error_class, error_message, _now()),
        )
        await drv.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def count_safety_events(
        self,
        session_id: str,
        *,
        type: SafetyType | None = None,
    ) -> int:
        drv = self._driver
        if type is None:
            sql = "SELECT COUNT(*) AS n FROM safety_events WHERE session_id = ?"
            params: tuple[Any, ...] = (session_id,)
        else:
            sql = "SELECT COUNT(*) AS n FROM safety_events WHERE session_id = ? AND type = ?"
            params = (session_id, type)
        row = await drv.query_one(sql, params)
        return int(row["n"]) if row else 0

    # ───────────────────────── target health (PR-J) ────────────────────────

    async def record_target_health(
        self,
        *,
        url: str,
        ok: bool,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> int:
        """Append a row to the target_health log; used by the pre-flight
        probe in ``omg migrate`` (PR-J)."""
        drv = self._driver
        cur = await drv.execute(
            """
            INSERT INTO target_health (url, ok, error_class, error_message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url, 1 if ok else 0, error_class, error_message, _now()),
        )
        await drv.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    async def recent_target_failures(
        self,
        *,
        url: str,
        within_seconds: float = 3600.0,
    ) -> int:
        """Return the count of consecutive most-recent failures for this URL
        within ``within_seconds``. A single success resets the streak.

        Used by the circuit breaker: if this returns ≥ 3, refuse to start
        a new session without ``--force-unhealthy-target``.
        """
        drv = self._driver
        cutoff = (datetime.now(tz=UTC) - timedelta(seconds=within_seconds)).isoformat()
        rows = await drv.query(
            "SELECT ok FROM target_health WHERE url = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 10",
            (url, cutoff),
        )
        streak = 0
        for row in rows:
            if int(row["ok"]) == 0:
                streak += 1
            else:
                break
        return streak
