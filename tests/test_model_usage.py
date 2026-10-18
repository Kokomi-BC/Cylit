"""模型 token 消耗统计（离线，临时 SQLite）：零填充 / 排序 / 空表 / days 夹取 / 写库失败不抛。

为什么不用真实库：这层是**纯聚合**，用真实 MySQL 跑只会让 CI 依赖外部服务；
临时 SQLite 走的是同一套 ORM 映射（``Date`` 列在两边都存成 ``YYYY-MM-DD``），
验的正是「天数连续、从旧到新、长度正好 days」这条契约。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.repositories.mysql import session as db_session
from backend.repositories.mysql.models import Base, ModelConfig, ModelUsage
from backend.services.model import usage_stats


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


def _today() -> date:
    return datetime.now().date()


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


def test_days_are_continuous_zero_filled_old_to_new(temp_db):
    today = _today()
    _add(today, prompt=10, completion=5)
    _add(today - timedelta(days=3), prompt=7, completion=1)

    out = usage_stats.stats(7)

    expected = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    assert [row["date"] for row in out["days"]] == expected
    assert len(out["days"]) == 7
    filled = {row["date"]: row for row in out["days"]}
    assert filled[today.isoformat()] == {"date": today.isoformat(), "prompt": 10,
                                         "completion": 5, "total": 15}
    old = (today - timedelta(days=3)).isoformat()
    assert filled[old]["total"] == 8
    # 没数据的那几天必须是 0，而不是缺行（前端按定宽画折线）
    zero_days = [d for d in expected if d not in (today.isoformat(), old)]
    assert zero_days and all(filled[d]["total"] == 0 for d in zero_days)

    assert out["totals"] == {"prompt": 17, "completion": 6, "total": 23, "requests": 2}
    assert out["today"] == {"date": today.isoformat(), "total": 15}
    assert out["range"] == {"days": 7, "from": expected[0], "to": today.isoformat()}


def test_by_model_aggregates_and_sorts_by_total_desc(temp_db):
    today = _today()
    with db_session.session_scope() as sess:
        sess.add(ModelConfig(id="gen-1", name="主力生成模型", provider="deepseek",
                             model_type="generation", model_name="deepseek-chat"))
    _add(today, config_id="gen-1", model_name="deepseek-chat", prompt=10, completion=10)
    _add(today, config_id="gen-1", model_name="deepseek-chat", prompt=5, completion=5)
    # 配置已被删掉的模型：反规范化字段还在，排行必须仍然显示它
    _add(today, config_id="gen-gone", model_name="qwen-plus", prompt=100, completion=20)
    _add(today - timedelta(days=1), config_id="gen-2", model_name="deepseek-flash",
         prompt=1, completion=1)

    out = usage_stats.stats(14)

    totals = [row["total"] for row in out["by_model"]]
    assert totals == sorted(totals, reverse=True)
    first = out["by_model"][0]
    assert first["model_config_id"] == "gen-gone"
    assert first["total"] == 120
    assert first["name"] == "qwen-plus"          # 配置没了 → 回落到 model_name

    by_id = {row["model_config_id"]: row for row in out["by_model"]}
    assert by_id["gen-1"]["name"] == "主力生成模型"   # 配置还在 → 用户起的显示名
    assert by_id["gen-1"]["total"] == 30            # 两次调用累加
    assert by_id["gen-2"]["total"] == 2


def test_empty_table_returns_zero_structure(temp_db):
    out = usage_stats.stats(14)

    assert len(out["days"]) == 14
    assert all(row["total"] == 0 for row in out["days"])
    assert out["by_model"] == []
    assert out["totals"] == {"prompt": 0, "completion": 0, "total": 0, "requests": 0}
    assert out["today"] == {"date": _today().isoformat(), "total": 0}
    assert out["range"]["days"] == 14


@pytest.mark.parametrize("given,expected", [
    (0, 1), (-5, 1), (1, 1), (30, 30), (90, 90), (1000, 90), ("abc", usage_stats.DEFAULT_DAYS),
    (None, usage_stats.DEFAULT_DAYS),
])
def test_days_is_clamped(temp_db, given, expected):
    out = usage_stats.stats(given)

    assert len(out["days"]) == expected
    assert out["range"]["days"] == expected
    assert out["days"][-1]["date"] == _today().isoformat()


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
