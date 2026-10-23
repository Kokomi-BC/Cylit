"""模型 token 消耗统计：写入（旁路，永不抛）+ 查询（多维、可分桶、可筛选）。

为什么单独一张表（model_usage）而不是从 task.stats_json 或消息表里凑：
翻译 / 理解 / 元数据 / 问答四条链路的入参口径完全不同，唯一共同的只有 provider 返回的 usage；
而任务会被清理、会被重跑覆盖，按天折线就没法对账。模型配置会被删改，所以
model_name / provider / model_type 反规范化存一份 —— 删掉一个模型，
历史消耗仍要出现在排行与折线里（否则「总消耗」会随删除凭空缩水）。

口径（三条，别再各写一套）：

· created_at 是全库约定的 **UTC naive**；day 与 hour_key 是**本地日 / 本地小时**
  （用户嘴里的「今日消耗」是他那一整天，不是 UTC 的那一天）；
· 金额与币种**写在行上**（cost / currency / billing）：单价明天改了，今天这笔账
  也不能跟着变 —— 记账那一刻的金额是事实，事后按单价重算是估算；
· 缺单价 = **未计费**（cost 为 NULL、billing='none'），绝不写 0。

查询契约见 GET /models/usage/stats（backend/api/models.py）：window / group /
vendor / model 四个入参，返回 window + series + filters + totals。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from backend.core.logging import get_logger
from backend.core.utils import now as utc_now
from backend.services.model import billing

log = get_logger(__name__)

#: 没设 usage_scope 时的来源标签（业务侧忘记标也不要丢数据）
DEFAULT_SOURCE = "generation"

#: 时间窗 → (分桶粒度, 桶数)。24h 必须是**小时桶**：把日数据冒充小时，
#: 折线看起来会像"一整天的量全堆在某一小时"，那是口径错，不是事故。
WINDOWS: dict[str, tuple[str, int]] = {
    "24h": ("hour", 24),
    "7d": ("day", 7),
    "14d": ("day", 14),
    "30d": ("day", 30),
}
DEFAULT_WINDOW = "7d"
#: 分组维度：按模型（cfg:<id> / gone:<model_name>）或按厂商（vendor:<id>）
GROUPS = ("model", "vendor")
ALL = "all"

#: 当前链路的来源标签。用 contextvar 而不是参数：record 是在 provider 代理里被调用的，
#: 那里拿不到业务上下文；而「标一次、整条链路都算对」只需要一个隐式变量。
_source: contextvars.ContextVar[str] = contextvars.ContextVar("model_usage_source", default="")


@contextmanager
def usage_scope(source: str) -> Iterator[None]:
    """标记这段调用链的来源（with usage_scope("translate"): ...）。

    ⚠️ contextvar **不跨线程**：翻译的页级调用跑在调度器的线程池里，所以口径要在线程内设置
    （见 translation_service.run_path_task 里的 scoped_process），任务入口设一次不算数。
    """
    token = _source.set(str(source or DEFAULT_SOURCE))
    try:
        yield
    finally:
        _source.reset(token)


def current_source() -> str:
    return _source.get() or DEFAULT_SOURCE


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record(*,
           model_config_id: str | None = None,
           model_name: str | None = None,
           provider: str | None = None,
           model_type: str | None = None,
           source: str | None = None,
           prompt_tokens: int = 0,
           completion_tokens: int = 0,
           total_tokens: int | None = None,
           provider_key_id: str | None = None,
           request_id: str | None = None,
           cache_read_tokens: int = 0,
           cache_write_tokens: int = 0,
           status: str = "ok",
           error_code: str | None = None,
           reported_cost: float | None = None,
           reported_currency: str | None = None,
           no_charge: bool = False,
           at: datetime | None = None) -> bool:
    """落一行用量；返回是否**真的写入**（去重命中 / 出错都返回 False）。

    调用方**不用传钱**：单价按 model_config_id 现查（billing.price_of），
    于是"改价"与"记账"不会在别处各算一遍。

    **任何异常都必须吞掉**：统计是旁路，绝不能让一次 LLM 调用因为写库失败而失败
    （反例就是「任务跑完了、结果也在，但账单查询把整个请求带崩」）。

    幂等：给了 request_id 就只认第一行（重试 / 重放 / 双写都不会记成两笔账）。

    `no_charge=True` 是给「被限额拦下」那类行用的：那次调用**根本没发出去**，
    没有 token、没有钱，所以 `cost` 必须是 NULL、`billing='none'`。
    这条路上**不能**调 `cost_of` —— 它会按 token 算出一个金额，而那笔钱没花。

    `at` 是**这次调用的时刻**（默认"现在"）：钱按**那一刻**的时段算（峰谷），
    `created_at` / `day` / `hour_key` 也一起按它定 —— 否则一行会"按今天的高峰价计费、
    记在昨天的桶里"，回看折线就永远对不上。测试与补录历史都靠它。
    """
    try:
        prompt = _int(prompt_tokens)
        completion = _int(completion_tokens)
        cache_read = _int(cache_read_tokens)
        cache_write = _int(cache_write_tokens)
        if total_tokens is None:
            total = prompt + completion
        else:
            # 厂商口径偶尔漏算 / 只给 total：以两项之和兜底，避免 total < prompt + completion
            total = max(_int(total_tokens), prompt + completion)

        from backend.repositories.mysql.models import ModelUsage
        from backend.repositories.mysql.session import session_scope

        # created_at：全库约定 UTC naive。给了 at 就用它（补录 / 测试），否则"现在"。
        stamp = utc_now() if at is None else billing.to_utc(at)
        rid = str(request_id).strip()[:64] if request_id else None
        with session_scope() as sess:
            if rid and sess.query(ModelUsage.id).filter(
                    ModelUsage.request_id == rid).first() is not None:
                log.debug("用量已记过（request_id 去重）：%s", rid)
                return False
            # 同一事务里取单价（**按这次调用的时刻**取，峰谷才对得上）：只多一次主键查询，
            # 换来"金额与落库永远一致"
            # at 原样透传：naive 就是**本地墙钟**（见 billing.wall_clock），
            # 而 stamp 是已换算好的 UTC —— 拿 stamp 去判时段会整体偏 8 小时。
            prices = billing.price_of(model_config_id, sess=sess, at=at)
            if no_charge:
                # 被限额拦下 / 其它「没真花出去」的行：金额存 NULL，绝不写 0
                money = {
                    "cost": None,
                    "currency": (billing.normalize_currency(prices.get("currency"))
                                 or billing.DEFAULT_CURRENCY),
                    "billing": billing.BILLING_NONE,
                }
            else:
                money = billing.cost_of(prices, prompt, completion,
                                        cache_read=cache_read,
                                        cache_write=cache_write,
                                        reported=reported_cost,
                                        reported_currency=reported_currency,
                                        at=at)
            sess.add(ModelUsage(
                model_config_id=str(model_config_id) if model_config_id else None,
                model_name=str(model_name)[:128] if model_name else None,
                provider=str(provider)[:32] if provider else None,
                model_type=str(model_type)[:16] if model_type else None,
                source=(str(source or current_source())[:32] or DEFAULT_SOURCE),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                request_id=rid,
                provider_key_id=(str(provider_key_id)[:64] if provider_key_id else None),
                currency=money["currency"],
                cost=money["cost"],
                billing=money["billing"],
                # 「这笔钱按哪一段时段算的」必须可追溯：没命中时段（含周末）= NULL
                price_rule_id=(None if no_charge else prices.get("rule_id")),
                status=(str(status or "ok")[:16] or "ok"),
                error_code=(str(error_code)[:64] if error_code else None),
                created_at=stamp,
                day=date.fromisoformat(billing.local_day(stamp)),
                hour_key=billing.hour_key(stamp),
            ))
        return True
    except Exception as exc:  # noqa: BLE001 —— 统计失败绝不能连累这次调用
        # 唯一索引撞车 = 并发下的"已记过"，是幂等的正常路径，不该刷警告
        if type(exc).__name__ == "IntegrityError":
            log.debug("用量已记过（唯一键去重）：%s", str(exc)[:120])
            return False
        log.warning("模型用量记录失败（已忽略，不影响本次调用）：%s", str(exc)[:200])
        return False


def record_result(config: dict | None, result: Any, *, source: str | None = None) -> bool:
    """从 provider 的 GenerationResult 落一条（model_manager 的记录代理用它）。"""
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
            cache_read_tokens=getattr(result, "cache_read_tokens", 0) or 0,
            cache_write_tokens=getattr(result, "cache_write_tokens", 0) or 0,
        )
    except Exception as exc:  # noqa: BLE001 —— 同上：连取属性都不许把调用带崩
        log.warning("模型用量记录失败（已忽略，不影响本次调用）：%s", str(exc)[:200])
        return False


# --------------------------------------------------------------------------
# 查询：常量与小工具
# --------------------------------------------------------------------------


def normalize_window(window: Any) -> str:
    """非法窗口一律回默认（前端传错不该换来 500）。"""
    key = str(window or "").strip().lower()
    return key if key in WINDOWS else DEFAULT_WINDOW


def _zero_totals() -> dict:
    return {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
            "requests": 0, "cost": {}, "unpriced": 0}


def _add_tokens(bucket: dict, tokens_in: int, tokens_out: int, tokens_total: int,
                requests: int) -> None:
    bucket["tokens_in"] += tokens_in
    bucket["tokens_out"] += tokens_out
    bucket["tokens_total"] += tokens_total
    bucket["requests"] += requests


def _amount(bucket: dict, currency: Any, amount: float) -> None:
    """把一笔金额累加进 cost（**按币种分开**，人民币与美元永不直接相加）。"""
    key = str(currency or "").strip().upper() or billing.DEFAULT_CURRENCY
    bucket["cost"][key] = round(bucket["cost"].get(key, 0.0) + float(amount),
                                billing.COST_PRECISION)


def _merge(target: dict, source: dict) -> dict:
    _add_tokens(target, source["tokens_in"], source["tokens_out"],
                source["tokens_total"], source["requests"])
    target["unpriced"] += source["unpriced"]
    for currency, amount in source["cost"].items():
        target["cost"][currency] = round(target["cost"].get(currency, 0.0) + amount,
                                        billing.COST_PRECISION)
    return target


def _parse_filter(value: Any) -> set[str] | None:
    """all / 空 → None（不筛）；否则是逗号分隔的取值集合。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == ALL:
        return None
    return {part.strip() for part in text.split(",") if part.strip()}


