"""分时段（峰谷）单价：命中判定 / 周末 / 边界 / 跨午夜 / 落账可追溯 / priced 语义不变。

为什么这些口径要单独钉住：峰谷计价最容易出的错不是"算错乘法"，而是**判错时段** ——
12:00 整算不算高峰、周六算不算高峰、跨午夜那段归哪一天，每一条的两种答案看起来都
"差不多"，但它们会直接改变账单（DeepSeek 口径下高峰是空闲的两倍）。所以每条口径都
有一个能随时重跑的断言，且数字全部写死（默认 1/4、高峰 2/8：1e6 输入 + 5e5 输出
→ 默认 3.0、高峰 6.0，与官方"空闲/高峰"两档同构）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.errors import NotFoundError, ValidationError
from backend.repositories.mysql import session as db_session
from backend.repositories.mysql.models import (
    Base, ModelConfig, ModelPriceRule, ModelProvider, ModelUsage,
)
from backend.services.model import billing, price_rules, usage_stats


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把全局 Engine / Session 换成临时 SQLite（不碰真实库）。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'peak.sqlite3').as_posix()}",
                           future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                           expire_on_commit=False)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", factory)
    from backend.services.model.model_manager import model_manager

    model_manager._cache.clear()
    try:
        yield engine
    finally:
        model_manager._cache.clear()
        engine.dispose()


# 2026-03-02 周一 … 2026-03-08 周日（下面几条断言把这个前提钉住，改日期就报错）
MONDAY = datetime(2026, 3, 2)
SATURDAY = datetime(2026, 3, 7)
SUNDAY = datetime(2026, 3, 8)


def _workday(hour: int = 10, minute: int = 0) -> datetime:
    """一个确定的工作日时刻（2026-03-04 周三）。"""
    return datetime(2026, 3, 4, hour, minute)


def _assert_calendar():
    assert MONDAY.weekday() == 0 and SATURDAY.weekday() == 5 and SUNDAY.weekday() == 6
    assert _workday().weekday() == 2


def _config(config_id="gen-1", *, price_in=1.0, price_out=4.0,
            price_cache_read=0.02, price_cache_write=None) -> None:
    with db_session.session_scope() as sess:
        if sess.get(ModelProvider, "deepseek") is None:
            sess.add(ModelProvider(id="deepseek", kind="official", label="DeepSeek",
                                   currency="CNY"))
        sess.add(ModelConfig(
            id=config_id, name="主力生成模型", provider="deepseek",
            model_type="generation", model_name="deepseek-flash",
            price_in=price_in, price_out=price_out,
            price_cache_read=price_cache_read, price_cache_write=price_cache_write,
            currency=None,
        ))


def _rule(rule_id="rule-1", *, start="09:00", end="12:00", label="高峰",
          price_in=2.0, price_out=8.0, price_cache_read=0.04,
          price_cache_write=None, model_config_id="gen-1") -> None:
    with db_session.session_scope() as sess:
        sess.add(ModelPriceRule(
            id=rule_id, model_config_id=model_config_id, label=label,
            start=start, end=end, price_in=price_in, price_out=price_out,
            price_cache_read=price_cache_read, price_cache_write=price_cache_write,
        ))


# --------------------------------------------------------------------------
# 命中判定：工作日 / 周末 / 边界
# --------------------------------------------------------------------------


def test_workday_peak_hits_rule_price(temp_db):
    """工作日 10:00 命中 09:00–12:00 → 用该段价（2/8），1e6 + 5e5 = 6.0。"""
    _assert_calendar()
    _config()
    _rule()
    prices = billing.price_of("gen-1", at=_workday(10))
    assert prices["source"] == "rule"
    assert prices["rule_id"] == "rule-1" and prices["rule_label"] == "高峰"
    assert (prices["price_in"], prices["price_out"]) == (2.0, 8.0)
    assert billing.cost_of(prices, 1_000_000, 500_000)["cost"] == 6.0

    # 时段之外（20:00）用默认价 1/4 → 3.0，且 rule_id 为空
    off = billing.price_of("gen-1", at=_workday(20))
    assert off["source"] == "model" and off["rule_id"] is None
    assert (off["price_in"], off["price_out"]) == (1.0, 4.0)
    assert billing.cost_of(off, 1_000_000, 500_000)["cost"] == 3.0


@pytest.mark.parametrize("when", [SATURDAY.replace(hour=10), SUNDAY.replace(hour=10),
                                 SATURDAY.replace(hour=10, minute=30)])
def test_weekend_never_hits_rule(temp_db, when):
    """周末一律用默认价（DeepSeek 口径里默认价就是空闲价）。"""
    _config()
    _rule()
    prices = billing.price_of("gen-1", at=when)
    assert prices["source"] == "model" and prices["rule_id"] is None
    assert billing.cost_of(prices, 1_000_000, 500_000)["cost"] == 3.0


@pytest.mark.parametrize("hour,minute,hit", [
    (8, 30, False),     # 08:30 —— 高峰 09:00 才开始
    (9, 0, True),       # 09:00 整 —— 左闭
    (11, 59, True),
    (12, 0, False),     # 12:00 整 —— 右开
])
def test_boundary_left_closed_right_open(temp_db, hour, minute, hit):
    _config()
    _rule()
    prices = billing.price_of("gen-1", at=_workday(hour, minute))
    assert (prices["source"] == "rule") is hit
    assert (prices["price_in"] == 2.0) is hit


def test_two_peak_rules_each_hit_its_own(temp_db):
    """09:00–12:00 与 14:00–18:00 两段各自命中（DeepSeek 官方就是这两段）。"""
    _config()
    _rule("rule-am", start="09:00", end="12:00", label="上午高峰",
          price_in=2.0, price_out=8.0)
    _rule("rule-pm", start="14:00", end="18:00", label="下午高峰",
          price_in=3.0, price_out=12.0)
    assert billing.price_of("gen-1", at=_workday(10))["rule_id"] == "rule-am"
    assert billing.price_of("gen-1", at=_workday(15))["rule_id"] == "rule-pm"
    # 12:00–14:00 那段午休是空闲价
    assert billing.price_of("gen-1", at=_workday(13))["source"] == "model"


def test_cross_midnight_rule_day_basis(temp_db):
    """跨午夜 23:00–01:00：白天那截看今天、凌晨那截看**前一天**。"""
    _assert_calendar()
    _config()
    _rule("rule-night", start="23:00", end="01:00", label="夜间",
          price_in=0.5, price_out=1.0)
    assert billing.price_of("gen-1", at=_workday(23, 30))["rule_id"] == "rule-night"
    # 周三 23:00 那段跨到周四 00:30 —— 按周三（工作日）算命中
    assert billing.price_of("gen-1", at=datetime(2026, 3, 5, 0, 30))["rule_id"] == "rule-night"
    # 周六 23:30：开始那天不是工作日 → 不命中
    assert billing.price_of("gen-1", at=SATURDAY.replace(hour=23, minute=30))["source"] == "model"
    # 周日 23:30 与周一 00:30：这段是从周日开的头 → 也不命中
    assert billing.price_of("gen-1", at=SUNDAY.replace(hour=23, minute=30))["source"] == "model"
    assert billing.price_of("gen-1", at=MONDAY.replace(hour=0, minute=30))["source"] == "model"
    # 周五 23:30 跨到周六 00:30 —— 按周五算命中
    assert billing.price_of("gen-1", at=datetime(2026, 3, 6, 23, 30))["rule_id"] == "rule-night"
    assert billing.price_of("gen-1", at=SATURDAY.replace(hour=0, minute=30))["rule_id"] == "rule-night"


def test_cache_read_price_follows_segment(temp_db):
    """缓存命中价同样分时段：默认 0.02，高峰 0.04（1e6 缓存读 → 0.02 / 0.04）。"""
    _config()
    _rule()
    peak = billing.price_of("gen-1", at=_workday(10))
    off = billing.price_of("gen-1", at=_workday(20))
    assert peak["price_cache_read"] == 0.04 and off["price_cache_read"] == 0.02
    assert billing.cost_of(peak, 1_000_000, 0, cache_read=1_000_000)["cost"] == 0.04
    assert billing.cost_of(off, 1_000_000, 0, cache_read=1_000_000)["cost"] == 0.02


def test_cost_of_propagates_at_to_price_of(temp_db):
    """cost_of(at=...) 把时刻透传给 price_of：同一个 config id，两个时刻两个价。"""
    _config()
    _rule()
    assert billing.cost_of("gen-1", 1_000_000, 500_000, at=_workday(10))["cost"] == 6.0
    assert billing.cost_of("gen-1", 1_000_000, 500_000, at=_workday(20))["cost"] == 3.0


# --------------------------------------------------------------------------
# 落账：price_rule_id 可追溯
# --------------------------------------------------------------------------


def _rows():
    with db_session.session_scope() as sess:
        return sess.query(ModelUsage).order_by(ModelUsage.id).all()


def test_record_marks_price_rule_id(temp_db):
    _config()
    _rule()
    assert usage_stats.record(model_config_id="gen-1", prompt_tokens=1_000_000,
                              completion_tokens=500_000, request_id="peak-test-1",
                              at=_workday(10))
    assert usage_stats.record(model_config_id="gen-1", prompt_tokens=1_000_000,
                              completion_tokens=500_000, request_id="peak-test-2",
                              at=_workday(20))
    peak, off = _rows()
    assert (peak.cost, peak.billing, peak.price_rule_id) == (6.0, "computed", "rule-1")
    # 时段之外：默认价 1/4 → 3.0，price_rule_id 必须是 NULL（"没按任何一段算"）
    assert (off.cost, off.billing, off.price_rule_id) == (3.0, "computed", None)
    # created_at / day 也跟着 at 走，否则"按高峰价计费却记在昨天"
    assert str(peak.day) == "2026-03-04" and peak.hour_key == "2026-03-04 10"


def test_record_no_charge_never_marks_rule(temp_db):
    """被限额拦下那类行（no_charge）连时段都不该标：那笔钱没花。"""
    _config()
    _rule()
    assert usage_stats.record(model_config_id="gen-1", prompt_tokens=10,
                              request_id="peak-test-3", no_charge=True,
                              at=_workday(10))
    (row,) = _rows()
    assert (row.cost, row.billing, row.price_rule_id) == (None, "none", None)


# --------------------------------------------------------------------------
# priced 语义不变（限额引擎依赖它）
# --------------------------------------------------------------------------


def test_priced_means_default_prices_only(temp_db):
    """只配时段、不配默认价 → priced=False（与是否命中时段无关）。"""
    _config(price_in=None, price_out=None, price_cache_read=None)
    _rule()
    peak = billing.price_of("gen-1", at=_workday(10))
    off = billing.price_of("gen-1", at=_workday(20))
    assert peak["priced"] is False and off["priced"] is False
    # 命中时段时手上是规则自己那套齐价 → 算得出钱；时段之外仍是未计费
    assert peak["source"] == "rule"
    assert billing.cost_of(peak, 1_000_000, 500_000)["cost"] == 6.0
    assert billing.cost_of(off, 1_000_000, 500_000) == {
        "cost": None, "currency": "CNY", "billing": "none"}
    # 限额引擎那条口径（priced 决定"能不能设限额"）没有被时段字段污染
    assert billing.price_of("gen-1")["priced"] is False


def test_half_configured_default_still_unpriced(temp_db):
    """默认价只配一半 → priced=False，且**时段外**照旧未计费（半算不许发生）。"""
    _config(price_in=1.0, price_out=None)
    _rule()
    assert billing.price_of("gen-1", at=_workday(20))["priced"] is False
    assert billing.cost_of("gen-1", 1_000_000, 500_000,
                           at=_workday(20))["cost"] is None


# --------------------------------------------------------------------------
# 校验：clean_rule / rules_overlap / 整批替换
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload,needle", [
    ({"start": "9:00", "end": "12:00", "price_in": 1, "price_out": 2}, "HH:MM"),
    ({"start": "24:00", "end": "12:00", "price_in": 1, "price_out": 2}, "HH:MM"),
    ({"start": "09:00", "end": "25:00", "price_in": 1, "price_out": 2}, "HH:MM"),
    ({"start": "09:00", "end": "09:00", "price_in": 1, "price_out": 2}, "开始与结束相同"),
    ({"start": "09:00", "end": "12:00", "price_in": 1}, "输出价"),
    ({"start": "09:00", "end": "12:00", "price_out": 2}, "输入价"),
    ({"start": "09:00", "end": "12:00", "price_in": -1, "price_out": 2}, "负数"),
])
def test_clean_rule_rejects_bad_payload(payload, needle):
    with pytest.raises(ValidationError) as exc:
        billing.clean_rule(payload)
    assert needle in str(exc.value)


def test_clean_rule_normalizes():
    out = billing.clean_rule({"label": " 高峰 ", "start": "09:00", "end": "12:00",
                              "price_in": "2", "price_out": 8.0,
                              "price_cache_read": "", "price_cache_write": None})
    assert out == {"label": "高峰", "start": "09:00", "end": "12:00",
                   "price_in": 2.0, "price_out": 8.0,
                   "price_cache_read": None, "price_cache_write": None}


def test_rules_overlap_splits_cross_midnight():
    assert billing.rules_overlap({"start": "09:00", "end": "12:00"},
                                 {"start": "11:00", "end": "13:00"})
    # 首尾相接不算重叠（左闭右开）
    assert not billing.rules_overlap({"start": "09:00", "end": "12:00"},
                                     {"start": "12:00", "end": "14:00"})
    # 跨午夜先拆两段：22:00–02:00 与 01:00–03:00 在 01:00–02:00 撞上
    assert billing.rules_overlap({"start": "22:00", "end": "02:00"},
                                 {"start": "01:00", "end": "03:00"})
    assert not billing.rules_overlap({"start": "22:00", "end": "02:00"},
                                     {"start": "03:00", "end": "04:00"})


def test_replace_rejects_overlap_naming_both(temp_db):
    _config()
    with pytest.raises(ValidationError) as exc:
        price_rules.replace("gen-1", {"items": [
            {"label": "上午高峰", "start": "09:00", "end": "12:00",
             "price_in": 2, "price_out": 8},
            {"label": "重叠段", "start": "11:00", "end": "13:00",
             "price_in": 3, "price_out": 9},
        ]})
    message = str(exc.value)
    assert "09:00–12:00（上午高峰）" in message and "11:00–13:00（重叠段）" in message
    # 整批拒 → 一行都没落
    with db_session.session_scope() as sess:
        assert sess.query(ModelPriceRule).count() == 0


def test_replace_is_delete_then_insert_and_survives_empty(temp_db):
    _config()
    first = price_rules.replace("gen-1", {"items": [
        {"label": "上午", "start": "09:00", "end": "12:00", "price_in": 2, "price_out": 8},
        {"label": "下午", "start": "14:00", "end": "18:00", "price_in": 2, "price_out": 8},
    ]})
    assert [item["label"] for item in first["items"]] == ["上午", "下午"]
    assert first["note"] == price_rules.NOTE and "warning" not in first
    second = price_rules.replace("gen-1", {"items": [
        {"label": "夜里", "start": "23:00", "end": "01:00", "price_in": 0.5, "price_out": 1},
    ]})
    assert [item["label"] for item in second["items"]] == ["夜里"]
    cleared = price_rules.replace("gen-1", {"items": []})
    assert cleared["items"] == []


def test_replace_warns_when_default_price_missing(temp_db):
    _config(price_in=None, price_out=None, price_cache_read=None)
    out = price_rules.replace("gen-1", {"items": [
        {"start": "09:00", "end": "12:00", "price_in": 2, "price_out": 8},
    ]})
    assert out["default"]["has_price"] is False
    assert out["warning"] == price_rules.NO_DEFAULT_PRICE_WARNING


def test_model_service_exposes_count_and_detail(temp_db):
    """列表带 price_rule_count（一次查完），详情带完整 price_rules。"""
    from backend.services.model import model_service
    from backend.services.model.model_manager import model_manager

    _config()
    _rule()
    _rule("rule-2", start="14:00", end="18:00", label="下午高峰",
          price_in=3.0, price_out=12.0)
    model_manager._cache.clear()
    rows = model_service.list_models("generation")
    assert [(row["id"], row["price_rule_count"]) for row in rows] == [("gen-1", 2)]
    detail = model_service.get_model("gen-1")
    assert [item["start"] for item in detail["price_rules"]] == ["09:00", "14:00"]


def test_describe_unknown_model_uses_existing_error(temp_db):
    with pytest.raises(ValidationError) as exc:
        price_rules.describe("nope")
    assert "模型不存在" in str(exc.value)


def test_counts_and_delete(temp_db):
    _config("gen-1")
    _config("gen-2")
    _rule("rule-1")
    _rule("rule-2", start="14:00", end="18:00")
    _rule("rule-3", model_config_id="gen-2", start="09:00", end="12:00")
    assert price_rules.counts(["gen-1", "gen-2", "gen-3"]) == {"gen-1": 2, "gen-2": 1}
    assert price_rules.delete("gen-1", "rule-1") == {"removed": "rule-1"}
    assert price_rules.counts(["gen-1"]) == {"gen-1": 1}
    with pytest.raises(NotFoundError):
        price_rules.delete("gen-1", "rule-1")
    # 别的模型的规则不能通过这个 model_id 删掉（不然一次手滑就能改掉另一条模型的账）
    with pytest.raises(NotFoundError):
        price_rules.delete("gen-2", "rule-2")
