"""首页概览 API（规划：首页刊头的数据 rail）。

只读，返回首页要用的几个计数，见 `services/home/home_service.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.api.response import envelope
from backend.services.home import home_service

router = APIRouter(tags=["home"])


@router.get("/home/summary")
async def summary(request: Request):
    return envelope(home_service.summary(), request=request)
