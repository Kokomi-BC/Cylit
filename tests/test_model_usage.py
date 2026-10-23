"""模型用量：写入路径（旁路、来源标记、代理不改变 provider 身份）与查询契约。

⚠️ 统计口径的细节用例已整体搬到 tests/test_model_billing.py（单价 / 缓存 / 币种 /
周期 / 分桶 / 幂等），这里只保留**写入侧**的行为契约，避免两处各写一遍而互相漂移。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.repositories.mysql import session as db_session
from backend.repositories.mysql.models import Base, ModelConfig, ModelUsage
from backend.services.model import billing, usage_stats


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把全局 Engine / Session 换成临时 SQLite（不碰真实库）。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'usage.sqlite3').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                           expire_on_commit=False)
    monkeypatch.setattr(db_session, "_engine", engine)
    monkeypatch.setattr(db_session, "_session_factory", factory)
    try:
        yield engine
    finally:
        engine.dispose()


def _add(day, *, config_id="gen-1", model_name="deepseek-flash",
         prompt=0, completion=0, total=None, source="translate") -> None:
    with db_session.session_scope() as sess:
        sess.add(ModelUsage(
            model_config_id=config_id, model_name=model_name, provider="deepseek",
            model_type="generation", source=source,
            prompt_tokens=prompt, completion_tokens=completion,
            total_tokens=(prompt + completion) if total is None else total,
            created_at=datetime(2026, 2, 1, 3, 0), day=day,
        ))


def test_window_points_are_continuous_zero_filled_old_to_new(temp_db):
    """新契约：window.points 连续、从旧到新、长度正好桶数（旧 days/by_model 已下线）。"""
    today = date.fromisoformat(billing.local_day())
    old = today - timedelta(days=3)
    _add(today, prompt=10, completion=5)
    _add(old, prompt=7, completion=1)

    out = usage_stats.stats("7d")

    keys = [p["key"] for p in out["window"]["points"]]
    assert keys == [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    assert "days" not in out and "by_model" not in out and "today" not in out
    # points 不带自己的 key：它按位置与 window.points 对齐（前端只需一套横轴）
    filled = dict(zip(keys, out["series"][0]["points"]))
    assert filled[today.isoformat()]["tokens_total"] == 15
    assert filled[old.isoformat()]["tokens_total"] == 8
    zero_days = [k for k in keys if k not in (today.isoformat(), old.isoformat())]
    assert zero_days and all(filled[k]["tokens_total"] == 0 for k in zero_days)
    assert out["totals"] == {"tokens_in": 17, "tokens_out": 6, "tokens_total": 23,
                             "requests": 2, "cost": {}, "unpriced": 2}


def test_empty_table_returns_zero_structure(temp_db):
    out = usage_stats.stats("14d")

    assert len(out["window"]["points"]) == 14
    assert out["series"] == []
    assert out["totals"]["requests"] == 0
    assert out["window"]["to"] == billing.local_day()


def _config(config_id, name, *, price_in=None, price_out=None) -> None:
    from backend.repositories.mysql.models import ModelConfig

    with db_session.session_scope() as sess:
        sess.add(ModelConfig(id=config_id, name=name, provider="deepseek",
                             model_type="generation", model_name="deepseek-flash",
                             price_in=price_in, price_out=price_out))


def test_same_custom_name_merges_inside_hour_buckets(temp_db):
    """24h 仍是小时桶，且同名合并发生在装配阶段：两条配置的那笔落进**同一个桶**。

    这条是"分桶不许变"的护栏的另一半：合并既要加总，又不能把点挪到别的桶里。
    """
    stamp = billing.to_utc(billing.local_now() - timedelta(hours=2))
    hour_key = billing.hour_key(stamp)
    _config("gen-a", "同名小时档")
    _config("gen-b", "同名小时档")
    with db_session.session_scope() as sess:
        for cid, tokens in (("gen-a", 10), ("gen-b", 5)):
            sess.add(ModelUsage(
                model_config_id=cid, model_name="deepseek-flash", provider="deepseek",
                source="translate", prompt_tokens=tokens, completion_tokens=0,
                total_tokens=tokens, currency="CNY", cost=None, billing="none",
                created_at=stamp, day=date.fromisoformat(billing.local_day(stamp)),
                hour_key=hour_key))

    out = usage_stats.stats("24h")

    assert len(out["series"]) == 1
    series = out["series"][0]
    assert series["key"] == "name:deepseek/同名小时档"
    assert series["model_config_ids"] == ["gen-a", "gen-b"]
    assert series["totals"]["tokens_total"] == 15
    assert series["totals"]["unpriced"] == 2          # 两条都没配价，各自记一次
    assert len(series["points"]) == 24
    index = [p["key"] for p in out["window"]["points"]].index(hour_key)
    assert series["points"][index]["tokens_total"] == 15
    assert sum(p["requests"] for p in series["points"]) == 2


def test_same_name_on_two_providers_stays_two_series_in_hour_window(temp_db):
    """24h 小时桶里也一样：同名**同厂商**合并，同名**不同厂商**是两条系列。

    分桶与合并不许互相干扰：同名不同厂商的两笔各自落在自己的桶里，
    合并只发生在 (名称, 厂商) 相同的那些行之间。
    """
    stamp = billing.to_utc(billing.local_now() - timedelta(hours=2))
    hour_key = billing.hour_key(stamp)
    with db_session.session_scope() as sess:
        for cid, name, pid, tokens in (("gen-a", "跨厂商小时档", "deepseek", 10),
                                       ("gen-b", "跨厂商小时档", "deepseek", 5),
                                       ("gen-c", "跨厂商小时档", "openai", 7)):
            sess.add(ModelConfig(id=cid, name=name, provider=pid,
                                 model_type="generation", model_name="same-upstream"))
            sess.add(ModelUsage(
                model_config_id=cid, model_name="same-upstream", provider=pid,
                source="translate", prompt_tokens=tokens, completion_tokens=0,
                total_tokens=tokens, currency="CNY", cost=None, billing="none",
                created_at=stamp, day=date.fromisoformat(billing.local_day(stamp)),
                hour_key=hour_key))

    out = usage_stats.stats("24h")

    by_key = {row["key"]: row for row in out["series"]}
    assert sorted(by_key) == ["name:deepseek/跨厂商小时档", "name:openai/跨厂商小时档"]
    ds, oa = by_key["name:deepseek/跨厂商小时档"], by_key["name:openai/跨厂商小时档"]
    assert ds["model_config_ids"] == ["gen-a", "gen-b"]   # 同名同厂商 → 合并
    assert ds["totals"]["tokens_total"] == 15
    assert oa["provider"] == "openai" and oa["providers"] == ["openai"]
    assert oa["model_config_ids"] == ["gen-c"]            # 同名不同厂商 → 分开
    assert oa["totals"]["tokens_total"] == 7
    index = [p["key"] for p in out["window"]["points"]].index(hour_key)
    assert ds["points"][index]["tokens_total"] == 15      # 合并进同一个小时桶
    assert oa["points"][index]["tokens_total"] == 7


def test_record_swallows_db_failure(temp_db, monkeypatch):
    """写库炸了也只能记日志：统计是旁路，绝不能让一次 LLM 调用失败。"""

    def boom():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db_session, "session_scope", boom)

    assert usage_stats.record(model_config_id="gen-1", prompt_tokens=1,
                              completion_tokens=2) is False   # 不抛，返回 False
    # 取属性的路径也不能抛（代理里传进来的可能是任意对象）
    assert usage_stats.record_result({"id": "gen-1"}, object()) is False


def test_record_uses_scope_defaulting_to_generation(temp_db):
    assert usage_stats.current_source() == usage_stats.DEFAULT_SOURCE
    with usage_stats.usage_scope("translate"):
        assert usage_stats.record(prompt_tokens=1, completion_tokens=1) is True
    # 退出作用域后要回到默认口径（contextvar 必须被 reset，不能一路带着走）
    assert usage_stats.record(prompt_tokens=1, completion_tokens=1) is True
    assert usage_stats.current_source() == usage_stats.DEFAULT_SOURCE

    with db_session.session_scope() as sess:
        sources = sorted(row.source for row in sess.query(ModelUsage).all())
    assert sources == ["generation", "translate"]


def test_generation_proxy_records_each_call_without_changing_the_provider(temp_db, monkeypatch):
    """记录代理：每次调用落一条，且**不改变** provider 的身份与能力判定。"""
    from backend.providers.base import GenerationProvider, GenerationResult
    from backend.services.model.model_manager import _with_usage

    class _TextOnly(GenerationProvider):
        def generate(self, prompt, *, system=None, temperature=None, max_tokens=None, **kw):
            return GenerationResult(text="ok", model="fake-1", prompt_tokens=3,
                                    completion_tokens=4)

        def structured(self, prompt, schema, *, system=None, **kw):
            # 真实 provider 都是这样实现的：structured 内部调 self.generate
            return {"text": self.generate(prompt, system=system).text}

    seen: list[tuple] = []
    monkeypatch.setattr(usage_stats, "record_result",
                        lambda cfg, res, **kw: seen.append((cfg.get("id"), res.total_tokens)))

    provider = _TextOnly({"id": "gen-1", "provider": "fake", "model_name": "fake-1",
                          "model_type": "generation"})
    proxy = _with_usage(provider)

    assert isinstance(proxy, _TextOnly)              # 业务侧的类型判断不能被代理打破
    assert proxy.supports_vision is False            # 不支持图片的 provider 不能"看起来支持"
    assert proxy.model_name == "fake-1"              # 状态与配置共享同一份

    assert proxy.generate("hi").text == "ok"
    proxy.structured("hi", {"a": {"type": "string"}})   # 内部那次 generate 也要落账
    assert seen == [("gen-1", 7), ("gen-1", 7)]
