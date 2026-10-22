"""模型提供方的持久化（`model_provider` 表）+ 历史数据回填。

用户口径（改造计划 §5）：Provider 一级实体。设计取舍：

· **不重建数据**：历史库里 provider 只是 `model_config.provider` 一个字符串，所以这里做的是
  「按字符串回填 provider 行」（`ensure_providers`，幂等，重复跑不产生重复记录）；
· **官方身份只由后端判定**：`kind=official` 只给 `providers_catalog.PROVIDER_META`
  （去掉 hidden）里登记过的 provider，请求体传入的 kind 一律忽略；
· **删除要检查引用**：还有模型挂在这家厂商上就拒绝删除（计划 §2.2 / §7.7）。
"""

from __future__ import annotations

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads
from backend.repositories.mysql.models import (
    ModelConfig, ModelProvider, ModelProviderKey,
)
from backend.repositories.mysql.session import session_scope

log = get_logger(__name__)

DEFAULT_ICON = ""

# ---------------------------------------------------------------- 折叠历史厂商
# 「多模态向量」曾经被当成一家独立厂商（provider=dashscope_multimodal），于是同一家
# 阿里云被拆成两个 provider：密钥、图标、目录全重复。现在能力下沉成**模型级协议**
# （parameters.protocol，见 providers/factory.build_embedding_provider），这几个厂商 id
# 一律折叠回 dashscope —— 幂等、可重跑，且**不动模型的 base_url / model_name / 密钥**。
FOLD_TARGET = "dashscope"
FOLD_SOURCES = ("dashscope_multimodal", "dashscope_vl", "qwen_vl")
#: 折叠时写进模型 parameters 的协议名（= 原 provider 名，factory 里就是这个键）
FOLD_PROTOCOL = "dashscope_multimodal"


def _official_ids() -> set[str]:
    """官方提供方 id 的唯一出处 = ``providers_catalog.PROVIDER_META``（跳过 hidden）。

    「一键导入预设」整套下线后目录不再从 presets 派生，这里必须跟着改 ——
    否则"目录算官方、入库算自定义"两套口径会打架。
    """
    from backend.services.model import providers_catalog

    return providers_catalog.official_ids()


def _meta_for(provider: str) -> dict:
    from backend.services.model import providers_catalog

    return providers_catalog.meta_for(provider)


