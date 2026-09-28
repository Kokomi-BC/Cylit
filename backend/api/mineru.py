"""MinerU 在线解析的配置与校验 API（规划 §20.1 的解析入口之一）。

三个动作，边界很清楚：

- ``GET /mineru/config``：当前引擎/模型版本/语言 + token 的**掩码**与来源；
- ``PUT /mineru/config``：保存 token（写 `data/secrets/mineru.key`）与三个设置项；
- ``POST /mineru/verify``：拿随机 task_id 去查一次任务，判断 token 是否可用
  （**不创建解析任务、不消耗配额**）。

⚠️ token 本体**只进不出**：接口永远只回掩码（`ab12…cd34`），不会把 token 回显给前端。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from backend.api.response import envelope
from backend.services.paper import mineru_service

router = APIRouter(tags=["mineru"])


@router.get("/mineru/config")
async def get_mineru_config(request: Request):
    return envelope(mineru_service.public_config(), request=request)


@router.put("/mineru/config")
async def put_mineru_config(request: Request, payload: dict = Body(default={})):
    """保存 MinerU 配置（字段可选：只传要改的那些）。

    ``{"token": "…"}`` 保存 token；``{"clear_token": true}`` 清除；
    ``{"engine_default": "mineru"|"local"}`` / ``{"model_version": "pipeline"|"vlm"}`` /
    ``{"language": "en"}`` 改设置项。
    """
    cfg = mineru_service.save_config(payload)
    applied = cfg.pop("applied", {}) or {}
    parts = []
    if applied.get("token") == "saved":
        parts.append("token 已保存")
    elif applied.get("token") == "cleared":
        parts.append("token 已清除")
    if "mineru_engine_default" in applied:
        parts.append(f"默认引擎改为{mineru_service.ENGINE_LABELS.get(applied['mineru_engine_default'], applied['mineru_engine_default'])}")
    if "mineru_model_version" in applied:
        parts.append(f"模型版本改为 {applied['mineru_model_version']}")
    if "mineru_language" in applied:
        parts.append(f"OCR 语言改为 {applied['mineru_language']}")
    if applied.get("token") == "saved":
        parts.append("建议点「验证」确认可用")
    return envelope(cfg, message="；".join(parts) or "没有需要保存的字段",
                    request=request)


@router.post("/mineru/verify")
async def verify_mineru(request: Request):
    """校验 token。**HTTP 一律 200**，结论放在 ``data.ok`` / ``data.state`` 里。

    为什么不把"token 过期"当 HTTP 错误抛：这是**用户要去修的东西**，不是请求出错。
    前端要能同时拿到"结论 + 原因 + 延迟"三样并原样展示，用错误通道反而要把
    这些信息塞进错误体再解一遍。
    """
    result = mineru_service.verify()
    return envelope(result, message=result.get("message") or "", request=request)
