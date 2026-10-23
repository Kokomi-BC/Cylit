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

周期边界（``period_bounds``）供后面的限额引擎复用：**周一为一周的开始**，
返回的是**本地（Asia/Shanghai）naive** 的 ``[start, end)``，end 为**开区间**（下一周期的起点），
所以"落在本周期"就是 ``start <= t < end``，不用再判 ``23:59:59.999999`` 这种边界。
"""

from __future__ import annotations

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
# 单价解析
# --------------------------------------------------------------------------


def _blank_price() -> dict:
    return {
        "currency": DEFAULT_CURRENCY,
        "price_in": None, "price_out": None,
        "price_cache_read": None, "price_cache_write": None,
        "priced": False, "source": "none",
    }


def price_of(model_config_id: str | None, *, sess=None) -> dict:
    """解析一个模型配置的**生效单价**与**生效币种**。

    返回键（契约）：``currency / price_in / price_out / price_cache_read /
    price_cache_write / priced / source``。

    · 币种链：模型 → 厂商 → ``'CNY'``；
    · 单价**只认模型级**（``source`` 目前只有 ``'model'`` / ``'none'``）：
      不做厂商级单价继承 —— 一家的默认价与它某一档模型的价经常不一样，
      继承一个"猜出来的价"比"未计费"更危险（错账看得见，错价看不见）；
    · ``priced`` 要求**输入价与输出价都有**：只配一半整体算未配置，
      不做"半算"（半算出来的数会被当真，而它连量级都可能是错的）。
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
            return {
                "currency": currency or DEFAULT_CURRENCY,
                "price_in": prices["price_in"], "price_out": prices["price_out"],
                "price_cache_read": prices["price_cache_read"],
                "price_cache_write": prices["price_cache_write"],
                "priced": priced,
                "source": "model" if priced else "none",
            }
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


def _prices_for(config: Any) -> dict:
    """``config`` 可以是 ``price_of`` 的结果、模型配置 dict、或 None。"""
    if isinstance(config, dict) and "priced" in config and "currency" in config:
        return config
    if isinstance(config, dict):
        resolved = price_of(config.get("id"))
        # 调用方自己已经带着单价（例如 agent 进程从 HTTP 拿到配置）时以它为准，
        # 这样"配置刚改过价、库里还没刷"的窗口里也能对上账。
        inline = {name: config.get(name) for name in PRICE_FIELDS}
        if all(v is not None for v in (inline["price_in"], inline["price_out"])):
            return {
                "currency": normalize_currency(config.get("currency")) or resolved["currency"],
                **inline, "priced": True, "source": "model",
            }
        return resolved
    return price_of(getattr(config, "id", None))


def _round_cost(value: float) -> float:
    return round(float(value), COST_PRECISION)


def cost_of(config: Any, prompt: int, completion: int,
            cache_read: int = 0, cache_write: int = 0,
            reported: Any = None, reported_currency: Any = None) -> dict:
    """算一次调用的金额：返回 ``{cost, currency, billing}``。

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
    prices = _prices_for(config)
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

    if not prices.get("priced"):
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
