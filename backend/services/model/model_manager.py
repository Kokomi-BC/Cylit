"""ModelManager（规划 §12.2）：配置解析 / 实例化缓存 / 默认路由 / 能力探测 / 兼容性校验。

业务代码只调用 ``get_generation()`` / ``get_embedding()`` / ``get_reranker()``，
**永远不写模型名**（规划 §12.2 默认路由）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from backend.core import provider_errors
from backend.core.errors import (
    LimitExceededError, ModelNotConfiguredError, ProviderError, ValidationError,
)
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, new_id, now
from backend.providers.base import EmbeddingProvider, GenerationProvider, ModelInfo
from backend.providers.factory import (
    build_embedding_provider, build_generation_provider, build_reranker_provider,
)
from backend.providers.generation.mock import MockEmbedding, MockGeneration
from backend.services.model import secret_resolver

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


# --------------------------------------------------------------------------
# 用量记录代理（token 消耗统计，见 ``services/model/usage_stats.py``）
# --------------------------------------------------------------------------
#: 每个 provider 类对应一个「记录子类」（只造一次）。
_usage_recorders: dict[type, type] = {}


def _usage_recorder(base: type) -> type:
    """给 provider 类造一个会落用量的子类。

    为什么用**子类**而不是 ``__getattr__`` 转发：
    1. ``structured()`` 内部是 ``self.generate(...)`` —— 只有真的继承下来，那次 generate
       才会走钩子（元数据补全 / 简报 / 公式修复全走 structured）；
    2. ``supports_vision`` 是 ``type(self).generate_vision is not GenerationProvider.generate_vision``：
       转发代理会让不支持图片的 provider 看起来支持；子类只在原类真的覆盖了它时才覆盖。
    """
    cached = _usage_recorders.get(base)
    if cached is not None:
        return cached

    def _record(self, result) -> None:
        from backend.services.model.usage_stats import record_result

        record_result(getattr(self, "config", None) or {}, result)

    def _reserve(self, prompt):
        """**发请求之前**过三级限额（模型 / 密钥 / 厂商），返回预留句柄。

        检查点放在这里 = 翻译 / 理解 / 元数据 / 公式 / 视觉读页 / 设置页探测
        **全部一次覆盖**（它们都从这里出发），而且天然满足「重试 / 换模型 / 换密钥
        都要重新检查」—— 每次调用都现查一遍，任何地方都没有缓存判定结果。
        """
        from backend.services.model import limits

        return limits.check_and_reserve(getattr(self, "config", None) or {},
                                        prompt=prompt)

    def _release(reservation) -> None:
        """释放预留：成功、失败、抛异常都要走到（漏了就是额度泄漏）。"""
        from backend.services.model import limits

        limits.release(reservation)

    def generate(self, prompt, *args, **kwargs):
        reservation = _reserve(self, prompt)
        try:
            try:
                result = base.generate(self, prompt, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 补字段，再原样上抛
                _attach_provider_detail(exc, self)
                raise
            # 先落账、再释放：反过来会留下一段「用量已记、预留已销」的空窗，
            # 那段时间里并发检查会看到一个偏小的 spent。
            _record(self, result)
            return result
        finally:
            _release(reservation)

    attrs: dict = {"generate": generate, "__module__": base.__module__}
    if getattr(base, "generate_vision", None) is not GenerationProvider.generate_vision:
        def generate_vision(self, prompt, images, *args, **kwargs):
            reservation = _reserve(self, prompt)
            try:
                try:
                    result = base.generate_vision(self, prompt, images, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001 —— 同上：只补字段
                    _attach_provider_detail(exc, self)
                    raise
                _record(self, result)
                return result
            finally:
                _release(reservation)

        attrs["generate_vision"] = generate_vision
    cls = type(f"UsageRecording{base.__name__}", (base,), attrs)
    _usage_recorders[base] = cls
    return cls


def _attach_provider_detail(exc: BaseException, provider) -> None:
    """给上游失败补 detail.provider（信封里就是它，见 backend/core/provider_errors.py）。

    **只补字段**：异常类型、retryable、状态码、重试策略一个字都不动 ——
    这一层是记账，不是判定。已有的 provider 字段（调用方填过）不覆盖。
    """
    if not isinstance(exc, ProviderError) or exc.detail.get("provider"):
        return
    config = getattr(provider, "config", None) or {}
    exc.detail["provider"] = provider_errors.classify(
        exc,
        model=str(getattr(provider, "model_name", "") or ""),
        provider_label=provider_errors.label_of(
            config.get("provider") if isinstance(config, dict) else ""),
        secrets=(getattr(provider, "api_key", None),))


def _with_usage(provider):
    """套一层薄代理：与缓存的 provider **共享实例状态**（HTTP 客户端 / 缓存 / config）。

    ``_get()`` 的缓存里存的仍是裸 provider，代理每次现造一个 —— 于是 ``get_generation()``
    的既有缓存口径（同一份配置复用同一个实例）完全不变。
    """
    cls = _usage_recorder(type(provider))
    proxy = cls.__new__(cls)
    proxy.__dict__ = provider.__dict__
    return proxy


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
            # 用哪把密钥：**只存选择**（模式 + 行 id），绝不缓存密钥内容或文件名快照 ——
            # 提供方默认密钥一变，跟随默认的模型下次调用就必须跟着变。
            "key_mode": getattr(row, "key_mode", None) or "auto",
            "provider_key_id": getattr(row, "provider_key_id", None),
            # 计费单价（**每百万 Token**）与币种：NULL / '' = 未配置 / 跟随厂商
            # （解析顺序见 services/model/billing.py）。agent 进程也拿这份配置，
            # 将来自己算账、对账时不用再查库。
            "price_in": getattr(row, "price_in", None),
            "price_out": getattr(row, "price_out", None),
            "price_cache_read": getattr(row, "price_cache_read", None),
            "price_cache_write": getattr(row, "price_cache_write", None),
            "currency": getattr(row, "currency", None) or "",
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
        """缓存键：模型身份 + **密钥指纹**。

        密钥指纹（`secret_resolver.cache_token`）只有"哪一行 / 更新时间"，没有内容：
        密钥行换了文件内容或换了默认，指纹就变，旧 Provider（揣着旧密钥）自然不再命中 ——
        这是"改了密钥却还是 401"的根治办法（见验收「缓存键现在不带密钥」）。
        """
        return (f"{cfg.get('id')}|{cfg.get('provider')}|{cfg.get('model_name')}"
                f"|{cfg.get('key_mode') or 'auto'}:{cfg.get('provider_key_id') or ''}"
                f"|{secret_resolver.cache_token(cfg)}")

    def _api_key(self, cfg: dict) -> str | None:
        """模型实际用的密钥 —— **统一走 `secret_resolver`**（唯一一套解析逻辑）。"""
        return secret_resolver.resolve_for_model(cfg)[0]

    def key_source(self, cfg: dict) -> dict:
        """密钥来源（给接口显示"这把密钥哪来的、还灵不灵"），不含明文。"""
        return secret_resolver.describe(cfg)

    def resolve_secret(self, cfg: dict) -> str | None:
        """公开的密钥解析入口（agent 服务经 HTTP 拿配置时要用，见 `services/agent/model_config`）。

        与 `_api_key` 是**同一个函数**（都只是 `secret_resolver.resolve_for_model` 的别名），
        否则会出现"设置页测通了、agent 却说没密钥"。
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
        # `agent` 也走生成 Provider：问答模型就是一个 OpenAI 兼容的 chat 端点
        # （base_url + model_name + secret_ref + parameters），只是消费方是问答运行时。
        # 少了这一支，设置页对问答模型点「测试」会拿到
        # "未配置可用的 agent 模型" —— 行明明在、默认也设了，用户只会一头雾水。
        if mtype in ("generation", "agent"):
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
        # 业务侧取生成模型的唯一门面 —— 用量统计就套在**返回处**（每次 generate 落一条）
        provider = self._get(self.get_config(model_id, "generation"))
        return _with_usage(provider)  # type: ignore[return-value]

    def get_embedding(self, model_id: str | None = None) -> EmbeddingProvider:
        return self._get(self.get_config(model_id, "embedding"))  # type: ignore[return-value]

    def get_reranker(self, model_id: str | None = None):
        return self._get(self.get_config(model_id, "reranker"))

    def get_vision(self, model_id: str | None = None):
        """视觉模型（扫描件读页）。未注册 ``vision`` 类型模型时抛错，调用方自行降级。"""
        # 也套用量代理：页级翻译与扫描件兜底走的是这条（token 最贵的两处）
        return _with_usage(self._get(self.get_config(model_id, "vision")))

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
    def test(self, model_id: str, persist: bool = True,
             key_id: str | None = None) -> dict:
        """探测一次（真调用）。

        ``key_id`` 是"换一把密钥试算"：只在**本次**探测里生效，**不落库、不改模型的
        key_mode / provider_key_id**（用户只是想验证"换成这把还能不能用"）。
        返回里带 ``key_id`` / ``key_label`` / ``key_status`` / ``failure``，
        前端据此说清"用的是哪把、失败在哪一步"（认证 / 没有 /models / 超时 / 未配置密钥）。
        """
        cfg = self.get_config(model_id)
        probe = cfg
        if key_id:
            provider_of_model = str(cfg.get("provider") or "").strip().lower()
            row = secret_resolver.key_row(key_id)
            if row is None:
                raise ValidationError(f"密钥不存在：{key_id}")
            if row["provider_id"] != provider_of_model:
                raise ValidationError(
                    "这把密钥不属于该模型所在的提供方（不能跨家借密钥）。",
                    detail={"key_provider": row["provider_id"],
                            "model_provider": provider_of_model},
                )
            probe = {**cfg, "key_mode": "key", "provider_key_id": key_id}
        api_key, source = secret_resolver.resolve_for_model(probe)
        head = {"model_id": model_id, "key_id": source.get("key_id") or "",
                "key_label": source.get("key_label") or "",
                "key_status": source.get("status") or "missing"}
        if head["key_status"] != "ok":
            # 还没摸到网络就已经知道不行：别拿一个空 key 去撞 401（那会让人以为密钥错了）
            detail = ("这把密钥的文件读不到了" if source.get("status") == "invalid"
                      else "没有可用密钥（提供方没有默认密钥，模型也没单独选一把）")
            head.update({"ok": False, "dimension": None, "latency_ms": None,
                         "model_name": "", "detail": detail, "failure": "no_key"})
            if persist and not key_id:
                self._persist_status(cfg.get("id"),
                                     ModelInfo(ok=False, detail=detail))
            return head
        try:
            # 探测也是一次真调用（真花钱），同样落一条用量
            provider = _with_usage(self._get(probe))
            info: ModelInfo = provider.test()  # type: ignore[attr-defined]
        except LimitExceededError:
            # 本地限额不是「这个模型连不上」：折成 ok=False 会把 429 伪装成
            # 「端点是坏的」，用户就会去改 base_url —— 这里必须原样上抛。
            raise
        except ProviderError as exc:
            info = ModelInfo(ok=False, detail=exc.message[:300])
        except Exception as exc:  # noqa: BLE001
            info = ModelInfo(ok=False, detail=f"{type(exc).__name__}: {str(exc)[:300]}")
        if persist and not key_id:
            # 借密钥试算的结果不写回模型的 status：那一行说的是"按它自己的配置灵不灵"
            self._persist_status(cfg.get("id"), info)
        head.update(info.to_dict())
        head["failure"] = _failure_of(info)
        return head

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
            provider = payload.get("provider") or "openai_compatible"
            key_mode, provider_key_id = _key_selection(payload, provider, cur=None)
            row = ModelConfig(
                id=model_id, name=payload.get("name") or model_id,
                provider=provider,
                model_type=mtype, model_name=payload.get("model_name"),
                base_url=payload.get("base_url"), device=payload.get("device"),
                parameters_json=_dumps(payload.get("parameters") or {}),
                secret_ref=payload.get("secret_ref"), enabled=True,
                is_default=False, status="unknown",
                key_mode=key_mode, provider_key_id=provider_key_id,
                **_price_updates(payload),
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
            # 单价 / 币种：显式传 null 就是"清空"（从此不计费），不传就是保持不动
            for field, value in _price_updates(payload).items():
                setattr(row, field, value)
            if "enabled" in payload and payload["enabled"] is not None:
                row.enabled = bool(payload["enabled"])
            # provider 变了也要重新校验：不然改到 B 家、密钥还指着 A 家的行 = 跨家借密钥
            if ("key_mode" in payload or "provider_key_id" in payload
                    or "provider" in payload):
                key_mode, provider_key_id = _key_selection(payload, row.provider, cur=row)
                row.key_mode = key_mode
                row.provider_key_id = provider_key_id
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
            # 时段规则随模型一起删：id 若被新模型复用，老规则会**静默接管**它的计价
            # （用户会看到"新模型按旧模型的高峰价收费"这种无从解释的账）。
            from backend.repositories.mysql.models import ModelPriceRule

            sess.query(ModelPriceRule).filter(
                ModelPriceRule.model_config_id == str(model_id)
            ).delete(synchronize_session=False)
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


def _failure_of(info: ModelInfo) -> str:
    """把一次探测失败归成前端能显示的一类（成功给空串）。"""
    if info.ok:
        return ""
    text = str(info.detail or "").lower()
    if "401" in text or "403" in text or "认证" in text or "unauthor" in text:
        return "auth"
    if "404" in text or "not found" in text:
        return "not_found"
    if "超时" in text or "timeout" in text or "timed out" in text:
        return "timeout"
    if "连接" in text or "connect" in text:
        return "network"
    return "error"


def _key_selection(payload: dict, provider: str | None,
                   cur=None) -> tuple[str, str | None]:
    """校验并归一化「模型用哪把密钥」（``key_mode`` + ``provider_key_id``）。

    三条硬规则：

    1. ``key_mode`` 只能是 auto / key / none；
    2. 引用的密钥行必须**存在**（否则静默回落 = 悄悄换账号）；
    3. 密钥行的 ``provider_id`` 必须等于模型自己的 provider —— **禁止跨家借密钥**
       （把 A 家的 key 打到 B 家的 base_url 上，轻则 401，重则把 B 家的数据发给 A 家）。

    只存"选择"，**不在这里写 secret_ref 快照**：提供方默认密钥一变，跟随默认的模型
    下次调用必须立刻跟着变（解析在调用时实时做，见 `secret_resolver`）。
    """
    mode = payload.get("key_mode")
    if mode is None:
        mode = getattr(cur, "key_mode", None) or "auto"
    mode = str(mode).strip().lower()
    if mode not in secret_resolver.KEY_MODES:
        raise ValidationError(f"key_mode 必须是 {secret_resolver.KEY_MODES}", detail={"key_mode": mode})
    if "provider_key_id" in payload:
        key_id = str(payload.get("provider_key_id") or "").strip() or None
    else:
        key_id = getattr(cur, "provider_key_id", None) or None
    if key_id:
        row = secret_resolver.key_row(key_id)
        if row is None:
            raise ValidationError(f"密钥不存在：{key_id}")
        pid = str(provider or "").strip().lower()
        if row["provider_id"] != pid:
            raise ValidationError(
                "这把密钥不属于该模型所在的提供方（不能跨家借密钥）。",
                detail={"key_provider": row["provider_id"], "model_provider": pid},
            )
    if mode == "key" and not key_id:
        raise ValidationError("key_mode=key 必须同时指定 provider_key_id（否则不知道用哪把）")
    return mode, key_id


def _price_updates(payload: dict) -> dict:
    """校验并取出「单价 / 币种」这几个字段（**只返回请求里真的带了的**）。

    为什么只取带了的：update 要能区分「没提这个字段」（保持不动）与
    「显式传 null」（清空价格 = 从此不计费）。校验交给 billing 的 clean_price /
    normalize_currency —— 那里是单价的唯一真源，非法值（负数 / 非数字 / 怪币种）一律
    ValidationError，不静默改成 0（0 会变成"这次免费"，是错账不是默认值）。
    """
    from backend.services.model import billing

    out: dict = {}
    for name in billing.PRICE_FIELDS:
        if name in payload:
            out[name] = billing.clean_price(payload.get(name), name)
    if "currency" in payload:
        out["currency"] = billing.normalize_currency(payload.get("currency")) or None
    return out


def _dumps(obj) -> str:
    from backend.core.utils import json_dumps

    return json_dumps(obj)


model_manager = ModelManager()
