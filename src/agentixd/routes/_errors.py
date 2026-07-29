"""Typed HTTPException factories for route handlers."""

from __future__ import annotations

from fastapi import HTTPException


def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def validation_error(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def service_unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=503, detail=detail)
