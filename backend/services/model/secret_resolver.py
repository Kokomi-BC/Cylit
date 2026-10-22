"""模型密钥解析的**唯一入口**（改造计划 A 阶段）。

为什么只能有一处：一个模型实际用哪把密钥，决定"烧谁的额度、看谁的数据"。两套实现
迟早会分叉 —— 设置页测通的那把和调用链实际用的不是同一把，线上就是 401 和
"明明配了密钥却说没配"（改造前 probe-models 就自己又写了一遍读文件）。

解析链（``resolve_for_model`` → ``(key, source)``）：

1. ``key_mode == 'none'``：无密钥（本地模型 / 免密端点），**绝不回退读文件**；
2. ``key_mode == 'key'`` + ``provider_key_id``：只认这一行；行不存在或文件读不到就
   报 ``invalid``，**不回退**到提供方默认或别的密钥（回退 = 悄悄用另一个账号）；
3. ``auto``：提供方默认密钥行 → 其 secret_ref；没有默认行就看 ``model_provider.secret_ref`；
4. 以上都没有：历史兼容 —— ``read_secret(cfg.secret_ref)`` → ``read_secret(cfg.id)``。
   老配置只用 id 当文件名，这条**不能删**，删了线上老模型立刻 "未配置密钥"。

返回的 ``key`` 是明文，只允许给 Provider 构造用：**任何 API 响应 / 日志 / 异常消息
都不许出现它**。``source`` 是给前端显示的「密钥来源」：
``{mode: provider|key|none|legacy, key_id, key_label, status: ok|missing|invalid}``。
"""

from __future__ import annotations

from backend.core.config import get_settings
from backend.repositories.mysql.models import ModelProvider, ModelProviderKey
from backend.repositories.mysql.session import session_scope

#: 模型行的密钥模式（与 ``model_config.key_mode`` 一一对应）
KEY_MODES = ("auto", "key", "none")


def read_secret_value(name: str | None) -> str | None:
    """按文件名读 ``data/secrets/<name>``（也认 ``<name>.txt``）；空文件等同没有。

    这是**读文件**的唯一入口（``key_store`` 也走它），别处不要再直接调
    ``get_settings().read_secret`` 解析模型密钥。
    """
    if not name:
        return None
    return (get_settings().read_secret(str(name).strip()) or "").strip() or None


def _source(mode: str, *, key_id: str = "", key_label: str = "",
            status: str = "missing") -> dict:
    return {
        "mode": mode,
        "key_id": key_id or "",
        "key_label": key_label or "",
        "status": status,
    }


def _normalize_mode(cfg: dict) -> str:
    mode = str(cfg.get("key_mode") or "auto").strip().lower()
    return mode if mode in KEY_MODES else "auto"


def key_row(key_id: str | None) -> dict | None:
    """按 id 取密钥行（不含明文）。行不存在返回 None —— 调用方负责报 invalid/400。"""
    if not key_id:
        return None
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, str(key_id))
        if row is None:
            return None
        return _row_dict(row)


def _default_key_row(provider_id: str | None) -> dict | None:
    pid = str(provider_id or "").strip().lower()
    if not pid:
        return None
    with session_scope() as sess:
        rows = (sess.query(ModelProviderKey)
                .filter(ModelProviderKey.provider_id == pid,
                        ModelProviderKey.is_default.is_(True))
                .order_by(ModelProviderKey.id).all())
        # 理论上同一家只有一条默认（切换在同一个事务里互斥）；万一历史数据有两条，
        # 按 id 稳定取第一条 —— 解析必须是确定的，不能每次翻到不同的账号。
        return _row_dict(rows[0]) if rows else None


def _provider_row(provider_id: str | None) -> dict | None:
    pid = str(provider_id or "").strip().lower()
    if not pid:
        return None
    with session_scope() as sess:
        row = sess.get(ModelProvider, pid)
        if row is None:
            return None
        return {
            "id": row.id,
            "secret_ref": (row.secret_ref or "").strip(),
            "base_url": (row.base_url or "").strip(),
            "auth": row.auth or "bearer",
            "updated_at": row.updated_at,
        }


