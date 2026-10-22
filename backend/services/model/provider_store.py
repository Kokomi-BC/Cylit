"""模型提供方的持久化（`model_provider` 表）+ 历史数据回填。

用户口径（改造计划 §5）：Provider 一级实体。设计取舍：

· **不重建数据**：历史库里 provider 只是 `model_config.provider` 一个字符串，所以这里做的是
  「按字符串回填 provider 行」（`ensure_providers`，幂等，重复跑不产生重复记录）；
· **官方身份只由后端判定**：`kind=official` 只给 `presets.py` 里真有的 provider，
  请求体传入的 kind 一律忽略；
· **删除要检查引用**：还有模型挂在这家厂商上就拒绝删除（计划 §2.2 / §7.7）。
"""

from __future__ import annotations

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.repositories.mysql.models import ModelConfig, ModelProvider
from backend.repositories.mysql.session import session_scope

log = get_logger(__name__)

DEFAULT_ICON = ""


def _official_ids() -> set[str]:
    from backend.services.model import presets

    ids = set()
    for preset in presets.list_presets():
        prov = str(preset.get("provider") or "").strip().lower()
        if prov:
            ids.add(prov)
    return ids


def _meta_for(provider: str) -> dict:
    from backend.services.model import providers_catalog

    return providers_catalog.PROVIDER_META.get(provider, {})


def _to_dict(row: ModelProvider) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "label": row.label,
        "icon": row.icon or "",
        "base_url": row.base_url or "",
        "auth": row.auth or "bearer",
        "note": row.note or "",
        "status": row.status or "unknown",
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


def ensure_providers() -> int:
    """把「模型行上出现过的 provider 字符串」补成 provider 记录（幂等）。

    返回本次新建的条数。读目录或启动时都可调用；重复调用返回 0。
    """
    official = _official_ids()
    created = 0
    with session_scope() as s:
        known = {row.id for row in s.query(ModelProvider).all()}
        seen: set[str] = set()
        for cfg in s.query(ModelConfig).all():
            prov = str(cfg.provider or "").strip().lower()
            if prov:
                seen.add(prov)
        for prov in sorted(seen - known):
            meta = _meta_for(prov)
            s.add(ModelProvider(
                id=prov,
                kind="official" if prov in official else "custom",
                label=meta.get("label") or prov,
                icon=meta.get("icon", DEFAULT_ICON),
                base_url="",
                auth=meta.get("auth", "bearer"),
                note=meta.get("note", ""),
            ))
            created += 1
        s.commit()
    if created:
        log.info("provider 回填：新建 %s 条", created)
    return created


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
