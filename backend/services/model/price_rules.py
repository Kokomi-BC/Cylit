"""分时段（峰谷）单价的**时段规则**：存储 + 校验 + 接口形状（``/models/{id}/price-rules``）。

口径只有一条，写在这里，别在别处再实现一遍（计算口径在 ``billing.price_of``）：

· ``model_config`` 上的四个单价 = **时段之外**的价（DeepSeek 口径里就是**空闲价**）；
· 这张表里的行 = 一段时段 + 它自己的一套单价，是**例外覆盖**：
  命中哪一段就用哪一段的价；
· 规则**只对工作日（周一至周五）生效** —— 周末一律落到默认价。所以
  "周末走空闲价"不用额外配置，它是"没命中规则"的自然结果；
· 时段**不带币种**：币种永远只认模型 → 厂商 → CNY 这条链。

为什么"整批替换"是唯一的写入口（PUT，不提供逐条新增）：时段是**一个集合**，
"同一模型内不能重叠"是集合级约束。逐条新增会让"加到第九条时才发现跟前八条里面的
某一条撞了"变成常态，而整批替换可以在同一个事务里先整体校验、再先删后插 ——
校验通过的那一刻落库的就是**校验过的那一版**。
"""

from __future__ import annotations

from typing import Any

from backend.core.errors import NotFoundError, ValidationError
from backend.core.utils import new_id, now
from backend.services.model import billing

#: 接口里那句说明（前端直接展示，别在两处各写一句）
NOTE = "默认单价用于时段之外的时刻（含周末）；时段规则只在工作日生效"

#: 没有默认价时 PUT 返回的警告（保存允许，但要说清"时段之外仍然不计费"）
NO_DEFAULT_PRICE_WARNING = "默认价未配置，时段之外的调用仍按未计费处理"


def _require_model(sess, model_config_id: str):
    """取模型行；不存在时的报错与 ``model_manager.get_config`` **逐字一致**。

    为什么不直接调 ``model_service.get_model``：它是本模块的**调用方**
    （列表/详情要补 price_rule_count 与 price_rules），调回去就是循环导入；
    而错误文案只要一样，用户就分不出是哪个入口 —— 那正是我们要的。
    """
    from backend.repositories.mysql.models import ModelConfig

    row = sess.get(ModelConfig, str(model_config_id))
    if row is None:
        raise ValidationError(f"模型不存在：{model_config_id}")
    return row


def _rows(sess, model_config_id: str) -> list[dict]:
    """这个模型的全部时段（接口形状；按开始时间排序，与人读的顺序一致）。"""
    from backend.repositories.mysql.models import ModelPriceRule

    rows = sess.query(ModelPriceRule).filter(
        ModelPriceRule.model_config_id == str(model_config_id)).all()
    items = [{
        "id": row.id, "label": row.label or "",
        "start": row.start, "end": row.end,
        "price_in": row.price_in, "price_out": row.price_out,
        "price_cache_read": row.price_cache_read,
        "price_cache_write": row.price_cache_write,
    } for row in rows]
    items.sort(key=lambda item: (str(item["start"]), str(item["end"]), str(item["id"])))
    return items


def list_rules(model_config_id: str) -> list[dict]:
    """一个模型的时段规则（不校验模型是否存在：未知 id 就是空数组）。"""
    from backend.repositories.mysql.session import session_scope

    if not model_config_id:
        return []
    with session_scope() as sess:
        return _rows(sess, model_config_id)


def counts(model_config_ids: Any = None) -> dict[str, int]:
    """各模型有几段时段（模型列表要的 price_rule_count）。

    ``model_config_ids`` 给 None 就统计全表；给一组 id 只统计这些
    （列表页一次查完，别在循环里一行一查）。
    """
    from sqlalchemy import func

    from backend.repositories.mysql.models import ModelPriceRule
    from backend.repositories.mysql.session import session_scope

    out: dict[str, int] = {}
    try:
        with session_scope() as sess:
            query = sess.query(ModelPriceRule.model_config_id, func.count(ModelPriceRule.id))
            ids = {str(cid) for cid in (model_config_ids or ()) if cid}
            if ids:
                query = query.filter(ModelPriceRule.model_config_id.in_(ids))
            for config_id, total in query.group_by(ModelPriceRule.model_config_id).all():
                out[str(config_id)] = int(total)
    except Exception as exc:  # noqa: BLE001 —— 列表页不该因为"数不了时段"而整体失败
        from backend.core.logging import get_logger

        get_logger(__name__).warning("统计时段规则数失败（按 0 处理）：%s", str(exc)[:200])
    return out