def _buckets(granularity: str, count: int) -> list[dict]:
    """连续、从旧到新、长度正好 count 的桶（含 key 与 label）。"""
    if granularity == "hour":
        end = billing.local_now().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=count - 1)
        return [{"key": (start + timedelta(hours=i)).strftime("%Y-%m-%d %H"),
                 "label": (start + timedelta(hours=i)).strftime("%H:00")}
                for i in range(count)]
    end_day = date.fromisoformat(billing.local_day())
    start_day = end_day - timedelta(days=count - 1)
    return [{"key": (start_day + timedelta(days=i)).isoformat(),
             "label": (start_day + timedelta(days=i)).strftime("%m/%d")}
            for i in range(count)]


# --------------------------------------------------------------------------
# 查询：聚合
# --------------------------------------------------------------------------


def _load_cells(sess, granularity: str, buckets: list[dict]) -> dict[tuple, dict[str, dict]]:
    """把窗口内的行聚成 {(config_id, model_name, provider): {bucket_key: cell}}。

    身份（cfg: / gone: / vendor:）**不在这里定** —— 那要先知道配置还在不在，
    所以这里只按行上的原始字段分组，身份留给 _build_models。

    小时窗按 created_at 现算：hour_key 是后加的列，老行是 NULL，
    不能因为"升级前那 24 小时"就静默少一段；天窗按 day 交给 SQL 聚合（30 天量级更大）。
    """
    from sqlalchemy import func

    from backend.repositories.mysql.models import ModelUsage

    out: dict[tuple, dict[str, dict]] = {}
    wanted = {b["key"] for b in buckets}

    def _cell(raw: tuple, bucket_key: str) -> dict:
        return out.setdefault(raw, {}).setdefault(bucket_key, _zero_totals())

    if granularity == "hour":
        start_local = datetime.strptime(buckets[0]["key"], "%Y-%m-%d %H")
        end_local = (datetime.strptime(buckets[-1]["key"], "%Y-%m-%d %H")
                     + timedelta(hours=1))
        rows = sess.query(
            ModelUsage.created_at, ModelUsage.model_config_id, ModelUsage.model_name,
            ModelUsage.provider, ModelUsage.currency, ModelUsage.cost,
            ModelUsage.prompt_tokens, ModelUsage.completion_tokens,
            ModelUsage.total_tokens,
        ).filter(
            ModelUsage.created_at >= billing.to_utc(start_local),
            ModelUsage.created_at < billing.to_utc(end_local),
        ).all()
        for (created_at, config_id, model_name, provider, currency, cost,
             tokens_in, tokens_out, tokens_total) in rows:
            bucket_key = billing.hour_key(created_at)
            if bucket_key not in wanted:
                continue
            cell = _cell((config_id, model_name, provider), bucket_key)
            _add_tokens(cell, _int(tokens_in), _int(tokens_out), _int(tokens_total), 1)
            if cost is None:
                cell["unpriced"] += 1
            else:
                _amount(cell, currency, cost)
        return out

    start_day = date.fromisoformat(buckets[0]["key"])
    end_day = date.fromisoformat(buckets[-1]["key"])
    rows = sess.query(
        ModelUsage.day, ModelUsage.model_config_id, ModelUsage.model_name,
        ModelUsage.provider, ModelUsage.currency,
        func.coalesce(func.sum(ModelUsage.prompt_tokens), 0),
        func.coalesce(func.sum(ModelUsage.completion_tokens), 0),
        func.coalesce(func.sum(ModelUsage.total_tokens), 0),
        func.count(ModelUsage.id),
        func.count(ModelUsage.cost),
        func.sum(ModelUsage.cost),
    ).filter(
        ModelUsage.day >= start_day, ModelUsage.day <= end_day,
    ).group_by(
        ModelUsage.day, ModelUsage.model_config_id, ModelUsage.model_name,
        ModelUsage.provider, ModelUsage.currency,
    ).all()
    for (day_value, config_id, model_name, provider, currency, tokens_in, tokens_out,
         tokens_total, requests, priced_rows, cost_sum) in rows:
        bucket_key = str(day_value)[:10]
        if bucket_key not in wanted:
            continue
        cell = _cell((config_id, model_name, provider), bucket_key)
        _add_tokens(cell, _int(tokens_in), _int(tokens_out), _int(tokens_total),
                    _int(requests))
        # 未计费 = 该桶里 cost 为 NULL 的行数；金额只累加非 NULL 的那些
        cell["unpriced"] += max(0, _int(requests) - _int(priced_rows))
        if cost_sum is not None:
            _amount(cell, currency, cost_sum)
    return out


