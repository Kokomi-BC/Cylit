"""统一响应封装（规划 §20 约定：``{data, error, trace_id}``）。"""

from __future__ import annotations

from typing import Any

from fastapi import Request


def ok(data: Any = None, request: Request | None = None, **extra) -> dict:
    trace_id = ""
    if request is not None:
        trace_id = getattr(request.state, "trace_id", "") or ""
    payload = {"data": data, "error": None, "trace_id": trace_id}
    payload.update(extra)
    return payload


def envelope(data: Any = None, message: str | None = None,
             request: Request | None = None) -> dict:
    trace_id = getattr(request.state, "trace_id", "") if request is not None else ""
    return {"data": data, "error": None, "trace_id": trace_id, "message": message}
