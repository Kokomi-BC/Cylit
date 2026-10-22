"""提供方级密钥的持久化（``model_provider_key``）+ 凭据文件读写 + 旧密钥迁移。

三件事统一放这里：

· **CRUD**：一条密钥一行（稳定 id / 显示名 / 所属提供方 / 凭据文件名 / 状态 / 是否默认）；
· **测试连接**：这把密钥能不能打通这家的 ``GET {base_url}/models``（复用 ``remote_models`` +
  ``endpoint_guard``，不另写一套 HTTP）；
· **迁移**（``ensure_keys``，幂等）：把"文件名散落在 ``model_provider.secret_ref`` /
  ``model_config.secret_ref`` / ``data/secrets/`` 里"的现状补成密钥行。

存储红线（与改造前一致，**不新增第二种存储**）：

· 明文只落 ``data/secrets/<name>``（``write_key_file``），库里只有文件名；
· 列表 / 详情一律只给 ``••••`` + 末 4 位（``mask``），``secret_ref`` **不返给前端**；
· 异常消息里不出现明文、也不出现完整 URL 与请求头（``remote_models`` 已经保证）。

迁移**绝不猜归属**：``data/secrets/`` 里没人引用的文件一律落在 ``provider_id=''`` 的
「未归属待确认」组，由用户 ``POST /models/providers/{id}/keys/{key_id}/assign`` 认领。
"""

from __future__ import annotations

import re

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import new_id, now
from backend.repositories.mysql.models import (
    ModelConfig, ModelProvider, ModelProviderKey,
)
from backend.repositories.mysql.session import session_scope
from backend.services.model import secret_resolver

log = get_logger(__name__)

#: 未归属组的 provider_id：空串（迁移时用，见模块文档）
UNASSIGNED = ""
#: ``data/secrets/`` 里**不是模型密钥**的凭据：迁移时跳过，否则模型页会多出
#: mineru.key / agent.token / websearch.key 这种别家子系统的凭据条目
#: （用户会以为是模型密钥）。
NON_MODEL_SECRETS = ("mineru.key", "agent.token", "websearch.key")
#: 掩码前缀（只给末 4 位，永不回原文）
MASK_PREFIX = "••••"
_UNSAFE = re.compile(r"[^a-z0-9_.-]+")


# --------------------------------------------------------------------------
# 凭据文件（唯一明文落盘路径）
# --------------------------------------------------------------------------


def mask(value: str | None) -> str:
    """``••••`` + 末 4 位；读不到给空串。**任何情况下都不返原文。**"""
    text = str(value or "").strip()
    return f"{MASK_PREFIX}{text[-4:]}" if text else ""


def write_key_file(name: str, value: str) -> str:
    """把明文写进 ``data/secrets/<name>``（沿用 ``POST /models/secret`` 的写法）。

    返回落盘用的文件名。空内容 / 带路径分隔符一律拒（不给自己留穿越的可能）。
    """
    clean = str(name or "").strip()
    if not clean or "/" in clean or "\\" in clean or ".." in clean:
        raise ValidationError("密钥名不合法（不能为空、不能含路径分隔符）")
    text = str(value or "").strip()
    if not text:
        raise ValidationError("密钥内容为空")
    s = secret_resolver.get_settings()
    s.secrets_dir.mkdir(parents=True, exist_ok=True)
    (s.secrets_dir / clean).write_text(text, encoding="utf-8")
    return clean


def _new_secret_name(provider_id: str, label: str) -> str:
    """给新密钥起个不会撞的文件名：``<provider>-<随机>.key``。"""
    pid = _UNSAFE.sub("-", str(provider_id or "").strip().lower()).strip("-")
    return f"{pid or 'unassigned'}-{new_id('k')[-8:]}.key"


# --------------------------------------------------------------------------
# 行 ↔ dict
# --------------------------------------------------------------------------


def _to_dict(row) -> dict:
    return {
        "id": row.id,
        "provider_id": row.provider_id or "",
        "label": row.label or "",
        # masked 由后端读文件算出来；secret_ref 本身**不出后端**
        "masked": mask(secret_resolver.read_secret_value(row.secret_ref)),
        "is_default": bool(row.is_default),
        "status": row.status or "unknown",
        "last_tested_at": (row.last_tested_at.isoformat(timespec="seconds")
                           if row.last_tested_at else None),
        "unassigned": not (row.provider_id or ""),
    }


