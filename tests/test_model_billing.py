"""计费与用量统计（离线，临时 SQLite）：单价 / 缓存拆分 / 币种隔离 / 幂等 / 分桶。

为什么不用真实库：这两层是**纯计算 + 纯聚合**，验的是口径而不是连通性：

· 单价 → 金额：每百万 Token 的公式、缓存拆分、半配价格、厂商回传费用；
· 币种：人民币与美元各算各的，**永远不相加**（一个错账比缺一个功能严重得多）；
· 周期边界：日 / 周（周一起）/ 月，跨月、跨年、周日与周一那两天；
· 写入口径：幂等 request_id、小时桶落位、同名不同配置不合并、删配置后 gone: 仍在。
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
    _config("gen-1", price_in=1, price_out=2)
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
    assert series["key"] == "cfg:gen-1"
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

    assert series["key"] == "cfg:gen-1" and series["kind"] == "model"
    assert series["label"] == "主力生成模型"
    assert series["model_name"] == "deepseek-chat"
    assert series["provider"] == "deepseek"
    assert series["provider_label"] == "DeepSeek"
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

    assert by_key["cfg:cn-1"]["totals"]["cost"] == {"CNY": 2.0}
    assert by_key["cfg:us-1"]["totals"]["cost"] == {"USD": 1.0}
    # 合计里两个币种各占一个键：3.0 这种数**不允许**出现
    assert out["totals"]["cost"] == {"CNY": 2.0, "USD": 1.0}
    assert out["currencies"] == ["CNY", "USD"]
    assert 3.0 not in out["totals"]["cost"].values()


def test_same_model_name_on_two_configs_is_not_merged(temp_db):
    _config("gen-a", name="A", model_name="deepseek-chat")
    _config("gen-b", name="B", model_name="deepseek-chat")
    usage_stats.record(model_config_id="gen-a", model_name="deepseek-chat",
                       provider="deepseek", prompt_tokens=10, completion_tokens=1)
    usage_stats.record(model_config_id="gen-b", model_name="deepseek-chat",
                       provider="deepseek", prompt_tokens=20, completion_tokens=2)

    out = _stats(window="7d")
    keys = sorted(row["key"] for row in out["series"])

    assert keys == ["cfg:gen-a", "cfg:gen-b"]
    assert {row["label"] for row in out["series"]} == {"A", "B"}


def test_deleted_config_falls_back_to_gone_series(temp_db):
    _config("gen-1", name="要删的", model_name="deepseek-chat")
    usage_stats.record(model_config_id="gen-1", model_name="deepseek-chat",
                       provider="deepseek", prompt_tokens=10, completion_tokens=1)

    with db_session.session_scope() as sess:
        sess.delete(sess.get(ModelConfig, "gen-1"))

    out = _stats(window="7d")

    assert [row["key"] for row in out["series"]] == ["gone:deepseek-chat"]
    assert out["series"][0]["label"] == "deepseek-chat"     # 回落到 model_name
    assert out["series"][0]["totals"]["tokens_total"] == 11


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
        ["cfg:gen-a", "cfg:gen-b"]
    assert out["filters"]["vendors"][0]["models"] == ["cfg:gen-a", "cfg:gen-b"]


def test_filters_stay_full_when_a_filter_is_applied(temp_db):
    _vendor("deepseek", label="DeepSeek")
    _vendor("openai", label="OpenAI", currency="USD")
    _config("gen-a", provider="deepseek", model_name="deepseek-chat")
    _config("gen-b", provider="openai", model_name="gpt-x")
    usage_stats.record(model_config_id="gen-a", provider="deepseek",
                       prompt_tokens=10, completion_tokens=1)
    usage_stats.record(model_config_id="gen-b", provider="openai",
                       prompt_tokens=20, completion_tokens=2)

    out = _stats(window="7d", vendor="openai")

    assert [row["key"] for row in out["series"]] == ["cfg:gen-b"]
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

    assert [row["key"] for row in out["series"]] == ["cfg:gen-a"]


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
    _row("no-such-config", "deepseek-gone", 1.0)      # ④ 配置已删 → gone:

    out = _stats(window="7d")
    series = {row["key"]: row for row in out["series"]}
    filters = {row["key"]: row for row in out["filters"]["models"]}

    # ① 配了价
    assert series["cfg:gen-live"]["priced_now"] is True
    assert series["cfg:gen-live"]["has_price"] is True
    # ② 单价清空、历史行带金额
    assert series["cfg:gen-cleared"]["priced_now"] is False
    assert series["cfg:gen-cleared"]["has_price"] is True
    assert series["cfg:gen-cleared"]["totals"]["cost"] == {"CNY": 6.0}
    # ③ 从没配价
    assert series["cfg:gen-never"]["priced_now"] is False
    assert series["cfg:gen-never"]["has_price"] is False
    # ④ 配置已删：永远算不出价，历史账还在
    assert series["gone:deepseek-gone"]["priced_now"] is False
    assert series["gone:deepseek-gone"]["has_price"] is True

    # filters.models 与 series 同口径
    for key in ("cfg:gen-live", "cfg:gen-cleared", "cfg:gen-never", "gone:deepseek-gone"):
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
