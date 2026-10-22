"""提供方目录排序（用户口径：DeepSeek → Claude → ChatGPT → Gemini → 其他 → 自定义）。

钉住的约定：默认回填只对「从没排过」的库生效且幂等；排序接口按数组下标写 0..n-1、
没提到的补 1000+i 且保持相对顺序、未知 id 400 并点名；GET 按 sort 升序返回，
自定义入口仍是单独的 custom 键（排在最后）。

数据隔离：每个用例结束都把 sort 全归 0 再让 ensure_providers 重跑默认回填，
所以线上原有的顺序不会被测试改掉（重复跑这个文件结果也稳定）。
"""

from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.repositories.mysql.session import healthcheck

SORT_DEFAULTS = {"deepseek", "anthropic", "openai", "gemini"}


def _reset_sort():
    """sort 全归 0 → ensure_providers 重跑默认回填（幂等的"恢复出厂顺序"）。"""
    from backend.repositories.mysql.models import ModelProvider
    from backend.repositories.mysql.session import session_scope
    from backend.services.model import provider_store

    with session_scope() as s:
        for row in s.query(ModelProvider).all():
            row.sort = 0
    provider_store.ensure_providers()
    return [(p["id"], p["sort"]) for p in provider_store.list_providers()]


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()      # 顺带补 model_provider.sort 这一列
    from backend.services.model import provider_store

    provider_store.ensure_providers()
    return True


@pytest.fixture
def default_order(db_ready):
    _reset_sort()
    yield
    _reset_sort()


@pytest.fixture
def client(db_ready):
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


def test_default_backfill_matches_user_order(default_order):
    """默认顺序：DeepSeek(0) → Claude(1) → ChatGPT/OpenAI(2) → Gemini(3) → 其他(10,11…)。"""
    from backend.services.model import provider_store, providers_catalog

    by_id = {p["id"]: p["sort"] for p in provider_store.list_providers()}
    assert by_id["deepseek"] == 0
    assert by_id["anthropic"] == 1
    assert by_id["openai"] == 2
    assert by_id["gemini"] == 3

    official = providers_catalog.official_ids()
    others = sorted(pid for pid in by_id if pid in official and pid not in SORT_DEFAULTS)
    assert others, "官方目录里应该还有别的家"
    assert [by_id[pid] for pid in others] == list(range(10, 10 + len(others)))
    # 自定义行（用户自己建的 / custom）永远排在官方之后
    for pid, value in by_id.items():
        if pid not in official:
            assert value >= 1000, pid


def test_default_backfill_is_idempotent(default_order):
    from backend.services.model import provider_store

    before = [(p["id"], p["sort"]) for p in provider_store.list_providers()]
    assert provider_store.ensure_providers() == 0
    assert [(p["id"], p["sort"]) for p in provider_store.list_providers()] == before


def test_set_order_rejects_unknown_id(default_order):
    from backend.services.model import provider_store

    with pytest.raises(ValidationError) as err:
        provider_store.set_order(["deepseek", "no-such-provider"])
    assert "no-such-provider" in str(err.value.message)
    with pytest.raises(ValidationError):
        provider_store.set_order([])


def test_set_order_puts_unlisted_last_and_is_idempotent(default_order):
    from backend.services.model import provider_store

    rows = provider_store.set_order(["moonshot", "deepseek"])
    by_id = {r["id"]: r["sort"] for r in rows}
    assert by_id["moonshot"] == 0 and by_id["deepseek"] == 1
    rest = sorted(pid for pid in by_id if pid not in ("moonshot", "deepseek"))
    assert [by_id[pid] for pid in rest] == list(range(1000, 1000 + len(rest)))

    pairs = [(r["id"], r["sort"]) for r in rows]
    assert ([(r["id"], r["sort"]) for r in provider_store.set_order(
        ["moonshot", "deepseek"])] == pairs)                             # 幂等
    # 未列出的保持相对顺序（同段内按 id 稳定）
    assert [r["id"] for r in rows][:2] == ["moonshot", "deepseek"]


def test_new_provider_is_appended_not_inserted_at_top(default_order):
    """新加的一家排在**最后**：sort=0 是"没排过"的默认值，给 0 会插队到第一位。"""
    from backend.repositories.mysql.models import ModelProvider, ModelProviderKey
    from backend.repositories.mysql.session import session_scope
    from backend.services.model import provider_store

    pid = "__test_new_ordered"
    try:
        before = {p["id"]: p["sort"] for p in provider_store.list_providers()}
        created = provider_store.upsert({"id": pid, "label": "新家"})
        assert created["sort"] == max(before.values()) + 1
        after = [p["id"] for p in provider_store.list_providers()]
        assert after[-1] == pid                  # 落在最后，不是插到第一位
        assert after[0] == "deepseek"
    finally:
        with session_scope() as s:
            s.query(ModelProviderKey).filter(
                ModelProviderKey.provider_id == pid).delete(synchronize_session=False)
            row = s.get(ModelProvider, pid)
            if row is not None:
                s.delete(row)


def test_get_providers_is_sorted_by_sort(default_order, client):
    data = client.get("/api/v1/models/providers").json()["data"]
    pairs = [(p["id"], p["sort"]) for p in data["providers"]]
    assert pairs == sorted(pairs, key=lambda x: (x[1], x[0]))
    assert dict(pairs)["deepseek"] == 0
    assert "custom" in data                     # 自定义入口仍是单独的键（排在最后）
    assert data["custom"]["kind"] == "custom"


def test_order_route_writes_index_and_names_unknown_id(default_order, client):
    res = client.put("/api/v1/models/providers/order",
                     json={"ids": ["gemini", "deepseek"]})
    assert res.status_code == 200
    items = res.json()["data"]["items"]
    assert [i["id"] for i in items][:2] == ["gemini", "deepseek"]
    assert [i["sort"] for i in items][:2] == [0, 1]
    assert all(i["sort"] >= 1000 for i in items[2:])

    again = client.put("/api/v1/models/providers/order",
                       json={"ids": ["gemini", "deepseek"]})
    assert ([(i["id"], i["sort"]) for i in again.json()["data"]["items"]]
            == [(i["id"], i["sort"]) for i in items])     # 幂等（updated_at 会变，不管它）

    bad = client.put("/api/v1/models/providers/order", json={"ids": ["nope-xyz"]})
    assert bad.status_code == 400
    assert "nope-xyz" in bad.text

    # 顺序真的落库了：GET 也按新顺序返回
    pairs = [(p["id"], p["sort"])
             for p in client.get("/api/v1/models/providers").json()["data"]["providers"]]
    assert pairs[0][0] == "gemini"