def _load_meta(sess, config_ids: set[str]) -> tuple[dict, dict, dict]:
    """一次把「还活着的配置」「厂商行（含显示名与币种）」「单价」取齐。"""
    from backend.repositories.mysql.models import ModelConfig, ModelProvider

    ids = {str(cid) for cid in config_ids if cid}
    configs = {r.id: r for r in sess.query(ModelConfig).filter(
        ModelConfig.id.in_(ids)).all()} if ids else {}
    providers = {r.id: r for r in sess.query(ModelProvider).all()}
    prices = {cid: billing.price_of(cid, sess=sess) for cid in ids}
    return configs, providers, prices


def _provider_label(provider_id: Any, providers: dict) -> str:
    pid = str(provider_id or "")
    row = providers.get(pid)
    label = getattr(row, "label", None) if row else None
    return label or pid or "未知厂商"


def _provider_currency(provider_id: Any, providers: dict) -> str:
    row = providers.get(str(provider_id or ""))
    return (billing.normalize_currency(getattr(row, "currency", None) if row else None)
            or billing.DEFAULT_CURRENCY)


def _build_models(cells: dict[tuple, dict[str, dict]], configs: dict,
                  providers: dict, prices: dict) -> dict[str, dict]:
    """把原始分组升成**模型级**统计身份：cfg:<id> / gone:<model_name>。

    两条**同名的不同配置**是两个 cfg: 键，绝不合并（合并就等于替用户把两笔账并成一笔）；
    配置删掉之后历史行落成 gone:<model_name>，仍然单独一条线，不会凭空消失。
    """
    out: dict[str, dict] = {}
    for (config_id, model_name, provider), bucket_cells in cells.items():
        cid = str(config_id) if config_id else ""
        cfg = configs.get(cid) if cid else None
        key = f"cfg:{cid}" if cfg is not None else f"gone:{model_name or ''}"
        entry = out.setdefault(key, {"key": key, "cells": {}, "config_id": cid or None,
                                     "model_name": model_name, "provider": provider,
                                     "row_currencies": set()})
        for bucket_key, cell in bucket_cells.items():
            _merge(entry["cells"].setdefault(bucket_key, _zero_totals()), cell)
            entry["row_currencies"] |= set(cell["cost"].keys())
        if cfg is not None:
            entry["provider"] = getattr(cfg, "provider", None) or provider
            entry["model_name"] = getattr(cfg, "model_name", None) or model_name

    for entry in out.values():
        cid = str(entry["config_id"]) if entry["config_id"] else ""
        price = (prices.get(cid) or {}) if cid else {}
        priced_now = bool(price.get("priced"))
        # 两个字段**刻意并存**：
        # · priced_now = 现在就能算出金额（只认当前配了输入价与输出价）—— 限额引擎只认它，
        #   拿宽松的 has_price 去判会放行一个算不出钱的模型；
        # · has_price = "现在能算价" 或 "历史行真的带过金额"：后者说明它曾经配过单价，
        #   前端据此区分"从没配过价"（要提示去配）与"改价前的账"（别去动它）。
        # gone: 系列（配置已删）永远 priced_now=False：没有配置行就没有单价，
        # 它的历史金额仍然是真账（has_price 可以为 true）。
        entry["priced_now"] = priced_now
        entry["has_price"] = priced_now or bool(entry["row_currencies"])
        entry["currency"] = price.get("currency") if priced_now else (
            sorted(entry["row_currencies"])[0] if entry["row_currencies"]
            else billing.DEFAULT_CURRENCY)
        cfg = configs.get(cid) if cid else None
        entry["label"] = getattr(cfg, "name", None) or entry["model_name"] or "未知模型"
        entry["provider_label"] = _provider_label(entry["provider"], providers)
    return out


