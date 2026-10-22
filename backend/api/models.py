"""模型管理 API（规划 §20.7）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from backend.api.response import envelope
from backend.core.utils import json_dumps
from backend.services.model import model_service

router = APIRouter(tags=["models"])


def _with_key_source(items: list[dict]) -> list[dict]:
    """给每行补「密钥来源」（``ok`` / ``missing`` / ``invalid``，前端据此标红）。

    只有来源与状态，**没有明文、也没有 secret_ref** —— 密钥内容是后端的事。
    """
    from backend.services.model import secret_resolver

    for item in items:
        item["key_source"] = secret_resolver.describe(item)
    return items


@router.get("/models")
async def list_models(request: Request, model_type: str | None = Query(None)):
    return envelope({"items": _with_key_source(model_service.list_models(model_type))},
                    request=request)


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


# ⚠️ `/models/providers`、`/models/{provider_id}/keys`、`/models/keys` 这些都必须声明在
# `/models/{model_id}` **之前**，否则 "providers" / "keys" 会被当成 model_id 捕获
# （接口不可达，返回"模型不存在：providers"）。

@router.get("/models/providers")
async def list_providers(request: Request):
    """官方提供方目录 + 自定义入口（模型页「+」里的那排卡片）。

    `kind` 由后端按 `providers_catalog.PROVIDER_META` 算出来 —— 前端无法把自定义端点
    伪装成官方。官方目录是后端**写死的清单**（"一键导入预设"那套模板已整套下线）。
    """
    from backend.services.model import key_store, provider_store, providers_catalog

    data = providers_catalog.catalog()
    # 官方目录 = 后端写死的 PROVIDER_META（唯一真源）；DB 里那些**自定义**家的记录补在后面，
    # 这样「+」既能挑官方厂商，也能看到/编辑自己建过的端点。
    official_ids = {p["id"] for p in data["providers"]}
    counts = provider_store.model_counts()
    db_rows = {row["id"]: row for row in provider_store.list_providers()}
    keys = key_store.provider_summary()

    def _decorate(item: dict) -> dict:
        """补「这家有几把密钥 / 默认是哪把 / 连得上吗」——不含任何密钥内容。"""
        pid = item["id"]
        db = db_rows.get(pid, {})
        base_url = str(db.get("base_url") or item.get("base_url") or "").strip()
        info = keys.get(pid, {})
        item["base_url"] = base_url
        item["has_base_url"] = bool(base_url)
        item["status"] = db.get("status") or item.get("status") or "unknown"
        item["key_count"] = int(info.get("key_count") or 0)
        item["default_key_id"] = info.get("default_key_id") or ""
        item["default_key_label"] = info.get("default_key_label") or ""
        return item

    for item in data["providers"]:
        _decorate(item)
    for row in db_rows.values():
        if row["id"] in official_ids:
            continue
        row["configured"] = counts.get(row["id"], 0)
        row["types"] = []
        data["providers"].append(_decorate(row))
    return envelope(data, request=request)


@router.post("/models/providers")
async def upsert_provider(request: Request, payload: dict = Body(default={})):
    """新建 / 更新一家提供方（`kind` 由后端判定，请求体里给什么都不算）。"""
    from backend.services.model import provider_store

    row = provider_store.upsert(payload)
    return envelope(row, message="提供方已保存", request=request)


@router.delete("/models/providers/{provider_id}")
async def delete_provider(request: Request, provider_id: str):
    """删除一家提供方；还有模型挂着会返回 409（不制造失效引用）。"""
    from backend.services.model import provider_store

    return envelope(provider_store.delete(provider_id), message="提供方已删除", request=request)


# ---------------------------------------------------------------------------
# 提供方级密钥（改造计划 A 阶段：一家可以有多把密钥，模型再挑一把）
#
# ⚠️ 与 /models/providers 同理：这些都必须声明在 /models/{model_id} **之前**，
# 否则 "keys" 会被当成 model_id 捕获（返回"模型不存在：keys"）。
# ---------------------------------------------------------------------------


@router.get("/models/keys")
async def list_keys(request: Request, unassigned: int = Query(0)):
    """全部密钥；``?unassigned=1`` 只给「未归属待确认」那一组（迁移进来的散装文件）。

    两个口径分开给：某家的列表在 ``GET /models/providers/{id}/keys``，
    这一条是"跨家总览 + 未归属组"，前端「待认领」区就吃它。
    """
    from backend.services.model import key_store

    if unassigned:
        rows = key_store.list_keys(unassigned=True)
        return envelope({"items": rows, "unassigned": len(rows)}, request=request)
    return envelope({"items": key_store.list_keys()}, request=request)


@router.get("/models/providers/{provider_id}/keys")
async def list_provider_keys(provider_id: str, request: Request):
    """这一家的密钥列表 + 「未归属」那一组（认领入口就在同一屏）。"""
    from backend.services.model import key_store

    return envelope({"items": key_store.list_keys(provider_id),
                     "unassigned": key_store.list_keys(unassigned=True)},
                    request=request)


@router.post("/models/providers/{provider_id}/keys")
async def create_provider_key(request: Request, provider_id: str,
                              payload: dict = Body(default={})):
    """新建一把密钥：``{label, api_key}``（明文落 data/secrets/）或 ``{label, secret_ref}``。

    给 api_key 时后端生成文件名并写文件，**库里只存文件名**（不新增第二种存储）。
    第一把自动成为默认（否则 auto 模式无密钥可用）。
    """
    from backend.services.model import key_store

    row = key_store.create_key(provider_id, payload)
    return envelope(row, message="密钥已添加", request=request)


@router.put("/models/keys/{key_id}")
async def update_key(key_id: str, request: Request, payload: dict = Body(default={})):
    """改显示名 / 状态 / 是否默认；带 ``api_key`` 时**只改文件内容，不换文件名**（不破坏引用）。"""
    from backend.services.model import key_store

    return envelope(key_store.update_key(key_id, payload), message="密钥已更新",
                    request=request)


@router.delete("/models/keys/{key_id}")
async def delete_key(key_id: str, request: Request,
                     force_default: str | None = Query(None)):
    """删一把密钥。

    · 被模型引用 → 409（报数量与模型名，先去改模型）；
    · 删默认密钥且还有别的 → 不自动指定，要求先手选；想一次做完给
      ``?force_default=<key_id>``（同一事务里交接完再删）。
    """
    from backend.services.model import key_store

    return envelope(key_store.delete_key(key_id, force_default=force_default),
                    message="密钥已删除", request=request)


@router.post("/models/keys/{key_id}/test")
async def test_key(key_id: str, request: Request):
    """用**这一把**密钥探该家的 ``GET {base_url}/models``，写回 status / last_tested_at。"""
    from backend.services.model import key_store

    return envelope(await key_store.test_key(key_id), request=request)


@router.post("/models/providers/{provider_id}/test")
async def test_provider(request: Request, provider_id: str,
                        payload: dict = Body(default={})):
    """用**这家的 base_url** 探一次 ``/models``（可带 ``{key_id}`` 指定用哪把，默认用默认密钥）。"""
    from backend.services.model import key_store

    key_id = str(payload.get("key_id") or "").strip() or None
    return envelope(await key_store.test_provider(provider_id, key_id=key_id),
                    request=request)


@router.post("/models/providers/{provider_id}/keys/{key_id}/assign")
async def assign_key(provider_id: str, key_id: str, request: Request):
    """把「未归属密钥」认领到这家（迁移用，幂等）。"""
    from backend.services.model import key_store

    return envelope(key_store.assign_key(key_id, provider_id), message="密钥已归属该提供方",
                    request=request)


@router.post("/models/probe-models")
async def probe_models(request: Request, payload: dict = Body(default={})):
    """拉取某个端点的模型列表（`GET {base}/models`）。

    两种用法：
    · 传 `model_id`：用**库里已存**的 base_url + 密钥（设置页对已有配置「刷新模型列表」）；
    · 传 `base_url`（+ 可选 `api_key` / `secret_ref`）：自定义提供方**保存前**先探一次。

    安全：两种用法都过 `endpoint_guard`（协议、元数据地址、内网、DNS 全部校验）；
    现填地址默认不放行内网，需要 `CYLIT_ALLOW_PRIVATE_MODEL_ENDPOINT=1`。
    响应里**永远不回显**密钥。
    """
    from backend.core.errors import ValidationError
    from backend.services.model import (
        endpoint_guard, model_service, remote_models, secret_resolver,
    )

    model_id = str(payload.get("model_id") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip() or None
    secret_ref = str(payload.get("secret_ref") or "").strip()
    stored = bool(model_id)

    if stored:
        cfg = next((m for m in model_service.list_models() if str(m.get("id")) == model_id), None)
        if not cfg:
            raise ValidationError("模型配置不存在")
        base_url = str(cfg.get("base_url") or "").strip()
        if not api_key:
            # **统一解析器**：密钥从哪来只有一处决定（改造前这里自己又读了一遍文件，
            # 于是"设置页测通了、调用链 401"这种事迟早发生）
            api_key = secret_resolver.resolve_for_model(cfg)[0]
    elif secret_ref and not api_key:
        # 没落库的自定义端点：借同一条解析链的"历史回落"读这个文件（不另写一套）
        api_key = secret_resolver.resolve_for_model(
            {"id": "", "secret_ref": secret_ref, "key_mode": "auto", "provider": ""})[0]

    if not base_url:
        raise ValidationError("请提供 Base URL（或传 model_id 用已保存的配置）")

    data = await remote_models.list_models(
        base_url,
        api_key=api_key,
        allow_private=endpoint_guard.allow_private_for(stored_config=stored),
    )
    # 只回列表与主机名；密钥、完整 URL、请求头都不出现
    return envelope(data, request=request)

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
async def test_model(model_id: str, request: Request, payload: dict = Body(default={})):
    """连通性 + 维度 + 延迟（规划 §12.2 能力探测）。

    可带 ``{key_id}`` **换一把密钥试算**：只在这次探测里生效，不写回模型的
    key_mode / provider_key_id（也不写回模型的 status —— 那一行说的是"按它自己的配置灵不灵"）。
    返回里 key_id / key_label 说明用的是哪把，failure 说明失败在哪一步
    （``no_key`` 未配置密钥 / ``auth`` 401、403 / ``not_found`` 没有 /models /
    ``timeout`` 超时 / ``network`` 连不上 / ``error`` 其它）。
    """
    key_id = str(payload.get("key_id") or "").strip() or None
    return envelope(model_service.test_model(model_id, key_id=key_id), request=request)


@router.put("/models/{model_id}/default")
async def set_default(model_id: str, request: Request):
    return envelope(model_service.set_default(model_id),
                    message="已设为该类型默认模型", request=request)


_ = json_dumps
