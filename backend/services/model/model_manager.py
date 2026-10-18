"""ModelManager（规划 §12.2）：配置解析 / 实例化缓存 / 默认路由 / 能力探测 / 兼容性校验。

业务代码只调用 ``get_generation()`` / ``get_embedding()`` / ``get_reranker()``，
**永远不写模型名**（规划 §12.2 默认路由）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from backend.core.config import get_settings
from backend.core.errors import (
    ModelNotConfiguredError, ProviderError, ValidationError,
)
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, new_id, now
from backend.providers.base import EmbeddingProvider, GenerationProvider, ModelInfo
from backend.providers.factory import (
    build_embedding_provider, build_generation_provider, build_reranker_provider,
)
from backend.providers.generation.mock import MockEmbedding, MockGeneration

log = get_logger(__name__)

#: ``agent`` 是**问答 agent 自己的模型**（`pr.py agent` 那个独立进程用）。
#: 单独一个类型而不是复用 ``generation``：翻译要"关思考 + 低温度 + 便宜"，
#: 问答要"开思考 + 会调工具 + 能读长上下文"，两者挤在一个配置里必然互相将就。
#: 不内置任何默认档：未注册时 `resolve_agent_config()` 回落到 ``generation`` 默认。
MODEL_TYPES = ("generation", "embedding", "reranker", "vision", "ocr", "agent")
# 本地模型 LRU 上限（省显存，规划 §12.2）
CACHE_SIZE = 4
# 需要密钥才能用的真模型之外，这些 provider 是"离线降级档"：同一类型里
# **不能**让它们压过真模型当默认（真实踩过：降级档被重新插入时又盖了个 is_default，
# 于是 reranker 取到 heuristic-overlap，重排静默降级 —— 检索质量掉了但界面看不出来）
MOCK_PROVIDERS = ("mock", "hash", "heuristic", "local")


def pick_default(candidates: list[dict]) -> dict | None:
    """从同类型的候选里挑默认：**先排降级档**，再看状态是否探测通过，最后按 id 稳定排序。

    纯函数（可离线测）：`get_config` 与 `repair_defaults` 共用同一套判定，避免两处不一致。
    """
    if not candidates:
        return None

    def key(cfg: dict) -> tuple:
        return (1 if str(cfg.get("provider")) in MOCK_PROVIDERS else 0,
                0 if cfg.get("status") == "ok" else 1,
                str(cfg.get("id") or ""))

    return min(candidates, key=key)


class ModelManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: "OrderedDict[str, object]" = OrderedDict()
        self._dim_memo: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def _rows(self) -> list:
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            return sess.query(ModelConfig).order_by(ModelConfig.id).all()

    def _row_to_config(self, row) -> dict:
        return {
            "id": row.id, "name": row.name, "provider": row.provider,
            "model_type": row.model_type, "model_name": row.model_name,
            "base_url": row.base_url, "device": row.device,
            "parameters": json_loads(row.parameters_json, {}) or {},
            "secret_ref": row.secret_ref, "enabled": bool(row.enabled),
            "is_default": bool(row.is_default), "status": row.status,
        }

    def list_configs(self, model_type: str | None = None) -> list[dict]:
        rows = self._rows()
        out = [self._row_to_config(r) for r in rows
               if not model_type or r.model_type == model_type]
        return out

    def get_config(self, model_id: str | None, model_type: str = "generation") -> dict:
        """取配置：指定 id > 该类型默认 > 该类型任一可用；都没有就抛未配置。

        ★ 「该类型默认」可能有多行（历史遗留 / 内置档补插时又盖了一次默认）——
        以前取"按 id 排序的第一个"，于是 `heuristic-reranker` 会压过 `qwen3-vl-rerank`，
        重排静默降级。现在统一走 `pick_default`（真模型优先）。
        """
        rows = self._rows()
        if model_id:
            for r in rows:
                if r.id == model_id:
                    return self._row_to_config(r)
            raise ValidationError(f"模型不存在：{model_id}")
        defaults = [self._row_to_config(r) for r in rows
                    if r.model_type == model_type and r.is_default and r.enabled]
        chosen = pick_default(defaults)
        if chosen is not None:
            return chosen
        for r in rows:
            if r.model_type == model_type and r.enabled:
                return self._row_to_config(r)
        raise ModelNotConfiguredError(model_type)

    # ------------------------------------------------------------------
    # 实例化与缓存
    # ------------------------------------------------------------------
    def _cache_key(self, cfg: dict) -> str:
        return f"{cfg.get('id')}|{cfg.get('provider')}|{cfg.get('model_name')}"

    def _api_key(self, cfg: dict) -> str | None:
        ref = cfg.get("secret_ref")
        if not ref:
            return None
        s = get_settings()
        key = s.read_secret(ref)
        if key is None:
            key = s.read_secret(cfg.get("id") or "")
        return key

    def resolve_secret(self, cfg: dict) -> str | None:
        """公开的密钥解析入口（agent 服务经 HTTP 拿配置时要用，见 `services/agent/model_config`）。

        ``secret_ref`` 优先，回落配置 id —— 这个顺序是历史约定（老配置只用 id 当文件名），
        对外暴露时必须与 `_api_key` 完全一致，否则会出现"设置页测通了、agent 却说没密钥"。
        """
        return self._api_key(cfg)

    def _get(self, cfg: dict):
        key = self._cache_key(cfg)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        mtype = cfg.get("model_type")
        api_key = self._api_key(cfg)
        if mtype == "generation":
            provider = build_generation_provider(cfg, api_key)
        elif mtype == "embedding":
            provider = build_embedding_provider(cfg, api_key)
        elif mtype == "reranker":
            provider = build_reranker_provider(cfg, api_key)
        elif mtype in ("vision", "ocr"):
            # 多模态端点与文本端点同一个 /chat/completions，只是消息体多一个
            # image_url part，所以直接复用生成 Provider（带 generate_vision）
            provider = build_generation_provider(cfg, api_key)
        else:
            raise ModelNotConfiguredError(str(mtype))
        with self._lock:
            self._cache[key] = provider
            while len(self._cache) > CACHE_SIZE:
                _, evicted = self._cache.popitem(last=False)
                try:
                    evicted.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
        return provider

    def get_generation(self, model_id: str | None = None) -> GenerationProvider:
        return self._get(self.get_config(model_id, "generation"))  # type: ignore[return-value]

    def get_embedding(self, model_id: str | None = None) -> EmbeddingProvider:
        return self._get(self.get_config(model_id, "embedding"))  # type: ignore[return-value]

    def get_reranker(self, model_id: str | None = None):
        return self._get(self.get_config(model_id, "reranker"))

    def get_vision(self, model_id: str | None = None):
        """视觉模型（扫描件读页）。未注册 ``vision`` 类型模型时抛错，调用方自行降级。"""
        return self._get(self.get_config(model_id, "vision"))

    def is_mock(self, model_type: str = "generation") -> bool:
        try:
            cfg = self.get_config(None, model_type)
        except ModelNotConfiguredError:
            return True
        return cfg.get("provider") in ("mock", "hash", "heuristic")

    # ------------------------------------------------------------------
    # 兼容性校验（规划 §12.2：维度变化拒绝静默换模型）
    # ------------------------------------------------------------------
    def check_embedding_dim(self, cfg: dict, expected: int | None) -> None:
        if not expected:
            return
        key = self._cache_key(cfg)
        known = self._dim_memo.get(key)
        if known is None:
            return
        if known != expected:
            raise ValidationError(
                f"Embedding 维度变化（{known} → {expected}）：拒绝静默换模型，"
                f"请先重建索引（规划 §11.3）。",
                detail={"old_dim": known, "new_dim": expected, "model_id": cfg.get("id")},
            )

    def remember_dim(self, cfg: dict, dim: int | None) -> None:
        if dim:
            self._dim_memo[self._cache_key(cfg)] = dim

    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    def test(self, model_id: str, persist: bool = True) -> dict:
        cfg = self.get_config(model_id)
        try:
            provider = self._get(cfg)
            info: ModelInfo = provider.test()  # type: ignore[attr-defined]
        except ProviderError as exc:
            info = ModelInfo(ok=False, detail=exc.message[:300])
        except Exception as exc:  # noqa: BLE001
            info = ModelInfo(ok=False, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        if persist:
            self._persist_status(cfg.get("id"), info)
        return {"model_id": model_id, **info.to_dict()}

    def _persist_status(self, model_id: str | None, info: ModelInfo) -> None:
        if not model_id:
            return
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(ModelConfig, model_id)
            if row is None:
                return
            row.status = "ok" if info.ok else "error"
            row.status_detail = info.detail[:500]
            row.last_tested_at = now()

    def repair_defaults(self) -> dict:
        """同一类型出现多个 ``is_default`` 时只留一个（启动时自动修）。

        保留规则与 `get_config` 完全一致（`pick_default`）：真模型 > 探测通过 > id 稳定。
        修完写日志 —— 这种状态会让检索/翻译**静默用降级档**，必须留痕。
        """
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        fixed: dict = {}
        with session_scope() as sess:
            for mtype in MODEL_TYPES:
                rows = [r for r in sess.query(ModelConfig).filter(
                    ModelConfig.model_type == mtype) if r.is_default]
                if len(rows) <= 1:
                    continue
                keeper = pick_default([self._row_to_config(r) for r in rows])
                keep_id = (keeper or {}).get("id")
                cleared = []
                for r in rows:
                    if r.id != keep_id:
                        r.is_default = False
                        cleared.append(r.id)
                fixed[mtype] = {"kept": keep_id, "cleared": cleared}
            if fixed:
                sess.flush()
        if fixed:
            log.warning("修复重复的默认模型（同类型多行 is_default）：%s", fixed)
            self._cache.clear()
        return fixed

    def create(self, payload: dict) -> dict:
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        mtype = payload.get("model_type") or "generation"
        if mtype not in MODEL_TYPES:
            raise ValidationError(f"model_type 必须是 {MODEL_TYPES}")
        model_id = payload.get("id") or f"{mtype}-{new_id('m')[-8:]}"
        with session_scope() as sess:
            if sess.get(ModelConfig, model_id) is not None:
                raise ValidationError(f"模型 id 已存在：{model_id}")
            row = ModelConfig(
                id=model_id, name=payload.get("name") or model_id,
                provider=payload.get("provider") or "openai_compatible",
                model_type=mtype, model_name=payload.get("model_name"),
                base_url=payload.get("base_url"), device=payload.get("device"),
                parameters_json=_dumps(payload.get("parameters") or {}),
                secret_ref=payload.get("secret_ref"), enabled=True,
                is_default=False, status="unknown",
            )
            sess.add(row)
            sess.flush()
            if payload.get("is_default"):
                self._set_default(sess, model_id, mtype)
            return self._row_to_config(row)

    def update(self, model_id: str, payload: dict) -> dict:
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(ModelConfig, model_id)
            if row is None:
                raise ValidationError(f"模型不存在：{model_id}")
            for field in ("name", "provider", "model_name", "base_url", "device",
                          "secret_ref"):
                if field in payload and payload[field] is not None:
                    setattr(row, field, payload[field])
            if "parameters" in payload and payload["parameters"] is not None:
                row.parameters_json = _dumps(payload["parameters"])
            if "enabled" in payload and payload["enabled"] is not None:
                row.enabled = bool(payload["enabled"])
            if payload.get("is_default"):
                self._set_default(sess, model_id, row.model_type)
            sess.flush()
            self._invalidate(model_id)
            return self._row_to_config(row)

    def delete(self, model_id: str) -> None:
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(ModelConfig, model_id)
            if row is None:
                return
            was_default = row.is_default
            mtype = row.model_type
            sess.delete(row)
            if was_default:
                # 回落到该类型的其它模型，避免"无默认模型"
                nxt = (sess.query(ModelConfig)
                       .filter(ModelConfig.model_type == mtype,
                               ModelConfig.enabled.is_(True),
                               ModelConfig.id != model_id)
                       .order_by(ModelConfig.id).first())
                if nxt is not None:
                    nxt.is_default = True
        self._invalidate(model_id)

    def set_default(self, model_id: str) -> dict:
        from backend.repositories.mysql.models import ModelConfig
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            row = sess.get(ModelConfig, model_id)
            if row is None:
                raise ValidationError(f"模型不存在：{model_id}")
            self._set_default(sess, model_id, row.model_type)
            sess.flush()
            return self._row_to_config(row)

    @staticmethod
    def _set_default(sess, model_id: str, model_type: str) -> None:
        from backend.repositories.mysql.models import ModelConfig

        for r in sess.query(ModelConfig).filter(ModelConfig.model_type == model_type):
            r.is_default = (r.id == model_id)

    def _invalidate(self, model_id: str) -> None:
        with self._lock:
            for key in [k for k in self._cache if k.startswith(f"{model_id}|")]:
                self._cache.pop(key, None)

    def types_summary(self) -> dict:
        out: dict = {}
        for t in MODEL_TYPES:
            try:
                cfg = self.get_config(None, t)
                out[t] = {"id": cfg.get("id"), "name": cfg.get("name"),
                          "provider": cfg.get("provider"),
                          "model_name": cfg.get("model_name"),
                          "is_mock": cfg.get("provider") in ("mock", "hash", "heuristic")}
            except ModelNotConfiguredError:
                out[t] = None
        return out


def _dumps(obj) -> str:
    from backend.core.utils import json_dumps

    return json_dumps(obj)


model_manager = ModelManager()
