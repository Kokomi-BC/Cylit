"""计费与用量统计（离线，临时 SQLite）：单价 / 缓存拆分 / 币种隔离 / 幂等 / 分桶。

为什么不用真实库：这两层是**纯计算 + 纯聚合**，验的是口径而不是连通性：

· 单价 → 金额：每百万 Token 的公式、缓存拆分、半配价格、厂商回传费用；
· 币种：人民币与美元各算各的，**永远不相加**（一个错账比缺一个功能严重得多）；
· 周期边界：日 / 周（周一起）/ 月，跨月、跨年、周日与周一那两天；
· 写入口径：幂等 request_id、小时桶落位、同名但**不同厂商**不合并、删配置后 gone: 仍在。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.errors import ValidationError
from backend.repositories.mysql import session as db_session
from backend.repositories.mysql.models import (
    Base, ModelConfig, ModelProvider, ModelUsage,
)
from backend.services.model import billing, usage_stats


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把全局 Engine / Session 换成临时 SQLite（不碰真实库）。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'billing.sqlite3').as_posix()}",
                           future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                           expire_on_commit=False)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", factory)
    try:
        yield engine
    finally:
        engine.dispose()


def _vendor(provider_id="deepseek", label="DeepSeek", currency="CNY") -> None:
    with db_session.session_scope() as sess:
        sess.add(ModelProvider(id=provider_id, kind="official", label=label,
                               currency=currency))


def _config(config_id="gen-1", *, name="主力生成模型", provider="deepseek",
            model_name="deepseek-chat", price_in=None, price_out=None,
            price_cache_read=None, price_cache_write=None,
            currency=None) -> None:
    with db_session.session_scope() as sess:
        sess.add(ModelConfig(
            id=config_id, name=name, provider=provider, model_type="generation",
            model_name=model_name, price_in=price_in, price_out=price_out,
            price_cache_read=price_cache_read, price_cache_write=price_cache_write,
            currency=currency,
        ))


def _prices(**kwargs) -> dict:
    """直接喂 price_of 形状的字典（省掉一次查库）。"""
    base = {"currency": "CNY", "price_in": None, "price_out": None,
            "price_cache_read": None, "price_cache_write": None,
            "priced": False, "source": "none"}
    base.update(kwargs)
    base["priced"] = base["price_in"] is not None and base["price_out"] is not None
    base["source"] = "model" if base["priced"] else "none"
    return base


# --------------------------------------------------------------------------
# 单价 → 金额
# --------------------------------------------------------------------------


def test_cost_input_and_output_only():
    """(1e6 输入, 5e5 输出) @ (2, 8) → 2.0 + 4.0 = 6.0。"""
    money = billing.cost_of(_prices(price_in=2, price_out=8), 1_000_000, 500_000)

    assert money == {"cost": 6.0, "currency": "CNY", "billing": "computed"}


def test_cost_splits_cache_read_when_cache_price_configured():
    """配了 cache_read 单价：缓存那部分按缓存价，其余输入按输入价。"""
    money = billing.cost_of(_prices(price_in=2, price_out=8, price_cache_read=0.5),
                            1_000_000, 100_000, cache_read=400_000)

    # billed_in = 1e6 - 4e5 = 6e5 → 1.2；输出 1e5*8/1e6 = 0.8；缓存 4e5*0.5/1e6 = 0.2
    assert money["cost"] == 2.2
    assert money["billing"] == "computed"


def test_cost_keeps_cache_as_normal_input_without_cache_price():
    """没配 cache_read 单价：不拆，缓存 token 按普通输入价算（宁可不少算）。"""
    money = billing.cost_of(_prices(price_in=2, price_out=8), 1_000_000, 100_000,
                            cache_read=400_000)

    # 整段 prompt 都按输入价：(1e6*2 + 1e5*8)/1e6 = 2.0 + 0.8
    assert money["cost"] == 2.8


def test_cost_cache_write_counted_only_when_priced():
    """写缓存只在配了 price_cache_write 时单独计价（多数厂商已含在 prompt 里）。"""
    without = billing.cost_of(_prices(price_in=2, price_out=8, price_cache_read=0.5),
                              1_000_000, 0, cache_read=0, cache_write=200_000)
    with_write = billing.cost_of(
        _prices(price_in=2, price_out=8, price_cache_read=0.5, price_cache_write=3),
        1_000_000, 0, cache_read=0, cache_write=200_000)

    assert without["cost"] == 2.0          # 2e6 的一半？不 —— 1e6 输入 * 2 / 1e6
    assert with_write["cost"] == 2.6       # 加上 2e5 * 3 / 1e6 = 0.6


def test_cost_is_none_when_only_input_price_is_configured():
    """只配输入、没配输出 = 整体未配置：不做半算，cost 为 NULL（绝不写 0）。"""
    money = billing.cost_of(_prices(price_in=2), 1_000_000, 500_000)

    assert money == {"cost": None, "currency": "CNY", "billing": "none"}


def test_cost_uses_reported_amount_verbatim():
    """厂商明确回传实际费用：按 reported 记，币种也以它为准（账单优先于估算）。"""
    money = billing.cost_of(_prices(price_in=2, price_out=8), 1_000, 1_000,
                            reported=0.0123456789, reported_currency="usd")

    assert money["billing"] == "reported"
    assert money["cost"] == 0.012346       # 6 位小数，不丢分位以下的量
    assert money["currency"] == "USD"


def test_cost_rounds_to_six_decimals():
    money = billing.cost_of(_prices(price_in=1, price_out=1), 3, 3)

    assert money["cost"] == 0.000006


def test_bad_price_and_currency_are_rejected():
    for bad in (-1, "-0.5", "abc", True):
        with pytest.raises(ValidationError):
            billing.clean_price(bad, "price_in")
    assert billing.clean_price(None) is None
    assert billing.clean_price("") is None
    assert billing.clean_price("2.5") == 2.5
    for bad in ("EUR", "rmb", 123):
        with pytest.raises(ValidationError):
            billing.normalize_currency(bad)
    assert billing.normalize_currency("") == ""
    assert billing.normalize_currency("usd") == "USD"


def test_write_path_rejects_bad_price_and_currency(temp_db):
    """写入口（model_manager.create）必须拒非法值 —— 不能靠前端拦。"""
    from backend.services.model.model_manager import model_manager

    with pytest.raises(ValidationError):
        model_manager.create({"name": "坏价", "provider": "deepseek",
                              "model_name": "x", "price_in": -3})
    with pytest.raises(ValidationError):
        model_manager.create({"name": "坏币种", "provider": "deepseek",
                              "model_name": "x", "currency": "EUR"})
    made = model_manager.create({"name": "好价", "provider": "deepseek",
                                 "model_name": "x", "price_in": "2", "price_out": 8,
                                 "currency": "usd"})
    assert made["price_in"] == 2.0 and made["price_out"] == 8.0
    assert made["currency"] == "USD"


# --------------------------------------------------------------------------
# 币种链
# --------------------------------------------------------------------------


def test_currency_chain_model_then_vendor_then_default(temp_db):
    _vendor("deepseek", currency="USD")
    _vendor("openai", currency="")           # 空 = 没定，落到默认
    _config("gen-1", provider="deepseek", price_in=1, price_out=2)
    _config("gen-2", provider="deepseek", price_in=1, price_out=2, currency="cny")
    _config("gen-3", provider="openai", price_in=1, price_out=2)

    assert billing.price_of("gen-1")["currency"] == "USD"   # 跟随厂商
    assert billing.price_of("gen-2")["currency"] == "CNY"   # 模型自己写的优先
    assert billing.price_of("gen-3")["currency"] == "CNY"   # 厂商空 → 默认
    assert billing.price_of("no-such")["priced"] is False


def test_prices_never_inherited_from_vendor(temp_db):
    """单价只认模型级：厂商有币种，但没有"厂商单价"这回事。"""
    _vendor("deepseek", currency="CNY")
    _config("gen-1", price_in=None, price_out=None)

    got = billing.price_of("gen-1")

    assert got["priced"] is False
    assert got["source"] == "none"
    assert got["currency"] == "CNY"


# --------------------------------------------------------------------------
# 周期边界
# --------------------------------------------------------------------------


def _local(*args) -> datetime:
    """本地墙钟时间（naive 在库里是 UTC，测试里直接用它表达"这一天这一刻"）。"""
    return datetime(*args, tzinfo=billing.LOCAL_TZ)


def test_period_bounds_day():
    start, end = billing.period_bounds("day", _local(2026, 10, 23, 15, 30))

    assert (start, end) == (datetime(2026, 10, 23), datetime(2026, 10, 24))


@pytest.mark.parametrize("given,expected_start", [
    (_local(2026, 10, 19, 0, 0), datetime(2026, 10, 19)),   # 周一自己
    (_local(2026, 10, 21, 12, 0), datetime(2026, 10, 19)),  # 周三 → 本周一
    (_local(2026, 10, 25, 23, 59), datetime(2026, 10, 19)),  # 周日 → 仍是上一个周一
    (_local(2026, 10, 26, 0, 0), datetime(2026, 10, 26)),   # 周一 0 点翻篇
])
def test_period_bounds_week_starts_monday(given, expected_start):
    start, end = billing.period_bounds("week", given)

    assert start == expected_start
    assert end - start == timedelta(days=7)


def test_period_bounds_month_crosses_year_and_short_months():
    start, end = billing.period_bounds("month", _local(2026, 12, 31, 23, 0))
    assert (start, end) == (datetime(2026, 12, 1), datetime(2027, 1, 1))

    start, end = billing.period_bounds("month", _local(2026, 2, 15))
    assert (start, end) == (datetime(2026, 2, 1), datetime(2026, 3, 1))

    start, end = billing.period_bounds("month", _local(2026, 1, 1, 0, 0))
    assert (start, end) == (datetime(2026, 1, 1), datetime(2026, 2, 1))


def test_period_bounds_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        billing.period_bounds("quarter")


# --------------------------------------------------------------------------
# 写入：幂等 / 金额按行存 / 小时键
# --------------------------------------------------------------------------


def test_record_stores_cost_from_model_prices(temp_db):
    _config("gen-1", price_in=2, price_out=8, currency="CNY")

    assert usage_stats.record(model_config_id="gen-1", model_name="deepseek-chat",
                              provider="deepseek", prompt_tokens=1_000_000,
                              completion_tokens=500_000) is True

    with db_session.session_scope() as sess:
        row = sess.query(ModelUsage).one()
        assert row.cost == 6.0
        assert row.currency == "CNY"
        assert row.billing == "computed"
        assert row.cache_read_tokens == 0
        assert row.hour_key == billing.hour_key(row.created_at)
        assert row.day == date.fromisoformat(billing.local_day(row.created_at))


def test_record_without_prices_leaves_cost_null(temp_db):
    _config("gen-1")

    usage_stats.record(model_config_id="gen-1", prompt_tokens=10, completion_tokens=5)

    with db_session.session_scope() as sess:
        row = sess.query(ModelUsage).one()
        assert row.cost is None              # 未计费 = NULL，不是 0
        assert row.billing == "none"


def test_record_is_idempotent_by_request_id(temp_db):
    _config("gen-1", price_in=1, price_out=1)

    first = usage_stats.record(model_config_id="gen-1", request_id="req-1",
                               prompt_tokens=10, completion_tokens=5)
    second = usage_stats.record(model_config_id="gen-1", request_id="req-1",
                                prompt_tokens=10, completion_tokens=5)

    assert (first, second) == (True, False)
    with db_session.session_scope() as sess:
        assert sess.query(ModelUsage).count() == 1


def test_record_dedupes_an_already_stored_request_id(temp_db):
    """预查询那条路径：库里已经有这个 request_id 就直接返回 False。"""
    _config("gen-1")
    with db_session.session_scope() as sess:
        sess.add(ModelUsage(model_config_id="gen-1", request_id="req-x",
                            prompt_tokens=1, completion_tokens=1,
                            created_at=datetime(2026, 10, 1, 3, 0), day=date(2026, 10, 1)))

    assert usage_stats.record(model_config_id="gen-1", request_id="req-x",
                              prompt_tokens=1, completion_tokens=1) is False
    with db_session.session_scope() as sess:
        assert sess.query(ModelUsage).count() == 1


def test_record_swallows_the_unique_index_collision(temp_db, monkeypatch):
    """并发下预查询看不见、插入才撞唯一索引：也要当成"已记过"，不抛、不重记。

    这是**另一个**分支（except 里的 IntegrityError），只有真撞索引才走得到，
    所以这里把预查询短路掉（返回 "没查到"），让插入去撞唯一索引。
    """
    from contextlib import contextmanager

    _config("gen-1")
    with db_session.session_scope() as sess:
        sess.add(ModelUsage(model_config_id="gen-1", request_id="req-y",
                            prompt_tokens=1, completion_tokens=1,
                            created_at=datetime(2026, 10, 1, 3, 0), day=date(2026, 10, 1)))

    real_scope = db_session.session_scope

    class _BlindQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None          # 假装"还没记过"

    class _Shim:
        def __init__(self, sess) -> None:
            self._sess = sess

        def query(self, *args, **kwargs):
            return _BlindQuery()

        def add(self, obj):
            self._sess.add(obj)

        def __getattr__(self, name):
            return getattr(self._sess, name)

    @contextmanager
    def _blind_scope():
        with real_scope() as sess:
            yield _Shim(sess)

    monkeypatch.setattr(db_session, "session_scope", _blind_scope)

    assert usage_stats.record(model_config_id="gen-1", request_id="req-y",
                              prompt_tokens=1, completion_tokens=1) is False
    # 只还原这一处 patch（monkeypatch.undo() 会把 fixture 换的临时库也一起还原，
    # 那样下面的断言就会打到真实 MySQL 上）
    monkeypatch.setattr(db_session, "session_scope", real_scope)
    with db_session.session_scope() as sess:
        assert sess.query(ModelUsage).count() == 1


def test_record_swallows_db_failure(temp_db, monkeypatch):
    """写库炸了也只能记日志：统计是旁路，绝不能让一次 LLM 调用失败。"""

    def boom():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db_session, "session_scope", boom)

    assert usage_stats.record(model_config_id="gen-1", prompt_tokens=1,
                              completion_tokens=2) is False
    assert usage_stats.record_result({"id": "gen-1"}, object()) is False


# --------------------------------------------------------------------------
# 查询：结构 / 分桶 / 筛选
# --------------------------------------------------------------------------


def _stats(**kwargs) -> dict:
    return usage_stats.stats(**kwargs)


def test_empty_db_returns_valid_empty_structure(temp_db):
    out = _stats(window="7d")

    assert out["window"]["kind"] == "7d"
    assert out["window"]["granularity"] == "day"
    assert len(out["window"]["points"]) == 7
    assert out["series"] == []
    assert out["filters"] == {"vendors": [], "models": []}
    assert out["totals"] == {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
                             "requests": 0, "cost": {}, "unpriced": 0}


def test_stats_survives_broken_db(temp_db, monkeypatch):
    def boom():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db_session, "session_scope", boom)
    out = _stats(window="24h")

    assert len(out["window"]["points"]) == 24
    assert out["series"] == [] and out["totals"]["requests"] == 0


@pytest.mark.parametrize("window,count", [("24h", 24), ("7d", 7), ("14d", 14),
                                          ("30d", 30), ("bogus", 7)])
def test_window_point_counts(temp_db, window, count):
    out = _stats(window=window)

    assert len(out["window"]["points"]) == count
    assert out["window"]["from"] == out["window"]["points"][0]["key"]
    assert out["window"]["to"] == out["window"]["points"][-1]["key"]


def test_day_window_buckets_are_continuous_and_old_to_new(temp_db):
    out = _stats(window="7d")
    keys = [p["key"] for p in out["window"]["points"]]

    days = [date.fromisoformat(k) for k in keys]
    assert all(days[i + 1] - days[i] == timedelta(days=1) for i in range(len(days) - 1))
    assert days[-1] == date.fromisoformat(billing.local_day())
    assert out["window"]["points"][0]["label"].count("/") == 1


def test_hour_window_buckets_a_three_hour_old_record(temp_db, monkeypatch):
    """24h = 24 个**小时桶**，3 小时前那笔必须落在正好的那个桶里。"""
    _config("gen-1", name="计时档", price_in=1, price_out=2)
    stamp = billing.to_utc(billing.local_now() - timedelta(hours=3))  # utc_now 的口径
    monkeypatch.setattr(usage_stats, "utc_now", lambda: stamp)

    assert usage_stats.record(model_config_id="gen-1", model_name="deepseek-chat",
                              provider="deepseek", prompt_tokens=100,
                              completion_tokens=50) is True
    out = _stats(window="24h")

    points = out["window"]["points"]
    assert len(points) == 24
    assert all(len(p["key"]) == 13 for p in points)          # YYYY-MM-DD HH
    assert all(p["label"].endswith(":00") for p in points)

    series = out["series"][0]
    # 身份 = (自定义名称, 厂商)：键 name:<provider>/<名称>，不是配置 id
    assert series["key"] == "name:deepseek/计时档"
    landed = [p for p in series["points"] if p["requests"]]
    assert len(landed) == 1
    expected = billing.hour_key(stamp)
    index = [p["key"] for p in points].index(expected)
    assert series["points"][index]["tokens_total"] == 150
    assert series["points"][index]["tokens_in"] == 100
    assert series["totals"]["requests"] == 1


def test_series_identity_and_totals(temp_db):
    _vendor("deepseek", label="DeepSeek", currency="CNY")
    _config("gen-1", name="主力生成模型", price_in=2, price_out=8)
    now_local = billing.local_now().replace(microsecond=0)
    stamp = billing.to_utc(now_local - timedelta(minutes=5))

    with db_session.session_scope() as sess:
        sess.add(ModelUsage(model_config_id="gen-1", model_name="deepseek-chat",
                            provider="deepseek", prompt_tokens=1_000_000,
                            completion_tokens=500_000, total_tokens=1_500_000,
                            currency="CNY", cost=6.0, billing="computed",
                            created_at=stamp,
                            day=date.fromisoformat(billing.local_day(stamp)),
                            hour_key=billing.hour_key(stamp)))
        sess.add(ModelUsage(model_config_id="gen-1", model_name="deepseek-chat",
                            provider="deepseek", prompt_tokens=10, completion_tokens=5,
                            total_tokens=15, currency="CNY", cost=None, billing="none",
                            created_at=stamp,
                            day=date.fromisoformat(billing.local_day(stamp)),
                            hour_key=billing.hour_key(stamp)))

    out = _stats(window="7d")
    series = out["series"][0]

    assert series["key"] == "name:deepseek/主力生成模型" and series["kind"] == "model"
    assert series["label"] == "主力生成模型"
    assert series["model_name"] == "deepseek-chat"         # 展示字段，不参与身份
    assert series["provider"] == "deepseek"
    assert series["provider_label"] == "DeepSeek"
    assert series["model_config_ids"] == ["gen-1"]          # 新增：参与合并的配置
    assert series["providers"] == ["deepseek"]              # 新增：参与合并的厂商
    assert series["currency"] == "CNY" and series["has_price"] is True
    assert series["totals"]["tokens_in"] == 1_000_010
    assert series["totals"]["tokens_out"] == 500_005
    assert series["totals"]["requests"] == 2
    assert series["totals"]["cost"] == {"CNY": 6.0}
    assert series["totals"]["unpriced"] == 1          # 未计费不计金额，只计数
    assert out["currencies"] == ["CNY"]
    assert out["filters"]["models"][0]["config_id"] == "gen-1"


def test_currencies_are_never_summed_together(temp_db):
    """CNY 行与 USD 行各自只出现在自己的 cost 键里。"""
    _vendor("deepseek", currency="CNY")
    _vendor("openai", currency="USD", label="OpenAI")
    _config("cn-1", name="人民币档", provider="deepseek", price_in=2, price_out=8,
            currency="CNY")
    _config("us-1", name="美元档", provider="openai", price_in=1, price_out=1,
            currency="USD")

    assert usage_stats.record(model_config_id="cn-1", provider="deepseek",
                              prompt_tokens=1_000_000, completion_tokens=0) is True
    assert usage_stats.record(model_config_id="us-1", provider="openai",
                              prompt_tokens=1_000_000, completion_tokens=0) is True

    out = _stats(window="7d")
    by_key = {row["key"]: row for row in out["series"]}

    assert by_key["name:deepseek/人民币档"]["totals"]["cost"] == {"CNY": 2.0}
    assert by_key["name:openai/美元档"]["totals"]["cost"] == {"USD": 1.0}
    # 合计里两个币种各占一个键：3.0 这种数**不允许**出现
    assert out["totals"]["cost"] == {"CNY": 2.0, "USD": 1.0}
    assert out["currencies"] == ["CNY", "USD"]
    assert 3.0 not in out["totals"]["cost"].values()


def test_same_model_name_on_two_configs_is_not_merged(temp_db):
    """旧不变量的护栏：**上游 model_name 相同**但自定义名称不同 → 仍是两条线。

    model_name（deepseek-chat）只是展示字段；用户起的名字才是"当着一条算"的承诺。
    """
    _config("gen-a", name="A", model_name="deepseek-chat")
    _config("gen-b", name="B", model_name="deepseek-chat")
    usage_stats.record(model_config_id="gen-a", model_name="deepseek-chat",
                       provider="deepseek", prompt_tokens=10, completion_tokens=1)
    usage_stats.record(model_config_id="gen-b", model_name="deepseek-chat",
                       provider="deepseek", prompt_tokens=20, completion_tokens=2)

    out = _stats(window="7d")
    keys = sorted(row["key"] for row in out["series"])

    assert keys == ["name:deepseek/A", "name:deepseek/B"]
    assert {row["label"] for row in out["series"]} == {"A", "B"}
    assert {tuple(row["model_config_ids"]) for row in out["series"]} == {("gen-a",), ("gen-b",)}


def test_deleted_config_falls_back_to_its_model_name(temp_db):
    _config("gen-1", name="要删的", model_name="deepseek-chat")
    usage_stats.record(model_config_id="gen-1", model_name="deepseek-chat",
                       provider="deepseek", prompt_tokens=10, completion_tokens=1)

    with db_session.session_scope() as sess:
        sess.delete(sess.get(ModelConfig, "gen-1"))

    out = _stats(window="7d")

    # 配置行没了就读不到自定义名称 → 身份回落到行上反规范化的 model_name
    assert [row["key"] for row in out["series"]] == ["name:deepseek/deepseek-chat"]
    assert out["series"][0]["label"] == "deepseek-chat"
    assert out["series"][0]["totals"]["tokens_total"] == 11
    assert out["series"][0]["model_config_ids"] == []       # 已删除配置不在其中


def test_group_vendor_folds_models_and_keeps_filters_full(temp_db):
    _vendor("deepseek", label="DeepSeek")
    _config("gen-a", name="A", provider="deepseek", model_name="deepseek-chat")
    _config("gen-b", name="B", provider="deepseek", model_name="deepseek-reasoner")
    usage_stats.record(model_config_id="gen-a", provider="deepseek",
                       prompt_tokens=10, completion_tokens=1)
    usage_stats.record(model_config_id="gen-b", provider="deepseek",
                       prompt_tokens=20, completion_tokens=2)

    out = _stats(window="7d", group="vendor")

    assert out["group"] == "vendor"
    assert [row["key"] for row in out["series"]] == ["vendor:deepseek"]
    vendor = out["series"][0]
    assert vendor["kind"] == "vendor" and vendor["label"] == "DeepSeek"
    assert "model_name" not in vendor and "config_id" not in vendor
    assert vendor["totals"]["tokens_total"] == 33
    # filters 永远按模型级全量：按厂商看的时候也要能挑到具体模型
    assert sorted(row["key"] for row in out["filters"]["models"]) == \
        ["name:deepseek/A", "name:deepseek/B"]
    assert out["filters"]["vendors"][0]["models"] == \
        ["name:deepseek/A", "name:deepseek/B"]


def test_filters_stay_full_when_a_filter_is_applied(temp_db):
    _vendor("deepseek", label="DeepSeek")
    _vendor("openai", label="OpenAI", currency="USD")
    _config("gen-a", name="国产档", provider="deepseek", model_name="deepseek-chat")
    _config("gen-b", name="海外档", provider="openai", model_name="gpt-x")
    usage_stats.record(model_config_id="gen-a", provider="deepseek",
                       prompt_tokens=10, completion_tokens=1)
    usage_stats.record(model_config_id="gen-b", provider="openai",
                       prompt_tokens=20, completion_tokens=2)

    out = _stats(window="7d", vendor="openai")

    assert [row["key"] for row in out["series"]] == ["name:openai/海外档"]
    assert out["totals"]["tokens_total"] == 22
    # 下拉框数据源不受影响：否则选了 openai 就再也没法切回 deepseek
    assert len(out["filters"]["models"]) == 2
    assert {row["id"] for row in out["filters"]["vendors"]} == {"deepseek", "openai"}


def test_model_filter_accepts_bare_config_id(temp_db):
    _config("gen-a", name="A")
    _config("gen-b", name="B")
    usage_stats.record(model_config_id="gen-a", prompt_tokens=10, completion_tokens=1)
    usage_stats.record(model_config_id="gen-b", prompt_tokens=20, completion_tokens=2)

    out = _stats(window="7d", model="gen-a")

    # 裸 config id 仍认 → 命中该配置所在的（合并后）系列
    assert [row["key"] for row in out["series"]] == ["name:deepseek/A"]


def test_priced_now_is_strict_while_has_price_keeps_the_history(temp_db):
    """两个字段并存：priced_now 只认「现在配了输入价与输出价」。

    限额引擎要用 priced_now —— 拿宽松的 has_price 去判，会把一个"算不出钱"的模型放行。
    has_price 保留宽松语义：改过价的配置，它的历史账仍然要显示成"有钱的"。
    """
    _config("gen-live", name="配了价", price_in=2, price_out=8)     # ① 现在能算价
    _config("gen-cleared", name="改价前配过")                        # ② 单价已清空
    _config("gen-never", name="从没配价")                            # ③ 没有单价
    now_utc = billing.to_utc(billing.local_now())

    def _row(config_id, model_name, cost, currency="CNY"):
        with db_session.session_scope() as sess:
            sess.add(ModelUsage(
                model_config_id=config_id, model_name=model_name, provider="deepseek",
                prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000,
                cost=cost, currency=currency, billing="computed" if cost else "none",
                created_at=now_utc,
                day=date.fromisoformat(billing.local_day(now_utc)),
                hour_key=billing.hour_key(now_utc)))

    assert usage_stats.record(model_config_id="gen-live", model_name="deepseek-chat",
                              provider="deepseek", prompt_tokens=1_000_000,
                              completion_tokens=0) is True
    _row("gen-cleared", "deepseek-cleared", 6.0)      # ② 历史行真的带过金额
    _row("gen-never", "deepseek-never", None)         # ③ 从来算不出金额
    _row("no-such-config", "deepseek-gone", 1.0)      # ④ 配置已删 → 名字回落 model_name

    out = _stats(window="7d")
    series = {row["key"]: row for row in out["series"]}
    filters = {row["key"]: row for row in out["filters"]["models"]}

    # ① 配了价
    assert series["name:deepseek/配了价"]["priced_now"] is True
    assert series["name:deepseek/配了价"]["has_price"] is True
    # ② 单价清空、历史行带金额
    assert series["name:deepseek/改价前配过"]["priced_now"] is False
    assert series["name:deepseek/改价前配过"]["has_price"] is True
    assert series["name:deepseek/改价前配过"]["totals"]["cost"] == {"CNY": 6.0}
    # ③ 从没配价
    assert series["name:deepseek/从没配价"]["priced_now"] is False
    assert series["name:deepseek/从没配价"]["has_price"] is False
    # ④ 配置已删：永远算不出价，历史账还在
    assert series["name:deepseek/deepseek-gone"]["priced_now"] is False
    assert series["name:deepseek/deepseek-gone"]["has_price"] is True

    # filters.models 与 series 同口径
    for key in ("name:deepseek/配了价", "name:deepseek/改价前配过",
                "name:deepseek/从没配价", "name:deepseek/deepseek-gone"):
        assert filters[key]["priced_now"] == series[key]["priced_now"]
        assert filters[key]["has_price"] == series[key]["has_price"]

    # vendor 级 = 旗下所有系列 AND（gen-never 算不出钱 → False）
    vendor = _stats(window="7d", group="vendor")["series"][0]
    assert vendor["key"] == "vendor:deepseek" and vendor["priced_now"] is False
    assert vendor["has_price"] is True
    assert out["filters"]["vendors"][0]["priced_now"] is False


def test_unknown_filter_selects_nothing_but_stays_valid(temp_db):
    _config("gen-a")
    usage_stats.record(model_config_id="gen-a", prompt_tokens=10, completion_tokens=1)

    out = _stats(window="7d", vendor="nobody")

    assert out["series"] == []
    assert out["totals"]["tokens_total"] == 0
    assert out["filters"]["models"]                      # 全量照旧


# --------------------------------------------------------------------------
# 查询：系列身份 = (自定义名称, 厂商) 二元组（同名**且同厂商**才合并）
# --------------------------------------------------------------------------


def _usage_row(config_id, model_name, *, provider="deepseek", cost=None,
               currency="CNY", tokens_in=1_000_000, tokens_out=0) -> None:
    """直接落一行用量（要造"具体金额 / 具体币种"时比走 record 更直白）。"""
    stamp = billing.to_utc(billing.local_now())
    with db_session.session_scope() as sess:
        sess.add(ModelUsage(
            model_config_id=config_id, model_name=model_name, provider=provider,
            prompt_tokens=tokens_in, completion_tokens=tokens_out,
            total_tokens=tokens_in + tokens_out,
            cost=cost, currency=currency, billing="computed" if cost else "none",
            created_at=stamp, day=date.fromisoformat(billing.local_day(stamp)),
            hour_key=billing.hour_key(stamp)))


def test_two_configs_with_the_same_custom_name_are_merged(temp_db):
    """同名两条配置 → 一条线：token / 请求 / 金额相加，配置 id 留两个。

    名字先 strip：带首尾空白的那个与干净的那个是同一条。
    """
    _vendor("deepseek", label="DeepSeek", currency="CNY")
    _config("gen-a", name="DeepSeek 合并验证", model_name="deepseek-chat",
            price_in=2, price_out=8)
    _config("gen-b", name="  DeepSeek 合并验证  ", model_name="deepseek-reasoner",
            price_in=2, price_out=8)

    assert usage_stats.record(model_config_id="gen-a", model_name="deepseek-chat",
                              provider="deepseek", prompt_tokens=1_000_000,
                              completion_tokens=0) is True
    assert usage_stats.record(model_config_id="gen-b", model_name="deepseek-reasoner",
                              provider="deepseek", prompt_tokens=500_000,
                              completion_tokens=100_000) is True

    out = _stats(window="7d")

    assert len(out["series"]) == 1
    series = out["series"][0]
    assert series["key"] == "name:deepseek/DeepSeek 合并验证"   # 键 = provider + strip 后的名称
    assert series["label"] == "DeepSeek 合并验证"
    assert series["model_config_ids"] == ["gen-a", "gen-b"]  # 参与合并的两个配置
    assert series["providers"] == ["deepseek"]
    assert series["provider"] == "deepseek"
    assert series["provider_label"] == "DeepSeek"
    # 展示字段取第一个非空值，不拼接
    assert series["model_name"] == "deepseek-chat"
    assert series["totals"]["tokens_in"] == 1_500_000
    assert series["totals"]["tokens_out"] == 100_000
    assert series["totals"]["tokens_total"] == 1_600_000
    assert series["totals"]["requests"] == 2
    assert series["totals"]["cost"] == {"CNY": 3.8}          # 2.0 + 1.8，同币种相加
    # 窗口内总量与合并后的系列一致
    assert out["totals"]["cost"] == {"CNY": 3.8}
    assert out["currencies"] == ["CNY"]


def test_merged_series_keeps_currencies_apart(temp_db):
    """同名合并也**不许**把币种加在一起：CNY 与 USD 各占一键。"""
    _vendor("deepseek", label="DeepSeek", currency="CNY")
    _config("cn-1", name="合并双币", provider="deepseek", model_name="a",
            price_in=2, price_out=8, currency="CNY")
    _config("cn-2", name="合并双币", provider="deepseek", model_name="b",
            price_in=1, price_out=1, currency="USD")

    assert usage_stats.record(model_config_id="cn-1", provider="deepseek",
                              prompt_tokens=1_000_000, completion_tokens=0) is True
    assert usage_stats.record(model_config_id="cn-2", provider="deepseek",
                              prompt_tokens=1_000_000, completion_tokens=0) is True

    out = _stats(window="7d")

    assert len(out["series"]) == 1
    series = out["series"][0]
    assert series["totals"]["cost"] == {"CNY": 2.0, "USD": 1.0}
    assert 3.0 not in series["totals"]["cost"].values()      # 3.0 这种数不允许出现
    assert series["providers"] == ["deepseek"]               # 同一厂商，只是币种不同
    # 同名同厂商但币种不同：仍是一条系列，cost 里两个币种各占一键（不相加）
    assert series["provider"] == "deepseek" and series["providers"] == ["deepseek"]
    assert series["priced_now"] is True                      # 两条都配了价 → AND 为 True


def test_same_name_across_vendors_stays_two_series(temp_db):
    """同名但**不同厂商**：两条独立的系列，各自带自己的 provider（本轮核心断言）。

    上一轮是"并成一条、provider=None、「多个厂商」"；现在身份是
    (自定义名称, 厂商) 二元组 —— 同名同厂商才合并，同名不同厂商必须分开。
    """
    _vendor("deepseek", label="DeepSeek", currency="CNY")
    _vendor("openai", label="OpenAI", currency="CNY")
    _config("ds-1", name="合并跨厂商", provider="deepseek", model_name="deepseek-chat",
            price_in=2, price_out=8, currency="CNY")
    _config("oa-1", name="合并跨厂商", provider="openai", model_name="gpt-x",
            price_in=3, price_out=9, currency="CNY")

    assert usage_stats.record(model_config_id="ds-1", provider="deepseek",
                              prompt_tokens=1_000_000, completion_tokens=0) is True
    assert usage_stats.record(model_config_id="oa-1", provider="openai",
                              prompt_tokens=1_000_000, completion_tokens=0) is True

    out = _stats(window="7d")

    assert len(out["series"]) == 2
    by_key = {row["key"]: row for row in out["series"]}
    assert sorted(by_key) == ["name:deepseek/合并跨厂商", "name:openai/合并跨厂商"]

    ds = by_key["name:deepseek/合并跨厂商"]
    oa = by_key["name:openai/合并跨厂商"]
    # provider / provider_label 永远是非空的那一家 —— 不再有"多个厂商"这种行
    assert ds["provider"] == "deepseek" and ds["provider_label"] == "DeepSeek"
    assert oa["provider"] == "openai" and oa["provider_label"] == "OpenAI"
    assert ds["providers"] == ["deepseek"]          # 数组仍是数组，但恒为长度 1
    assert oa["providers"] == ["openai"]
    assert ds["model_config_ids"] == ["ds-1"]       # 各自只含同一家的配置
    assert oa["model_config_ids"] == ["oa-1"]
    assert ds["label"] == oa["label"] == "合并跨厂商"
    assert ds["totals"]["cost"] == {"CNY": 2.0}
    assert oa["totals"]["cost"] == {"CNY": 3.0}
    assert ds["totals"]["tokens_total"] == 1_000_000
    assert oa["totals"]["tokens_total"] == 1_000_000
    # 合计仍是全部相加（同一币种可以和）
    assert out["totals"]["cost"] == {"CNY": 5.0}
    assert out["totals"]["tokens_total"] == 2_000_000

    # 下拉框：一项 = 一个 (厂商, 名称) 组 → 两项，各带自己的 provider
    filters = {row["key"]: row for row in out["filters"]["models"]}
    assert sorted(filters) == ["name:deepseek/合并跨厂商", "name:openai/合并跨厂商"]
    assert filters["name:deepseek/合并跨厂商"]["provider"] == "deepseek"
    assert filters["name:openai/合并跨厂商"]["provider"] == "openai"
    assert filters["name:deepseek/合并跨厂商"]["model_config_ids"] == ["ds-1"]
    assert filters["name:openai/合并跨厂商"]["model_config_ids"] == ["oa-1"]
    assert filters["name:deepseek/合并跨厂商"]["config_id"] == "ds-1"
    # 厂商下拉里各自只有自己那一条（不再两条都挂两个厂商）
    assert sorted(row["models"] for row in out["filters"]["vendors"]) == \
        [["name:deepseek/合并跨厂商"], ["name:openai/合并跨厂商"]]

    # vendor= 现在就是"留下这家的系列"：不需要再扣掉别家的贡献
    only_ds = _stats(window="7d", vendor="deepseek")
    assert [row["key"] for row in only_ds["series"]] == ["name:deepseek/合并跨厂商"]
    assert only_ds["series"][0]["totals"]["cost"] == {"CNY": 2.0}
    # filters 不受 vendor= 影响（下拉框永远是窗口内全量）
    assert len(only_ds["filters"]["models"]) == 2


def test_model_filter_accepts_name_and_config_id(temp_db):
    """model= 各种写法都认：name:<名称>（旧）/ cfg:<id>（→ 所在系列）/ 裸 id / gone:。"""
    _vendor("deepseek", label="DeepSeek")
    _config("gen-a", name="合并 A", model_name="deepseek-chat", price_in=1, price_out=2)
    _config("gen-b", name="合并 A", model_name="deepseek-reasoner", price_in=1, price_out=2)
    _config("gen-c", name="另一个", model_name="deepseek-chat", price_in=1, price_out=2)

    assert usage_stats.record(model_config_id="gen-a", provider="deepseek",
                              prompt_tokens=10, completion_tokens=1) is True
    assert usage_stats.record(model_config_id="gen-b", provider="deepseek",
                              prompt_tokens=20, completion_tokens=2) is True
    assert usage_stats.record(model_config_id="gen-c", provider="deepseek",
                              prompt_tokens=40, completion_tokens=4) is True

    # 旧写法 name:<名称>（只给名字）：返回该名字下**所有厂商**的那几条（这里只有一家）
    by_name = _stats(window="7d", model="name:合并 A")
    assert [row["key"] for row in by_name["series"]] == ["name:deepseek/合并 A"]
    assert by_name["series"][0]["totals"]["tokens_total"] == 33

    # 新写法 name:<provider>/<名称>：精确到一家
    by_new = _stats(window="7d", model="name:deepseek/合并 A")
    assert [row["key"] for row in by_new["series"]] == ["name:deepseek/合并 A"]
    assert [row["key"] for row in
            _stats(window="7d", model="name:openai/合并 A")["series"]] == []

    # cfg:<id> → 该配置**所在系列**（合并后返回的仍是 name: 系列，不是只剩它自己那份）
    by_cfg = _stats(window="7d", model="cfg:gen-gone-does-not-exist,cfg:gen-a")
    assert [row["key"] for row in by_cfg["series"]] == ["name:deepseek/合并 A"]
    assert by_cfg["series"][0]["model_config_ids"] == ["gen-a", "gen-b"]

    # 裸 id（老前端）等价于 cfg:
    assert [row["key"] for row in _stats(window="7d", model="gen-c")["series"]] == \
        ["name:deepseek/另一个"]

    # 老链接的 gone:<model_name>：它的系列名就是回落出的 model_name（跨厂商全出）
    assert [row["key"] for row in
            _stats(window="7d", model="gone:合并 A")["series"]] == ["name:deepseek/合并 A"]

    assert _stats(window="7d", model="all")["series"] != []
    assert _stats(window="7d", model="name:没有人叫这个")["series"] == []
    assert _stats(window="7d", model="name:deepseek/没有人叫这个")["series"] == []


def test_deleted_config_row_merges_into_the_same_name_series(temp_db):
    """已删除配置的行 + 存活配置同名 → 一条线。

    配置行删了就读不到自定义名称，所以已删那侧的"名字"只能是行上留下的 model_name；
    存活配置把自定义名称起成同一个字面量，两者才落在同一个 name: 键上。
    """
    _vendor("deepseek", label="DeepSeek")
    _config("gen-live", name="deepseek-chat", model_name="deepseek-chat",
            price_in=2, price_out=8)
    assert usage_stats.record(model_config_id="gen-live", model_name="deepseek-chat",
                              provider="deepseek", prompt_tokens=10,
                              completion_tokens=1) is True
    _usage_row("gen-gone", "deepseek-chat", cost=6.0, tokens_in=1_000_000)

    out = _stats(window="7d")

    assert len(out["series"]) == 1
    series = out["series"][0]
    assert series["key"] == "name:deepseek/deepseek-chat"
    assert series["totals"]["tokens_total"] == 1_000_011
    assert series["totals"]["requests"] == 2
    # 6.0（历史行）+ 10*2/1e6 + 1*8/1e6 = 6.000028
    assert series["totals"]["cost"] == {"CNY": 6.000028}
    # 已删除配置不在 model_config_ids 里（没有配置可点）
    assert series["model_config_ids"] == ["gen-live"]
    # 一条算不出价（已删）→ 整体 priced_now 为 False，历史金额仍是真账
    assert series["priced_now"] is False
    assert series["has_price"] is True


def test_deleted_config_row_splits_by_provider(temp_db):
    """已删配置的行：同名**同厂商**才并进去，**同名不同厂商**另起一条。

    已删那侧读不到自定义名称，名字只能取行上留下的 model_name；厂商也一样取行上的
    provider —— 身份是 (名称, 厂商) 二元组，所以这里必须分成两条。
    """
    _vendor("deepseek", label="DeepSeek")
    _vendor("openai", label="OpenAI", currency="USD")
    # 存活配置在 openai，名字与那行已删记录的 model_name 相同
    _config("oa-live", name="历史同名", provider="openai", model_name="历史同名",
            price_in=1, price_out=1, currency="USD")
    assert usage_stats.record(model_config_id="oa-live", model_name="历史同名",
                              provider="openai", prompt_tokens=10,
                              completion_tokens=1) is True
    _usage_row("ds-gone", "历史同名", provider="deepseek", cost=6.0, tokens_in=1_000_000)

    out = _stats(window="7d")
    by_key = {row["key"]: row for row in out["series"]}

    assert sorted(by_key) == ["name:deepseek/历史同名", "name:openai/历史同名"]
    gone = by_key["name:deepseek/历史同名"]
    live = by_key["name:openai/历史同名"]
    assert gone["provider"] == "deepseek" and gone["providers"] == ["deepseek"]
    assert live["provider"] == "openai" and live["providers"] == ["openai"]
    assert gone["model_config_ids"] == []              # 已删配置没有 id 可点
    assert live["model_config_ids"] == ["oa-live"]
    assert gone["totals"]["cost"] == {"CNY": 6.0}      # 历史金额仍是真账
    assert gone["priced_now"] is False and gone["has_price"] is True
    assert live["priced_now"] is True
    # 合计按币种分开，永不相加
    assert out["totals"]["cost"] == {"CNY": 6.0, "USD": 0.000011}
    assert out["totals"]["tokens_total"] == 1_000_011


def test_series_key_without_provider_keeps_the_dash_segment(temp_db):
    """没有厂商的行：键里仍要有"斜杠前那一段" —— name:-/<名称>（前端只当不透明键）。

    配置上的 provider 不可为空，但**行**上的 provider 可以是 NULL（老数据 / 旁路补录），
    所以键格式不能假设它一定有值。
    """
    assert usage_stats.series_key("", "无厂商档") == "name:-/无厂商档"
    assert usage_stats.series_key(None, "无厂商档") == "name:-/无厂商档"
    assert usage_stats.series_key("deepseek", "无厂商档") == "name:deepseek/无厂商档"

    _usage_row("no-config", "无厂商档", provider=None, cost=1.0)

    out = _stats(window="7d")
    series = out["series"][0]
    assert series["key"] == "name:-/无厂商档"
    assert series["providers"] == []               # 一家厂商都没有
    assert series["provider"] is None
    assert series["provider_label"] == "未知厂商"   # 但 label 永远不是空串


def test_model_filter_separates_vendors_for_the_same_name(temp_db):
    """model=：新键精确到一家；旧写法 name:<名称> 把该名字下所有厂商都交出来。"""
    _vendor("deepseek", label="DeepSeek")
    _vendor("openai", label="OpenAI")
    _config("ds-1", name="筛选同名", provider="deepseek", model_name="deepseek-chat",
            price_in=1, price_out=1)
    _config("oa-1", name="筛选同名", provider="openai", model_name="gpt-x",
            price_in=1, price_out=1)
    _config("ds-2", name="筛选别的", provider="deepseek", model_name="deepseek-chat",
            price_in=1, price_out=1)
    for cid, provider in (("ds-1", "deepseek"), ("oa-1", "openai"), ("ds-2", "deepseek")):
        assert usage_stats.record(model_config_id=cid, provider=provider,
                                  prompt_tokens=10, completion_tokens=1) is True

    # 新键：只那一条
    one = _stats(window="7d", model="name:deepseek/筛选同名")
    assert [row["key"] for row in one["series"]] == ["name:deepseek/筛选同名"]
    assert one["series"][0]["provider"] == "deepseek"
    # 旧写法（无 provider 段）：该名字下所有厂商的两条都出来
    both = _stats(window="7d", model="name:筛选同名")
    assert sorted(row["key"] for row in both["series"]) == \
        ["name:deepseek/筛选同名", "name:openai/筛选同名"]
    assert both["totals"]["tokens_total"] == 22
    # cfg:<id> 仍选到它所在的那一条系列
    by_cfg = _stats(window="7d", model="cfg:oa-1")
    assert [row["key"] for row in by_cfg["series"]] == ["name:openai/筛选同名"]
    # 逗号分隔也能混着给（新键 + cfg:）
    mixed = _stats(window="7d", model="name:deepseek/筛选同名,cfg:ds-2")
    assert sorted(row["key"] for row in mixed["series"]) == \
        ["name:deepseek/筛选别的", "name:deepseek/筛选同名"]


def test_priced_now_is_and_over_merged_configs(temp_db):
    """同名合并的 priced_now 用 AND：一条没配价 → 整体 False（与 vendor 级同口径）。"""
    _vendor("deepseek", label="DeepSeek")
    _config("gen-priced", name="半配价", model_name="a", price_in=2, price_out=8)
    _config("gen-free", name="半配价", model_name="b")            # 没配价

    assert usage_stats.record(model_config_id="gen-priced", provider="deepseek",
                              prompt_tokens=1_000_000, completion_tokens=0) is True
    assert usage_stats.record(model_config_id="gen-free", provider="deepseek",
                              prompt_tokens=10, completion_tokens=0) is True

    out = _stats(window="7d")
    series = out["series"][0]
    assert series["key"] == "name:deepseek/半配价"
    assert series["priced_now"] is False        # AND：gen-free 算不出钱
    assert series["has_price"] is True          # 但历史行真的带过金额
    assert series["totals"]["unpriced"] == 1
    assert out["filters"]["models"][0]["priced_now"] is False
    # 厂商级同样是 AND
    assert _stats(window="7d", group="vendor")["series"][0]["priced_now"] is False

    # 两条都配价 → True
    with db_session.session_scope() as sess:
        row = sess.get(ModelConfig, "gen-free")
        row.price_in, row.price_out = 1, 1
    both = _stats(window="7d")["series"][0]
    assert both["priced_now"] is True
    assert both["totals"]["cost"] == {"CNY": 2.0}