def _set_default(sess, provider_id: str, key_id: str) -> None:
    """同一家提供方至多一条默认（与 ``model_manager._set_default`` 同一手法）。"""
    pid = provider_id or UNASSIGNED
    for row in sess.query(ModelProviderKey).filter(ModelProviderKey.provider_id == pid):
        row.is_default = (row.id == key_id)


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def list_keys(provider_id: str | None = None, *, unassigned: bool = False) -> list[dict]:
    """某家的密钥列表 / 未归属组（``unassigned=True``）/ 全量（都不传）。

    首次读目录时顺手跑一次迁移（``ensure_keys``）—— 前端不需要知道"迁移"这件事。
    """
    ensure_keys()
    pid = str(provider_id or "").strip().lower()
    with session_scope() as sess:
        q = sess.query(ModelProviderKey)
        if unassigned:
            q = q.filter(ModelProviderKey.provider_id == UNASSIGNED)
        elif pid:
            q = q.filter(ModelProviderKey.provider_id == pid)
        rows = q.order_by(ModelProviderKey.provider_id, ModelProviderKey.created_at,
                          ModelProviderKey.id).all()
        return [_to_dict(r) for r in rows]


def provider_summary() -> dict[str, dict]:
    """每家的密钥概况（给 ``GET /models/providers`` 补字段用）。不含任何凭据内容。"""
    out: dict[str, dict] = {}
    for row in list_keys():
        pid = row["provider_id"]
        if not pid:
            continue
        info = out.setdefault(pid, {"key_count": 0, "default_key_id": "",
                                    "default_key_label": ""})
        info["key_count"] += 1
        if row["is_default"]:
            info["default_key_id"] = row["id"]
            info["default_key_label"] = row["label"]
    return out


def get_key(key_id: str) -> dict:
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, str(key_id or ""))
        if row is None:
            raise NotFoundError("密钥不存在")
        return _to_dict(row)


def references(key_id: str) -> list[dict]:
    """哪些模型选了这把密钥（删之前要报数量和名字，见 ``delete_key``）。"""
    with session_scope() as sess:
        rows = (sess.query(ModelConfig)
                .filter(ModelConfig.provider_key_id == str(key_id or ""))
                .order_by(ModelConfig.id).all())
        return [{"id": r.id, "name": r.name or r.id} for r in rows]


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def create_key(provider_id: str, payload: dict) -> dict:
    """新建一把密钥：``{label, api_key}`` 或 ``{label, secret_ref}``。

    给 ``api_key`` 就由后端生成文件名并写进 ``data/secrets/``（**不落库**）；
    给 ``secret_ref`` 表示"文件已经在那了"，只登记引用。
    第一家密钥自动成为默认（否则 ``auto`` 模式无密钥可用）。
    """
    pid = str(provider_id or "").strip().lower()
    ensure_keys()
    label = str(payload.get("label") or "").strip() or "默认密钥"
    api_key = str(payload.get("api_key") or "").strip()
    secret_ref = str(payload.get("secret_ref") or "").strip()
    if api_key and secret_ref:
        raise ValidationError("api_key 与 secret_ref 只能给一个（给了 api_key 就由后端起文件名）")
    if not api_key and not secret_ref:
        raise ValidationError("请提供 api_key（明文写进 data/secrets/）或 secret_ref（已存在的文件名）")
    if secret_ref:
        if "/" in secret_ref or "\\" in secret_ref or ".." in secret_ref:
            raise ValidationError("secret_ref 不能含路径分隔符")
        if secret_resolver.read_secret_value(secret_ref) is None:
            raise ValidationError(f"data/secrets/ 里没有这个密钥文件：{secret_ref}")
    with session_scope() as sess:
        if pid and sess.get(ModelProvider, pid) is None:
            raise NotFoundError("提供方不存在")
        if api_key:
            secret_ref = write_key_file(_new_secret_name(pid, label), api_key)
        siblings = (sess.query(ModelProviderKey)
                    .filter(ModelProviderKey.provider_id == pid).count())
        want = payload.get("is_default")
        is_default = True if want is None and not siblings else bool(want)
        key_id = new_id("pk")
        row = ModelProviderKey(
            id=key_id, provider_id=pid, label=label, secret_ref=secret_ref,
            is_default=False, status=str(payload.get("status") or "unverified")[:16],
        )
        sess.add(row)
        sess.flush()
        if is_default:
            _set_default(sess, pid, key_id)
        sess.flush()
        return _to_dict(row)


