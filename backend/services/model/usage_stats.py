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

系列身份 = **模型的自定义名称**（model_config.name 去首尾空白）：名字相同就并成一条
（token / 请求 / 费用相加，币种各算各的）；名字不同哪怕同一个上游 model_name 也是两条。
model_name 只是展示字段；限制与限额仍按配置 id 走（不在这条链路上）。

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
#: 分组维度：按模型（name:<自定义名称>）或按厂商（vendor:<id>）
GROUPS = ("model", "vendor")
ALL = "all"

#: 模型级身份键前缀：**系列按用户起的自定义名称合并**（两条配置同名 = 一条线；
#: 同一个上游 model_name 但名字不同 = 两条线）。出参里只会出现 name: 键，
#: cfg: / gone: 仅作为筛选入参保留（老链接、老前端不能炸，见 _selector）。
NAME_PREFIX = "name:"
#: 合并系列横跨多个厂商时的落点：宁可说"多个厂商"，也不挑一个冒充
MULTI_VENDOR_LABEL = "多个厂商"
UNKNOWN_MODEL_LABEL = "未知模型"

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

    身份（name: / vendor:）**不在这里定** —— 那要先知道配置叫什么名字、还在不在，
    所以这里只按行上的原始字段分组，身份与同名合并都留给 _build_models。

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


def _series_name(cfg, row_model_name: Any) -> str:
    """系列身份 = 模型的自定义名称（model_config.name 去掉首尾空白后的字面量）。

    空名称回落到行上**反规范化**的 model_name（配置被删也只剩它可读），再空回落到「未知模型」。
    上游 model_name（如 deepseek-chat）只在这里兜底，绝不是身份：用户把两条配成同一个名字，
    就是在说"当着一条算"；名字不同哪怕同一个上游模型也必须分开。
    """
    name = str(getattr(cfg, "name", None) or "").strip() if cfg is not None else ""
    if name:
        return name
    return str(row_model_name or "").strip() or UNKNOWN_MODEL_LABEL


def _new_entry(key: str) -> dict:
    """一条合并系列的可累积状态（cells 最后由 by_provider 汇总得到）。"""
    return {"key": key, "label": None, "name": None, "cells": {}, "by_provider": {},
            "priced_by_provider": {}, "currency_by_provider": {},
            "config_ids_by_provider": {}, "row_currencies": set(), "model_name": None}


def _provider_has_price(entry: dict, pid: str) -> bool:
    """该厂商的贡献"有钱可算"：现在配了价，或历史行的金额真的带过币种。"""
    if entry["priced_by_provider"].get(pid):
        return True
    return any(cell["cost"] for cell in entry["by_provider"].get(pid, {}).values())


def _derive(entry: dict, providers: dict) -> dict:
    """从 by_provider 推出对外字段（provider / provider_label / priced_now / currency / ...）。

    单独成函数是因为**按厂商筛选**后要按"剩下的那些厂商"重算一遍
    （合并系列里另一个厂商的贡献被去掉，钱自然要变小）。
    """
    pids = list(entry["by_provider"])
    nonempty = [pid for pid in pids if pid]
    entry["providers"] = nonempty
    if len(nonempty) == 1:
        entry["provider"] = nonempty[0]
        entry["provider_label"] = _provider_label(nonempty[0], providers)
    elif nonempty:
        # 同名合并到多个厂商：不挑一个冒充，两个字段各自说清
        entry["provider"] = None
        entry["provider_label"] = MULTI_VENDOR_LABEL
    else:
        entry["provider"] = None
        entry["provider_label"] = _provider_label(None, providers)
    # 两个字段**刻意并存**：
    # · priced_now = 现在就能算出金额（只认当前配了输入价与输出价）—— 限额引擎只认它，
    #   拿宽松的 has_price 去判会放行一个算不出钱的模型；同名合并时用 **AND**：
    #   参与合并的配置里只要有一条没配价，这条线现在就"算不出钱"（与 vendor 级同口径）；
    # · has_price = "现在能算价" 或 "历史行真的带过金额"：后者说明它曾经配过单价，
    #   前端据此区分"从没配过价"（要提示去配）与"改价前的账"（别去动它）。
    # 已删配置的贡献 priced 恒为 False：没有配置行就没有单价，但它的历史金额仍是真账。
    entry["priced_now"] = all(bool(entry["priced_by_provider"].get(pid)) for pid in pids)
    priced_currencies = [str(c) for c in
                         (entry["currency_by_provider"].get(pid) for pid in pids) if c]
    if entry["priced_now"] and priced_currencies:
        entry["currency"] = priced_currencies[0]
    elif entry["row_currencies"]:
        entry["currency"] = sorted(entry["row_currencies"])[0]
    else:
        entry["currency"] = (priced_currencies[0] if priced_currencies
                             else billing.DEFAULT_CURRENCY)
    entry["has_price"] = bool(entry["priced_now"]) or bool(entry["row_currencies"])
    ids: list[str] = []
    for pid in pids:
        for cid in entry["config_ids_by_provider"].get(pid) or []:
            if cid not in ids:
                ids.append(cid)
    entry["model_config_ids"] = ids
    return entry


