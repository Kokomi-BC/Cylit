"""问答模型配置：agent 经 HTTP 取，**设置页仍是密钥的唯一入口**。

规划 §4.2 的边界：agent 服务不直连数据库、不读密钥文件，"用哪个模型、密钥是什么"
由主后端在回环 + 内部令牌的前提下下发。这样密钥只有一处入口（设置页），
agent 侧即使被读内存也拿不到别的模型的凭据。

模型类型用独立的 ``agent``（而不是复用 ``generation``）：翻译要"关思考 + 低温度 + 便宜"，
问答要"开思考 + 会调工具 + 能读长上下文"，挤在一个配置里必然互相将就。
没注册 ``agent`` 模型时**回落到 ``generation`` 默认**，存量用户零配置可用。
"""

from __future__ import annotations

from backend.core.errors import ValidationError
from backend.core.logging import get_logger
from backend.services.model.model_manager import ModelNotConfiguredError, model_manager

log = get_logger(__name__)


def _snapshot(cfg: dict, *, include_key: bool) -> dict:
    data = {
        "config_id": cfg.get("id"), "provider": cfg.get("provider"),
        "model_name": cfg.get("model_name"), "base_url": cfg.get("base_url"),
        "parameters": dict(cfg.get("parameters") or {}),
        "is_mock": cfg.get("provider") in ("mock", "hash", "local"),
    }
    if include_key:
        data["api_key"] = model_manager.resolve_secret(cfg)
        data["has_key"] = bool(data["api_key"])
    return data


def resolve_config(config_id: str | None = None) -> dict:
    """解析「这一轮问答实际用哪条模型配置」——**唯一一套**解析链（返回原始配置）。

    会话选的 `agent` 配置 → `agent` 默认 → 回落 `generation` 默认。
    单独抽出来是因为主后端的**限额检查**也要这条配置（问的是"这条模型实不实用"），
    再写一遍就会与 agent 真正用的模型分叉 —— 那种分叉在限额上等于「检了一个
    模型、用了另一个模型」，限额就白设了。
    """
    if config_id:
        try:
            hit = model_manager.get_config(config_id)
        except (ValidationError, ModelNotConfiguredError):
            hit = None
        if hit and hit.get("model_type") == "agent":
            return hit
    try:
        return model_manager.get_config(None, "agent")
    except ModelNotConfiguredError:
        return model_manager.get_config(None, "generation")


def resolve(config_id: str | None = None, effort: str | None = None) -> dict:
    """问答模型（会话选的 ``agent`` 配置 → ``agent`` 默认 → 回落 ``generation``）。

    ``config_id`` 是会话行上的 ``model_config.id``：命中且**确实是 agent 类型**才用它。
    其它类型（用户把 generation 的 id 写了进来）或 id 已删掉都算无效直接回落默认 ——
    不报错，是因为"会话里选过的模型被删了"不该让这条会话从此问不出话。

    ``effort`` 是会话里选的推理等级，**原样**带出去：厂商各叫各的档位名，校验、映射、
    改大小写都会把用户填的 high 变成厂商不认识的东西；没选才用该模型配置里的那一档。
    """
    cfg = resolve_config(config_id)
    # 「回落」= 实际用的不是 agent 类型（那只能是从 generation 默认兜来的）
    fallback = cfg.get("model_type") != "agent"
    out = _snapshot(cfg, include_key=True)
    out["fallback_to_generation"] = fallback
    out["requested_config_id"] = str(config_id or "") or None
    used = effort or out["parameters"].get("reasoning_effort") or ""
    out["effort_used"] = str(used) or None
    out["usable"] = bool(out.get("api_key")) and not out["is_mock"]

    try:
        out["vision"] = _snapshot(model_manager.get_config(None, "vision"), include_key=True)
    except ModelNotConfiguredError:
        out["vision"] = None
    if not out["usable"]:
        log.warning("问答模型不可用（provider=%s has_key=%s）：agent 会给出明确提示而不是硬答",
                    out.get("provider"), out.get("has_key"))
    return out


def config_for_frontend() -> dict:
    """设置页用的只读摘要（**不含密钥明文**，只说有没有）。"""
    data = resolve()
    data.pop("api_key", None)
    if isinstance(data.get("vision"), dict):
        data["vision"].pop("api_key", None)
    return data
