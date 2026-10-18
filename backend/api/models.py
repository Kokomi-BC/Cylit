"""模型管理 API（规划 §20.7）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from backend.api.response import envelope
from backend.core.utils import json_dumps
from backend.services.model import model_service

router = APIRouter(tags=["models"])


@router.get("/models")
async def list_models(request: Request, model_type: str | None = Query(None)):
    return envelope({"items": model_service.list_models(model_type)}, request=request)


@router.get("/models/types")
async def model_types(request: Request):
    """各类型当前默认（前端不写模型名，只认 model_type）。"""
    return envelope(model_service.types_summary(), request=request)


@router.get("/models/usage/stats")
async def model_usage_stats(request: Request, days: int = Query(14)):
    """模型 token 消耗：总消耗 / 今日 / 按天折线 / 分模型排行。

    days 夹到 1..90（契约见 ``services/model/usage_stats.stats``）：
    days 连续零填充、从旧到新、长度正好 days；by_model 按 total 降序。
    """
    from backend.services.model import usage_stats

    return envelope(usage_stats.stats(days), request=request)


# 注意：`/models/presets` 必须**声明在** `/models/{model_id}` 之前，
# 否则 "presets" 会被当成 model_id 捕获，接口不可达（返回"模型不存在：presets"）。
@router.get("/models/presets")
async def get_presets(request: Request):
    from backend.services.model import presets

    return envelope({"presets": presets.list_presets()}, request=request)


@router.post("/models/import-presets")
async def import_presets(request: Request, payload: dict = Body(default={})):
    """一键导入常见 Provider 预设（DeepSeek / 通义 / Ollama 等），只需填密钥。"""
    from backend.services.model import presets

    base_url = payload.get("base_url")
    provider = payload.get("provider") or "deepseek"
    created = presets.create_preset(provider, base_url=base_url,
                                    model_name=payload.get("model_name"))
    return envelope(created, message="预设已导入，请在 data/secrets/ 放入密钥后测试",
                    request=request)


@router.post("/models/secret")
async def write_secret(request: Request, payload: dict = Body(...)):
    """写入密钥文件到 data/secrets/（不落库、不进代码，规划 §12.2）。"""
    from backend.core.config import get_settings

    name = (payload.get("name") or "").strip()
    value = (payload.get("value") or "").strip()
    if not name or not value:
        return envelope(None, message="name 与 value 必填", request=request)
    if "/" in name or "\\" in name or ".." in name:
        return envelope(None, message="密钥名不能包含路径分隔符", request=request)
    s = get_settings()
    path = s.secrets_dir / name
    path.write_text(value, encoding="utf-8")
    return envelope({"saved": str(path), "bytes": len(value)},
                    message="密钥已写入（仅本机）", request=request)


@router.get("/models/secrets/list")
async def list_secrets(request: Request):
    from backend.core.config import get_settings

    s = get_settings()
    files = sorted(p.name for p in s.secrets_dir.glob("*") if p.is_file())
    return envelope({"secrets": files, "dir": str(s.secrets_dir)}, request=request)


@router.post("/models")
async def create_model(request: Request, payload: dict = Body(...)):
    return envelope(model_service.create_model(payload), message="已创建",
                    request=request)


@router.get("/models/{model_id}")
async def get_model(model_id: str, request: Request):
    return envelope(model_service.get_model(model_id), request=request)


@router.put("/models/{model_id}")
async def update_model(model_id: str, request: Request, payload: dict = Body(...)):
    return envelope(model_service.update_model(model_id, payload), request=request)


@router.delete("/models/{model_id}")
async def delete_model(model_id: str, request: Request):
    res = model_service.delete_model(model_id)
    return envelope(res, request=request)


@router.post("/models/{model_id}/test")
async def test_model(model_id: str, request: Request):
    """连通性 + 维度 + 延迟（规划 §12.2 能力探测）。"""
    return envelope(model_service.test_model(model_id), request=request)


@router.put("/models/{model_id}/default")
async def set_default(model_id: str, request: Request):
    return envelope(model_service.set_default(model_id),
                    message="已设为该类型默认模型", request=request)


_ = json_dumps
