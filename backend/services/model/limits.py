
"""限额引擎：模型 / 密钥 / 厂商三级限额的**判定、预留、快照**与写入。

口径（照这个实现，别在别处再写一套）：

1. **三级作用域各自独立累计**。一次调用适用的限额 = 这三条（存在且 enabled）。
   全部都要满足，任何一条用尽即拦。各自的金额与币种**独立累计** ——
   绝不把同一笔费用加进一个共享总额：共享总额会让「A 模型的额度被 B 模型吃掉」，
   而且两个币种一旦相加，这张表就永久不可复核（见 billing 的三条铁律）。
2. **周期现算**。每次判定都调 `billing.period_bounds(limit.period)` 拿本地
   `[start, end)`，换成 UTC 后与 `model_usage.created_at` 比。所以「翻篇」不需要
   任何手动重置或定时任务（定时任务漏跑一次就是白送一个周期）。
3. **只认同币种**。`currency = limit.currency 且 cost IS NOT NULL` 的行才计入 spent；
   其它币种的行**不参与**累计，但必须报出来（`foreign_currency_rows`）——
   装作它们不存在，用户就会看到「这个月才花了 3 块」而账单上是 30 块。
4. **未计费行（cost IS NULL）不计金额，但必须报**（`unpriced_rows`）：
   有它们的时候 spent 只是**下限**，界面上要说清「统计不完整」。
5. **未配单价的作用域不许设限额**（见 `unpriced_models`）：一个算不出钱的模型
   永远花不到限额，那条限额就是一张安慰剂 —— 用户以为有闸门，其实没有。
6. **检查在发请求之前**，靠预留（reservation）把「账已算、钱未花」这段窗口占住；
   调用结束在 finally 里释放，异常路径漏了就是额度泄漏。

并发怎么真的原子（这是本模块唯一难的地方）：

· MySQL：在一个事务里对每条适用限额的 `model_limit` 行做 `SELECT ... FOR UPDATE`。
  锁的是**限额那一行**（不是用量表）：同一限额的并发调用被排成队，后到的那个看到的是
  前一个已经提交的预留，于是「刚好只够 2 次」时不会有第 3 个成功；
· SQLite：没有行级锁，退化成**进程内互斥 + 串行写事务**（同一个进程里所有检查排队进，
  等价于 MySQL 的行锁效果）。多进程共享一个 SQLite 文件不在支持范围内（那本来也不是
  这个项目部的部署形态：单进程 FastAPI）。
· 锁的粒度刻意是「这一条限额」而不是全局：不同模型各自限额时不该互相等待。

预算相关的错误一律 `LimitExceededError`（HTTP 429 / code=LIMIT_EXCEEDED，
message 明说「本地限额触发」），**绝不伪装成供应商 429**。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from typing import Any, Iterator

from backend.core.errors import LimitExceededError, NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import new_id
from backend.core.utils import now as utc_now
from backend.services.model import billing, secret_resolver

log = get_logger(__name__)

#: 三级作用域（顺序 = 「谁更具体」，报错时按这个顺序挑最具体的那条来讲）
SCOPES = ("model", "key", "vendor")
#: PUT 里 period='none' 的意思是「删掉这条限额」（不是「不限金额」）
PERIOD_NONE = "none"
#: 预留有效期：够一次最慢的生成（含排队），又短到进程崩溃后不用等太久
RESERVATION_MINUTES = 10
#: 估算用：prompt 的 token 粗略按 4 字符 1 token；补全没给 max_tokens 时按 512
CHARS_PER_TOKEN = 4
DEFAULT_COMPLETION_TOKENS = 512
#: 被拦下的调用落账用的 error_code（与 LimitExceededError.code 同一个值）
ERROR_CODE = "LIMIT_EXCEEDED"
#: `detail["limit"]` 的**固定形状**（前端横幅只认这一份数）
SNAPSHOT_FIELDS = (
    "scope", "scope_id", "label", "period", "amount", "currency",
    "spent", "remaining", "percent", "period_start", "period_end", "reset_at",
    "unpriced_rows", "foreign_currency_rows",
)
_TIME_FMT = "%Y-%m-%d %H:%M"
_PERIOD_CN = {"day": "日", "week": "周", "month": "月"}

#: SQLite 下的「退化行锁」：进程内互斥（原因见模块文档）
_sqlite_lock = threading.RLock()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _fmt(value: datetime | None) -> str:
    return value.strftime(_TIME_FMT) if value else ""


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return number


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _field(row: Any, name: str, default: Any = None) -> Any:
    """读一个字段：同时支持 ORM 行与 `_row_dict` 出来的 dict。

    `_snapshot_in` 有两条调用路径（检查时拿到的是 ORM 行，列表里拿到的是 dict），
    以前只认 `getattr`，于是列表那条**静默把 period 当成 day** —— 月限额在
    横幅上会显示成日限额，周期末尾也不对。所以取字段只能走这一个口子。
    """
    value = row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)
    return default if value is None else value


def _stamp(now: datetime | None = None) -> datetime:
    """统一成 **UTC naive**（拿去比 `model_usage.created_at`）。

    `now` 是「本地 naive 或 aware」，与 `billing.period_bounds` 同一个口径。
    """
    if now is None:
        return billing.to_utc(billing.local_now())
    return billing.to_utc(now)


def _dialect() -> str:
    try:
        from backend.repositories.mysql.session import get_engine

        return get_engine().dialect.name or ""
    except Exception:  # noqa: BLE001 —— 拿不到方言就按「非 SQLite」处理
        return ""


@contextmanager
def _write_session() -> Iterator[Any]:
    """一个**写事务**；SQLite 下先拿进程内互斥（见模块文档的并发一节）。"""
    guard = _sqlite_lock if _dialect() == "sqlite" else nullcontext()
    from backend.repositories.mysql.session import new_session

    with guard:
        sess = new_session()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()


# --------------------------------------------------------------------------
# 作用域解析
# --------------------------------------------------------------------------


def scope_ids(config: dict | None) -> dict[str, str]:
    """一条模型配置 → 它适用的作用域 `{scope: scope_id}`。

    密钥那一级**不自己解析**：走 `secret_resolver.describe`（唯一真源）——
    `key_mode='key'` 用 `provider_key_id`；`'auto'` 用该厂商的默认密钥行；
    `'none'` 没有密钥级限额（本地模型 / 免密端点）。自己再写一套解析，迟早出现
    「限额算在 A 密钥上、请求实际用 B 密钥」—— 那等于没有限额。
    """
    cfg = config or {}
    out: dict[str, str] = {}
    model_id = str(cfg.get("id") or "").strip()
    if model_id:
        out["model"] = model_id
    source = secret_resolver.describe(cfg)
    key_id = str(source.get("key_id") or "").strip()
    if key_id:
        out["key"] = key_id
    vendor_id = str(cfg.get("provider") or "").strip()
    if vendor_id:
        out["vendor"] = vendor_id
    return out


def _scope_filter(scope: str, scope_id: str) -> list:
    """限额作用域 → `model_usage` 的行条件。

    `vendor` 级按行上反规范化的 `provider` 列比：`record()` 写的就是
    `cfg["provider"][:32]`，所以这里也要按 32 截断后再比（否则长 id 永远匹配不上）。
    """
    from backend.repositories.mysql.models import ModelUsage

    if scope == "model":
        return [ModelUsage.model_config_id == scope_id]
    if scope == "key":
        return [ModelUsage.provider_key_id == scope_id]
    return [ModelUsage.provider == str(scope_id)[:32]]


def limits_for(config: dict | None) -> list[dict]:
    """这条配置**适用**的限额（存在且 enabled 的才返回）。

    任何异常都按「没有限额」处理：限额是旁路，查不出来不该让调用直接失败
    （与 `usage_stats` 同一条口径）。
    """
    ids = scope_ids(config)
    if not ids:
        return []
    try:
        from sqlalchemy import and_, or_

        from backend.repositories.mysql.models import ModelLimit
        from backend.repositories.mysql.session import session_scope

        wanted = or_(*[and_(ModelLimit.scope == scope, ModelLimit.scope_id == sid)
                       for scope, sid in ids.items()])
        with session_scope() as sess:
            rows = (sess.query(ModelLimit)
                    .filter(ModelLimit.enabled.is_(True), wanted)
                    .order_by(ModelLimit.scope).all())
            return [_row_dict(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("查询限额失败（按没有限额处理）：%s", str(exc)[:200])
        return []


def _row_dict(row) -> dict:
    return {
        "id": row.id, "scope": row.scope, "scope_id": row.scope_id,
        "enabled": bool(row.enabled), "period": row.period,
        "amount": _num(row.amount),
        "currency": billing.normalize_currency(row.currency) or billing.DEFAULT_CURRENCY,
        "note": row.note,
    }


def _label(sess, scope: str, scope_id: str) -> str:
    """人话名字（报错与列表都要能一眼看出是哪一条限额）。"""
    from backend.repositories.mysql.models import (
        ModelConfig, ModelProvider, ModelProviderKey,
    )

    try:
        if scope == "model":
            row = sess.get(ModelConfig, scope_id)
            if row is not None:
                return row.name or row.model_name or scope_id
        elif scope == "key":
            row = sess.get(ModelProviderKey, scope_id)
            if row is not None:
                return row.label or scope_id
        else:
            row = sess.get(ModelProvider, scope_id)
            if row is not None:
                return row.label or scope_id
    except Exception:  # noqa: BLE001 —— 名字取不到就用 id，绝不因此失败
        pass
    return scope_id


# --------------------------------------------------------------------------
# 累计（spent / unpriced / foreign）
# --------------------------------------------------------------------------


def _spent_parts(sess, scope: str, scope_id: str, currency: str,
                 start_utc: datetime, end_utc: datetime) -> tuple[float, int, int]:
    """窗口内该作用域的 (同币种已计费金额, 未计费行数, 其它币种行数)。"""
    from sqlalchemy import func, or_

    from backend.repositories.mysql.models import ModelUsage

    base = _scope_filter(scope, scope_id)
    window = (ModelUsage.created_at >= start_utc, ModelUsage.created_at < end_utc)
    spent = sess.query(func.coalesce(func.sum(ModelUsage.cost), 0.0)).filter(
        *base, *window, ModelUsage.cost.isnot(None),
        ModelUsage.currency == currency,
    ).scalar()
    unpriced = sess.query(func.count(ModelUsage.id)).filter(
        *base, *window, ModelUsage.cost.is_(None)).scalar()
    foreign = sess.query(func.count(ModelUsage.id)).filter(
        *base, *window, ModelUsage.cost.isnot(None),
        or_(ModelUsage.currency.is_(None), ModelUsage.currency != currency),
    ).scalar()
    return _num(spent), _int(unpriced), _int(foreign)


def _reserved(sess, limit_id: str, stamp: datetime) -> float:
    """该限额下**未过期**的预留之和（过期行视为不存在）。"""
    from sqlalchemy import func

    from backend.repositories.mysql.models import ModelLimitReservation

    total = sess.query(func.coalesce(func.sum(ModelLimitReservation.amount), 0.0)).filter(
        ModelLimitReservation.limit_id == limit_id,
        ModelLimitReservation.expires_at > stamp,
    ).scalar()
    return _num(total)


def _snapshot_in(sess, row, stamp: datetime) -> dict:
    """一条限额在 `stamp` 这一刻的完整状态（含预留）。

    `spent` **含未过期预留**：否则「列表说还有余额」和「调用被拦住」会打架，
    用户会以为限额坏了。预留只活一次调用，释放后那部分由真实用量行接管。
    """
    period = str(_field(row, "period", "") or "").strip().lower()
    if period not in billing.PERIOD_KINDS:
        period = "day"
    start_local, end_local = billing.period_bounds(period, stamp)
    start_utc, end_utc = billing.to_utc(start_local), billing.to_utc(end_local)
    currency = (billing.normalize_currency(_field(row, "currency"))
                or billing.DEFAULT_CURRENCY)
    amount = _num(_field(row, "amount", 0.0))
    scope = str(_field(row, "scope", "") or "")
    scope_id = str(_field(row, "scope_id", "") or "")

    usage_spent, unpriced, foreign = _spent_parts(
        sess, scope, scope_id, currency, start_utc, end_utc)
    reserved = _reserved(sess, str(_field(row, "id", "") or ""), stamp)
    spent = round(usage_spent + reserved, billing.COST_PRECISION)
    remaining = round(max(0.0, amount - spent), billing.COST_PRECISION)
    if amount > 0:
        percent = round(spent / amount * 100.0, 1)
    else:
        percent = 100.0 if spent > 0 else 0.0

    return {
        "scope": scope, "scope_id": scope_id,
        "label": _label(sess, scope, scope_id),
        "period": period, "amount": round(amount, billing.COST_PRECISION),
        "currency": currency, "spent": spent, "remaining": remaining,
        "percent": percent,
        "period_start": _fmt(start_local), "period_end": _fmt(end_local),
        "reset_at": _fmt(end_local),
        "unpriced_rows": unpriced, "foreign_currency_rows": foreign,
    }


def snapshot(row, *, now: datetime | None = None) -> dict:
    """公开版 `_snapshot_in`（自己开一个会话）。"""
    from backend.repositories.mysql.session import session_scope

    stamp = _stamp(now)
    with session_scope() as sess:
        return _snapshot_in(sess, row, stamp)


# --------------------------------------------------------------------------
# 估算
# --------------------------------------------------------------------------


def estimate_for(config: dict | None, prompt: str = "",
                 *, completion_tokens: int | None = None) -> float:
    """这次调用**大概**要花多少（只用于预留，**绝不写进 `model_usage.cost`**）。

    口径：`prompt_tokens ≈ len(prompt) / 4`，`completion ≈ parameters.max_tokens / 2`
    （没配 max_tokens 就按 512）。宁可高估一点：高估的代价是这次多占一点点额度
    （调用完就释放），低估的代价是并发放过去、真的超。

    ⚠️ 估算就是**最坏情况**（`max_tokens` 那一档）：把 `max_tokens` 配成 393216 的
    模型，一次估算能到几块钱，于是「额度还剩一块」时它会直接被拦 —— 这是口径
    本身的结果，不是 bug。想让估算贴近实际，就把 `max_tokens` 配成真正会用到的上限。
    算不出价（该模型没配单价）→ 0.0，但仍然要检查（这是兜底：配置阶段已经拦过
    「未配价不许设限额」）。
    """
    cfg = config or {}
    params = cfg.get("parameters") or {}
    if not isinstance(params, dict):
        params = {}
    prompt_tokens = max(0, len(str(prompt or "")) // CHARS_PER_TOKEN)
    explicit = _int(completion_tokens if completion_tokens is not None
                    else params.get("max_tokens"))
    completion = explicit // 2 if explicit else DEFAULT_COMPLETION_TOKENS
    money = billing.cost_of(cfg, prompt_tokens, max(0, completion))
    return round(_num(money.get("cost")), billing.COST_PRECISION)


# --------------------------------------------------------------------------
# 检查 + 预留
# --------------------------------------------------------------------------


def _prune_expired(sess, stamp: datetime) -> int:
    """顺手清理过期预留（判定本身不依赖这一步，见 `_reserved`）。"""
    from backend.repositories.mysql.models import ModelLimitReservation

    try:
        return sess.query(ModelLimitReservation).filter(
            ModelLimitReservation.expires_at <= stamp,
        ).delete(synchronize_session=False)
    except Exception:  # noqa: BLE001 —— 清理失败不该影响判定
        return 0


def _locked_limits(sess, ids: list[str]) -> list:
    """锁住这批限额行（MySQL: `SELECT ... FOR UPDATE`；SQLite: 已被进程锁串行化）。"""
    from backend.repositories.mysql.models import ModelLimit

    query = sess.query(ModelLimit).filter(ModelLimit.id.in_(ids))
    if _dialect() != "sqlite":
        query = query.with_for_update()
    return query.all()


def _exceeded_message(snap: dict, estimate: float) -> str:
    period = _PERIOD_CN.get(snap["period"], snap["period"])
    head = (f"本地限额触发：{snap['label']}（{period}限额 "
            f"{snap['amount']:g} {snap['currency']}）"
            f"已用 {snap['spent']:g}（{snap['percent']}%），"
            f"本次预估 {estimate:g}，调用未发出。")
    tail = ""
    if snap["unpriced_rows"]:
        tail += f" 另有 {snap['unpriced_rows']} 次调用未计费，统计不完整。"
    if snap["foreign_currency_rows"]:
        tail += (f" 另有 {snap['foreign_currency_rows']} 次调用是其它币种，"
                 f"未计入本限额。")
    return head + tail + f" 周期结束（{snap['reset_at']}）后自动恢复。"


def check_and_reserve(config: dict | None, estimate_amount: float | None = None,
                      *, prompt: str = "",
                      now: datetime | None = None) -> dict | None:
    """**发请求之前**检查全部适用限额，通过则占住额度并返回句柄（没有限额返回 None）。

    一个事务里做完四件事：

    1. 对每条适用限额的 `model_limit` 行 `SELECT ... FOR UPDATE`（SQLite 退化为
       进程内互斥 + 串行写事务）；
    2. `spent = 已计费金额 + 未过期预留之和`；
    3. 任意一条 `spent + estimate >= amount` → 回滚并抛 `LimitExceededError`
       （`>=` 而不是 `>`：刚好用满也算用尽，否则最后一次总能多花一笔）；
    4. 全部通过才插入预留（`expires_at = now + 10min`）。

    返回的句柄必须交给 `release()`（`finally` 里）。基础设施异常（连不上库等）
    按「本次放行」处理并记 warning：限额是旁路，库挂了的时候翻译不该整条停摆
    （那时本来也没有一条能落账的用量行）。
    """
    cfg = config or {}
    # SQLite 没有行锁：把**整段检查**（含读限额）串行化，否则一个线程在写预留时
    # 另一个线程的读会撞上 "database is locked"，而那会被下面的兜底当成「放行」——
    # 并发下凭空多放过去几笔，正是这条闸门最不能出错的地方。
    guard = _sqlite_lock if _dialect() == "sqlite" else nullcontext()
    with guard:
        return _check_and_reserve(cfg, estimate_amount, prompt=prompt, now=now)


def _check_and_reserve(cfg: dict, estimate_amount: float | None, *,
                       prompt: str, now: datetime | None) -> dict | None:
    limits = limits_for(cfg)
    if not limits:
        return None
    estimate = None if estimate_amount is None else _num(estimate_amount)
    if estimate is None:
        estimate = estimate_for(cfg, prompt)
    stamp = _stamp(now)
    handle = {"token": new_id("rsv")[:32].replace("-", ""), "ids": [],
              "limit_ids": [], "estimate": estimate}
    try:
        with _write_session() as sess:
            rows = _locked_limits(sess, [row["id"] for row in limits])
            if not rows:
                return None
            _prune_expired(sess, stamp)
            worst = None
            for row in rows:
                snap = _snapshot_in(sess, row, stamp)
                if snap["spent"] + estimate >= _num(_field(row, "amount", 0.0)):
                    if worst is None or snap["percent"] > worst["percent"]:
                        worst = snap
            if worst is not None:
                raise LimitExceededError(
                    _exceeded_message(worst, estimate),
                    detail={"limit": worst, "estimate": estimate},
                )
            from backend.repositories.mysql.models import ModelLimitReservation

            expires_at = stamp + timedelta(minutes=RESERVATION_MINUTES)
            for row in rows:
                reservation_id = new_id("rsv")
                sess.add(ModelLimitReservation(
                    id=reservation_id, limit_id=row.id, token=handle["token"],
                    amount=estimate, created_at=stamp, expires_at=expires_at,
                ))
                handle["ids"].append(reservation_id)
                handle["limit_ids"].append(row.id)
            sess.flush()
    except LimitExceededError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 基础设施异常：放行，理由见 docstring
        log.warning("限额检查失败（本次放行，不影响这次调用）：%s", str(exc)[:200])
        return None
    return handle


def release(reservation: Any) -> None:
    """释放预留（句柄、句柄列表、或 None 都接受）。

    **异常路径漏了就是额度泄漏**：所以调用方的形态是
    `handle = check_and_reserve(...)` / `try: 真调用` / `finally: release(handle)`。
    即便如此仍留了 `expires_at` 这道兜底（进程被 kill 时没人执行 finally）。
    """
    if isinstance(reservation, (list, tuple, set)):
        for item in reservation:
            release(item)
        return
    if not reservation:
        return
    ids = list((reservation or {}).get("ids") or [])
    if not ids:
        return
    try:
        from backend.repositories.mysql.models import ModelLimitReservation
        from backend.repositories.mysql.session import session_scope

        with session_scope() as sess:
            sess.query(ModelLimitReservation).filter(
                ModelLimitReservation.id.in_(ids),
            ).delete(synchronize_session=False)
    except Exception as exc:  # noqa: BLE001 —— 释放失败只能靠过期自愈
        log.warning("限额预留释放失败（%d 分钟后自动失效）：%s",
                    RESERVATION_MINUTES, str(exc)[:200])


# --------------------------------------------------------------------------
# 单价前置校验：没配价的作用域不许设限额
# --------------------------------------------------------------------------


def _enabled_configs(sess) -> list[dict]:
    """所有**启用**的模型配置（限额校验用；不含密钥明文）。"""
    from backend.repositories.mysql.models import ModelConfig

    rows = (sess.query(ModelConfig)
            .filter(ModelConfig.enabled.is_(True))
            .order_by(ModelConfig.id).all())
    return [{
        "id": row.id, "name": row.name, "provider": row.provider,
        "model_type": row.model_type, "model_name": row.model_name,
        "key_mode": getattr(row, "key_mode", None) or "auto",
        "provider_key_id": getattr(row, "provider_key_id", None),
        "price_in": getattr(row, "price_in", None),
        "price_out": getattr(row, "price_out", None),
        "currency": getattr(row, "currency", None) or "",
    } for row in rows]


def scope_configs(scope: str, scope_id: str) -> list[dict]:
    """限额作用域 → 旗下的**启用**模型。

    · `model`：它自己；
    · `vendor`：`provider == scope_id` 的全部启用模型；
    · `key`：**实际解析到这把密钥**的启用模型（口径同 `secret_resolver`）——
      按 `provider_key_id` 硬比会把 `key_mode='auto'` 的模型漏掉，
      于是「这把密钥下的模型都配了价」这句话就不成立了。
    """
    from backend.repositories.mysql.session import session_scope

    try:
        with session_scope() as sess:
            rows = _enabled_configs(sess)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取模型配置失败（限额校验按无模型处理）：%s", str(exc)[:200])
        return []
    if scope == "model":
        return [row for row in rows if row["id"] == scope_id]
    if scope == "vendor":
        wanted = str(scope_id).strip().lower()
        return [row for row in rows
                if str(row.get("provider") or "").strip().lower() == wanted]
    out = []
    for row in rows:
        if str(secret_resolver.describe(row).get("key_id") or "") == scope_id:
            out.append(row)
    return out


def unpriced_models(scope: str, scope_id: str) -> list[dict]:
    """该作用域下**现在算不出钱**的启用模型（`price_of(id).priced == False`）。

    严格用 `priced`（输入价与输出价都要有），不用宽松的历史口径：拿宽松口径
    去放行，就会允许给一个「只配了输入价」的模型设限额 —— 而它每次的 cost 都是 NULL，
    限额永远拦不住任何东西。
    """
    out = []
    for row in scope_configs(scope, scope_id):
        config_id = str(row.get("id") or "")
        try:
            if billing.price_of(config_id).get("priced"):
                continue
        except Exception:  # noqa: BLE001 —— 查价失败按未配价处理（保守）
            pass
        out.append({"id": config_id,
                    "name": row.get("name") or row.get("model_name") or config_id})
    return out


def _ensure_entity(scope: str, scope_id: str) -> None:
    """scope / scope_id 必须是真实存在的实体，否则 400 并点名。"""
    from sqlalchemy import func

    from backend.repositories.mysql.models import (
        ModelConfig, ModelProvider, ModelProviderKey,
    )
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        if scope == "model":
            if sess.get(ModelConfig, scope_id) is None:
                raise ValidationError(f"模型不存在：{scope_id}",
                                      detail={"scope": scope, "scope_id": scope_id})
            return
        if scope == "key":
            if sess.get(ModelProviderKey, scope_id) is None:
                raise ValidationError(f"密钥不存在：{scope_id}",
                                      detail={"scope": scope, "scope_id": scope_id})
            return
        if sess.get(ModelProvider, scope_id) is not None:
            return
        # 提供方行是**惰性**建的（先有模型、后有 provider 行）：模型上有这个
        # provider 字符串就认它，否则用户会碰到「明明配了模型却说厂商不存在」。
        hit = (sess.query(ModelConfig.id)
               .filter(func.lower(ModelConfig.provider) == scope_id.strip().lower())
               .first())
        if hit is None:
            raise ValidationError(f"厂商不存在：{scope_id}",
                                  detail={"scope": scope, "scope_id": scope_id})


# --------------------------------------------------------------------------
# 查询 / 写入（接口层用）
# --------------------------------------------------------------------------


def _item(row: dict, usable: bool, *, now: datetime | None = None) -> dict:
    snap = snapshot(row, now=now)
    item = {"id": row["id"], "enabled": row["enabled"], "note": row.get("note") or ""}
    item.update({key: snap[key] for key in SNAPSHOT_FIELDS})
    item["usable"] = bool(usable)
    return item


def list_limits(*, now: datetime | None = None) -> list[dict]:
    """全部限额 + 每条**现算**的 spent / remaining / percent / reset_at。

    `usable` = `enabled` 且该作用域下全部启用模型都 `priced_now`：
    不可用的那条**照样会拦**（判定不看这句话），它只是告诉前端「这条限额统计不完整，
    先去配单价」。
    """
    from backend.repositories.mysql.models import ModelLimit
    from backend.repositories.mysql.session import session_scope

    try:
        with session_scope() as sess:
            rows = [_row_dict(row) for row in
                    sess.query(ModelLimit).order_by(ModelLimit.scope,
                                                    ModelLimit.scope_id).all()]
    except Exception as exc:  # noqa: BLE001 —— 空库 / 查询失败 → 空列表，绝不 500
        log.warning("限额列表查询失败（返回空列表）：%s", str(exc)[:200])
        return []

    cache: dict[tuple, bool] = {}
    items = []
    for row in rows:
        scope_key = (row["scope"], row["scope_id"])
        if scope_key not in cache:
            try:
                cache[scope_key] = not unpriced_models(row["scope"], row["scope_id"])
            except Exception:  # noqa: BLE001
                cache[scope_key] = False
        try:
            items.append(_item(row, row["enabled"] and cache[scope_key], now=now))
        except Exception as exc:  # noqa: BLE001 —— 单条坏行不该毁掉整张列表
            log.warning("限额 %s 统计失败（跳过）：%s", row["id"], str(exc)[:200])
    return items


def summary(*, now: datetime | None = None) -> dict:
    """横幅用的三态：`none` / `warn`（>=80%）/ `exceeded`（>=100%）。

    只看 `enabled` 的条目：停用的限额不拦任何调用，拿它去报警就是纯噪音
    （`usable` 也不看 —— 那是「统计完整不完整」，与「拦不拦」无关）。
    `most_urgent` = 百分比最高的那条（并列先看 percent、再看 reset_at 更近）。
    没有限额配置 → `{level:"none", most_urgent:null, items:[]}`，不报错。
    """
    items = list_limits(now=now)
    active = [item for item in items if item["enabled"]]
    level = "none"
    if any(item["percent"] >= 100 for item in active):
        level = "exceeded"
    elif any(item["percent"] >= 80 for item in active):
        level = "warn"
    active.sort(key=lambda item: (-item["percent"], item["reset_at"], item["id"]))
    return {"level": level,
            "most_urgent": active[0] if active else None,
            "items": items}


def _positive_amount(value: Any) -> float:
    if isinstance(value, bool):
        raise ValidationError("amount 必须是数字（金额），收到布尔值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"amount 必须是数字（金额）：{value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError(f"amount 必须是有限的数字：{value!r}")
    if number <= 0:
        raise ValidationError(f"amount 必须大于 0：{value!r}（不限额 = 删掉这条限额）")
    return number


def _find(scope: str, scope_id: str):
    from backend.repositories.mysql.models import ModelLimit
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        return (sess.query(ModelLimit)
                .filter(ModelLimit.scope == scope, ModelLimit.scope_id == scope_id)
                .first())


def upsert(payload: dict, *, now: datetime | None = None) -> dict:
    """保存一条限额（作用域内**至多一条**：有则更新，无则新建）。

    `period='none'` = 删掉这条（唯一能表达「从此不再限额」的写法）；
    `enabled=false` = 停用但留痕（列表里还能看到它，usable=false）。
    """
    from backend.repositories.mysql.models import ModelLimit
    from backend.repositories.mysql.session import session_scope

    data = payload or {}
    scope = str(data.get("scope") or "").strip().lower()
    if scope not in SCOPES:
        raise ValidationError(f"scope 必须是 {'/'.join(SCOPES)}：{data.get('scope')!r}")
    scope_id = str(data.get("scope_id") or "").strip()
    if not scope_id:
        raise ValidationError("scope_id 必填（模型 id / 密钥 id / 厂商 id）")
    if len(scope_id) > 64:
        raise ValidationError(f"scope_id 过长（最多 64）：{scope_id[:80]}")

    period = str(data.get("period") or "day").strip().lower()
    if period == PERIOD_NONE:
        removed = delete(scope=scope, scope_id=scope_id)
        return {"deleted": True, "scope": scope, "scope_id": scope_id,
                "removed": removed}

    if period not in billing.PERIOD_KINDS:
        raise ValidationError(
            f"period 必须是 {'/'.join(billing.PERIOD_KINDS)} 或 '{PERIOD_NONE}'："
            f"{data.get('period')!r}")
    enabled = data.get("enabled")
    enabled = True if enabled is None else bool(enabled)
    amount = _positive_amount(data.get("amount"))
    currency = billing.normalize_currency(
        data.get("currency") or billing.DEFAULT_CURRENCY, allow_empty=False)

    # 校验顺序固定：实体存在 → 单价配齐 → 数字合理。先报「实体不存在」比先报
    # 「金额不合法」有用（用户真正需要修的是前者）。
    _ensure_entity(scope, scope_id)
    missing = unpriced_models(scope, scope_id)
    if missing:
        names = "、".join(item["name"] for item in missing)
        raise ValidationError(
            f"该作用域下还有模型没配单价，不能设限额（限额永远拦不住一个算不出钱的模型）："
            f"{names}",
            detail={"unpriced_models": missing, "scope": scope, "scope_id": scope_id},
        )

    note = str(data.get("note") or "").strip()[:255] or None
    with session_scope() as sess:
        row = (sess.query(ModelLimit)
               .filter(ModelLimit.scope == scope, ModelLimit.scope_id == scope_id)
               .first())
        if row is None:
            row = ModelLimit(id=new_id("lim")[:64], scope=scope, scope_id=scope_id,
                             enabled=enabled, period=period, amount=amount,
                             currency=currency, note=note)
            sess.add(row)
        else:
            row.enabled = enabled
            row.period = period
            row.amount = amount
            row.currency = currency
            row.note = note
        sess.flush()
        saved = _row_dict(row)
    usable = saved["enabled"] and not unpriced_models(scope, scope_id)
    return _item(saved, usable, now=now)


def delete(limit_id: str | None = None, *, scope: str = "",
           scope_id: str = "") -> int:
    """删限额（按 id，或按「作用域 + 实体」）。返回删掉的条数。"""
    from backend.repositories.mysql.models import ModelLimit
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        query = sess.query(ModelLimit)
        if limit_id:
            query = query.filter(ModelLimit.id == limit_id)
        elif scope and scope_id:
            query = query.filter(ModelLimit.scope == scope,
                                 ModelLimit.scope_id == scope_id)
        else:
            raise ValidationError("delete 需要 limit_id 或 (scope, scope_id)")
        removed = query.delete(synchronize_session=False)
    if limit_id and not removed:
        raise NotFoundError(f"限额不存在：{limit_id}")
    return int(removed or 0)