def _scope_to(entry: dict, keep: list[str]) -> dict:
    """同一条系列的"只看这些厂商"视图：cells 只由这些厂商的贡献重建。"""
    clone = _new_entry(entry["key"])
    clone["label"] = entry.get("label")
    clone["name"] = entry.get("name")
    clone["model_name"] = entry.get("model_name")
    for pid in keep:
        if pid not in entry["by_provider"]:
            continue
        clone["by_provider"][pid] = entry["by_provider"][pid]
        clone["priced_by_provider"][pid] = bool(entry["priced_by_provider"].get(pid))
        clone["currency_by_provider"][pid] = entry["currency_by_provider"].get(pid) or ""
        clone["config_ids_by_provider"][pid] = list(
            entry["config_ids_by_provider"].get(pid) or [])
        for bucket_key, cell in entry["by_provider"][pid].items():
            _merge(clone["cells"].setdefault(bucket_key, _zero_totals()), cell)
            clone["row_currencies"] |= set(cell["cost"].keys())
    return clone


def _build_models(cells: dict[tuple, dict[str, dict]], configs: dict,
                  providers: dict, prices: dict) -> dict[str, dict]:
    """把原始分组升成**模型级**统计身份，并按**自定义名称**把同名配置合并成一条。

    身份 = name:<model_config.name.strip()>（空名称回落行上的 model_name）。
    同名就并成一条线：token / 请求 / 未计费相加，金额按币种**分别**相加（币种永不互加）。
    同一个上游 model_name 但自定义名称不同 → 仍是两条线：model_name 只是展示字段，
    用户起的名字才是"当着一条算"的那句承诺。

    合并前的原始分组仍是 (config_id, model_name, provider)：provider 决定厂商维度
    （group=vendor 与 vendor= 筛选要按厂商拆账，所以 by_provider 留着每个厂商的贡献），
    config_id 决定 cfg: 筛选与 model_config_ids 出参。
    """
    out: dict[str, dict] = {}
    for (config_id, model_name, provider), bucket_cells in cells.items():
        cid = str(config_id) if config_id else ""
        cfg = configs.get(cid) if cid else None
        name = _series_name(cfg, model_name)
        key = f"{NAME_PREFIX}{name}"
        entry = out.get(key)
        if entry is None:
            entry = out[key] = _new_entry(key)
            entry["name"] = name
        # 厂商身份：配置还在就以配置为准（用户可能改过 provider），否则用行上反规范化的值
        pid = str((getattr(cfg, "provider", None) if cfg is not None else None)
                  or provider or "")
        target = entry["by_provider"].setdefault(pid, {})
        for bucket_key, cell in bucket_cells.items():
            _merge(target.setdefault(bucket_key, _zero_totals()), cell)
            entry["row_currencies"] |= set(cell["cost"].keys())
        price = (prices.get(cid) or {}) if (cfg is not None and cid) else {}
        priced = bool(price.get("priced"))
        entry["priced_by_provider"][pid] = (
            entry["priced_by_provider"].get(pid, True) and priced)
        if priced:
            entry["currency_by_provider"].setdefault(pid, price.get("currency") or "")
        if cid and cfg is not None:
            ids = entry["config_ids_by_provider"].setdefault(pid, [])
            if cid not in ids:
                ids.append(cid)
        if not entry["model_name"]:
            # 展示字段：同名合并后取**第一个非空值**，不拼接、不去重
            entry["model_name"] = (
                (getattr(cfg, "model_name", None) if cfg is not None else None)
                or model_name)

    for entry in out.values():
        # 对外只认合并后的 cells；by_provider 是为了按厂商拆账才留的中间态
        for bucket_cells in entry["by_provider"].values():
            for bucket_key, cell in bucket_cells.items():
                _merge(entry["cells"].setdefault(bucket_key, _zero_totals()), cell)
        _derive(entry, providers)
        entry["label"] = entry["name"] or entry["model_name"] or UNKNOWN_MODEL_LABEL
    return out