def update_key(key_id: str, payload: dict) -> dict:
    """改 ``label`` / ``status`` / ``is_default``；带 ``api_key`` 时**只改文件内容**。

    特意不换文件名：换了就等于换了一把密钥，``model_config.provider_key_id`` 的引用
    和用户对"这是哪把密钥"的认知全断（计划里的硬要求）。
    """
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, str(key_id or ""))
        if row is None:
            raise NotFoundError("密钥不存在")
        if payload.get("label") is not None:
            label = str(payload.get("label") or "").strip()
            if label:
                row.label = label
        if payload.get("status") is not None:
            row.status = str(payload.get("status") or "unknown")[:16]
        api_key = str(payload.get("api_key") or "").strip()
        if api_key:
            ref = (row.secret_ref or "").strip() or _new_secret_name(row.provider_id, row.label)
            row.secret_ref = write_key_file(ref, api_key)
            # 内容变了，旧结论作废：等用户重新点"测试"
            if payload.get("status") is None:
                row.status = "unverified"
        if payload.get("is_default"):
            _set_default(sess, row.provider_id or UNASSIGNED, row.id)
        # 手动碰一下 updated_at：缓存键里有它的指纹（见 secret_resolver.cache_token），
        # 不碰的话"只换文件内容"不会让已建好的 Provider 失效。
        row.updated_at = now()
        sess.flush()
        return _to_dict(row)


def _secret_ref_in_use(sess, secret_ref: str) -> bool:
    """还有别的行指着这个文件吗（密钥行 / 提供方 / 模型，含"模型 id 当文件名"的回落）。"""
    if not secret_ref:
        return True
    if sess.query(ModelProviderKey).filter(
            ModelProviderKey.secret_ref == secret_ref).count():
        return True
    if sess.query(ModelProvider).filter(
            ModelProvider.secret_ref == secret_ref).count():
        return True
    if sess.query(ModelConfig).filter(
            ModelConfig.secret_ref == secret_ref).count():
        return True
    # 历史回落把「模型 id」也当文件名读（见 secret_resolver），别把它正在用的文件删了
    return bool(sess.query(ModelConfig).filter(ModelConfig.id == secret_ref).count())


def _remove_secret_file(secret_ref: str) -> str:
    """删掉 ``data/secrets/<secret_ref>``（连带 ``.txt` 变体），返回删掉的文件名。

    删文件失败（占用 / 权限）只记 warning、**绝不影响删行** —— 行是权威记录，
    文件是它的附属物；反过来让"删不掉文件"卡住"删不掉密钥"，用户只会更困惑。
    """
    name = str(secret_ref or "").strip()
    if not name:
        return ""
    if "/" in name or "\\" in name or ".." in name:
        log.warning("密钥文件名不合法，跳过删文件：%s", name)
        return ""
    s = secret_resolver.get_settings()
    removed = ""
    for candidate in (s.secrets_dir / name, s.secrets_dir / f"{name}.txt"):
        if not candidate.is_file():
            continue
        try:
            candidate.unlink(missing_ok=True)
            removed = name
            log.info("已删除密钥文件 %s", name)      # 只记文件名，永远不记内容
        except OSError as exc:  # noqa: BLE001 - 失败也要继续
            log.warning("删除密钥文件失败（忽略，密钥行已删）：%s（%s）",
                        name, type(exc).__name__)
    return removed


