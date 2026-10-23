"""检索耗时流水（retrieval_log）：落库 → 聚合接口 → 保留期 → 幂等建索引。

需要 MySQL（不可用时整文件 skip），口径与 tests/test_knowledge_trend.py 一致。

**数据隔离**：`retrieval_log` 是**遥测流水**（90 天自动清，删掉不影响任何业务数据），
所以这里整表借用：模块开始把现有行读进内存、清空，用例结束原样写回 ——
开发机上跑一次测试不会弄丢自己的耗时历史，同时每个用例都能看到"确切的"行数
（否则实到 4 条、接口报 5 条，断言只能放宽成"大约"，等于没测）。

钉住的约定：

- `GET /knowledge/index/latency`：`series` **只含有检索的日子**（不补 0），日期升序；
  `count` 是当天行数，`avg/p50/p95` 都是 total_ms 的口径，且 p50 ≤ p95、avg 落在 min..max；
- 全窗口没有数据：`series = []`、`totals.count = 0`、avg/p50/p95 为 `None`（不是 0）；
- `totals` 是**整窗口一次算**（count / avg_ms / p50_ms / p95_ms），不是各天数值的平均；
- 保留期 90 天：`prune_logs(force=True)` 删掉 100 天前的行，10 天前的行留下；
- `ensure_indexes()` 重复执行不报错（第二次全部走"已存在"分支、返回空列表）。

⚠️ **本机 8710 上的后端一直在跑**，它每做一次检索就往这张表写一行（而且都是**今天**）。
所以除空窗口用例外，断言一律锚在**历史上的某一天** —— 没有任何进程会往过去补数据；
空窗口用例在检测到并发写入时 skip（而不是误报失败）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backend.repositories.mysql.session import healthcheck

pytestmark = pytest.mark.e2e

API = "/api/v1"

#: 快照 / 还原时搬的列（显式列出来：加列时这里会报错，而不是悄悄漏掉一列）
_LOG_COLUMNS = ("id", "ts", "mode", "scope", "total_ms", "vector_ms", "lexical_ms",
                "fused_ms", "rerank_ms", "hits", "top_k", "rerank_skipped")


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


def _snapshot() -> list[dict]:
    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        return [{column: getattr(row, column) for column in _LOG_COLUMNS}
                for row in sess.query(RetrievalLog).all()]


def _wipe() -> None:
    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        sess.query(RetrievalLog).delete(synchronize_session=False)


def _restore(rows: list[dict]) -> None:
    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope

    if not rows:
        return
    with session_scope() as sess:
        sess.add_all([RetrievalLog(**row) for row in rows])


@pytest.fixture(autouse=True)
def isolated_log(db_ready):
    """整表借用 + 原样还回去（见文件头「数据隔离」）。"""
    saved = _snapshot()
    _wipe()
    yield
    _wipe()
    _restore(saved)


def _add(rows: list[tuple[datetime, float]], *, mode: str = "similar",
         scope: str = "all", top_k: int = 6, rerank_skipped: bool = False) -> None:
    """按 (ts, total_ms) 造流水行；各阶段耗时只是按比例填，用例只断言 total_ms 口径。"""
    from backend.core.utils import new_id
    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        for ts, total_ms in rows:
            sess.add(RetrievalLog(
                id=new_id("rl"), ts=ts, mode=mode, scope=scope,
                total_ms=total_ms, vector_ms=total_ms * 0.5, lexical_ms=total_ms * 0.2,
                fused_ms=1.0, rerank_ms=total_ms * 0.2,
                hits=6, top_k=top_k, rerank_skipped=rerank_skipped))


def _today_utc() -> datetime:
    """今天这一刻的 UTC naive（全库约定的时间口径）。"""
    from backend.core.utils import now

    return now()


def _day_of(ts: datetime) -> str:
    """UTC naive → **本地**日期串（与接口分桶同一口径：库里存 UTC，柱子上是本地那天）。"""
    from datetime import timezone

    return ts.replace(tzinfo=timezone.utc).astimezone().date().isoformat()


def _window_ms(days: int) -> list[float]:
    """直接读库、按接口**同一窗口口径**取窗口内所有 total_ms（只读，用来独立核对边界）。

    不是拿接口的结果反推：p50 落在"真实行的 [min, max]"之间这件事，得有一份自己查出来的
    数据当基准。窗口左端算法与接口逐字一致（本地 start 日 00:00 → UTC naive）。
    """
    from datetime import time as dtime, timezone

    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope

    start = date.today() - timedelta(days=days - 1)
    lower = datetime.combine(start, dtime.min).astimezone(timezone.utc).replace(tzinfo=None)
    with session_scope() as sess:
        rows = (sess.query(RetrievalLog.total_ms)
                .filter(RetrievalLog.ts >= lower).all())
    return [float(value or 0.0) for (value,) in rows]


# --------------------------------------------------------------------------
# 1) 落库后接口的 count / avg / p50 / p95
# --------------------------------------------------------------------------


def test_latency_percentiles_and_average(client):
    """4 条流水 → 那一天 count=4、avg=25、p50=20、p95=40（nearest-rank 的确切取值）。

    数据造在 **20 天前**：窗口右端是"今天"，而本机后端一直在跑，今天随时会多出别人的
    检索行 —— 只有历史某一天是完全确定的（见文件头 ⚠️）。
    """
    values = [10.0, 20.0, 30.0, 40.0]
    past = (_today_utc() - timedelta(days=20)).replace(microsecond=0)
    _add([(past, ms) for ms in values])

    res = client.get(f"{API}/knowledge/index/latency", params={"days": 30})
    assert res.status_code == 200, res.text
    data = res.json()["data"]

    assert data["days"] == 30
    assert isinstance(data["generated_at"], str) and data["generated_at"]
    assert "nearest-rank" in data["note"]          # 口径写在出参里，不是只写在注释里

    dates = [row["date"] for row in data["series"]]
    assert dates == sorted(dates)                  # 日期升序
    point = {row["date"]: row for row in data["series"]}[_day_of(past)]
    assert point["count"] == len(values)
    assert point["avg_ms"] == 25.0
    assert point["p50_ms"] == 20.0                 # 升序第 ceil(0.5*4)=2 项
    assert point["p95_ms"] == 40.0                 # 升序第 4 项
    # 点上的不变量：p50 ≤ p95，且 avg 落在 min..max 之间
    assert point["p50_ms"] <= point["p95_ms"]
    assert min(values) <= point["avg_ms"] <= max(values)

    # totals 的字段就是出参契约里的四个（count / avg_ms / p50_ms / p95_ms），
    # 且是**整窗口一次算**：count 等于各天 count 之和，avg 是 count 加权平均
    # （所以必然落在各天 avg 的 [min, max] 之间）—— 这两条与并发写入无关，恒成立。
    totals = data["totals"]
    assert set(totals) == {"count", "avg_ms", "p50_ms", "p95_ms"}
    assert totals["count"] == sum(row["count"] for row in data["series"])
    assert totals["count"] >= len(values)
    day_avgs = [row["avg_ms"] for row in data["series"]]
    assert min(day_avgs) <= totals["avg_ms"] <= max(day_avgs)

    # totals.p50_ms 必须存在、且落在窗口内**真实行**的 [min, max] 之间
    # （边界从库里自己读一遍：不是拿接口的结果反推，才算独立核对）
    assert totals["p50_ms"] is not None
    assert totals["p50_ms"] <= totals["p95_ms"]
    window = _window_ms(30)
    if window:
        assert min(window) <= totals["p50_ms"] <= max(window)


def test_latency_series_is_sorted_and_skips_empty_days(client):
    """20 天前 / 22 天前各一批 → 两天都出现、日期升序；中间 21 天前那天**不出现**（不补 0）。

    同样锚在历史日：今天的柱子随时会被本机后端的检索改掉，而"过去的两天"只有这里有数据。
    """
    base = (_today_utc() - timedelta(days=20)).replace(microsecond=0)
    _add([(base, 100.0), (base, 300.0), (base - timedelta(days=2), 50.0)])

    data = client.get(f"{API}/knowledge/index/latency", params={"days": 30}).json()["data"]
    dates = [row["date"] for row in data["series"]]
    assert dates == sorted(dates)                              # 日期升序
    assert _day_of(base) in dates
    assert _day_of(base - timedelta(days=2)) in dates
    assert _day_of(base - timedelta(days=1)) not in dates       # 中间那天没检索 → 不补 0

    row = {item["date"]: item for item in data["series"]}[_day_of(base)]
    assert row["count"] == 2
    assert row["p50_ms"] <= row["p95_ms"]
    assert min(100.0, 300.0) <= row["avg_ms"] <= max(100.0, 300.0)
    assert data["totals"]["count"] >= 3


# --------------------------------------------------------------------------
# 2) 空窗口 → series [] 且 totals.count 0
# --------------------------------------------------------------------------


def test_empty_window_returns_empty_series(client):
    """一条流水都没有（autouse 已清表）：series=[]、count=0、avg/p50/p95 是 None 不是 0。

    本机后端在跑时它可能刚好在窗口里写进一行（"空窗口"这一轮就不成立了）—— 这时
    **skip 而不是断言失败**：那不是代码错，是测不到。检测方式是与库里的真实行数比对
    （接口先查、库后查，只会多不会少）。
    """
    data = client.get(f"{API}/knowledge/index/latency", params={"days": 30}).json()["data"]
    if _window_ms(30):
        pytest.skip("窗口内出现了并发写入的检索流水（本机 8710 后端在跑），本轮测不到空窗口")

    assert data["series"] == []
    assert data["totals"]["count"] == 0
    assert data["totals"]["avg_ms"] is None
    assert data["totals"]["p50_ms"] is None          # 与 avg / p95 一致：没有数据就没有分位数
    assert data["totals"]["p95_ms"] is None


def test_latency_days_is_clamped(client):
    assert client.get(f"{API}/knowledge/index/latency",
                      params={"days": 0}).json()["data"]["days"] == 1
    assert client.get(f"{API}/knowledge/index/latency",
                      params={"days": 900}).json()["data"]["days"] == 180
    assert client.get(f"{API}/knowledge/index/latency").json()["data"]["days"] == 30  # 默认


# --------------------------------------------------------------------------
# 3) 保留期：删 100 天前，留 10 天前
# --------------------------------------------------------------------------


def test_prune_deletes_only_expired_rows(db_ready):
    from backend.repositories.mysql.models import RetrievalLog
    from backend.repositories.mysql.session import session_scope
    from backend.services.knowledge import retrieval

    # 抹掉微秒：MySQL 的 DATETIME 只到秒（小数会四舍五入，差一秒断言就不准）
    base = _today_utc().replace(microsecond=0)
    old_ts = base - timedelta(days=100)
    keep_ts = base - timedelta(days=10)
    _add([(old_ts, 12.0), (keep_ts, 34.0)])

    assert retrieval.RETENTION_DAYS == 90
    assert retrieval.prune_logs(force=True) == 1          # 只删超期的那一条

    with session_scope() as sess:
        left = {row.ts for row in sess.query(RetrievalLog).all()}
    assert old_ts not in left
    assert keep_ts in left


# --------------------------------------------------------------------------
# 4) 幂等建索引
# --------------------------------------------------------------------------


def test_ensure_indexes_is_idempotent(db_ready):
    """重复执行不报错；第二次全部走"已存在"分支（MySQL 1061 / SQLite already exists）。"""
    from sqlalchemy import inspect

    from backend.repositories.mysql.session import ensure_indexes, get_engine

    ensure_indexes()                       # 首建或已存在，都不许抛
    assert ensure_indexes() == []          # 第二次：一个都没新建 → 幂等

    names = set()
    engine = get_engine()
    for table in ("paper", "chunk"):
        names |= {index["name"] for index in inspect(engine).get_indexes(table)}
    assert {"ix_paper_created_at", "ix_chunk_created_at"} <= names