def _row_dict(row) -> dict:
    return {
        "id": row.id,
        "provider_id": row.provider_id or "",
        "label": row.label or "",
        "secret_ref": (row.secret_ref or "").strip(),
        "is_default": bool(row.is_default),
        "status": row.status or "unknown",
        "updated_at": row.updated_at,
    }


def resolve_for_model(cfg: dict | None) -> tuple[str | None, dict]:
    """解析一个模型配置实际该用的密钥 → ``(明文, source)``。

    明文只给 Provider 构造用；source 里没有任何凭据内容（文件名也不给：
    secret_ref 不出后端，前端只需要知道"用的是哪一把、什么状态"）。
    """
    cfg = cfg or {}
    mode = _normalize_mode(cfg)

    if mode == "none":
        return None, _source("none", status="ok")

    if mode == "key":
        wanted = str(cfg.get("provider_key_id") or "").strip()
        row = key_row(wanted)
        if row is None:
            # 行被删了 / 没选具体行：明确报 invalid，**不回退**（否则会悄悄换账号）
            return None, _source("key", key_id=wanted, status="invalid")
        value = read_secret_value(row["secret_ref"])
        if value is None:
            return None, _source("key", key_id=row["id"], key_label=row["label"],
                                 status="invalid")
        return value, _source("key", key_id=row["id"], key_label=row["label"],
                              status="ok")

    pid = str(cfg.get("provider") or "").strip().lower()
    row = _default_key_row(pid)
    if row is not None:
        value = read_secret_value(row["secret_ref"])
        if value is None:
            # 默认密钥行指向的文件不在：报 missing，**不回退**到别的行/别的文件
            return None, _source("provider", key_id=row["id"], key_label=row["label"],
                                 status="missing")
        return value, _source("provider", key_id=row["id"], key_label=row["label"],
                              status="ok")

    prov = _provider_row(pid)
    if prov is not None and prov["secret_ref"]:
        value = read_secret_value(prov["secret_ref"])
        if value is None:
            return None, _source("provider", status="missing")
        return value, _source("provider", status="ok")

    # 历史兼容：老配置只用 secret_ref（回落到 id）当文件名
    for name in (cfg.get("secret_ref"), cfg.get("id")):
        value = read_secret_value(str(name or ""))
        if value is not None:
            return value, _source("legacy", status="ok")
    return None, _source("legacy", status="missing")


def describe(cfg: dict | None) -> dict:
    """只要「密钥来源」，不要明文（列表接口给前端标红用）。"""
    return resolve_for_model(cfg)[1]


def cache_token(cfg: dict | None) -> str:
    """密钥指纹（进 model_manager 的缓存键）：**不含明文、不含文件名以外的凭据**。

    为什么要它：缓存键原来只有 id|provider|model_name，密钥行换了文件内容 / 换了默认，
    已经建好的 Provider 还揣着旧密钥 —— 用户改了密钥却"还是 401"。把密钥行的
    updated_at 掺进键里，行一变缓存自然换键（旧的留在缓存里等 LRU 淘汰即可）。
    """
    cfg = cfg or {}
    mode = _normalize_mode(cfg)
    if mode == "none":
        return "none"
    if mode == "key":
        row = key_row(cfg.get("provider_key_id"))
        if row is None:
            return "key:missing"
        return f"key:{row['id']}:{_ts(row['updated_at'])}"
    pid = str(cfg.get("provider") or "").strip().lower()
    row = _default_key_row(pid)
    if row is not None:
        return f"provider:{row['id']}:{_ts(row['updated_at'])}"
    prov = _provider_row(pid)
    if prov is not None and prov["secret_ref"]:
        return f"provider-ref:{prov['id']}:{_ts(prov['updated_at'])}"
    return f"legacy:{cfg.get('secret_ref') or cfg.get('id') or ''}"


def _ts(value) -> str:
    return value.isoformat(timespec="microseconds") if value else ""
