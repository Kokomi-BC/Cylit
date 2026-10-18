"""模型 token 消耗统计：写入（旁路，永不抛）+ 查询（设置页那四块数据）。

为什么单独一张表（``model_usage``）而不是从 ``task.stats_json`` 或消息表里凑：
翻译 / 理解 / 元数据 / 问答四条链路的入参口径完全不同，唯一共同的只有 provider 返回的 usage；
而任务会被清理、会被重跑覆盖，按天折线就没法对账。模型配置会被删改，所以
``model_name`` / ``provider`` / ``model_type`` 反规范化存一份 —— 删掉一个模型，
历史消耗仍要出现在排行榜里（否则「总消耗」会随删除凭空缩水）。

口径：``day``（按天聚合的键）取**本地日** —— 用户嘴里的「今日消耗」是他那一整天，
不是 UTC 的那一天（``created_at`` 仍按全库约定存 UTC naive）。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from backend.core.logging import get_logger
from backend.core.utils import now

log = get_logger(__name__)

#: 没设 ``usage_scope`` 时的来源标签（业务侧忘记标也不要丢数据）
DEFAULT_SOURCE = "generation"
#: 默认时间窗（前端不传 days 时）
DEFAULT_DAYS = 14
MIN_DAYS = 1
MAX_DAYS = 90

#: 当前链路的来源标签。用 contextvar 而不是参数：``record`` 是在 provider 代理里被调用的，
#: 那里拿不到业务上下文；而「标一次、整条链路都算对」只需要一个隐式变量。
_source: contextvars.ContextVar[str] = contextvars.ContextVar("model_usage_source", default="")


@contextmanager
def usage_scope(source: str) -> Iterator[None]:
    """标记这段调用链的来源（``with usage_scope("translate"): ...``）。

    ⚠️ contextvar **不跨线程**：翻译的页级调用跑在调度器的线程池里，所以口径要在线程内设置
    （见 ``translation_service.run_path_task`` 里的 ``scoped_process``），任务入口设一次不算数。
    """
    token = _source.set(str(source or DEFAULT_SOURCE))
    try:
        yield
    finally:
        _source.reset(token)


def current_source() -> str:
    return _source.get() or DEFAULT_SOURCE


def clamp_days(days: Any) -> int:
    # 非法值一律回默认窗口，不给 500
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


def _today() -> date:
    return datetime.now().date()


def _day_key(value: Any) -> str:
    # 哪天：DB 里可能是 date / datetime / 字符串（SQLite 与 MySQL 返回类型不一样）
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def record(*,
           model_config_id: str | None = None,
           model_name: str | None = None,
           provider: str | None = None,
           model_type: str | None = None,
           source: str | None = None,
           prompt_tokens: int = 0,
           completion_tokens: int = 0,
           total_tokens: int | None = None) -> bool:
    """落一行用量；返回是否写入成功。

    **任何异常都必须吞掉**：统计是旁路，绝不能让一次 LLM 调用因为写库失败而失败
    （反例就是「任务跑完了、结果也在，但账单查询把整个请求带崩了」）。
    """
    try:
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        if total_tokens is None:
            total = prompt + completion
        else:
            # 厂商口径偶尔漏算 / 只给 total：以两项之和兜底，避免 total < prompt+completion
            total = max(max(0, int(total_tokens)), prompt + completion)

        from backend.repositories.mysql.models import ModelUsage
        from backend.repositories.mysql.session import session_scope

        stamp = now()               # created_at：全库约定 UTC naive
        day = _today()              # 聚合键：本地日
        with session_scope() as sess:
            sess.add(ModelUsage(
                model_config_id=str(model_config_id) if model_config_id else None,
                model_name=str(model_name)[:128] if model_name else None,
                provider=str(provider)[:32] if provider else None,
                model_type=str(model_type)[:16] if model_type else None,
                source=(str(source or current_source())[:32] or DEFAULT_SOURCE),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                created_at=stamp,
                day=day,
            ))
        return True
    except Exception as exc:  # noqa: BLE001 —— 统计失败绝不能连累这次调用
        log.warning("模型用量记录失败（已忽略，不影响本次调用）：%s", str(exc)[:200])
        return False


def record_result(config: dict | None, result: Any, *, source: str | None = None) -> bool:
    """从 provider 的 ``GenerationResult`` 落一条（``model_manager`` 的记录代理用它）。"""
    cfg = config or {}
    try:
        return record(
            model_config_id=cfg.get("id"),
            model_name=cfg.get("model_name") or getattr(result, "model", "") or None,
            provider=cfg.get("provider"),
            model_type=cfg.get("model_type"),
            source=source,
            prompt_tokens=getattr(result, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(result, "completion_tokens", 0) or 0,
        )
    except Exception as exc:  # noqa: BLE001 —— 同上：连取属性都不许把调用带崩
        log.warning("模型用量记录失败（已忽略，不影响本次调用）：%s", str(exc)[:200])
        return False


def stats(days: int = DEFAULT_DAYS) -> dict:
    """按天折线 + 分模型排行 + 合计 + 今日（契约见 ``GET /models/usage/stats``）。

    ``days`` 连续零填充、从旧到新、长度正好 ``days``；``by_model`` 按 total 降序。
    查询失败时**返回全零结构**而不是 500 —— 设置页少一块图，比整页报错好。
    """
    n = clamp_days(days)
    end = _today()
    start = end - timedelta(days=n - 1)
    curve = [{
        "date": (start + timedelta(days=i)).isoformat(),
        "prompt": 0, "completion": 0, "total": 0,
} for i in range(n)]
    index = {row["date"]: row for row in curve}
    by_model: list[dict] = []
    requests = 0

    try:
        from sqlalchemy import func

        from backend.repositories.mysql.models import ModelConfig, ModelUsage
        from backend.repositories.mysql.session import session_scope

        window = (ModelUsage.day >= start, ModelUsage.day <= end)
        with session_scope() as sess:
            day_rows = sess.query(
                ModelUsage.day,
                func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
                func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
                func.count(ModelUsage.id),
            ).filter(*window).group_by(ModelUsage.day).all()
            for day_value, prompt, completion, total, count in day_rows:
                row = index.get(_day_key(day_value))
                if row is None:
                    continue
                row["prompt"] = int(prompt or 0)
                row["completion"] = int(completion or 0)
                row["total"] = int(total or 0)
                requests += int(count or 0)

            grouped = sess.query(
                ModelUsage.model_config_id, ModelUsage.model_name,
                func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
                func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
                func.coalesce(func.sum(ModelUsage.total_tokens), 0),
            ).filter(*window).group_by(
                ModelUsage.model_config_id, ModelUsage.model_name).all()
            wanted = {str(g[0]) for g in grouped if g[0]}
            display: dict[str, str] = {}
            if wanted:
                display = {r.id: r.name for r in sess.query(ModelConfig).filter(
                    ModelConfig.id.in_(wanted)).all()}
            for config_id, model_name, prompt, completion, total in grouped:
                key = str(config_id) if config_id else ""
                by_model.append({
                    "model_config_id": key or None,
                    "name": display.get(key) or (model_name or "") or "未知模型",
                    "model_name": model_name or "",
                    "prompt": int(prompt or 0),
                    "completion": int(completion or 0),
                    "total": int(total or 0),
                })
    except Exception as exc:  # noqa: BLE001 —— 查询失败降级成全零，不让设置页 500
        log.warning("模型用量统计查询失败（返回空统计）：%s", str(exc)[:200])
        by_model = []

    by_model.sort(key=lambda r: (-r["total"], str(r["name"]), r["model_name"]))

    totals = {
        "prompt": sum(r["prompt"] for r in curve),
        "completion": sum(r["completion"] for r in curve),
        "total": sum(r["total"] for r in curve),
        "requests": requests,
}
    today_row = index.get(end.isoformat()) or {"total": 0}
    return {
        "days": curve,
        "by_model": by_model,
        "totals": totals,
        "today": {"date": end.isoformat(), "total": int(today_row["total"])},
        "range": {"days": n, "from": start.isoformat(), "to": end.isoformat()},
}