def _to_dict(row: ModelProvider) -> dict:
    # 图标回落：历史行的 icon 是空串，目录里已经给它登记了图标键 ——
    # 读的时候补上，不必回头改数据。
    meta = _meta_for(row.id)
    return {
        "id": row.id,
        "kind": row.kind,
        "label": row.label,
        "icon": row.icon or meta.get("icon", DEFAULT_ICON),
        "base_url": row.base_url or "",
        "auth": row.auth or "bearer",
        "note": row.note or "",
        "status": row.status or "unknown",
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


def ensure_providers() -> int:
    """把**官方目录**与「模型行上出现过的 provider 字符串」补成 provider 记录（幂等）。

    为什么官方目录要**全部**建行：以前只回填"模型行上出现过的"，于是还没配过模型的
    官方家（moonshot 这种）新建密钥会被当成不存在的提供方 → 404；提供方级测试连接也
    找不到行。返回本次新建的条数；重复调用返回 0。
    """
    official = _official_ids()
    created = 0
    with session_scope() as s:
        known = {row.id: row for row in s.query(ModelProvider).all()}
        seen: set[str] = set()
        for cfg in s.query(ModelConfig).all():
            prov = str(cfg.provider or "").strip().lower()
            if prov:
                seen.add(prov)
        for prov in sorted(seen | official):
            if prov in known:
                continue
            meta = _meta_for(prov)
            s.add(ModelProvider(
                id=prov,
                kind="official" if prov in official else "custom",
                label=meta.get("label") or prov,
                icon=meta.get("icon", DEFAULT_ICON),
                base_url=meta.get("base_url", ""),
                auth=meta.get("auth", "bearer"),
                note=meta.get("note", ""),
            ))
            created += 1
        # 已存在的官方行顺手对齐（幂等）：官方身份只由后端判定；base_url / icon 空着就补
        # 默认值 —— 但**绝不覆盖**用户改过的非空值（那是他的端点）。
        for pid, row in known.items():
            if pid not in official:
                continue
            meta = _meta_for(pid)
            row.kind = "official"
            if not (row.base_url or "").strip() and meta.get("base_url"):
                row.base_url = str(meta["base_url"])
            if not (row.icon or "").strip() and meta.get("icon"):
                row.icon = str(meta["icon"])
        # 多模态厂商折叠进百炼（幂等；启动时经 ensure_keys → 这里跑到）
        folded = _fold_into_dashscope(s)
        s.commit()
    if any(folded.values()):
        log.info("折叠多模态厂商 → %s：模型 %s / 密钥 %s / 删行 %s",
                 FOLD_TARGET, folded["models"], folded["keys"], folded["providers"])
    if created:
        log.info("provider 回填：新建 %s 条", created)
    return created


def fold_multimodal_providers() -> dict:
    """把「多模态向量家」折叠进 ``dashscope``（幂等；返回本次改了什么）。

    三件事，同一个事务：

    1. 模型：``provider in FOLD_SOURCES`` → ``dashscope``，并在 ``parameters`` 里**补**
       ``protocol='dashscope_multimodal'``（已有 protocol 不覆盖，可重跑）；
    2. 密钥行：``provider_id`` 改到 ``dashscope``；那家已有默认密钥就把这把的
       ``is_default`` 置 False（**不制造两个默认**）；
    3. 折叠后没有任何模型 / 密钥再指向原 id → 删掉那条 provider 行。

    **不动**模型行上的 base_url / model_name / secret_ref：多模态向量端点由协议自己解析
    （空 base_url → DashScope 原生 host），改了就换端口。
    """
    moved = {"models": [], "keys": [], "providers": []}
    with session_scope() as s:
        moved = _fold_into_dashscope(s)
        s.commit()
    if any(moved.values()):
        log.info("折叠多模态厂商 → %s：模型 %s / 密钥 %s / 删行 %s",
                 FOLD_TARGET, moved["models"], moved["keys"], moved["providers"])
    return moved


def _fold_into_dashscope(s) -> dict:
    moved = {"models": [], "keys": [], "providers": []}
    for cfg in s.query(ModelConfig).filter(ModelConfig.provider.in_(FOLD_SOURCES)).all():
        cfg.provider = FOLD_TARGET
        params = json_loads(cfg.parameters_json, {}) or {}
        if not str(params.get("protocol") or "").strip():
            params["protocol"] = FOLD_PROTOCOL
            cfg.parameters_json = json_dumps(params)
        moved["models"].append(cfg.id)
    s.flush()

    existing_defaults = {
        k.id for k in s.query(ModelProviderKey).filter(
            ModelProviderKey.provider_id == FOLD_TARGET,
            ModelProviderKey.is_default.is_(True))
    }
    for key in s.query(ModelProviderKey).filter(
            ModelProviderKey.provider_id.in_(FOLD_SOURCES)).all():
        key.provider_id = FOLD_TARGET
        if key.is_default:
            if existing_defaults:
                key.is_default = False
            else:
                existing_defaults.add(key.id)
        moved["keys"].append(key.id)
    s.flush()

    for pid in FOLD_SOURCES:
        still = (s.query(ModelConfig).filter(ModelConfig.provider == pid).count()
                 + s.query(ModelProviderKey).filter(ModelProviderKey.provider_id == pid).count())
        if still:
            continue
        row = s.get(ModelProvider, pid)
        if row is not None:
            s.delete(row)
            moved["providers"].append(pid)
    s.flush()
    return moved


def list_providers() -> list[dict]:
    ensure_providers()
    with session_scope() as s:
        rows = s.query(ModelProvider).order_by(ModelProvider.id).all()
        return [_to_dict(r) for r in rows]


def model_counts() -> dict[str, int]:
    with session_scope() as s:
        out: dict[str, int] = {}
        for cfg in s.query(ModelConfig).all():
            prov = str(cfg.provider or "").strip().lower()
            if prov:
                out[prov] = out.get(prov, 0) + 1
        return out


def upsert(payload: dict) -> dict:
    """新建或更新一家提供方。kind **不读请求体**（官方身份由后端判定）。"""
    from backend.services.model import endpoint_guard

    pid = str(payload.get("id") or payload.get("provider") or "").strip().lower()
    label = str(payload.get("label") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    if not pid:
        raise ValidationError("缺少提供方 ID")
    if not label:
        raise ValidationError("提供方名称必填")
    if base_url:
        base_url = endpoint_guard.normalize_base_url(base_url)
    official = pid in _official_ids()
    auth = "none" if str(payload.get("auth") or "").strip() == "none" else "bearer"
    with session_scope() as s:
        row = s.get(ModelProvider, pid)
        if row is None:
            meta = _meta_for(pid)
            row = ModelProvider(
                id=pid,
                kind="official" if official else "custom",
                label=label,
                icon=str(payload.get("icon") or meta.get("icon", DEFAULT_ICON)),
                base_url=base_url,
                auth=auth,
                secret_ref=str(payload.get("secret_ref") or "") or None,
                note=str(payload.get("note") or meta.get("note", "")),
            )
            s.add(row)
        else:
            row.label = label
            if base_url:
                row.base_url = base_url
            row.auth = auth
            if payload.get("secret_ref") is not None:
                row.secret_ref = str(payload.get("secret_ref") or "") or None
            if payload.get("note") is not None:
                row.note = str(payload.get("note") or "")
            if payload.get("icon") is not None:
                row.icon = str(payload.get("icon") or "")
            row.kind = "official" if official else "custom"
        s.commit()
        s.refresh(row)
        return _to_dict(row)


def delete(provider_id: str) -> dict:
    """删除一家提供方；还有模型挂着就拒（计划 §2.2：防止产生失效引用）。"""
    pid = str(provider_id or "").strip().lower()
    if not pid:
        raise ValidationError("缺少提供方 ID")
    counts = model_counts()
    if counts.get(pid):
        raise ConflictError(
            "这一家还有 %s 个模型在用；先删模型，或在「编辑」里把它们改到别家。" % counts[pid]
        )
    with session_scope() as s:
        row = s.get(ModelProvider, pid)
        if row is None:
            raise NotFoundError("提供方不存在")
        s.delete(row)
        s.commit()
    return {"id": pid, "deleted": True}
