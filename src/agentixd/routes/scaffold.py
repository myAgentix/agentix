"""Scaffold routes — /admin/scaffold/driver and /admin/scaffold/agent."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from agentixd.routes._errors import validation_error
from agentixd.scaffold import render_agent, render_driver

router = APIRouter(prefix="/admin/scaffold", tags=["scaffold"])


class ScaffoldDriverRequest(BaseModel):
    name: str
    modality: str = "chat"
    description: str = ""


class ScaffoldAgentRequest(BaseModel):
    name: str
    description: str = ""


@router.post("/driver")
async def scaffold_driver(body: ScaffoldDriverRequest) -> dict[str, str]:
    """Generate a driver stub. Returns {filename, content} — caller writes to disk."""
    try:
        filename, content = render_driver(body.name, body.modality)
    except ValueError as exc:
        raise validation_error(str(exc)) from exc
    return {"filename": filename, "content": content}


@router.post("/agent")
async def scaffold_agent(body: ScaffoldAgentRequest) -> list[dict[str, str]]:
    """Generate an agent app skeleton. Returns list of {path, content} — caller writes to disk."""
    files = render_agent(body.name, body.description)
    return files