def _fold_vendors(models: dict[str, dict], providers: dict) -> dict[str, dict]:
    """模型级 → 厂商级（group=vendor）：同厂商的模型合成一条，金额仍按币种分开。"""
    out: dict[str, dict] = {}
    for entry in models.values():
        provider = str(entry.get("provider") or "")
        key = f"vendor:{provider}"
        target = out.setdefault(key, {"key": key, "cells": {}, "has_price": False,
                                      "priced_now": True,
                                      "currency": _provider_currency(provider, providers),
                                      "provider": provider,
                                      "provider_label": _provider_label(provider, providers)})
        target["has_price"] = target["has_price"] or entry["has_price"]
        # 厂商级 priced_now 用 **AND**：旗下只要有一个算不出钱的模型就不能当"已配价"
        # （限额要卡的是这家真的花得起钱，一条线算不出来就得先去配）
        target["priced_now"] = target["priced_now"] and bool(entry.get("priced_now"))
        for bucket_key, cell in entry["cells"].items():
            _merge(target["cells"].setdefault(bucket_key, _zero_totals()), cell)
    for entry in out.values():
        entry["label"] = entry["provider_label"]
    return out


def _series_payload(entry: dict, buckets: list[dict], kind: str) -> dict:
    """一条 series 的出参（points 与 window.points 逐位对齐）。"""
    totals = _zero_totals()
    points = []
    for bucket in buckets:
        cell = entry["cells"].get(bucket["key"])
        point = _zero_totals() if cell is None else {
            "tokens_in": cell["tokens_in"], "tokens_out": cell["tokens_out"],
            "tokens_total": cell["tokens_total"], "requests": cell["requests"],
            "cost": dict(cell["cost"]), "unpriced": cell["unpriced"],
        }
        points.append(point)
        _merge(totals, point)
    payload = {
        "key": entry["key"], "kind": kind,
        "label": entry.get("label") or "未知模型",
        "provider": entry.get("provider"),
        "provider_label": entry.get("provider_label") or "",
        "currency": entry.get("currency") or billing.DEFAULT_CURRENCY,
        "has_price": bool(entry.get("has_price")),
        "priced_now": bool(entry.get("priced_now")),
        "points": points,
        "totals": totals,
    }
    if kind == "model":
        payload["model_name"] = entry.get("model_name") or ""
    return payload