def _fold_vendors(models: dict[str, dict], providers: dict) -> dict[str, dict]:
    """模型级 → 厂商级（group=vendor）：同厂商的贡献合成一条，金额仍按币种分开。

    合并系列可能横跨多个厂商（那条线的 provider 是 None），所以这里按 **by_provider 的贡献**
    折叠，不能按 entry["provider"] 折 —— 否则多厂商那条会掉进一个空厂商桶里。
    """
    out: dict[str, dict] = {}
    for entry in models.values():
        for pid, bucket_cells in entry["by_provider"].items():
            key = f"vendor:{pid}"
            target = out.get(key)
            if target is None:
                target = out[key] = _new_entry(key)
                target["provider"] = pid or None
                target["provider_label"] = _provider_label(pid, providers)
                target["currency"] = _provider_currency(pid, providers)
                target["has_price"] = False
                target["priced_now"] = True
                target["by_provider"][pid] = {}
            target["has_price"] = target["has_price"] or _provider_has_price(entry, pid)
            # 厂商级 priced_now 用 **AND**：旗下只要有一个算不出钱的模型就不能当"已配价"
            # （限额要卡的是这家真的花得起钱，一条线算不出来就得先去配）
            target["priced_now"] = target["priced_now"] and bool(
                entry["priced_by_provider"].get(pid))
            for bucket_key, cell in bucket_cells.items():
                _merge(target["cells"].setdefault(bucket_key, _zero_totals()), cell)
                _merge(target["by_provider"][pid].setdefault(bucket_key, _zero_totals()), cell)
    for entry in out.values():
        pid = str(entry["provider"] or "")
        entry["label"] = entry["provider_label"]
        entry["priced_by_provider"] = {pid: bool(entry["priced_now"])}
        entry["currency_by_provider"] = {pid: entry["currency"]}
        entry["config_ids_by_provider"] = {pid: []}
        entry["row_currencies"] = set()
        for cell in entry["cells"].values():
            entry["row_currencies"] |= set(cell["cost"].keys())
        entry["providers"] = [pid] if pid else []
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
        "label": entry.get("label") or UNKNOWN_MODEL_LABEL,
        "provider": entry.get("provider"),
        "provider_label": entry.get("provider_label") or "",
        # 参与这条系列的厂商（去重、按出现顺序）；多厂商时 provider 是 None
        "providers": list(entry.get("providers") or []),
        "currency": entry.get("currency") or billing.DEFAULT_CURRENCY,
        "has_price": bool(entry.get("has_price")),
        "priced_now": bool(entry.get("priced_now")),
        "points": points,
        "totals": totals,
    }
    if kind == "model":
        # model_name 只是展示字段（同名合并后取第一个非空值），**不是**身份
        payload["model_name"] = entry.get("model_name") or ""
        # 参与合并的配置（已删除配置不在其中：删了就没有 id 可以点过去）
        payload["model_config_ids"] = list(entry.get("model_config_ids") or [])
    return payload


def _sort_key(payload: dict) -> tuple:
    return (-payload["totals"]["tokens_total"], str(payload["label"]), payload["key"])


