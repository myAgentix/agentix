"""Session routes — /run/sessions/*.

The daemon keeps an in-memory map of live Session objects (id → Session).
Each run_turn call retrieves the session, calls engine.run_turn(), and
updates the map. SQLite + MinIO handle durable persistence automatically
(the Engine calls save_session after every turn).
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agentixd.routes._errors import not_found, service_unavailable
from agentixd.routes._kernel_dep import get_kernel

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/run", tags=["sessions"])


class CreateSessionRequest(BaseModel):
    customer_id: str
    budget_usd: float | None = None
    app_meta: dict[str, Any] | None = None
    control_plane_id: str | None = None
    parent_session_id: str | None = None


class RunTurnRequest(BaseModel):
    message: str | None = None


def _kernel_required(request: Request) -> Any:
    kernel = get_kernel(request)
    if not kernel.ready:
        raise service_unavailable(f"kernel not ready: {kernel.error or 'still initializing'}")
    return kernel


def _deserialize_app_meta(raw: str | None) -> dict | None:
    return json.loads(raw) if raw else None


def _session_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row.get("ended_at"),
        "total_input_tokens": row.get("total_input_tokens", 0),
        "total_output_tokens": row.get("total_output_tokens", 0),
        "total_cost_usd": row.get("total_cost_usd", 0.0),
        "app_meta": _deserialize_app_meta(row.get("app_meta")),
        "control_plane_id": row.get("control_plane_id"),
        "parent_session_id": row.get("parent_session_id"),
    }


async def _get_or_resume_session(kernel: Any, session_id: str) -> Any:
    """Return in-memory session or resume it from the SQLite/MinIO checkpoint."""
    session = kernel._active_sessions.get(session_id)
    if session is not None:
        return session
    from agentix.core.session import resume_or_create

    row = await kernel.sqlite.get_session(session_id)
    if not row:
        raise not_found(f"session {session_id!r} not found")
    session = await resume_or_create(
        session_id,
        customer_id=row["customer_id"],
        sqlite=kernel.sqlite,
        minio=kernel.minio,
        budget_usd=float(row.get("total_cost_usd", kernel._cfg.budget_usd)),
    )
    kernel._active_sessions[session_id] = session
    return session


@router.post("/sessions", status_code=201)
async def create_session(body: CreateSessionRequest, request: Request) -> dict[str, Any]:
    """Create a new agent session."""
    kernel = _kernel_required(request)

    from agentix.core.session import create_session as _create

    session = await _create(
        kernel.sqlite,
        customer_id=body.customer_id,
        budget_usd=body.budget_usd or kernel._cfg.budget_usd,
        app_meta=body.app_meta,
        control_plane_id=body.control_plane_id,
        parent_session_id=body.parent_session_id,
    )
    kernel._active_sessions[session.id] = session

    # Build a per-session engine if the plugin registered a factory (e.g. ludo middleware chain).
    if kernel._session_engine_factory is not None:
        try:
            kernel._session_engines[session.id] = kernel._session_engine_factory(kernel, session, body.app_meta)
        except Exception as exc:
            log.warning("session engine factory failed — using global engine", session_id=session.id, error=str(exc))

    log.info("session created", session_id=session.id, customer_id=body.customer_id)

    row = await kernel.sqlite.get_session(session.id)
    return _session_row_to_dict(row)


@router.get("/sessions")
async def list_sessions(
    request: Request,
    customer_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List sessions from the SQLite store."""
    kernel = _kernel_required(request)
    rows = await kernel.sqlite.list_sessions(customer_id=customer_id, status=status, limit=limit)
    return [_session_row_to_dict(r) for r in rows]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    """Get a session by ID."""
    kernel = _kernel_required(request)
    row = await kernel.sqlite.get_session(session_id)
    if not row:
        raise not_found(f"session {session_id!r} not found")
    return _session_row_to_dict(row)


@router.post("/sessions/{session_id}/turn")
async def run_turn(session_id: str, body: RunTurnRequest, request: Request) -> dict[str, Any]:
    """Submit a turn to an active session.

    If the session is not in the in-memory map (e.g. daemon restarted),
    it is resumed from the MinIO checkpoint.
    """
    kernel = _kernel_required(request)

    # Retrieve or resume the session
    session = await _get_or_resume_session(kernel, session_id)

    if session.status not in ("running", "paused"):
        raise HTTPException(
            status_code=409,
            detail=f"session {session_id!r} is {session.status} — cannot submit a turn",
        )

    from agentix.core.types import Message

    user_message = Message(role="user", content=body.message) if body.message else None

    # Use the per-session engine (with app middleware) when available; fall back to global.
    engine = kernel._session_engines.get(session_id, kernel.engine)

    hook = kernel._pre_turn_hook
    try:
        if hook is not None:
            async with hook(kernel, session):
                turn = await engine.run_turn(session, user_message=user_message)
        else:
            turn = await engine.run_turn(session, user_message=user_message)
    except Exception as exc:
        log.error("run_turn failed", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Clean up per-session engine once the session reaches a terminal state.
    if session.status in ("completed", "failed"):
        kernel._session_engines.pop(session_id, None)

    log.info("turn complete", session_id=session_id, status=turn.status, turn_index=turn.turn_index)

    content = turn.assistant_message.content if turn.assistant_message else None
    return {
        "session_id": turn.session_id,
        "turn_index": turn.turn_index,
        "role": "assistant",
        "content": content,
        "status": turn.status,
        "input_tokens": turn.usage.input_tokens,
        "output_tokens": turn.usage.output_tokens,
        "cost_usd": turn.cost_usd,
        "latency_ms": turn.elapsed_ms,
    }


@router.get("/sessions/{session_id}/turns")
async def list_turns(session_id: str, request: Request) -> list[dict[str, Any]]:
    """List all turns for a session from SQLite."""
    kernel = _kernel_required(request)
    row = await kernel.sqlite.get_session(session_id)
    if not row:
        raise not_found(f"session {session_id!r} not found")

    # Query turns from SQLite
    import aiosqlite

    turns: list[dict[str, Any]] = []
    async with aiosqlite.connect(kernel._cfg.sqlite_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT turn_index, role, tool_name, tool_ok, input_tokens, output_tokens, cost_usd, latency_ms, created_at FROM turns WHERE session_id = ? ORDER BY turn_index",
            (session_id,),
        ) as cur:
            async for r in cur:
                turns.append(
                    {
                        "session_id": session_id,
                        "turn_index": r["turn_index"],
                        "role": r["role"],
                        "tool_name": r["tool_name"],
                        "tool_ok": r["tool_ok"],
                        "input_tokens": r["input_tokens"] or 0,
                        "output_tokens": r["output_tokens"] or 0,
                        "cost_usd": r["cost_usd"] or 0.0,
                        "latency_ms": r["latency_ms"],
                        "created_at": r["created_at"],
                        "status": "ok" if r["tool_ok"] is not False else "error",
                    }
                )
    return turns