def delete_key(key_id: str, *, force_default: str | None = None) -> dict:
    """删一把密钥（**行 + 它独占的凭据文件**）。

    两条拒绝（都是为了不悄悄改变线上行为）：

    · 还被 ``model_config.provider_key_id`` 引用 → 409，报数量与模型名；
    · 删的是默认密钥且这家还有别的密钥 → **不自动指定**（默认一把换成另一把属于
      用户决策），要求先手选；确实想一次做完就传 ``force_default=<key_id>``。

    删完行之后，若这个文件**没有任何其他引用**（别的密钥行 / 提供方 / 模型），
    连文件一起删掉。为什么必须删：文件留在 ``data/secrets/`` 里，下一次
    ``ensure_keys()`` 会把它当成"未归属待确认"重新捡回来 —— 用户以为删掉的密钥
    又出现在界面上，而里面还是他以为已经删掉的那把 key（安全与体验双输）。
    """
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, str(key_id or ""))
        if row is None:
            raise NotFoundError("密钥不存在")
        used = (sess.query(ModelConfig)
                .filter(ModelConfig.provider_key_id == row.id).all())
        if used:
            names = "、".join((r.name or r.id) for r in used[:5])
            raise ConflictError(
                f"这把密钥还被 {len(used)} 个模型引用（{names}）；先把它们改到别的密钥再删。",
                detail={"count": len(used), "model_ids": [r.id for r in used]},
            )
        pid = row.provider_id or UNASSIGNED
        siblings = [r for r in sess.query(ModelProviderKey)
                    .filter(ModelProviderKey.provider_id == pid)
                    .order_by(ModelProviderKey.id) if r.id != row.id]
        new_default = ""
        if row.is_default and siblings:
            if not force_default:
                raise ConflictError(
                    "这是该提供方的默认密钥；删之前请先指定新的默认密钥"
                    "（PUT /models/keys/<id> 传 is_default=true），"
                    "或用 ?force_default=<key_id> 一次完成交接。",
                    detail={"candidates": [{"id": r.id, "label": r.label}
                                           for r in siblings]},
                )
            target = next((r for r in siblings if r.id == force_default), None)
            if target is None:
                raise ValidationError("force_default 必须是同一家提供方的另一把密钥")
            target.is_default = True
            new_default = target.id
        elif force_default and not row.is_default:
            raise ValidationError("只有删除默认密钥时才需要 force_default")
        secret_ref = (row.secret_ref or "").strip()
        sess.delete(row)
        sess.flush()
        # flush 之后这一行已经不在库里了，再问"还有谁用这个文件"才是准确的
        orphaned = bool(secret_ref) and not _secret_ref_in_use(sess, secret_ref)
    # 事务提交成功之后才动文件：回滚的话文件不该少
    file_removed = _remove_secret_file(secret_ref) if orphaned else ""
    return {"id": key_id, "deleted": True, "new_default": new_default,
            "file_removed": file_removed}


def assign_key(key_id: str, provider_id: str) -> dict:
    """把「未归属密钥」认领到某家（迁移用，**幂等**：已归属同家时原样返回）。"""
    pid = str(provider_id or "").strip().lower()
    if not pid:
        raise ValidationError("缺少提供方 ID")
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, str(key_id or ""))
        if row is None:
            raise NotFoundError("密钥不存在")
        if sess.get(ModelProvider, pid) is None:
            raise NotFoundError("提供方不存在")
        if (row.provider_id or "") == pid:
            return _to_dict(row)
        row.provider_id = pid
        siblings = [r for r in sess.query(ModelProviderKey)
                    .filter(ModelProviderKey.provider_id == pid) if r.id != row.id]
        if row.is_default and siblings:
            # 那家已经有默认密钥了：认领过来的这把不抢默认（用户的默认不该被迁移改掉）
            row.is_default = False
        elif not siblings and not row.is_default:
            row.is_default = True
        row.updated_at = now()
        sess.flush()
        return _to_dict(row)


# --------------------------------------------------------------------------
# 测试连接
# --------------------------------------------------------------------------


async def _probe(base_url: str, api_key: str | None) -> dict:
    """探一次 ``GET {base_url}/models``（复用 remote_models + endpoint_guard）。"""
    from backend.services.model import endpoint_guard, remote_models

    if not (base_url or "").strip():
        return {"ok": False, "status": "error", "count": 0,
                "message": "这家提供方还没填 Base URL（模型页「编辑」里补上再测）"}
    try:
        data = await remote_models.list_models(
            base_url, api_key=api_key,
            allow_private=endpoint_guard.allow_private_for(stored_config=True),
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败就是失败，别把异常抛给前端
        message = str(getattr(exc, "message", "") or type(exc).__name__)
        return {"ok": False, "status": "error", "count": 0, "message": message[:200]}
    count = int(data.get("count") or 0)
    return {"ok": True, "status": "ok", "count": count,
            "message": f"连接正常，取到 {count} 个模型"}


def _persist_key_status(key_id: str, status: str) -> None:
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, key_id)
        if row is None:
            return
        row.status = status[:16]
        row.last_tested_at = now()
        row.updated_at = now()


