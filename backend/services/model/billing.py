"""计费真源：单价 → 一次调用的 (cost, currency, billing)，以及周期边界。

三条铁律（写在这里，因为后面每个模块都要按它判）：

1. **币种不做任何换算**。人民币与美元各算各的 —— 汇总时按币种分开累加，
   永远不出现"CNY + USD"这种数（一个汇率假设就能让整张对账单不可复核）。
2. **缺价格 = 未计费**（``cost`` 存 NULL，``billing='none'``），**绝不写 0**。
   0 会被读成"这次真的免费"，把"不知道"伪装成"知道"。
3. **单价单位是每百万 Token**（厂商报价就长这样），换算只在本模块的 ``cost_of`` 里做一次。
   别在别处再除一次 1e6 —— 除两次的结果看起来"很便宜"，但没人会发现。

币种解析链（**单价只认模型级**，不做厂商级单价继承）：
    ``model_config.currency`` → ``model_provider.currency`` → ``'CNY'``

分时段（峰谷）收费（``price_of(at=...)``）：
    ``model_config`` 上的四个单价 = **"时段之外"的价**（按 DeepSeek 口径就是**空闲价**）；
    ``model_price_rule`` 里的一行 = 一段时段，它带来**自己的一套单价**，是**例外覆盖**。
    规则**只对工作日（周一至周五）生效**，周末一律落到默认价 —— DeepSeek 官方的两段
    高峰（北京时间 09:00–12:00、14:00–18:00）就是两条规则，其余时段（含周末、法定节假日）
    免费时段就是"没命中任何规则"，自然用默认价 = 空闲价，与官方规则同构。
    时段**不存币种**：一个模型不可能人民币时段、美元时段，币种永远只认模型/厂商那一级。
    命中时段时 ``source='rule'`` 且带回 ``rule_id`` / ``rule_label``，落账时写进
    ``model_usage.price_rule_id`` —— "这笔钱按哪一段算的"必须可追溯。
    ``priced`` 的口径**刻意不跟着变**：它只回答"默认价齐不齐"（限额引擎与界面靠它判
    "能不能设限额"），与这一次是否命中时段无关。

周期边界（``period_bounds``）供后面的限额引擎复用：**周一为一周的开始**，
返回的是**本地（Asia/Shanghai）naive** 的 ``[start, end)``，end 为**开区间**（下一周期的起点），
所以"落在本周期"就是 ``start <= t < end``，不用再判 ``23:59:59.999999`` 这种边界。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.core.errors import ValidationError
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 支持的币种。**不做任何换算** —— 加进来的前提是"它自己一列统计"。
CURRENCIES = ("CNY", "USD")
#: 币种链的最后一级（模型与厂商都没写时用）
DEFAULT_CURRENCY = "CNY"
#: 单价字段（单位：每百万 Token；NULL = 未配置）
PRICE_FIELDS = ("price_in", "price_out", "price_cache_read", "price_cache_write")

#: 金额是哪来的
BILLING_COMPUTED = "computed"   # 按模型单价算出来的
BILLING_REPORTED = "reported"   # 厂商明确回传了实际费用（原样保留，不重算）
BILLING_NONE = "none"           # 没配单价 → 不计费（cost 为 NULL）

#: 金额保留几位小数。取 6 位（= 百万分之一货币单位）：一次调用常常只值 0.0003 元，
#: 只保留 2 位会把绝大多数条目舍成 0，"按次加总"和"按单价重算"就对不上了。
COST_PRECISION = 6

#: 中国自 1991 年起不实行夏令时 → 固定 +08:00 就够准。
#: **刻意不用 zoneinfo**：Windows 上常常没装 tzdata，``ZoneInfo("Asia/Shanghai")``
#: 会抛 ZoneInfoNotFoundError，把整条计费链带崩（而计费是旁路，本该永不抛）。
LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

#: ``period_bounds`` 支持的周期
PERIOD_KINDS = ("day", "week", "month")


# --------------------------------------------------------------------------
# 时间：库里是 UTC naive，桶是本地时间
# --------------------------------------------------------------------------


def local_now() -> datetime:
    """现在的**本地** naive 时间（Asia/Shanghai）。"""
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def to_local(value: datetime | None = None) -> datetime:
    """把库里的 UTC naive（或任意 aware）时间换成本地 naive 时间。"""
    if value is None:
        return local_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)


def to_utc(value: datetime) -> datetime:
    """本地 naive → UTC naive（拿去和 ``created_at`` 比时用）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_day(value: datetime | None = None) -> str:
    """本地日（``YYYY-MM-DD``）：``model_usage.day`` 的口径。"""
    return to_local(value).date().isoformat()


