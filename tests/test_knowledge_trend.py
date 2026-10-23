"""索引管理重做补的后端：趋势 / chunk 搜索与分页 / settings 默认值 / 概览的索引时间。

需要 MySQL（不可用时整文件 skip），口径与 tests/test_trash.py 一致。

钉住的约定：

- GET /knowledge/index/trend：正好 days 条、日期连续不缺天、cum_* 单调不减，
  最后一天的累计等于同口径的 paper / chunk 总数（deleted_at IS NULL 的论文才计）；
- GET /knowledge/chunks：q 大小写不敏感地命中 id 或 content_preview，total 是
  **过滤后、分页前**的条数，count 仍是本次返回的条数（老调用方语义不变）；
- GET /settings：values 是当前生效值，defaults 是**代码默认**（retrieval.DEFAULT_K）；
  两者口径不同 —— 存过的值只影响 values，不影响 defaults（否则「恢复默认」是空操作）；
- GET /knowledge/overview：items[*].indexed_at = 该篇现存 chunk 的最晚 created_at
  （本地 ISO 串，无 chunk 为 None）—— 论文表格的「最近索引时间」。

数据隔离：论文 / chunk 用 __test_index_trend_* 伪 id，用例前后整批删掉。
趋势是**全库**口径，所以断言"与同口径计数相等"，不写死条数。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

API = "/api/v1"
P_TODAY = "__test_index_trend_p1__"          # 今天创建
P_OLD = "__test_index_trend_p2__"            # 3 天前创建
P_DELETED = "__test_index_trend_deleted__"   # 今天创建但已软删（不该进图）
PAPER_IDS = (P_TODAY, P_OLD, P_DELETED)

# (paper, 最后一段 tag, preview, order_global)：q=alpha 命中 3 条
# （一 preview 小写、一 preview 大写、一只在 id 里）—— id 与 preview 两条路都覆盖到。
P_TODAY_CHUNKS = (
    ("c0", "Gamma 引入：与关键词无关的一段", 0),
    ("c1", "Alpha vector quantization 的概念", 1),
    ("alpha-in-final-segment", "与检索无关的预览", 2),
    ("c3", "ALPHA 大写也命中", 3),
)


@pytest.fixture(scope="module")
def db_ready():
    if not healthcheck().get("ok"):
        pytest.skip("MySQL 不可用（设置 PR_DB_BACKEND=sqlite 或启动 MySQL）")
    from backend.repositories.mysql.session import init_db

    init_db()
    return True


@pytest.fixture
def client(db_ready):
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


def _wipe(ids: tuple[str, ...] = PAPER_IDS) -> None:
    from backend.repositories.mysql.models import Chunk, Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        sess.query(Chunk).filter(Chunk.paper_id.in_(ids)).delete(synchronize_session=False)
        sess.query(Paper).filter(Paper.id.in_(ids)).delete(synchronize_session=False)


@pytest.fixture
def seeded(db_ready):
    """两篇活论文（今天 / 3 天前）+ 一篇软删论文 + 4 条今天的 chunk + 1 条 3 天前的 chunk。"""
    from backend.repositories.mysql.models import Chunk, Paper
    from backend.repositories.mysql.session import session_scope

    _wipe()
    today = datetime.now()
    with session_scope() as sess:
        sess.add(Paper(id=P_TODAY, title="趋势测试件（今天）", status="parsed",
                       in_knowledge_base=True, created_at=today))
        sess.add(Paper(id=P_OLD, title="趋势测试件（3 天前）", status="parsed",
                       created_at=today - timedelta(days=3)))
        sess.add(Paper(id=P_DELETED, title="趋势测试件（已删）", status="parsed",
                       created_at=today, deleted_at=today))
        for tag, preview, order in P_TODAY_CHUNKS:
            sess.add(Chunk(id=f"{P_TODAY}:text:{tag}", paper_id=P_TODAY, chunk_type="text",
                           page_no=1, order_global=order, content_preview=preview,
                           metadata_json='{"lang": "en", "road": "a"}', created_at=today))
        sess.add(Chunk(id=f"{P_OLD}:text:c0", paper_id=P_OLD, chunk_type="text",
                       page_no=1, order_global=0, content_preview="老 chunk",
                       metadata_json='{"lang": "en", "road": "a"}',
                       created_at=today - timedelta(days=3)))
    yield PAPER_IDS
    _wipe()


def _totals() -> tuple[int, int]:
    """现存的 paper / chunk 总数（与 overview() 的 papers_total / chunks 同一条查询）。"""
    from sqlalchemy import func

    from backend.repositories.mysql.models import Chunk, Paper
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        papers = int(sess.query(func.count(Paper.id))
                     .filter(Paper.deleted_at.is_(None)).scalar() or 0)
        chunks = int(sess.query(func.count(Chunk.id)).scalar() or 0)
    return papers, chunks


def test_trend_has_exactly_days_continuous_points(client, seeded):
    res = client.get(f"{API}/knowledge/index/trend", params={"days": 7})
    assert res.status_code == 200
    data = res.json()["data"]

    assert data["days"] == 7
    series = data["series"]
    assert len(series) == 7

    today = date.today()
    start = today - timedelta(days=6)
    assert [row["date"] for row in series] == [
        (start + timedelta(days=i)).isoformat() for i in range(7)]
    assert series[-1]["date"] == today.isoformat()   # 右端 = 服务器本地"今天"
    assert isinstance(data["generated_at"], str) and data["generated_at"]
    assert "现存记录" in data["note"] or "重建索引" in data["note"]


def test_trend_cum_is_monotonic_and_ends_at_totals(client, seeded):
    data = client.get(f"{API}/knowledge/index/trend", params={"days": 30}).json()["data"]
    series = data["series"]

    for prev, cur in zip(series, series[1:]):
        assert cur["cum_papers"] >= prev["cum_papers"]
        assert cur["cum_chunks"] >= prev["cum_chunks"]

    papers_total, chunks_total = _totals()
    assert series[-1]["cum_papers"] == papers_total
    assert series[-1]["cum_chunks"] == chunks_total

    # 造的三条记录必须真的出现在桶里：今天 +1 篇（软删那篇不算）、3 天前 +1 篇
    by_date = {row["date"]: row for row in series}
    assert by_date[date.today().isoformat()]["papers"] >= 1
    assert by_date[(date.today() - timedelta(days=3)).isoformat()]["papers"] >= 1


def test_trend_days_is_clamped(client, seeded):
    assert client.get(f"{API}/knowledge/index/trend", params={"days": 0}).json()["data"]["days"] == 1
    data = client.get(f"{API}/knowledge/index/trend", params={"days": 900}).json()["data"]
    assert data["days"] == 180 and len(data["series"]) == 180


def test_chunks_q_is_case_insensitive_substring(client, seeded):
    def ids(params):
        body = client.get(f"{API}/knowledge/chunks",
                          params={"paper_id": P_TODAY, "limit": 50, **params}).json()["data"]
        return [it["id"] for it in body["items"]], body

    lower, body = ids({"q": "alpha"})
    assert body["total"] == 3 and body["count"] == 3
    assert f"{P_TODAY}:text:c1" in lower                 # preview 命中（小写查询命中 ALPHA 之外的两条）
    assert f"{P_TODAY}:text:alpha-in-final-segment" in lower  # id 命中
    assert f"{P_TODAY}:text:c3" in lower                 # preview 大写也命中

    upper, body_upper = ids({"q": "ALPHA"})              # 查询本身大写也一致
    assert body_upper["total"] == 3
    assert sorted(upper) == sorted(lower)

    assert ids({"q": "nomatch-xyz"})[1]["total"] == 0


def test_chunks_offset_and_total(client, seeded):
    first = client.get(f"{API}/knowledge/chunks",
                       params={"paper_id": P_TODAY, "limit": 2}).json()["data"]
    assert first["total"] == 4          # 过滤后、分页前
    assert first["count"] == 2          # 本次返回条数（语义不变）
    assert [it["id"] for it in first["items"]] == [f"{P_TODAY}:text:c0",
                                                   f"{P_TODAY}:text:c1"]

    second = client.get(f"{API}/knowledge/chunks",
                        params={"paper_id": P_TODAY, "limit": 2, "offset": 2}).json()["data"]
    assert second["total"] == 4 and second["count"] == 2
    assert [it["id"] for it in second["items"]] == [
        f"{P_TODAY}:text:alpha-in-final-segment", f"{P_TODAY}:text:c3"]

    # offset 与 q 一起用：total 是过滤后的总数，不是剩下的
    filtered = client.get(f"{API}/knowledge/chunks",
                          params={"paper_id": P_TODAY, "q": "alpha", "limit": 10,
                                  "offset": 2}).json()["data"]
    assert filtered["total"] == 3 and filtered["count"] == 1

    empty = client.get(f"{API}/knowledge/chunks",
                       params={"paper_id": P_TODAY, "offset": 99}).json()["data"]
    assert empty["total"] == 4 and empty["count"] == 0 and empty["items"] == []


def test_settings_defaults_cover_rag_keys(client):
    from backend.services.knowledge import retrieval

    data = client.get(f"{API}/settings").json()["data"]
    defaults = data["defaults"]

    # 接口不抄默认数字：这四个键必须等于代码默认 retrieval.DEFAULT_K
    for key, name in (("rag_top_k_vector", "vector"), ("rag_top_k_lexical", "lexical"),
                      ("rag_top_k_fused", "fused"), ("rag_top_n_rerank", "rerank")):
        assert key in defaults, key
        assert defaults[key] == retrieval.DEFAULT_K[name]
        assert isinstance(defaults[key], int)

    assert "agent_style" in defaults
    assert isinstance(defaults["allow_general_knowledge"], bool)


def _raw_setting(key: str) -> str | None:
    """直接读 setting 表里的那一行（不经过 rag_k 的回退链），用来还原。"""
    from backend.repositories.mysql.models import Setting
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Setting, key)
        return None if row is None else row.value


def _restore_setting(key: str, value: str | None) -> None:
    from backend.repositories.mysql.models import Setting
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(Setting, key)
        if value is None:
            if row is not None:
                sess.delete(row)
            return
        if row is None:
            row = Setting(key=key)
            sess.add(row)
        row.value = value


def test_settings_defaults_are_code_defaults_not_live(client):
    """回归锁：defaults 取**代码默认**，不是当前生效值。

    用户把 20 改成 30 之后，defaults 若跟着变成 30，「恢复默认」点下去只是把表单填回
    它现在的值 —— 一个空操作。values 该跟着变，defaults 不该变。
    """
    from backend.services.knowledge import retrieval

    key = "rag_top_k_fused"
    live = retrieval.DEFAULT_K["fused"] + 7          # 一定与代码默认不同
    before = _raw_setting(key)
    try:
        res = client.put(f"{API}/settings", json={key: live})
        assert res.status_code == 200, res.text

        data = client.get(f"{API}/settings").json()["data"]
        assert data["values"][key] == live                    # 当前生效值 = 改后的
        assert retrieval.rag_k("fused") == live               # 读取点也确实用了新值
        assert data["defaults"][key] == retrieval.DEFAULT_K["fused"]   # 默认值不动
        assert data["defaults"][key] != data["values"][key]
    finally:
        _restore_setting(key, before)

    # 还原成功（没存过的仍是没存过）
    assert _raw_setting(key) == before

    # 再钉一次**没存过**那条路径：values 这时必须回落到 DEFAULT_K（_live_default 不变）
    _restore_setting(key, None)
    try:
        data = client.get(f"{API}/settings").json()["data"]
        assert data["values"][key] == retrieval.DEFAULT_K["fused"]
        assert data["defaults"][key] == retrieval.DEFAULT_K["fused"]
    finally:
        _restore_setting(key, before)


P_INDEXED = "__test_index_trend_indexed__"       # 有 chunk（两条，时间不同）
P_NO_CHUNKS = "__test_index_trend_nochunks__"    # 没有 chunk
INDEX_IDS = (P_INDEXED, P_NO_CHUNKS)


def _overview_items_by_id(client) -> dict:
    body = client.get(f"{API}/knowledge/overview").json()["data"]
    return {it["id"]: it for it in body["items"]}


def test_overview_items_carry_indexed_at(client, db_ready):
    """items[*].indexed_at = 该篇**现存 chunk 的最晚 created_at**；没有 chunk 为 None。

    这是前端「最近索引时间」的唯一数据源：重建索引会重写 chunk 行，所以它等价于
    上次建/更新索引的时刻。格式是本地 ISO 串（与 trend 的 generated_at 同一种写法）。
    """
    from backend.core.utils import now as utc_now
    from backend.repositories.mysql.models import Chunk, Paper
    from backend.repositories.mysql.session import session_scope

    # 时间用全库约定造（UTC naive，同 core.utils.now）：库里存 UTC、出参是本地墙上时间，
    # 所以期望值可以精确算出来，而不是"非空就行"。
    # 抹掉微秒：MySQL 的 DATETIME 只到秒（小数部分会四舍五入，差一秒就断言不准）
    older = (utc_now() - timedelta(days=5)).replace(microsecond=0)
    newer = older + timedelta(days=1)
    _wipe(INDEX_IDS)
    with session_scope() as sess:
        sess.add(Paper(id=P_INDEXED, title="索引时间（有 chunk）", status="parsed",
                       in_knowledge_base=True, created_at=newer))
        sess.add(Paper(id=P_NO_CHUNKS, title="索引时间（无 chunk）", status="parsed",
                       created_at=newer))
        sess.add(Chunk(id=f"{P_INDEXED}:text:c0", paper_id=P_INDEXED, chunk_type="text",
                       page_no=1, order_global=0, content_preview="较早的 chunk",
                       created_at=older))
        sess.add(Chunk(id=f"{P_INDEXED}:text:c1", paper_id=P_INDEXED, chunk_type="text",
                       page_no=1, order_global=1, content_preview="较晚的 chunk",
                       created_at=newer))
    try:
        items = _overview_items_by_id(client)

        indexed = items[P_INDEXED]["indexed_at"]
        assert isinstance(indexed, str) and indexed
        assert len(indexed) == 19                     # YYYY-MM-DDTHH:MM:SS，无时区后缀
        datetime.fromisoformat(indexed)               # 标准库解析得动

        # 取的是**最晚**那条 chunk，且是本地墙上时间（库里是 UTC naive）
        def local(dt):
            return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

        assert indexed == local(newer).isoformat(timespec="seconds")
        assert indexed != local(older).isoformat(timespec="seconds")

        # 没有 chunk 的论文：None（不是空串，也不是论文自己的时间）
        assert items[P_NO_CHUNKS]["indexed_at"] is None
    finally:
        _wipe(INDEX_IDS)