def _sort_key(payload: dict) -> tuple:
    return (-payload["totals"]["tokens_total"], str(payload["label"]), payload["key"])


def _filters(models: dict[str, dict]) -> dict:
    """下拉框的数据源：**窗口内的全量**（不受 vendor/model 筛选影响）。"""
    models_out = [{
        "key": entry["key"],
        "label": entry.get("label") or "未知模型",
        "provider": entry.get("provider"),
        "provider_label": entry.get("provider_label") or "",
        "config_id": entry.get("config_id"),
        "has_price": bool(entry.get("has_price")),
        "priced_now": bool(entry.get("priced_now")),
        "currency": entry.get("currency") or billing.DEFAULT_CURRENCY,
    } for entry in models.values()]
    models_out.sort(key=lambda row: (str(row["label"]), row["key"]))

    vendors: dict[str, dict] = {}
    for row in models_out:
        provider = str(row.get("provider") or "")
        vendor = vendors.setdefault(provider, {
            "id": provider, "label": row.get("provider_label") or provider or "未知厂商",
            "models": [], "has_price": False, "priced_now": True,
        })
        vendor["models"].append(row["key"])
        vendor["has_price"] = vendor["has_price"] or bool(row["has_price"])
        # 与 series 同口径：厂商"现在全部能算价"才 True（AND）
        vendor["priced_now"] = vendor["priced_now"] and bool(row["priced_now"])
    return {"vendors": sorted(vendors.values(), key=lambda v: (v["label"], v["id"])),
            "models": models_out}