def hour_key(value: datetime | None = None) -> str:
    """本地小时桶键（``YYYY-MM-DD HH``）：``model_usage.hour_key`` 的口径。"""
    return to_local(value).strftime("%Y-%m-%d %H")


# --------------------------------------------------------------------------
# 校验（写入口用：非法单价 / 币种一律 ValidationError，不静默吞）
# --------------------------------------------------------------------------


def normalize_currency(value: Any, *, allow_empty: bool = True) -> str:
    """币种规范化：``''``/None → ``''``（= 跟随厂商）；其它必须是 CNY/USD（大小写不敏感）。"""
    text = str(value or "").strip().upper()
    if not text:
        if allow_empty:
            return ""
        return DEFAULT_CURRENCY
    if text not in CURRENCIES:
        raise ValidationError(f"币种必须是 {'/'.join(CURRENCIES)} 或空（跟随厂商）：{value!r}")
    return text


def clean_price(value: Any, field: str = "price") -> float | None:
    """单价校验：None/'' → None（未配置）；否则必须是非负数字。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是数字（每百万 Token 的单价），收到布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 必须是数字（每百万 Token 的单价）：{value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):   # NaN / inf
        raise ValidationError(f"{field} 必须是有限数字：{value!r}")
    if number < 0:
        raise ValidationError(f"{field} 不能是负数：{value!r}")
    return number


# --------------------------------------------------------------------------
# 分时段（峰谷）收费：时段规则（校验 + 命中判定）
#
# 语义只有一条，别在别处再实现一遍：
#   · 模型上的四个单价 = **时段之外**的价（DeepSeek 口径里就是空闲价）；
#   · ``model_price_rule`` 的一行 = 一段时段 + 它自己的一套单价，是**例外覆盖**；
#   · 规则**只对工作日（周一至周五）生效** —— 周末/节假日的调用一律落到默认价。
#
# 判定用**这次调用的开始时刻**（本地 Asia/Shanghai 的 HH:MM），区间左闭右开
# （``start <= t < end``）：09:00 命中、12:00 不命中。
# --------------------------------------------------------------------------

#: 一个时段规则的单价字段（与模型默认价同一套口径）
RULE_PRICE_FIELDS = PRICE_FIELDS

_MINUTES_PER_DAY = 24 * 60
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: Any, field: str = "start") -> int:
    """``'09:00'`` → 540（分钟）。格式非法一律 ValidationError，不猜。"""
    text = str(value if value is not None else "").strip()
    matched = _HHMM_RE.match(text)
    if not matched:
        raise ValidationError(f"{field} 必须是 HH:MM 格式的 24 小时时刻（如 09:00）：{value!r}")
    return int(matched.group(1)) * 60 + int(matched.group(2))


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def rule_intervals(start_min: int, end_min: int) -> list[tuple[int, int]]:
    """把一段时段拆成**一天之内**的若干左闭右开区间（跨午夜拆两段）。

    · ``09:00–12:00`` → ``[(540, 720)]``；
    · ``23:00–01:00`` → ``[(1380, 1440), (0, 60)]``  ← ``[start,24:00) ∪ [00:00,end)``。

    拆开是为了"同一模型内不许重叠"这条校验：``22:00–02:00`` 与 ``01:00–03:00`` 在朴素比较下
    看着不重叠，实际 01:00–02:00 直接算两次钱。
    """
    if start_min < end_min:
        return [(start_min, end_min)]
    return [(start_min, _MINUTES_PER_DAY), (0, end_min)]


def _rule_bounds(rule: Any) -> tuple[int, int] | None:
    """取一条规则的 (start, end) 分钟；形状不对就当没这段（读库时容错，写库时已校验）。"""
    if not isinstance(rule, dict):
        return None
    try:
        return parse_hhmm(rule.get("start"), "start"), parse_hhmm(rule.get("end"), "end")
    except ValidationError:
        return None


def rules_overlap(a: Any, b: Any) -> bool:
    """两段规则在**同一天内**是否重叠（跨午夜的段先拆成两段再比）。"""
    bounds_a, bounds_b = _rule_bounds(a), _rule_bounds(b)
    if bounds_a is None or bounds_b is None:
        return False
    left = rule_intervals(*bounds_a)
    right = rule_intervals(*bounds_b)
    return any(a0 < b1 and b0 < a1 for a0, a1 in left for b0, b1 in right)


def clean_rule(payload: Any) -> dict:
    """校验并规范化**一条**时段规则（写入口唯一实现）。

    · ``start`` / ``end`` 必须是 ``HH:MM`` 且 ``start != end``（相等是"零长度"，等于配了个永远不会命中的时段）；
    · **输入价与输出价都必填**（只配一半的时段和没配一样，却会让"按时段计费"看起来生效了）；
    · 缓存价可选（NULL = 未配置，沿用 ``cost_of`` 的"没配缓存价就按普通输入算"）；
    · 价格必须非负有限数字；
    · ``label`` 只是给人看的（"高峰"），可以为空。
    """
    if not isinstance(payload, dict):
        raise ValidationError("时段规则必须是一个对象（{label, start, end, price_in, ...}）")
    start_min = parse_hhmm(payload.get("start"), "start")
    end_min = parse_hhmm(payload.get("end"), "end")
    if start_min == end_min:
        raise ValidationError(
            f"时段 {_hhmm(start_min)}–{_hhmm(end_min)} 的开始与结束相同（零长度时段永远命中不了）")
    span = f"{_hhmm(start_min)}–{_hhmm(end_min)}"
    label = str(payload.get("label") or "").strip()[:64]
    named = f"时段 {span}" + (f"（{label}）" if label else "")
    prices: dict[str, float | None] = {}
    for name in RULE_PRICE_FIELDS:
        value = clean_price(payload.get(name), name)
        if name in ("price_in", "price_out") and value is None:
            what = "输入价（price_in）" if name == "price_in" else "输出价（price_out）"
            raise ValidationError(f"{named} 缺少{what}")
        prices[name] = value
    return {"label": label, "start": _hhmm(start_min), "end": _hhmm(end_min), **prices}


def wall_clock(value: datetime | None = None) -> datetime:
    """把时刻换成**当地墙钟**（aware → 本地 naive；naive 一律当它已经是本地时间）。

    为什么不直接用 `to_local`：那个的约定是"**库里拿出来的 UTC naive**"，
    所以会把 naive 值当成 UTC 再减 8 小时。而时段规则比的是"墙上那个钟"，
    调用方手里的 `at` 通常就是本地时刻（`local_now()` 也返回本地 naive）——
    拿 `to_local` 处理它会把 09:00 判成 17:00，于是"高峰永远命中在下午"。
    两条口径都保留：**读库时间用 `to_local`，判时段用这里**。
    如果手上的值是从库里读出来的 UTC naive，请先 `to_local()` 再传进来。
    """
    if value is None:
        return local_now()
    if value.tzinfo is None:
        return value
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)


def match_rule(rules: Any, at: datetime | None = None) -> dict | None:
    """这条调用时刻**命中**哪一段规则（没有 → None）。

    三条口径（改之前先看这里，别在调用点自己判一遍）：

    1. 用**开始时刻**的本地 HH:MM 与星期几：``start <= t < end``（左闭右开）；
    2. **只对工作日生效**：周六/周日直接不命中（周末 = 默认价 = DeepSeek 的空闲价）；
    3. **跨午夜的段以"开始时刻所在的那一天"为准**：``23:00–01:00`` 尾段（00:00–01:00）
       看的是**前一天**是不是工作日 —— 周五 23:30 跨到周六 00:30 那一截仍算命中（它属于
       周五这段高峰），周日 23:30 与周一 00:30 都不命中（这段是从周日开的头）。
    """
    local = wall_clock(at)
    minute = local.hour * 60 + local.minute
    workday = local.weekday() < 5
    prev_workday = (local - timedelta(days=1)).weekday() < 5
    for rule in (rules or ()):
        bounds = _rule_bounds(rule)
        if bounds is None:
            continue
        start_min, end_min = bounds
        if start_min < end_min:
            if workday and start_min <= minute < end_min:
                return rule
        else:
            # 跨午夜 = [start,24:00) ∪ [00:00,end)：白天那截看今天，凌晨那截看前一天
            if workday and minute >= start_min:
                return rule
            if prev_workday and minute < end_min:
                return rule
    return None


def _rule_prices(base: dict, rule: dict) -> dict:
    """把命中的规则盖到默认价上（币种 / ``priced`` 保持默认价那一套，见 price_of）。

    四个单价是**整段替换**，不做逐字段继承：规则里 ``price_cache_read`` 为 NULL 就是
    "这一段没配缓存价"，退回 ``cost_of`` 的既有口径（缓存那部分按普通输入价算）。
    继承默认价的缓存价反而更危险 —— 高峰段按空闲的缓存价收费会**少算一半**。
    """
    out = dict(base)
    for name in RULE_PRICE_FIELDS:
        out[name] = rule.get(name)
    out["source"] = "rule"
    out["rule_id"] = str(rule.get("id") or "") or None
    out["rule_label"] = str(rule.get("label") or "")
    return out


# --------------------------------------------------------------------------
# 单价解析
# --------------------------------------------------------------------------


def _blank_price() -> dict:
    return {
        "currency": DEFAULT_CURRENCY,
        "price_in": None, "price_out": None,
        "price_cache_read": None, "price_cache_write": None,
        "priced": False, "source": "none",
        "rule_id": None, "rule_label": "",
    }


def _rules_for(sess, model_config_id: str) -> list[dict]:
    """这个模型的全部时段规则（形状与 `clean_rule` 一致，多一个 `id`）。

    直接读表而不再开一个 session：`price_of` 已经把 session 攥在手里了，
    拆出去只在"同一个事务里取价记账"这条路上多一次连接、多一次竞态机会。
    """
    from backend.repositories.mysql.models import ModelPriceRule

    rows = sess.query(ModelPriceRule).filter(
        ModelPriceRule.model_config_id == str(model_config_id)).all()
    return [{
        "id": row.id, "label": row.label or "",
        "start": row.start, "end": row.end,
        "price_in": row.price_in, "price_out": row.price_out,
        "price_cache_read": row.price_cache_read,
        "price_cache_write": row.price_cache_write,
    } for row in rows]


def price_of(model_config_id: str | None, *, sess=None,
             at: datetime | None = None) -> dict:
    """解析一个模型配置在 ``at`` 这一刻的**生效单价**与**生效币种**。

    返回键（契约）：``currency / price_in / price_out / price_cache_read /
    price_cache_write / priced / source / rule_id / rule_label``。

    · 币种链：模型 → 厂商 → ``'CNY'``。**时段规则不参与币种**：一个模型不可能
      人民币时段、美元时段并存，币种永远只认模型/厂商那一级；
    · 单价**只认模型级**，不做厂商级单价继承 —— 一家的默认价与它某一档模型的价
      经常不一样，继承一个"猜出来的价"比"未计费"更危险（错账看得见，错价看不见）；
    · ``source``：``'model'``（用默认价）/ ``'rule'``（命中时段，单价整段换成规则
      那一套）/ ``'none'``（没配默认价，且这一刻没命中任何规则）；
    · ``at``：这次调用的时刻（本地 naive / UTC naive / aware 都行，默认"现在"）。
      判定口径全在 ``match_rule``：**用开始时刻**、**只对工作日生效**、左闭右开；
    · ``priced`` **只回答"默认价齐不齐"**（要求输入价与输出价都有），与是否命中
      时段**无关**：限额引擎与界面靠它判"这个模型能不能设限额"，一旦让它跟着时段
      走，"同一个模型在 12:00 前后能不能设限额"就成了实现细节。
    """
    empty = _blank_price()
    if not model_config_id:
        return empty
    try:
        from backend.repositories.mysql.models import ModelConfig, ModelProvider
        from backend.repositories.mysql.session import session_scope

        with (session_scope() if sess is None else _nullctx(sess)) as s:
            row = s.get(ModelConfig, str(model_config_id))
            if row is None:
                return empty
            prices = {name: getattr(row, name, None) for name in PRICE_FIELDS}
            currency = normalize_currency(getattr(row, "currency", None))
            if not currency:
                provider_id = str(getattr(row, "provider", "") or "")
                provider = s.get(ModelProvider, provider_id) if provider_id else None
                currency = normalize_currency(
                    getattr(provider, "currency", None) if provider else None)
            priced = prices["price_in"] is not None and prices["price_out"] is not None
            base = {
                "currency": currency or DEFAULT_CURRENCY,
                "price_in": prices["price_in"], "price_out": prices["price_out"],
                "price_cache_read": prices["price_cache_read"],
                "price_cache_write": prices["price_cache_write"],
                "priced": priced,
                "source": "model" if priced else "none",
                "rule_id": None, "rule_label": "",
            }
            # 命中时段 = 单价整段换成规则那一套（例外覆盖）；币种与 priced 不跟着变
            hit = match_rule(_rules_for(s, str(model_config_id)), at)
            return _rule_prices(base, hit) if hit is not None else base
    except Exception as exc:  # noqa: BLE001 —— 查价失败按未计费处理，绝不连累调用
        log.warning("解析模型单价失败（按未计费处理）：%s", str(exc)[:200])
        return empty


class _nullctx:
    """把"已经有了 session"包成上下文管理器（避免 price_of 再开一个连接）。"""

    def __init__(self, sess) -> None:
        self._sess = sess

    def __enter__(self):
        return self._sess

    def __exit__(self, *exc_info) -> bool:
        return False


def _prices_for(config: Any, at: datetime | None = None) -> dict:
    """``config`` 可以是 ``price_of`` 的结果、模型配置 dict、或 None。"""
    if isinstance(config, str):
        # 直接给 model_config.id 也认（调用方常常只有 id）
        return price_of(config, at=at)
    if isinstance(config, dict) and "priced" in config and "currency" in config:
        # 已经是"某一刻解好的生效价"了（price_of 的返回值）：原样用，不重新解一遍 ——
        # 重新解就会把调用方攥着的那一刻冲成"现在"。
        return config
    if isinstance(config, dict):
        resolved = price_of(config.get("id"), at=at)
        # 命中时段先返回：规则只存在库里（请求体里没有时段信息），这一刻的规则价
        # 比行内带来的默认价更准，否则"agent 走了内联价"就等于偷偷关掉了峰谷。
        if resolved.get("source") == "rule":
            return resolved
        # 调用方自己已经带着单价（例如 agent 进程从 HTTP 拿到配置）时以它为准，
        # 这样"配置刚改过价、库里还没刷"的窗口里也能对上账。
        inline = {name: config.get(name) for name in PRICE_FIELDS}
        if all(v is not None for v in (inline["price_in"], inline["price_out"])):
            return {
                "currency": normalize_currency(config.get("currency")) or resolved["currency"],
                **inline, "priced": True, "source": "model",
                "rule_id": None, "rule_label": "",
            }
        return resolved
    return price_of(getattr(config, "id", None), at=at)


def _billable(prices: dict) -> bool:
    """这一档价**算不算得出钱**（和 ``priced`` 不是一回事，见下）。

    ``priced`` 的口径是"**默认价**齐不齐"（限额引擎、界面都靠它，不许改）；
    而命中时段时手上有的是**规则自己的一套齐价** —— 这时算得出钱，且那笔钱是真的，
    所以计费这边单独判一次：要么默认价齐，要么命中规则且规则价齐。
    "只配了时段、没配默认价"的模型在**时段内**按规则价计费、**时段外**仍是未计费
    （cost=NULL、billing='none'）—— 这与 PUT 时段接口那条 warning 说的是同一件事。
    """
    if prices.get("price_in") is None or prices.get("price_out") is None:
        return False
    return bool(prices.get("priced")) or prices.get("source") == "rule"


def _round_cost(value: float) -> float:
    return round(float(value), COST_PRECISION)


def cost_of(config: Any, prompt: int, completion: int,
            cache_read: int = 0, cache_write: int = 0,
            reported: Any = None, reported_currency: Any = None,
            at: datetime | None = None) -> dict:
    """算一次调用的金额：返回 ``{cost, currency, billing}``。

    ``at`` 是**这次调用**的时刻，只影响"命中哪一段时段"（透传给 ``price_of``）；
    公式一个字没变。
    · 厂商**明确回传实际费用**时用 ``billing='reported'``，金额与币种原样保留 ——
      我们算出来的只是估算，人家给的是账单，冲突时听账单的；
    · 否则按单价算 ``billing='computed'``；没配单价 ``billing='none'`` 且 ``cost=None``。

    缓存 Token 的处理（**刻意按这个方向偏**）：

    · 配了 ``price_cache_read`` → 拆出缓存部分：
      ``billed_in = max(0, prompt - cache_read)``，缓存读按缓存价、其余输入按输入价；
    · **没配** ``price_cache_read`` → 不拆，缓存那部分按**普通输入价**算
      （宁可不少算：算多了最多是保守，算少了会让"这个月怎么这么便宜"变成一次误判）；
    · ``cache_write`` 只在**配了 ``price_cache_write``** 时才单独计价：多数厂商已经把
      写缓存的 token 含在 prompt_tokens 里，没配写价还硬加一遍就是重复计费。
    """
    prices = _prices_for(config, at=at)
    currency = normalize_currency(prices.get("currency")) or DEFAULT_CURRENCY

    # 厂商回传的实际费用优先（币种以它为准，免得把 CNY 账单记成 USD）
    if reported is not None and not isinstance(reported, bool):
        try:
            amount = float(reported)
        except (TypeError, ValueError):
            amount = None
        if amount is not None and amount == amount and 0 <= amount < float("inf"):
            return {
                "cost": _round_cost(amount),
                "currency": normalize_currency(reported_currency) or currency,
                "billing": BILLING_REPORTED,
            }

    if not _billable(prices):
        return {"cost": None, "currency": currency, "billing": BILLING_NONE}

    prompt = max(0, int(prompt or 0))
    completion = max(0, int(completion or 0))
    cache_read = max(0, int(cache_read or 0))
    cache_write = max(0, int(cache_write or 0))

    price_in = float(prices["price_in"])
    price_out = float(prices["price_out"])
    price_cache_read = prices.get("price_cache_read")
    price_cache_write = prices.get("price_cache_write")

    if price_cache_read is not None:
        billed_in = max(0, prompt - cache_read)
        total = (billed_in * price_in + completion * price_out
                 + cache_read * float(price_cache_read))
        if price_cache_write is not None:
            total += cache_write * float(price_cache_write)
    else:
        # 没配缓存价：整段 prompt 都按普通输入计（见 docstring）
        total = prompt * price_in + completion * price_out

    return {"cost": _round_cost(total / 1_000_000.0),
            "currency": currency, "billing": BILLING_COMPUTED}


# --------------------------------------------------------------------------
# 周期边界（限额引擎复用；本轮不接限额）
# --------------------------------------------------------------------------


def period_bounds(kind: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """返回本地 ``[start, end)``（naive；end = 下一周期的起点）。

    · ``day``：今天 00:00 → 明天 00:00；
    · ``week``：**本周一** 00:00 → 下周一 00:00（周日属于上一周，周一才翻篇）；
    · ``month``：本月 1 日 00:00 → 下月 1 日 00:00（跨年由 date 运算自己处理）。

    ``now`` 传 aware 时间也行（会换成本地），传 None 就用 Asia/Shanghai 的当前时间 ——
    所以"跨周期自动滚动"不需要调用方自己算，每次现算即可。
    """
    key = str(kind or "").strip().lower()
    if key not in PERIOD_KINDS:
        raise ValidationError(f"周期必须是 {'/'.join(PERIOD_KINDS)}：{kind!r}")
    local = to_local(now).replace(minute=0, second=0, microsecond=0)
    day_start = local.replace(hour=0)
    if key == "day":
        return day_start, day_start + timedelta(days=1)
    if key == "week":
        # weekday(): 周一=0 … 周日=6 —— 周一减 0 天、周日减 6 天，正好落在同一个周一
        start = day_start - timedelta(days=day_start.weekday())
        return start, start + timedelta(days=7)
    start = day_start.replace(day=1)
    # 加 32 天再取 1 号 = 下个月 1 号（不用管本月是 28/30/31 天）
    return start, (start + timedelta(days=32)).replace(day=1)
