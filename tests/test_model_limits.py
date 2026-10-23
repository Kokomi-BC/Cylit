
"""限额引擎：周期边界 / 币种隔离 / 三级并联 / 未配价拒绝 / 并发原子性 / 预留过期 /
blocked 记账 / summary 三态 / model_manager 接入点。

为什么这么多口径要单独钉住：限额是**唯一**会让调用「不发出去」的东西 ——
判宽了等于没有（钱照花），判窄了整个功能停摆（明明还有余额却说超了）。
所以每条口径都有一个能被随时重跑的断言，而不是靠"看着对"。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from backend.core.errors import LimitExceededError, ValidationError
from backend.repositories.mysql import session as db_session
from backend.repositories.mysql.models import (
    Base, ModelConfig, ModelLimit, ModelLimitReservation, ModelProvider,
    ModelProviderKey, ModelUsage,
)
from backend.services.model import billing, limits


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把全局 Engine / Session 换成临时 SQLite（不碰真实库）。

    `check_same_thread=False` + 宽松 timeout：并发用例在 8 个线程里同跑，
    但断言的是「**最多**只有 2 个成功」这条不变量，而不是"每个线程都拿到连接"——
    真被锁住时宁可让那条线程抛错（用例失败可见），也不要静默放行一笔。
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'limits.sqlite3').as_posix()}",
                           future=True,
                           connect_args={"check_same_thread": False, "timeout": 15})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                           expire_on_commit=False)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", factory)
    # provider 实例缓存跨用例复用（键是 id|provider|model_name|密钥指纹）：
    # 不清掉的话第二个用例拿到的是第一个用例里那份 config（价格/参数都不一样），
    # 而线上是靠 model_manager._invalidate 在改配置时清缓存，不会出现这种串味。
    from backend.services.model.model_manager import model_manager

    model_manager._cache.clear()
    try:
        yield engine
    finally:
        model_manager._cache.clear()
        engine.dispose()


# --------------------------------------------------------------------------
# 造数据的小工具
# --------------------------------------------------------------------------


def _vendor(provider_id="deepseek", label="DeepSeek", currency="CNY"):
    with db_session.session_scope() as sess:
        sess.add(ModelProvider(id=provider_id, kind="official", label=label,
                               currency=currency))


def _key(key_id="key-1", provider_id="deepseek", label="主账号", default=True):
    with db_session.session_scope() as sess:
        sess.add(ModelProviderKey(id=key_id, provider_id=provider_id, label=label,
                                  secret_ref="no-such-secret", is_default=default))


def _config(config_id="gen-1", *, name=None, provider="deepseek",
            model_type="generation", model_name="deepseek-chat",
            price_in=2.0, price_out=8.0, currency="CNY",
            key_mode="none", provider_key_id=None, enabled=True):
    with db_session.session_scope() as sess:
        sess.add(ModelConfig(
            id=config_id, name=name or config_id, provider=provider,
            model_type=model_type, model_name=model_name,
            price_in=price_in, price_out=price_out, currency=currency,
            enabled=enabled, is_default=True, key_mode=key_mode,
            provider_key_id=provider_key_id, status="ok",
        ))


def _raw_limit(scope="model", scope_id="gen-1", *, amount=10.0, period="day",
               currency="CNY", enabled=True) -> str:
    """直接落一行限额（跳过 upsert 的校验：有些用例要的就是"库里已经有一条怪限额"）。"""
    limit_id = f"lim-{scope}-{scope_id}"[:64]
    with db_session.session_scope() as sess:
        sess.add(ModelLimit(id=limit_id, scope=scope, scope_id=scope_id,
                            enabled=enabled, period=period, amount=amount,
                            currency=currency))
    return limit_id


def _row(scope="model", scope_id="gen-1"):
    with db_session.session_scope() as sess:
        return (sess.query(ModelLimit)
                .filter(ModelLimit.scope == scope, ModelLimit.scope_id == scope_id)
                .one())


def _usage(local_dt: datetime, cost: float | None, *, currency="CNY",
           config_id="gen-1", provider="deepseek", key_id=None):
    """插一行用量：`created_at` 存 UTC naive（全库约定），入参是本地时间。"""
    stamp = billing.to_utc(local_dt)
    with db_session.session_scope() as sess:
        sess.add(ModelUsage(
            model_config_id=config_id, model_name="m", provider=provider,
            model_type="generation", source="translate",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            provider_key_id=key_id, currency=currency, cost=cost,
            billing="none" if cost is None else "computed",
            created_at=stamp, day=local_dt.date(),
            hour_key=billing.hour_key(stamp),
        ))


def _config_dict(config_id="gen-1") -> dict:
    """走真实配置解析（含密钥链、单价链），与调用链看到的是同一份。"""
    from backend.services.model.model_manager import model_manager

    return model_manager.get_config(config_id)


# --------------------------------------------------------------------------
# ① 周期边界（复用 billing.period_bounds）
# --------------------------------------------------------------------------


def test_day_limit_ignores_yesterday(temp_db):
    """昨天花的钱不算进今天的限额（本地日边界，不是 UTC 日）。"""
    _vendor()
    _config()
    _raw_limit(amount=10.0, period="day")
    now = datetime(2026, 3, 10, 12, 0)
    _usage(datetime(2026, 3, 10, 9, 0), 4.0)          # 今天
    _usage(datetime(2026, 3, 9, 23, 59), 6.0)         # 昨天 23:59

    snap = limits.snapshot(_row(), now=now)

    assert snap["spent"] == 4.0
    assert snap["remaining"] == 6.0
    assert snap["period_start"] == "2026-03-10 00:00"
    assert snap["reset_at"] == "2026-03-11 00:00"


def test_week_boundary_splits_sunday_night_and_monday_morning(temp_db):
    """周日 23:59 属于上一周，周一 00:01 属于新的一周（周一是一周的开始）。"""
    _vendor()
    _config()
    _raw_limit(amount=100.0, period="week")
    sunday_late = datetime(2026, 3, 1, 23, 59)        # 周日
    monday_early = datetime(2026, 3, 2, 0, 1)         # 周一
    _usage(sunday_late, 7.0)
    _usage(monday_early, 5.0)

    this_week = limits.snapshot(_row(), now=datetime(2026, 3, 2, 12, 0))
    last_week = limits.snapshot(_row(), now=sunday_late)

    assert this_week["spent"] == 5.0
    assert this_week["period_start"] == "2026-03-02 00:00"
    assert last_week["spent"] == 7.0
    assert last_week["period_start"] == "2026-02-23 00:00"
    assert last_week["reset_at"] == "2026-03-02 00:00"


def test_month_limit_uses_calendar_month(temp_db):
    _vendor()
    _config()
    _raw_limit(amount=100.0, period="month")
    _usage(datetime(2026, 3, 31, 22, 0), 3.0)
    _usage(datetime(2026, 4, 1, 0, 30), 9.0)

    march = limits.snapshot(_row(), now=datetime(2026, 3, 31, 23, 0))
    april = limits.snapshot(_row(), now=datetime(2026, 4, 1, 1, 0))

    assert (march["spent"], march["period_start"]) == (3.0, "2026-03-01 00:00")
    assert (april["spent"], april["period_end"]) == (9.0, "2026-05-01 00:00")


# --------------------------------------------------------------------------
# ② 币种隔离
# --------------------------------------------------------------------------


def test_cny_limit_ignores_usd_rows_but_reports_them(temp_db):
    """CNY 限额不把 USD 行算进来 —— 但必须报出来（不能装作不存在）。"""
    _vendor()
    _config()
    _raw_limit(amount=10.0, currency="CNY")
    now = datetime(2026, 3, 10, 12, 0)
    _usage(datetime(2026, 3, 10, 9, 0), 3.0, currency="CNY")
    _usage(datetime(2026, 3, 10, 9, 30), 99.0, currency="USD")
    _usage(datetime(2026, 3, 10, 10, 0), None, currency="CNY")   # 未计费

    snap = limits.snapshot(_row(), now=now)

    assert snap["spent"] == 3.0            # 不是 102
    assert snap["foreign_currency_rows"] == 1
    assert snap["unpriced_rows"] == 1
    # 未计费 / 其它币种都不该被算进 remaining
    assert snap["remaining"] == 7.0


def test_blocked_message_says_local_limit_not_vendor(temp_db):
    """被拦的报错必须说「本地限额触发」，且 code 不是厂商口径的那种。"""
    _vendor()
    _config()
    _raw_limit(amount=1.0)
    _usage(datetime(2026, 3, 10, 9, 0), 5.0)

    with pytest.raises(LimitExceededError) as excinfo:
        limits.check_and_reserve(_config_dict(), estimate_amount=0.1,
                                 now=datetime(2026, 3, 10, 12, 0))

    exc = excinfo.value
    assert exc.http_status == 429
    assert exc.code == "LIMIT_EXCEEDED"
    assert "本地限额触发" in exc.message
    assert set(limits.SNAPSHOT_FIELDS) <= set(exc.detail["limit"])


# --------------------------------------------------------------------------
# ③ 三级并联：任一条用尽即拦
# --------------------------------------------------------------------------


def test_key_level_exhausted_blocks_even_when_model_level_has_room(temp_db):
    _vendor()
    _key()
    _config(key_mode="key", provider_key_id="key-1")
    _raw_limit("model", "gen-1", amount=100.0)          # 模型级还很宽
    _raw_limit("key", "key-1", amount=5.0)              # 密钥级快满
    _usage(datetime(2026, 3, 10, 9, 0), 4.5, key_id="key-1")

    cfg = _config_dict()
    assert [row["scope"] for row in limits.limits_for(cfg)] == ["key", "model"]

    with pytest.raises(LimitExceededError) as excinfo:
        limits.check_and_reserve(cfg, estimate_amount=1.0,
                                 now=datetime(2026, 3, 10, 12, 0))
    assert excinfo.value.detail["limit"]["scope"] == "key"


def test_model_level_exhausted_blocks_even_when_key_level_has_room(temp_db):
    _vendor()
    _key()
    _config(key_mode="key", provider_key_id="key-1")
    _raw_limit("model", "gen-1", amount=5.0)            # 模型级快满
    _raw_limit("key", "key-1", amount=100.0)            # 密钥级还很宽
    _usage(datetime(2026, 3, 10, 9, 0), 4.5, key_id="key-1")

    with pytest.raises(LimitExceededError) as excinfo:
        limits.check_and_reserve(_config_dict(), estimate_amount=1.0,
                                 now=datetime(2026, 3, 10, 12, 0))
    assert excinfo.value.detail["limit"]["scope"] == "model"


def test_vendor_level_accumulates_all_models_of_that_vendor(temp_db):
    _vendor()
    _config("gen-1")
    _config("gen-2")
    _raw_limit("vendor", "deepseek", amount=10.0)
    _usage(datetime(2026, 3, 10, 9, 0), 6.0, config_id="gen-1")
    _usage(datetime(2026, 3, 10, 9, 30), 3.5, config_id="gen-2")

    snap = limits.snapshot(_row("vendor", "deepseek"), now=datetime(2026, 3, 10, 12, 0))

    assert snap["spent"] == 9.5
    assert snap["label"] == "DeepSeek"
    with pytest.raises(LimitExceededError):
        limits.check_and_reserve(_config_dict("gen-2"), estimate_amount=1.0,
                                 now=datetime(2026, 3, 10, 12, 0))


def test_three_levels_do_not_share_one_total(temp_db):
    """同一笔费用只进各自作用域的账：别把三层加成一个共享总额。"""
    _vendor()
    _key()
    _config(key_mode="key", provider_key_id="key-1")
    _raw_limit("model", "gen-1", amount=100.0)
    _raw_limit("key", "key-1", amount=100.0)
    _raw_limit("vendor", "deepseek", amount=100.0)
    _usage(datetime(2026, 3, 10, 9, 0), 2.0, key_id="key-1")

    now = datetime(2026, 3, 10, 12, 0)
    spans = [limits.snapshot(row, now=now) for row in (
        _row("model", "gen-1"), _row("key", "key-1"), _row("vendor", "deepseek"))]

    assert [snap["spent"] for snap in spans] == [2.0, 2.0, 2.0]   # 各自 2.0，不是 6.0


# --------------------------------------------------------------------------
# ④ 未配单价的作用域不许设限额
# --------------------------------------------------------------------------


def test_put_rejects_scope_with_unpriced_model(temp_db):
    _vendor()
    _config("gen-1", price_in=None, price_out=None)     # 没配价

    with pytest.raises(ValidationError) as excinfo:
        limits.upsert({"scope": "model", "scope_id": "gen-1", "period": "day",
                       "amount": 10, "currency": "CNY"})

    assert excinfo.value.http_status == 400
    assert excinfo.value.detail["unpriced_models"] == [{"id": "gen-1", "name": "gen-1"}]
    assert "gen-1" in excinfo.value.message


def test_put_rejects_vendor_scope_when_any_child_model_is_unpriced(temp_db):
    _vendor()
    _config("gen-1")                                     # 有价
    _config("gen-2", price_in=None)                      # 半配价也算未配

    with pytest.raises(ValidationError) as excinfo:
        limits.upsert({"scope": "vendor", "scope_id": "deepseek", "period": "month",
                       "amount": 50, "currency": "CNY"})

    assert [item["id"] for item in excinfo.value.detail["unpriced_models"]] == ["gen-2"]


def test_put_rejects_key_scope_when_child_model_is_unpriced(temp_db):
    _vendor()
    _key()
    _config(key_mode="key", provider_key_id="key-1")
    _config("gen-2", key_mode="auto", price_in=None)     # auto → 用默认密钥 key-1

    with pytest.raises(ValidationError) as excinfo:
        limits.upsert({"scope": "key", "scope_id": "key-1", "period": "day",
                       "amount": 5, "currency": "CNY"})

    assert [item["id"] for item in excinfo.value.detail["unpriced_models"]] == ["gen-2"]


# --------------------------------------------------------------------------
# ⑤ 没限额 / 停用 = 放行
# --------------------------------------------------------------------------


def test_no_limit_and_disabled_limit_let_everything_through(temp_db):
    _vendor()
    _config()

    assert limits.check_and_reserve(_config_dict(), estimate_amount=10_000.0) is None

    _raw_limit(amount=0.5, enabled=False)               # 停用但留痕
    assert limits.check_and_reserve(_config_dict(), estimate_amount=10_000.0) is None

    assert limits.upsert({"scope": "model", "scope_id": "gen-1", "period": "none"}
                         ).get("deleted") is True
    assert limits.list_limits() == []


# --------------------------------------------------------------------------
# ⑥ 写入校验（400 路径）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("payload, reason", [
    ({"scope": "nope", "scope_id": "gen-1", "period": "day", "amount": 10}, "scope"),
    ({"scope": "model", "scope_id": "", "period": "day", "amount": 10}, "scope_id"),
    ({"scope": "model", "scope_id": "ghost", "period": "day", "amount": 10}, "不存在"),
    ({"scope": "model", "scope_id": "gen-1", "period": "year", "amount": 10}, "period"),
    ({"scope": "model", "scope_id": "gen-1", "period": "day", "amount": 0}, "amount"),
    ({"scope": "model", "scope_id": "gen-1", "period": "day", "amount": -1}, "amount"),
    ({"scope": "model", "scope_id": "gen-1", "period": "day", "amount": "abc"}, "amount"),
    ({"scope": "model", "scope_id": "gen-1", "period": "day", "amount": 10,
      "currency": "JPY"}, "CNY"),
])
def test_put_validates_every_field(temp_db, payload, reason):
    _vendor()
    _config()

    with pytest.raises(ValidationError) as excinfo:
        limits.upsert(payload)
    assert reason in (excinfo.value.message + str(excinfo.value.detail))


def test_put_upserts_single_row_per_scope(temp_db):
    """同一作用域只留一条：第二次 PUT 是更新，不是又插一条。"""
    _vendor()
    _config()

    first = limits.upsert({"scope": "model", "scope_id": "gen-1", "period": "day",
                           "amount": 10, "currency": "CNY", "note": "先这样"})
    second = limits.upsert({"scope": "model", "scope_id": "gen-1", "period": "week",
                            "amount": 30, "currency": "USD", "note": "改主意了"})

    assert first["id"] == second["id"]
    assert (second["period"], second["amount"], second["currency"]) == ("week", 30.0, "USD")
    with db_session.session_scope() as sess:
        assert sess.query(ModelLimit).count() == 1


def test_list_limits_reports_usable_and_real_period(temp_db):
    """列表里的 period 必须是真的（dict 行也要走同一套字段访问）。"""
    _vendor()
    _config()
    limits.upsert({"scope": "model", "scope_id": "gen-1", "period": "month",
                   "amount": 20.0, "currency": "CNY"})

    items = limits.list_limits()

    assert len(items) == 1
    assert items[0]["period"] == "month"
    assert items[0]["usable"] is True
    assert items[0]["label"] == "gen-1"
    assert items[0]["reset_at"].endswith("00:00")
    assert items[0]["period_start"].endswith("01 00:00")


# --------------------------------------------------------------------------
# ⑦ 并发：限额刚好只够 2 次
# --------------------------------------------------------------------------


def test_concurrent_checks_admit_exactly_two(temp_db):
    """8 个线程同时冲一条「只够 2 次」的限额：成功正好 2 个，其余全被拦。"""
    _vendor()
    _config()
    _raw_limit(amount=3.0, period="day")
    cfg = _config_dict()

    ok: list = []
    blocked: list = []
    unexpected: list = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            ok.append(limits.check_and_reserve(cfg, estimate_amount=1.0))
        except LimitExceededError as exc:
            blocked.append(exc)
        except Exception as exc:  # noqa: BLE001 —— 真出意外要看得见
            unexpected.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert unexpected == []
    assert len(ok) == 2
    assert len(blocked) == 6
    assert all(exc.code == "LIMIT_EXCEEDED" for exc in blocked)
    # 不变量：任何时刻占住的额度都不超过限额
    with db_session.session_scope() as sess:
        reserved = sess.query(func.coalesce(func.sum(ModelLimitReservation.amount), 0.0)).scalar()
    assert reserved <= 3.0

    for handle in ok:
        limits.release(handle)
    assert limits.check_and_reserve(cfg, estimate_amount=1.0) is not None


# --------------------------------------------------------------------------
# ⑧ 预留过期：不释放也不会永久占额
# --------------------------------------------------------------------------


def test_stale_reservation_stops_counting_after_expiry(temp_db):
    """进程被 kill 时没人执行 release —— 过期之后那笔占位必须自己消失。"""
    _vendor()
    _config()
    limit_id = _raw_limit(amount=3.0, period="day")
    cfg = _config_dict()
    now = datetime(2026, 3, 10, 12, 0)
    stamp = billing.to_utc(now)

    with db_session.session_scope() as sess:
        sess.add(ModelLimitReservation(
            id="rsv-stale", limit_id=limit_id, token="token-stale",
            amount=5.0, created_at=stamp,
            expires_at=stamp + timedelta(minutes=10)))

    with pytest.raises(LimitExceededError):
        limits.check_and_reserve(cfg, estimate_amount=1.0, now=now)

    with db_session.session_scope() as sess:
        sess.get(ModelLimitReservation, "rsv-stale").expires_at = stamp - timedelta(minutes=1)

    handle = limits.check_and_reserve(cfg, estimate_amount=1.0, now=now)
    assert handle is not None
    limits.release(handle)

    with db_session.session_scope() as sess:
        assert sess.query(ModelLimitReservation).count() == 0    # 顺手清理了


def test_release_is_safe_on_none_and_twice(temp_db):
    _vendor()
    _config()
    _raw_limit(amount=10.0)
    handle = limits.check_and_reserve(_config_dict(), estimate_amount=1.0)

    limits.release(handle)
    limits.release(handle)      # 再释放一次不该炸
    limits.release(None)


# --------------------------------------------------------------------------
# ⑨ blocked 记账
# --------------------------------------------------------------------------


def test_blocked_ask_is_recorded_as_unbilled_and_idempotent(temp_db):
    """被拦的那次问答落 blocked 行：cost 为 NULL、billing='none'、request_id 幂等。"""
    from backend.api.agent import _record_blocked

    _vendor()
    _config()
    cfg = _config_dict()

    _record_blocked(cfg, "conv-1", "msg-1")
    _record_blocked(cfg, "conv-1", "msg-1")      # 重放同一条提问

    with db_session.session_scope() as sess:
        rows = sess.query(ModelUsage).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.request_id == "ask-blocked:conv-1:msg-1"
    assert (row.status, row.error_code) == ("blocked", "LIMIT_EXCEEDED")
    assert row.cost is None and row.billing == "none"
    assert row.source == "ask"
    assert row.model_config_id == "gen-1"


def test_blocked_row_does_not_add_to_spent(temp_db):
    """blocked 行没有金额，因此不会反过来把限额推高（否则会自我强化）。"""
    _vendor()
    _config()
    _raw_limit(amount=10.0)
    from backend.api.agent import _record_blocked

    _record_blocked(_config_dict(), "conv-1", "msg-1")

    # blocked 行的 created_at 是**现在**：所以要拿现在去判周期，不能用写死的日期
    snap = limits.snapshot(_row(), now=billing.local_now())
    assert snap["spent"] == 0.0
    assert snap["unpriced_rows"] == 1


# --------------------------------------------------------------------------
# ⑩ summary 三态与 most_urgent
# --------------------------------------------------------------------------


def test_summary_is_empty_and_quiet_without_limits(temp_db):
    assert limits.summary() == {"level": "none", "most_urgent": None, "items": []}


def test_summary_walks_none_warn_exceeded(temp_db):
    _vendor()
    _config("gen-a")
    _config("gen-b")
    _config("gen-c")
    now = datetime(2026, 3, 10, 12, 0)
    for cid, amount in (("gen-a", 10.0), ("gen-b", 10.0), ("gen-c", 10.0)):
        _raw_limit("model", cid, amount=amount)
    _usage(datetime(2026, 3, 10, 9, 0), 1.0, config_id="gen-a")     # 10%
    _usage(datetime(2026, 3, 10, 9, 0), 8.5, config_id="gen-b")     # 85%
    _usage(datetime(2026, 3, 10, 9, 0), 12.0, config_id="gen-c")    # 120%

    out = limits.summary(now=now)
    assert out["level"] == "exceeded"
    assert out["most_urgent"]["scope_id"] == "gen-c"
    assert out["most_urgent"]["percent"] == 120.0

    with db_session.session_scope() as sess:
        sess.query(ModelLimit).filter(ModelLimit.scope_id == "gen-c").one().enabled = False
    out = limits.summary(now=now)
    assert out["level"] == "warn"
    assert out["most_urgent"]["scope_id"] == "gen-b"

    with db_session.session_scope() as sess:
        sess.query(ModelLimit).filter(ModelLimit.scope_id == "gen-b").one().enabled = False
    out = limits.summary(now=now)
    assert out["level"] == "none"
    assert out["most_urgent"]["scope_id"] == "gen-a"     # 停用的不参与报警


def test_summary_most_urgent_breaks_tie_by_reset_at(temp_db):
    """百分比并列时选周期更早结束的那条（它更快"翻篇"）。"""
    _vendor()
    _config("gen-day")
    _config("gen-month")
    now = datetime(2026, 3, 10, 12, 0)
    _raw_limit("model", "gen-day", amount=10.0, period="day")
    _raw_limit("model", "gen-month", amount=10.0, period="month")
    _usage(datetime(2026, 3, 10, 9, 0), 5.0, config_id="gen-day")
    _usage(datetime(2026, 3, 10, 9, 0), 5.0, config_id="gen-month")

    out = limits.summary(now=now)

    assert {item["percent"] for item in out["items"]} == {50.0}
    assert out["most_urgent"]["scope_id"] == "gen-day"
    assert out["most_urgent"]["reset_at"] == "2026-03-11 00:00"


# --------------------------------------------------------------------------
# ⑪ 接入点：model_manager 的生成代理真的会拦（不是只提供一个函数）
# --------------------------------------------------------------------------


def test_generation_path_is_gated_before_calling_provider(temp_db, monkeypatch):
    """限额在**发请求之前**生效：provider 的 generate 一次都不该被调到。"""
    from backend.providers.generation.mock import MockGeneration
    from backend.services.model.model_manager import model_manager

    _vendor()
    _config("gen-mock", provider="mock", model_name="mock-generation",
            price_in=1_000_000.0, price_out=1_000_000.0)
    _raw_limit("model", "gen-mock", amount=1.0)

    calls: list = []

    def _boom(self, prompt, *args, **kwargs):   # noqa: ANN001
        calls.append(prompt)
        raise AssertionError("被限额拦下的调用不该真的发到 provider")

    monkeypatch.setattr(MockGeneration, "generate", _boom)
    _usage(datetime(2026, 3, 10, 9, 0), 5.0, config_id="gen-mock", provider="mock")

    with pytest.raises(LimitExceededError):
        model_manager.get_generation("gen-mock").generate("你好")

    assert calls == []


def test_generation_path_passes_and_releases_reservations(temp_db):
    """正常调用：预留被释放（不泄漏），用量照旧落一行。"""
    from backend.services.model.model_manager import model_manager

    _vendor()
    _config("gen-mock", provider="mock", model_name="mock-generation")
    _raw_limit("model", "gen-mock", amount=10.0)

    result = model_manager.get_generation("gen-mock").generate("你好")

    assert result.text
    with db_session.session_scope() as sess:
        assert sess.query(ModelLimitReservation).count() == 0
        rows = sess.query(ModelUsage).filter(
            ModelUsage.model_config_id == "gen-mock").all()
    assert len(rows) == 1
    assert rows[0].cost is not None        # 配了单价 → 真的落了一笔金额