def _selected(entries: dict[str, dict], vendor: Any, model: Any) -> list[dict]:
    vendors = {v.lower() for v in _parse_filter(vendor) or []}
    ids = _parse_filter(model)
    # 裸 id / 裸 model_name 都认：前端与 curl 各自方便，解析时补成完整身份键
    wanted = None
    if ids is not None:
        wanted = set()
        for value in ids:
            wanted.add(value)
            if ":" not in value:
                wanted.add(f"cfg:{value}")
                wanted.add(f"gone:{value}")
    out = []
    for entry in entries.values():
        if vendors and str(entry.get("provider") or "").lower() not in vendors:
            continue
        if wanted is not None and entry["key"] not in wanted:
            continue
        out.append(entry)
    return out


def stats(window: Any = DEFAULT_WINDOW, vendor: Any = ALL, model: Any = ALL,
          group: Any = "model") -> dict:
    """多系列 / 可筛选 / 按小时或按天分桶的用量统计（契约见 GET /models/usage/stats）。

    三条不变量：

    · window.points 连续、从旧到新、长度正好是桶数；series[].points 与它**逐位对齐**；
    · 筛选只影响 series / totals，filters 永远是**窗口内的全量**
      （否则下拉框一选就把自己筛没）；
    · 空库 / 查询失败返回**合法空结构**（空 series、零 totals），绝不 500。
    """
    kind = normalize_window(window)
    granularity, count = WINDOWS[kind]
    grp = str(group or "").strip().lower()
    grp = grp if grp in GROUPS else "model"
    buckets = _buckets(granularity, count)
    out = {
        "window": {"kind": kind, "granularity": granularity,
                   "from": buckets[0]["key"], "to": buckets[-1]["key"],
                   "points": buckets},
        "group": grp,
        "currencies": [],
        "series": [],
        "filters": {"vendors": [], "models": []},
        "totals": _zero_totals(),
    }

    try:
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            cells = _load_cells(sess, granularity, buckets)
            config_ids = {str(key[0]) for key in cells if key[0]}
            configs, providers, prices = _load_meta(sess, config_ids)
            models = _build_models(cells, configs, providers, prices)
            # filters 永远按**模型级**全量出（group=vendor 时下拉框仍要能挑到具体模型）
            out["filters"] = _filters(models)
            entries = models if grp == "model" else _fold_vendors(models, providers)
            chosen = _selected(entries, vendor, model)
    except Exception as exc:  # noqa: BLE001 —— 查询失败降级成空结构，不让设置页 500
        log.warning("模型用量统计查询失败（返回空统计）：%s", str(exc)[:200])
        return out

    series = [_series_payload(entry, buckets, grp) for entry in chosen]
    series.sort(key=_sort_key)
    totals = _zero_totals()
    for payload in series:
        _merge(totals, payload["totals"])
    out["series"] = series
    out["totals"] = totals
    out["currencies"] = sorted(totals["cost"].keys())
    return out