async def test_key(key_id: str) -> dict:
    """用**这一把**密钥探该家的 ``/models``，把结论写回 status / last_tested_at。"""
    with session_scope() as sess:
        row = sess.get(ModelProviderKey, str(key_id or ""))
        if row is None:
            raise NotFoundError("密钥不存在")
        secret_ref = (row.secret_ref or "").strip()
        provider_id = row.provider_id or ""
        prov = sess.get(ModelProvider, provider_id) if provider_id else None
        base_url = (prov.base_url or "") if prov is not None else ""
    api_key = secret_resolver.read_secret_value(secret_ref)
    if api_key is None:
        res = {"ok": False, "status": "missing", "count": 0,
               "message": "密钥文件读不到（data/secrets/ 里没有这个文件）"}
    else:
        res = await _probe(base_url, api_key)
    _persist_key_status(str(key_id), res["status"])
    return res


def _persist_provider_status(provider_id: str, status: str) -> None:
    with session_scope() as sess:
        row = sess.get(ModelProvider, provider_id)
        if row is None:
            return
        row.status = status[:16]
        row.updated_at = now()


async def test_provider(provider_id: str, key_id: str | None = None) -> dict:
    """用**这家的** base_url 探一次 ``/models``（可指定用哪把密钥，默认用默认密钥）。"""
    pid = str(provider_id or "").strip().lower()
    with session_scope() as sess:
        prov = sess.get(ModelProvider, pid)
        if prov is None:
            raise NotFoundError("提供方不存在")
        base_url = prov.base_url or ""
        auth = prov.auth or "bearer"
        provider_ref = (prov.secret_ref or "").strip()
        chosen_id, chosen_label, chosen_ref = "", "", ""
        if key_id:
            key = sess.get(ModelProviderKey, str(key_id))
            if key is None:
                raise NotFoundError("密钥不存在")
            if (key.provider_id or "") != pid:
                raise ValidationError("这把密钥不属于该提供方")
            chosen_id, chosen_label = key.id, key.label or ""
            chosen_ref = (key.secret_ref or "").strip()
        else:
            default = (sess.query(ModelProviderKey)
                       .filter(ModelProviderKey.provider_id == pid,
                               ModelProviderKey.is_default.is_(True))
                       .order_by(ModelProviderKey.id).first())
            if default is not None:
                chosen_id, chosen_label = default.id, default.label or ""
                chosen_ref = (default.secret_ref or "").strip()
            else:
                chosen_ref = provider_ref
    api_key = secret_resolver.read_secret_value(chosen_ref)
    if api_key is None and auth != "none":
        res = {"ok": False, "status": "missing", "count": 0,
               "message": (f"密钥「{chosen_label or chosen_id}」的文件读不到"
                           if chosen_id else "这家还没有可用密钥，先添加一把再测")}
    else:
        res = await _probe(base_url, api_key)
    _persist_provider_status(pid, res["status"])
    return res


# --------------------------------------------------------------------------
# 迁移（幂等）
# --------------------------------------------------------------------------


def _secret_files() -> list[str]:
    """``data/secrets/`` 下的密钥文件名（``<name>.txt`` 归一成 ``<name>``）。

    ``read_secret`` 两种都认，所以库里记名字时统一去掉 `.txt`，免得同一个文件出现两条。
    """
    s = secret_resolver.get_settings()
    names: set[str] = set()
    if not s.secrets_dir.is_dir():
        return []
    for path in s.secrets_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".txt") and len(name) > 4:
            name = name[:-4]
        names.add(name)
    return sorted(names)


