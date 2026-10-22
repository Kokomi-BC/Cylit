"""模型密钥：解析链 / 多密钥实体 / 迁移 / 接口（改造计划 A 阶段验收）。

钉住的约定（错一条线上就会"悄悄用错账号"）：

· 三种 key_mode：``auto`` 跟提供方默认密钥、``key`` 锁定一把、``none`` 不用密钥；
· ``key`` 模式的密钥行失效（文件没了 / 行被删）→ ``invalid`` 且**绝不回退**；
· 提供方默认密钥一换，``auto`` 的模型**实时**跟着换，``key`` 的不动；
· 列表只给 ``••••`` + 末 4 位，绝不回显 ``secret_ref`` 与明文；
· 删被引用的密钥 → 409；删默认密钥且还有别的 → 不自动指定，要么先手选要么 ``force_default``；
· 迁移幂等、未归属**不猜归属**；老配置的解析结果与改造前逐条一致。

数据隔离：测试自建的 provider / 模型 / 密钥一律用 ``__test`` 前缀，跑完直接删行；
密钥文件写进 ``tmp_path``（monkeypatch 掉 ``secrets_dir``），不碰仓库里真实的 data/secrets。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.core.errors import ConflictError, ValidationError
from backend.repositories.mysql.session import healthcheck

U = uuid.uuid4().hex[:6]


def _pid(tag: str) -> str:
    return f"__test_{tag}_{U}"


def _kid(tag: str) -> str:
    return f"__test_{tag}_{U}"


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()  # 顺带建 model_provider_key / 补 model_config 的两列
    from backend.services.model import key_store

    key_store.ensure_keys()
    return True


@pytest.fixture
def sandbox(db_ready):
    """自建数据的清理（provider / 模型 / 密钥全部 ``__test`` 前缀）。"""

    def wipe() -> None:
        from backend.repositories.mysql.models import (
            ModelConfig, ModelProvider, ModelProviderKey,
        )
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            sess.query(ModelConfig).filter(
                ModelConfig.id.like("__test%")).delete(synchronize_session=False)
            sess.query(ModelProviderKey).filter(
                ModelProviderKey.provider_id.like("__test%")).delete(
                    synchronize_session=False)
            sess.query(ModelProviderKey).filter(
                ModelProviderKey.label.like("__test%")).delete(synchronize_session=False)
            sess.query(ModelProvider).filter(
                ModelProvider.id.like("__test%")).delete(synchronize_session=False)

    wipe()
    yield
    wipe()


@pytest.fixture
def secrets(tmp_path, monkeypatch, db_ready):
    """把 data/secrets 指到临时目录：测试绝不读写真实密钥。"""
    from backend.core.config import get_settings

    d = tmp_path / "secrets"
    d.mkdir()
    monkeypatch.setattr(get_settings(), "secrets_dir", d)
    return d


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _provider(pid: str) -> str:
    from backend.services.model import provider_store

    provider_store.upsert({"id": pid, "label": pid,
                           "base_url": "https://api.example.com/v1"})
    return pid


def _model(model_id: str, provider: str, **extra) -> dict:
    from backend.services.model import model_service

    payload = {"id": model_id, "name": model_id, "provider": provider,
               "model_type": "generation", "model_name": "test-model",
               "base_url": "https://api.example.com/v1"}
    payload.update(extra)
    return model_service.create_model(payload)


def _key_with_file(provider: str, tag: str, value: str, **extra) -> dict:
    """建一把"文件真在里面"的密钥（走 create_key 的 api_key 分支）。"""
    from backend.services.model import key_store

    payload = {"label": f"__test_{tag}_{U}", "api_key": value}
    payload.update(extra)
    return key_store.create_key(provider, payload)


def _stub_remote(monkeypatch) -> list[dict]:
    """桩掉 remote_models.list_models（**不联网**），返回它收到的调用记录。"""
    from backend.services.model import remote_models

    calls: list[dict] = []

    async def fake(base_url, *, api_key=None, allow_private=False, timeout=8.0):
        calls.append({"base_url": base_url, "api_key": api_key,
                      "allow_private": allow_private})
        return {"items": [{"id": "m1"}, {"id": "m2"}], "count": 2,
                "host": "api.example.com"}

    monkeypatch.setattr(remote_models, "list_models", fake)
    return calls


# --------------------------------------------------------------------------
# 解析链
# --------------------------------------------------------------------------


def test_auto_uses_provider_default_key(sandbox, secrets):
    from backend.services.model import key_store, secret_resolver

    pid, mid = _pid("auto_p"), _pid("auto_m")
    _provider(pid)
    key = _key_with_file(pid, "auto", "sk-DEFAULT1111")
    cfg = _model(mid, pid)

    value, source = secret_resolver.resolve_for_model(cfg)
    assert value == "sk-DEFAULT1111"
    assert source["mode"] == "provider"
    assert source["status"] == "ok"
    assert source["key_id"] == key["id"]
    assert source["key_label"] == key["label"]
    # 明文、文件名都不进 source
    assert "1111" not in str(source) and key["label"] in str(source)
    assert "secret_ref" not in source
    assert key_store.get_key(key["id"])["masked"] == "••••1111"


def test_auto_follows_default_key_switch(sandbox, secrets):
    from backend.services.model import key_store, secret_resolver

    pid, mid = _pid("switch_p"), _pid("switch_m")
    _provider(pid)
    first = _key_with_file(pid, "a", "sk-FIRST2222")
    second = _key_with_file(pid, "b", "sk-SECOND3333")
    cfg = _model(mid, pid)

    assert secret_resolver.resolve_for_model(cfg)[0] == "sk-FIRST2222"
    key_store.update_key(second["id"], {"is_default": True})
    # 模型行一个字段都没动，解析结果实时跟着默认密钥换
    assert secret_resolver.resolve_for_model(cfg)[0] == "sk-SECOND3333"
    assert cfg["key_mode"] == "auto" and not cfg["provider_key_id"]
    assert key_store.get_key(second["id"])["is_default"] is True
    assert key_store.get_key(first["id"])["is_default"] is False


def test_key_mode_does_not_follow_default_switch(sandbox, secrets):
    from backend.services.model import key_store, secret_resolver

    pid, mid = _pid("lock_p"), _pid("lock_m")
    _provider(pid)
    locked = _key_with_file(pid, "lock", "sk-LOCKED4444")
    other = _key_with_file(pid, "other", "sk-OTHER5555")
    cfg = _model(mid, pid, key_mode="key", provider_key_id=locked["id"])

    assert secret_resolver.resolve_for_model(cfg) == (
        "sk-LOCKED4444",
        {"mode": "key", "key_id": locked["id"], "key_label": locked["label"],
         "status": "ok"},
    )
    key_store.update_key(other["id"], {"is_default": True})
    assert secret_resolver.resolve_for_model(cfg)[0] == "sk-LOCKED4444"


def test_key_mode_missing_file_is_invalid_without_fallback(sandbox, secrets):
    from backend.services.model import key_store, secret_resolver

    pid, mid = _pid("bad_p"), _pid("bad_m")
    _provider(pid)
    good = _key_with_file(pid, "good", "sk-GOOD6666")
    broken = _key_with_file(pid, "broken", "sk-BROKEN7777")
    cfg = _model(mid, pid, key_mode="key", provider_key_id=broken["id"])

    # 只有这一把的文件没了：报 invalid，**不许**回退到提供方默认（那是另一个账号）
    (secrets / _ref_of(broken["id"])).unlink()
    value, source = secret_resolver.resolve_for_model(cfg)
    assert value is None
    assert source["status"] == "invalid"
    assert source["key_id"] == broken["id"]
    assert good["is_default"] is True  # 默认那把还在，且没被拿来用


def test_key_mode_unknown_row_is_invalid(sandbox, secrets):
    from backend.services.model import secret_resolver

    pid, mid = _pid("gone_p"), _pid("gone_m")
    _provider(pid)
    _key_with_file(pid, "live", "sk-LIVE8888")
    cfg = _model(mid, pid)
    # create/update 会拦下"行不存在"（400），这里直接构造"行被删了"的历史状态
    value, source = secret_resolver.resolve_for_model(
        {**cfg, "key_mode": "key", "provider_key_id": "pk-nope"})
    assert value is None and source["status"] == "invalid"


def test_none_mode_never_reads_files(sandbox, secrets):
    from backend.services.model import secret_resolver

    pid, mid = _pid("none_p"), _pid("none_m")
    _provider(pid)
    _key_with_file(pid, "none", "sk-NONE9999")
    cfg = _model(mid, pid, key_mode="none", secret_ref="whatever.key")
    value, source = secret_resolver.resolve_for_model(cfg)
    assert value is None
    assert source["mode"] == "none" and source["status"] == "ok"


def test_legacy_fallback_keeps_old_behaviour(sandbox, secrets):
    """老配置只用 id 当文件名：这条回落不能删（删了线上老模型立刻没密钥）。"""
    from backend.services.model import key_store, secret_resolver

    pid, mid = _pid("legacy_p"), _pid("legacy_m")
    _provider(pid)
    key_store.write_key_file(mid, "sk-LEGACY0000")
    cfg = _model(mid, pid, secret_ref="absent.key")

    value, source = secret_resolver.resolve_for_model(cfg)
    assert value == "sk-LEGACY0000"
    assert source["mode"] == "legacy" and source["status"] == "ok"
    assert _old_api_key(cfg) == value  # 与改造前逐字一致


def test_missing_key_is_distinguishable_from_invalid(sandbox, secrets):
    from backend.services.model import secret_resolver

    pid, mid = _pid("nokey_p"), _pid("nokey_m")
    _provider(pid)
    cfg = _model(mid, pid)  # 这家没密钥、模型也没 secret_ref
    value, source = secret_resolver.resolve_for_model(cfg)
    assert value is None
    assert source["status"] == "missing"


def _ref_of(key_id: str) -> str:
    from backend.repositories.mysql.models import ModelProviderKey
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        return sess.get(ModelProviderKey, key_id).secret_ref


def _old_api_key(cfg: dict) -> str | None:
    """改造前的 ``model_manager._api_key``（原样抄下来做等价性断言）。"""
    from backend.core.config import get_settings

    ref = cfg.get("secret_ref")
    if not ref:
        return None
    s = get_settings()
    key = s.read_secret(ref)
    if key is None:
        key = s.read_secret(cfg.get("id") or "")
    return key


def test_existing_models_resolve_exactly_as_before(db_ready):
    """等价性断言：库里**改造前就有**的配置行，新旧解析结果必须逐条相同。"""
    from backend.services.model.model_manager import model_manager

    rows = [c for c in model_manager.list_configs() if not str(c.get("id")).startswith("__test")]
    checked = 0
    for cfg in rows:
        if cfg.get("provider_key_id"):
            continue  # 用户显式锁定的行不属于"老配置"
        assert model_manager._api_key(cfg) == _old_api_key(cfg), cfg.get("id")
        checked += 1
    assert checked >= 1, "没有可对比的历史配置行"


# --------------------------------------------------------------------------
# 密钥 CRUD / 引用检查
# --------------------------------------------------------------------------


def test_list_keys_never_leaks_secret(sandbox, secrets):
    from backend.services.model import key_store

    pid = _pid("mask_p")
    _provider(pid)
    _key_with_file(pid, "mask", "sk-abcdefgh1234")
    rows = key_store.list_keys(pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["masked"] == "••••1234"
    assert set(row) == {"id", "provider_id", "label", "masked", "is_default",
                        "status", "last_tested_at", "unassigned"}
    blob = repr(rows)
    assert "sk-abcdefgh1234" not in blob and "secret_ref" not in blob


def test_delete_key_in_use_conflicts(sandbox, secrets):
    from backend.services.model import key_store

    pid, mid = _pid("use_p"), _pid("use_m")
    _provider(pid)
    key = _key_with_file(pid, "used", "sk-USED1234")
    _model(mid, pid, key_mode="key", provider_key_id=key["id"])
    assert key_store.references(key["id"])[0]["id"] == mid
    with pytest.raises(ConflictError) as err:
        key_store.delete_key(key["id"])
    assert "1 个模型" in str(err.value.message)
    assert mid in str(err.value.detail["model_ids"])


def test_delete_default_key_requires_handover(sandbox, secrets):
    from backend.services.model import key_store

    pid = _pid("hand_p")
    _provider(pid)
    first = _key_with_file(pid, "first", "sk-HAND1111")
    second = _key_with_file(pid, "second", "sk-HAND2222")
    with pytest.raises(ConflictError) as err:
        key_store.delete_key(first["id"])
    assert [c["id"] for c in err.value.detail["candidates"]] == [second["id"]]
    # 显式交接：同一事务里把默认给另一把，再删
    assert key_store.delete_key(first["id"], force_default=second["id"])["new_default"] == second["id"]
    assert key_store.get_key(second["id"])["is_default"] is True


def test_create_key_rejects_path_and_missing_file(sandbox, secrets):
    from backend.services.model import key_store

    pid = _pid("bad_create_p")
    _provider(pid)
    with pytest.raises(ValidationError):
        key_store.create_key(pid, {"label": "x", "secret_ref": "../etc/passwd"})
    with pytest.raises(ValidationError):
        key_store.create_key(pid, {"label": "x", "secret_ref": "not-there.key"})
    with pytest.raises(ValidationError):
        key_store.create_key(pid, {"label": "x"})


def test_update_key_changes_file_not_ref(sandbox, secrets):
    from backend.services.model import key_store

    pid = _pid("upd_p")
    _provider(pid)
    key = _key_with_file(pid, "upd", "sk-OLD0000")
    before = _ref_of(key["id"])
    key_store.update_key(key["id"], {"api_key": "sk-NEW1111", "label": "改名了"})
    assert _ref_of(key["id"]) == before  # 文件名不变 = 不破坏引用
    assert (secrets / before).read_text(encoding="utf-8") == "sk-NEW1111"
    row = key_store.get_key(key["id"])
    assert row["label"] == "改名了" and row["masked"] == "••••1111"


# --------------------------------------------------------------------------
# 跨提供方 / 模型级测试
# --------------------------------------------------------------------------


def test_cross_provider_key_is_rejected(sandbox, secrets):
    from backend.services.model import key_store, model_service

    pid_a, pid_b = _pid("cross_a"), _pid("cross_b")
    _provider(pid_a)
    _provider(pid_b)
    foreign = _key_with_file(pid_a, "foreign", "sk-FOREIGN1")
    mid = _pid("cross_m")
    with pytest.raises(ValidationError):
        _model(mid, pid_b, key_mode="key", provider_key_id=foreign["id"])
    cfg = _model(mid, pid_b)
    with pytest.raises(ValidationError):
        model_service.update_model(mid, {"provider_key_id": foreign["id"]})
    with pytest.raises(ValidationError):
        model_service.update_model(mid, {"key_mode": "key", "provider_key_id": "pk-nope"})
    assert key_store.get_key(foreign["id"])["provider_id"] == pid_a
    assert model_service.get_model(mid)["key_mode"] == "auto"


def test_model_test_with_key_id_uses_key_without_persisting(sandbox, secrets, monkeypatch):
    from backend.services.model import key_store, model_service, model_manager as mm_mod

    pid, mid = _pid("probe_p"), _pid("probe_m")
    _provider(pid)
    _key_with_file(pid, "default", "sk-DEF7777")
    alt = _key_with_file(pid, "alt", "sk-ALT8888")
    _model(mid, pid)

    seen: list[str | None] = []

    class _Fake:
        def __init__(self, cfg, key):
            self.config = cfg
            self.key = key
            seen.append(key)

        def test(self):
            from backend.providers.base import ModelInfo

            return ModelInfo(ok=True, model_name="fake", detail="stub ok")

    monkeypatch.setattr(mm_mod, "build_generation_provider", lambda cfg, key: _Fake(cfg, key))
    res = model_service.test_model(mid, key_id=alt["id"])
    assert res["ok"] is True and res["failure"] == ""
    assert res["key_id"] == alt["id"] and res["key_label"] == alt["label"]
    assert seen == ["sk-ALT8888"]
    # 试算不落库：模型的密钥选择一个字段都没变
    cfg = model_service.get_model(mid)
    assert cfg["key_mode"] == "auto" and not cfg["provider_key_id"]
    # 不传 key_id 就按模型现在的解析结果（= 提供方默认密钥）
    res2 = model_service.test_model(mid)
    default_row = next(k for k in key_store.list_keys(pid) if k["is_default"])
    assert res2["key_id"] == default_row["id"]
    assert seen[-1] == "sk-DEF7777"


def test_model_test_reports_no_key(sandbox, secrets):
    from backend.services.model import model_service

    pid, mid = _pid("probe_nokey_p"), _pid("probe_nokey_m")
    _provider(pid)
    _model(mid, pid)
    res = model_service.test_model(mid)
    assert res["ok"] is False and res["failure"] == "no_key"
    assert res["key_status"] == "missing"


# --------------------------------------------------------------------------
# 提供方测试连接（桩测，不联网）
# --------------------------------------------------------------------------


def test_provider_test_connection_uses_requested_key(sandbox, secrets, monkeypatch):
    from backend.services.model import key_store

    calls = _stub_remote(monkeypatch)
    pid = _pid("conn_p")
    _provider(pid)
    default = _key_with_file(pid, "conn_default", "sk-CONN1111")
    alt = _key_with_file(pid, "conn_alt", "sk-CONN2222")

    res = asyncio.run(key_store.test_provider(pid, key_id=alt["id"]))
    assert res == {"ok": True, "status": "ok", "count": 2,
                   "message": "连接正常，取到 2 个模型"}
    assert calls[-1]["api_key"] == "sk-CONN2222"      # 指定哪把就用哪把
    assert calls[-1]["base_url"] == "https://api.example.com/v1"
    assert calls[-1]["allow_private"] is True         # 库里的地址按 stored_config 放行内网

    asyncio.run(key_store.test_provider(pid))          # 不指定 → 默认密钥
    assert calls[-1]["api_key"] == "sk-CONN1111"
    # 结论写回**提供方**的 status（提供方级测试不替某一把密钥背书）
    from backend.services.model import provider_store

    row = next(p for p in provider_store.list_providers() if p["id"] == pid)
    assert row["status"] == "ok"
    other_pid = _pid("conn_other")
    _provider(other_pid)
    with pytest.raises(ValidationError):
        # 跨家借密钥：拒绝（拿 A 家的 key 打 B 家的地址）
        asyncio.run(key_store.test_provider(other_pid, key_id=default["id"]))


def test_key_test_writes_back_status(sandbox, secrets, monkeypatch):
    from backend.services.model import key_store

    calls = _stub_remote(monkeypatch)
    pid = _pid("keytest_p")
    _provider(pid)
    key = _key_with_file(pid, "keytest", "sk-KEY9999")
    res = asyncio.run(key_store.test_key(key["id"]))
    assert res["ok"] is True and res["count"] == 2
    assert calls[-1]["api_key"] == "sk-KEY9999"
    row = key_store.get_key(key["id"])
    assert row["status"] == "ok" and row["last_tested_at"]


# --------------------------------------------------------------------------
# 迁移
# --------------------------------------------------------------------------


def test_ensure_keys_is_idempotent(sandbox, secrets):
    from backend.services.model import key_store

    key_store.ensure_keys()
    again = key_store.ensure_keys()
    assert again["created"] == 0


def test_ensure_keys_marks_unknown_files_unassigned_and_never_guesses(sandbox, secrets):
    from backend.services.model import key_store

    key_store.write_key_file(f"__test_orphan_{U}.key", "sk-ORPHAN000")
    key_store.ensure_keys()
    rows = [r for r in key_store.list_keys(unassigned=True)
            if r["label"] == f"__test_orphan_{U}.key"]
    assert len(rows) == 1
    row = rows[0]
    assert row["unassigned"] is True and row["provider_id"] == ""
    assert row["status"] == "unverified"
    assert row["masked"] == "••••N000"  # "sk-ORPHAN000" 的末 4 位

    # 认领到某家（幂等）：不抢那家已有的默认
    pid = _pid("claim_p")
    _provider(pid)
    default = _key_with_file(pid, "claim_default", "sk-CLAIM111")
    claimed = key_store.assign_key(row["id"], pid)
    assert claimed["provider_id"] == pid and claimed["is_default"] is False
    assert key_store.get_key(default["id"])["is_default"] is True
    assert key_store.assign_key(row["id"], pid)["provider_id"] == pid


def test_ensure_keys_creates_provider_default_from_provider_secret_ref(sandbox, secrets):
    """规则 1：提供方自己记着 secret_ref、且这家还没有密钥行 → 建一条默认密钥。"""
    from backend.services.model import key_store, provider_store

    pid = _pid("ref_p")
    provider_store.upsert({"id": pid, "label": pid, "secret_ref": "legacy_ref.key"})
    key_store.write_key_file("legacy_ref.key", "sk-REF2222")
    key_store.ensure_keys()
    rows = key_store.list_keys(pid)
    assert len(rows) == 1
    assert rows[0]["label"] == "默认密钥" and rows[0]["is_default"] is True
    assert rows[0]["status"] == "unverified"


# --------------------------------------------------------------------------
# 接口层
# --------------------------------------------------------------------------


@pytest.fixture
def client(db_ready):
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


def test_models_api_exposes_key_source_and_hides_secret(sandbox, secrets, client):
    from backend.services.model import key_store

    pid, mid = _pid("api_p"), _pid("api_m")
    _provider(pid)
    key = _key_with_file(pid, "api", "sk-API3333")
    _model(mid, pid, key_mode="key", provider_key_id=key["id"])

    items = client.get("/api/v1/models").json()["data"]["items"]
    row = next(i for i in items if i["id"] == mid)
    assert row["key_mode"] == "key" and row["provider_key_id"] == key["id"]
    assert row["key_source"] == {"mode": "key", "key_id": key["id"],
                                 "key_label": key["label"], "status": "ok"}

    # 密钥文件没了 → 前端能标红成 invalid
    (secrets / _ref_of(key["id"])).unlink()
    items = client.get("/api/v1/models").json()["data"]["items"]
    row = next(i for i in items if i["id"] == mid)
    assert row["key_source"]["status"] == "invalid"

    data = client.get("/api/v1/models/providers").json()["data"]
    entry = next(p for p in data["providers"] if p["id"] == pid)
    assert entry["key_count"] == 1
    assert entry["default_key_id"] == key["id"]
    assert entry["has_base_url"] is True and entry["base_url"].startswith("https://")
    blob = repr(data) + repr(items)
    assert "sk-API3333" not in blob           # 明文永不出现在响应里
    assert "secret_ref" not in repr(data)     # 提供方目录里也没有凭据文件名
    assert "secret_ref" not in repr(row["key_source"])


def test_keys_api_roundtrip(sandbox, secrets, client):
    from backend.services.model import key_store

    pid = _pid("api_keys_p")
    _provider(pid)
    created = client.post(f"/api/v1/models/providers/{pid}/keys",
                          json={"label": "__test_http", "api_key": "sk-HTTP4444"}).json()["data"]
    assert created["masked"] == "••••4444" and created["is_default"] is True

    got = client.get(f"/api/v1/models/providers/{pid}/keys").json()["data"]
    assert [k["id"] for k in got["items"]] == [created["id"]]
    assert "unassigned" in got

    unassigned = client.get("/api/v1/models/keys", params={"unassigned": 1}).json()["data"]
    assert isinstance(unassigned["items"], list) and unassigned["unassigned"] == len(unassigned["items"])

    upd = client.put(f"/api/v1/models/keys/{created['id']}",
                     json={"label": "__test_http2", "status": "ok"}).json()["data"]
    assert upd["label"] == "__test_http2" and upd["status"] == "ok"

    assert client.delete(f"/api/v1/models/keys/{created['id']}").json()["data"]["deleted"] is True
    assert key_store.list_keys(pid) == []