def describe(model_config_id: str) -> dict:
    """GET /models/{model_id}/price-rules 的响应体。

    ``default`` 是"时段之外"那套价：``currency`` 是模型行自己写的（'' = 跟随厂商），
    ``currency_effective`` 是解析后的结果（模型 → 厂商 → CNY）—— 与 ``GET /models`` 同一口径，
    前端不用自己再猜一遍链子。
    ``has_price`` / ``priced_now`` 都是 ``billing.price_of(...)['priced']``：默认价齐不齐，
    **与"现在是否命中时段"无关**（所以这两个键在语义上永远相等，留两个名字是为了
    接口读起来不歧义 —— 一个说的是配置，一个说的是"此刻能不能算钱"）。
    """
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = _require_model(sess, model_config_id)
        price = billing.price_of(model_config_id, sess=sess)
        items = _rows(sess, model_config_id)
        raw_currency = billing.normalize_currency(getattr(row, "currency", None))
    priced = bool(price["priced"])
    return {
        "default": {
            "price_in": price["price_in"],
            "price_out": price["price_out"],
            "price_cache_read": price["price_cache_read"],
            "price_cache_write": price["price_cache_write"],
            "currency": raw_currency,
            "currency_effective": price["currency"],
            "has_price": priced,
            "priced_now": priced,
        },
        "items": items,
        "note": NOTE,
    }


def _named(rule: dict) -> str:
    """错误信息里点名一段时段：``09:00–12:00（高峰）``。"""
    label = str(rule.get("label") or "").strip()
    span = f"{rule.get('start')}–{rule.get('end')}"
    return f"时段 {span}（{label}）" if label else f"时段 {span}"


def _clean_items(items: Any) -> list[dict]:
    """逐条校验 + 两两判重叠；任何一条不合法就整批拒（不会存一半）。"""
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValidationError("items 必须是时段规则数组（要清空就传空数组）")
    cleaned: list[dict] = []
    for index, raw in enumerate(items, start=1):
        try:
            cleaned.append(billing.clean_rule(raw))
        except ValidationError as exc:
            raise ValidationError(f"第 {index} 条时段规则不合法：{exc.message}") from exc
    for i in range(len(cleaned)):
        for j in range(i + 1, len(cleaned)):
            if billing.rules_overlap(cleaned[i], cleaned[j]):
                raise ValidationError(
                    f"{_named(cleaned[i])} 与 {_named(cleaned[j])} 重叠"
                    "（同一模型的时段之间不允许重叠）")
    return cleaned


def replace(model_config_id: str, payload: Any) -> dict:
    """整批替换一个模型的时段（同一事务：校验 → 先删后插）。

    ``payload`` 是请求体：``{items: [{label, start, end, price_in, price_out,
    price_cache_read, price_cache_write}, ...]}``。

    模型**没有默认价**也允许保存（用户可能先配时段、再配默认价），但响应里带
    ``warning`` 说清"时段之外的调用仍按未计费处理" —— 静默保存才是坑。
    """
    from backend.repositories.mysql.models import ModelPriceRule
    from backend.repositories.mysql.session import session_scope

    body = payload if isinstance(payload, dict) else {}
    cleaned = _clean_items(body.get("items"))
    stamp = now()
    with session_scope() as sess:
        _require_model(sess, model_config_id)
        sess.query(ModelPriceRule).filter(
            ModelPriceRule.model_config_id == str(model_config_id)
        ).delete(synchronize_session=False)
        for rule in cleaned:
            sess.add(ModelPriceRule(id=new_id("rule"), model_config_id=str(model_config_id),
                                    created_at=stamp, updated_at=stamp, **rule))
    out = describe(model_config_id)
    if not out["default"]["has_price"]:
        out["warning"] = NO_DEFAULT_PRICE_WARNING
    return out


def delete(model_config_id: str, rule_id: str) -> dict:
    """删掉一段时段（不存在 → 404，与限额删除同一口径）。"""
    from backend.repositories.mysql.models import ModelPriceRule
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        row = sess.get(ModelPriceRule, str(rule_id))
        if row is None or str(row.model_config_id) != str(model_config_id):
            raise NotFoundError(f"时段规则不存在：{rule_id}")
        sess.delete(row)
    return {"removed": str(rule_id)}
