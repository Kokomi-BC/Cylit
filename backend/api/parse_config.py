"""解析引擎的可调默认值：本地（PyMuPDF）与 MinerU 各一套。

- `GET /parse/config`：当前生效的默认值（库里存的覆盖代码默认，见 `parse_prefs`）；
- `PUT /parse/config`：**只提交要改的键**（省略 = 不动），返回新的完整配置。

为什么不并进 `/mineru/config`：那个接口的形状是"MinerU 这一个服务"（token / 掩码 /
验证），而这里的键是**两个引擎共用的导入默认值**（读页开关、读页 dpi、插图开关、三项
MinerU 参数）。合成一个响应，前端就得从同一个对象里猜哪几个字段属于谁。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Request

from backend.api.response import envelope
from backend.services.paper import parse_prefs

log = logging.getLogger(__name__)

router = APIRouter(tags=["parse-config"])


@router.get("/parse/config")
async def get_parse_config(request: Request):
    """当前生效的解析默认值（不含任何密钥）。"""
    return envelope(parse_prefs.public(), request=request)


@router.put("/parse/config")
async def put_parse_config(request: Request, payload: dict = Body(default={})):
    """保存解析默认值（字段可选，省略 = 不动），返回新配置。"""
    cfg = parse_prefs.save(payload)
    applied = cfg.pop("applied", {})
    message = "解析默认值已更新：" + "、".join(sorted(applied)) if applied else "没有需要保存的改动"
    return envelope(cfg, message=message, request=request)