def ensure_keys() -> dict:
    """把历史现状补成密钥行（**幂等**：重复调用返回 0 新建）。

    规则（改造计划 A 阶段）：

    1. 某家有 ``secret_ref`` 且还没有任何密钥行 → 建一条「默认密钥」并设为默认；
    2. 某家没有 ``secret_ref``，但挂着它的模型里有非空 ``secret_ref``（历史预设写入的
       ``deepseek.key`` 这种）→ 用这个文件名建该家的默认密钥行；**同一文件只建一次**
       （两家的模型指向同一个文件时，只归第一家的默认行，另一家继续走历史回落）；
    3. ``data/secrets/`` 里存在、且没有任何密钥行 / 模型 / 提供方引用的文件 → 建
       ``provider_id=''``（未归属）、``status='unverified'`` 的行，label 用文件名。
       **绝不猜归属**。

    **不动** ``model_config.secret_ref`` 的既有值，也**不给任何模型写 provider_key_id**
    —— 老模型因此仍是 ``auto`` + 历史回落，行为与改造前完全一致。
    """
    from backend.services.model import provider_store

    provider_store.ensure_providers()
    counts = {"provider_keys": 0, "model_refs": 0, "unassigned": 0}
    with session_scope() as sess:
        providers = {p.id: p for p in sess.query(ModelProvider).all()}
        key_rows = sess.query(ModelProviderKey).all()
        have_keys = {k.provider_id for k in key_rows if k.provider_id}
        used_refs = {(k.secret_ref or "").strip() for k in key_rows
                     if (k.secret_ref or "").strip()}
        model_rows = sess.query(ModelConfig).all()

        # ---- 规则 1：提供方自己记着文件名 ----
        for pid in sorted(providers):
            ref = (providers[pid].secret_ref or "").strip()
            if not ref or pid in have_keys:
                continue
            sess.add(ModelProviderKey(
                id=new_id("pk"), provider_id=pid, label="默认密钥", secret_ref=ref,
                is_default=True, status="unverified",
            ))
            used_refs.add(ref)
            have_keys.add(pid)
            counts["provider_keys"] += 1

        # ---- 规则 2：提供方没记，但模型行记着 ----
        wanted: dict[str, dict[str, int]] = {}
        for cfg in model_rows:
            pid = str(cfg.provider or "").strip().lower()
            ref = (cfg.secret_ref or "").strip()
            if pid and ref:
                wanted.setdefault(pid, {})
                wanted[pid][ref] = wanted[pid].get(ref, 0) + 1
        for pid in sorted(wanted):
            if pid in have_keys or pid not in providers:
                continue
            # 用得最多的那个文件优先（等价性最大）；同票按名字稳定排序
            for ref in sorted(wanted[pid], key=lambda r: (-wanted[pid][r], r)):
                if ref in used_refs:
                    continue
                sess.add(ModelProviderKey(
                    id=new_id("pk"), provider_id=pid, label="默认密钥", secret_ref=ref,
                    is_default=True, status="unverified",
                ))
                used_refs.add(ref)
                have_keys.add(pid)
                counts["model_refs"] += 1
                break

        # ---- 规则 3：文件没人引用 → 未归属 ----
        referenced = set(used_refs)
        referenced.update((p.secret_ref or "").strip() for p in providers.values())
        for cfg in model_rows:
            referenced.add((cfg.secret_ref or "").strip())
            # 老配置的回落把「模型 id」也当文件名用（见 secret_resolver），
            # 所以 id 也算引用：否则会把正在被 id 回落用着的文件错标成"未归属"。
            referenced.add(str(cfg.id or ""))
        referenced.discard("")
        for name in _secret_files():
            if name in NON_MODEL_SECRETS or name in referenced:
                continue
            sess.add(ModelProviderKey(
                id=new_id("pk"), provider_id=UNASSIGNED, label=name, secret_ref=name,
                is_default=False, status="unverified",
            ))
            referenced.add(name)
            counts["unassigned"] += 1

        sess.flush()
        total = sess.query(ModelProviderKey).count()
    created = counts["provider_keys"] + counts["model_refs"] + counts["unassigned"]
    if created:
        log.info("密钥迁移：新建 %s 条（提供方默认 %s / 模型回落 %s / 未归属 %s）",
                 created, counts["provider_keys"], counts["model_refs"],
                 counts["unassigned"])
    return {**counts, "created": created, "total": total}
