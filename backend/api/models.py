"""模型管理 API（规划 §20.7）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request

from backend.api.response import envelope
from backend.core.utils import json_dumps
from backend.services.model import model_service

router = APIRouter(tags=["models"])


def _with_key_source(items: list[dict]) -> list[dict]:
    """给每行补「密钥来源」（``ok`` / ``missing`` / ``invalid``），并据此定 ``has_secret``。

    为什么 ``has_secret`` 要在这里重算：``model_service`` 的老口径是
    "模型行自己写了 secret_ref 才算有密钥"，它不认识"跟随提供方默认密钥"和
    "key_mode=none 不需要密钥"这两种新形态 —— 于是能正常调用的模型被前端标成
    「未配置密钥」（实测 vision 那行：has_secret=False 而 key_source=provider/ok）。
    现在**唯一真源就是 key_source**：解析到密钥（status=ok）才算有。
    ``is_mock`` 的模型（mock/hash/heuristic）本来就不需要密钥，保持原值不动。

    只有来源与状态，**没有明文、也没有 secret_ref** —— 密钥内容是后端的事。
    """
    from backend.services.model import secret_resolver

    for item in items:
        source = secret_resolver.describe(item)
        item["key_source"] = source
        if not item.get("is_mock"):
            item["has_secret"] = source["status"] == "ok"
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
async def model_usage_stats(request: Request,
                            window: str = Query("7d"),
                            vendor: str = Query("all"),
                            model: str = Query("all"),
                            group: str = Query("model")):
    """模型用量统计：多维系列 + 按小时/按天分桶 + 可筛选（契约见 usage_stats.stats）。

    · window：24h | 7d | 14d | 30d（默认 7d）。24h 是**小时桶**，
      不是把日数据摊成小时（那样折线会像"一整天的量全堆在某一小时"）；
    · vendor：厂商 id 或 all；model：统计身份键（cfg:<id> / gone:<model_name>）
      逗号分隔，或者 all；
    · group：model | vendor（默认 model）。
    · 筛选只影响 series / totals：filters 永远是窗口内全量（否则下拉框一选就把自己筛没）；
      空库与查询异常都返回**合法空结构**（空 series、零 totals），不抛 500。

    金额按币种分开返回（cost 是 {币种: 金额} 字典）—— **人民币与美元永不直接相加**。
    """
    from backend.services.model import usage_stats

    return envelope(usage_stats.stats(window=window, vendor=vendor, model=model,
                                      group=group), request=request)


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
        # 展示顺序：官方目录里还没入库的家沿用 0（= 没排过），入库后就是用户拖出来的值
        item["sort"] = int(db.get("sort") or 0)
        return item

    for item in data["providers"]:
        _decorate(item)
    for row in db_rows.values():
        if row["id"] in official_ids:
            continue
        row["configured"] = counts.get(row["id"], 0)
        row["types"] = []
        data["providers"].append(_decorate(row))
    # 数组按 sort 升序（同 sort 按 id 稳定）；自定义入口是单独的 custom 键，仍排在最后
    data["providers"].sort(key=lambda p: (p["sort"], p["id"]))
    return envelope(data, request=request)




@router.put("/models/providers/order")
async def reorder_providers(request: Request, payload: dict = Body(default={})):
    """按数组下标重排提供方目录（模型页拖拽）。

    body：`{"ids": ["deepseek", "anthropic", "openai", ...]}`——
    下标即新的 sort（0..n-1）；没提到的提供方保持相对顺序、排在后面（补 1000+i）。
    未知 id → 400 并点名是哪个；同样的 body 调两次结果一样（幂等）。
    """
    from backend.services.model import provider_store

    ids = payload.get("ids")
    if not isinstance(ids, list):
        from backend.core.errors import ValidationError

        raise ValidationError("ids 必须是提供方 id 数组")
    rows = provider_store.set_order(ids)
    return envelope({"items": rows}, message="提供方顺序已保存", request=request)


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


def _stored_probe_base_url(cfg: dict) -> tuple[str, str]:
    """已存配置的**探测端点**：模型自己的 base_url → 提供方行 → 官方目录默认端点。

    为什么需要回落：官方提供方下不少模型是**故意**把 base_url 留空的（端点由 adapter
    按厂商默认 host 解析，多模态向量那条尤其如此）。以前这里直接报
    「请提供 Base URL」，于是这些模型在编辑弹窗里永远拉不到模型列表。

    只用于**这一次探测**，绝不写回 model_config.base_url —— 写回去会把
    “留空 = 用 adapter 默认端点”的语义破坏掉（多模态协议会被带到错误端点）。
    返回 (端点, 来源)；来源仅用于调试/日志，不写库。
    """
    base = str(cfg.get("base_url") or "").strip()
    if base:
        return base, "model"
    provider = str(cfg.get("provider") or "").strip().lower()
    if not provider:
        return "", ""
    from backend.services.model import provider_store, providers_catalog

    row = next((p for p in provider_store.list_providers() if p["id"] == provider), None)
    if row is not None:
        base = str(row.get("base_url") or "").strip()
        if base:
            return base, "provider"
    base = providers_catalog.default_base_url(provider)
    return (base, "catalog") if base else ("", "")


@router.post("/models/probe-models")
async def probe_models(request: Request, payload: dict = Body(default={})):
    """拉取某个端点的模型列表（`GET {base}/models`）。

    两种用法：
    · 传 `model_id`：用**库里已存**的 base_url + 密钥（设置页对已有配置「刷新模型列表」）；
      该模型 base_url 留空时按「提供方行 → 官方目录默认端点」回落（只用于探测，不写库）；
    · 传 `base_url`（+ 可选 `api_key` / `key_id` / `provider` / `secret_ref`）：保存前先探一次。
      创建流程里前端还没落库，「跟随提供方默认密钥」时传 `provider`（后端走同一条
      `secret_resolver` 解析这家默认密钥），「指定密钥」时传 `key_id`，`key_mode=none` 则不带密钥。
      不带这些参数就是匿名探测（只对公开模型列表的端点有效）。

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
    key_id = str(payload.get("key_id") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    key_mode = str(payload.get("key_mode") or "").strip().lower()
    stored = bool(model_id)

    if stored:
        cfg = next((m for m in model_service.list_models() if str(m.get("id")) == model_id), None)
        if not cfg:
            raise ValidationError("模型配置不存在")
        # 模型 base_url 为空是**合法**的（端点交给 adapter / 官方默认值解析）：
        # 探测时按 提供方行 → 官方目录默认端点 回落，但不写回这条模型
        base_url, _source = _stored_probe_base_url(cfg)
        if not api_key:
            # **统一解析器**：密钥从哪来只有一处决定（改造前这里自己又读了一遍文件，
            # 于是"设置页测通了、调用链 401"这种事迟早发生）
            api_key = secret_resolver.resolve_for_model(cfg)[0]
    elif not api_key and key_mode == "none":
        # 明确「无需密钥」：本地推理 / 公开端点，绝不回退去读别人的密钥
        api_key = None
    elif not api_key and key_id:
        # 创建流程里选了「独立指定密钥」：行还没挂到模型上，按 id 走同一条解析链
        api_key = secret_resolver.resolve_for_model(
            {"key_mode": "key", "provider_key_id": key_id})[0]
    elif not api_key and provider:
        # 创建流程里「跟随提供方默认密钥」：这家有默认密钥就用它（没有则匿名探测）
        api_key = secret_resolver.resolve_for_model(
            {"provider": provider, "key_mode": "auto"})[0]
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