def _filters(models: dict[str, dict], providers: dict) -> dict:
    """下拉框的数据源：**窗口内的全量**（不受 vendor/model 筛选影响）。

    键与 series 同一套（name:<自定义名称>）：下拉里选中的那一条，就是图上的那一条。
    """
    models_out = []
    for entry in models.values():
        ids = list(entry.get("model_config_ids") or [])
        models_out.append({
            "key": entry["key"],
            "label": entry.get("label") or UNKNOWN_MODEL_LABEL,
            "provider": entry.get("provider"),
            "provider_label": entry.get("provider_label") or "",
            "providers": list(entry.get("providers") or []),
            # 只有唯一一条配置时才给 config_id（合并系列没有唯一的配置可点，
            # 前端要跳配置请用 model_config_ids）
            "config_id": ids[0] if len(ids) == 1 else None,
            "model_config_ids": ids,
            "has_price": bool(entry.get("has_price")),
            "priced_now": bool(entry.get("priced_now")),
            "currency": entry.get("currency") or billing.DEFAULT_CURRENCY,
        })
    models_out.sort(key=lambda row: (str(row["label"]), row["key"]))

    vendors: dict[str, dict] = {}
    for row in models_out:
        entry = models[row["key"]]
        # 合并系列可能横跨多个厂商：每个参与厂商的下拉里都要能挑到它
        for pid in (row["providers"] or [""]):
            vendor = vendors.setdefault(pid, {
                "id": pid, "label": _provider_label(pid, providers),
                "models": [], "has_price": False, "priced_now": True,
            })
            vendor["models"].append(row["key"])
            vendor["has_price"] = vendor["has_price"] or _provider_has_price(entry, pid)
            # 与 series 同口径：厂商"现在全部能算价"才 True（AND）
            vendor["priced_now"] = vendor["priced_now"] and bool(
                entry["priced_by_provider"].get(pid))
    return {"vendors": sorted(vendors.values(), key=lambda v: (v["label"], v["id"])),
            "models": models_out}


def _selector(model: Any) -> tuple[set[str], set[str]] | None:
    """解析 model= → (要匹配的系列键, 要匹配的配置 id)。None = 不筛。

    四种入参都认，老链接 / 老前端不会炸：

    · name:<自定义名称> —— 现在的身份键；
    · cfg:<config_id> —— 该配置**所在系列**（命中合并后的 name: 系列，而不是只留它自己那份）；
    · gone:<model_name> —— 配置已删时的历史键（它的系列就是回落出的 name: 键）；
    · 裸值 —— 先当名称、再当 config id 试（老前端传的就是裸 config id）。
    """
    values = _parse_filter(model)
    if values is None:
        return None
    names: set[str] = set()
    cfgs: set[str] = set()
    for value in values:
        head, _, rest = value.partition(":")
        prefix = head.strip().lower() if rest else ""
        if prefix == "cfg":
            cfgs.add(rest.strip())
        elif prefix == "gone":
            names.add(f"{NAME_PREFIX}{rest.strip()}")
        elif prefix == "name":
            names.add(f"{NAME_PREFIX}{rest.strip()}")
        else:
            names.add(f"{NAME_PREFIX}{value}")
            cfgs.add(value)
    return names, cfgs


def _selected(entries: dict[str, dict], vendor: Any, model: Any,
              providers: dict) -> list[dict]:
    vendor_ids = {v.lower() for v in _parse_filter(vendor) or []}
    wanted = _selector(model)
    out = []
    for entry in entries.values():
        picked = entry
        if vendor_ids:
            keep = [pid for pid in entry.get("by_provider") or {}
                    if pid and pid.lower() in vendor_ids]
            if not keep:
                continue
            # 厂商筛选 = 只算这家的账：合并系列里另一个厂商的贡献被真的去掉（金额随之变小），
            # 但返回的仍然是**这条合并系列**（provider 归位成剩下那一个）
            picked = _derive(_scope_to(entry, keep), providers)
        if wanted is not None:
            names, cfgs = wanted
            if (picked["key"] not in names
                    and not (cfgs & set(picked.get("model_config_ids") or []))):
                continue
        out.append(picked)
    return out


def stats(window: Any = DEFAULT_WINDOW, vendor: Any = ALL, model: Any = ALL,
          group: Any = "model") -> dict:
    """多系列 / 可筛选 / 按小时或按天分桶的用量统计（契约见 GET /models/usage/stats）。

    三条不变量（身份 = name:<自定义名称>，同名即合并；cfg: / gone: 只作为入参兼容）：

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
            out["filters"] = _filters(models, providers)
            entries = models if grp == "model" else _fold_vendors(models, providers)
            chosen = _selected(entries, vendor, model, providers)
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
