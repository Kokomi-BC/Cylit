"""模型服务：把 ModelManager 的字典结果整理为 API 契约（规划 §20.7）。"""

from __future__ import annotations

from backend.services.model import billing, price_rules
from backend.services.model.model_manager import MODEL_TYPES, model_manager


def _with_price(cfg: dict) -> dict:
    """补「生效币种」与「配没配单价」（口径全在 billing.price_of，这里不重算）。

    currency_effective 是**解析后**的结果（模型 → 厂商 → CNY）：前端要显示
    "这行按什么币种记账"，就不能让它自己再猜一遍链子。
    """
    price = billing.price_of(cfg.get("id"))
    cfg["currency_effective"] = price["currency"]
    cfg["has_price"] = bool(price["priced"])
    return cfg


def list_models(model_type: str | None = None) -> list[dict]:
    rows = model_manager.list_configs(model_type)
    # 时段条数一次查完（一行一次会把列表页变成 N+1 次查询，而模型页每次刷新都调它）
    rule_counts = price_rules.counts([cfg.get("id") for cfg in rows])
    out = []
    for cfg in rows:
        cfg = _with_price(dict(cfg))
        # ⚠️ 仅供参考：这里只认"模型行自己写了 secret_ref"（老口径），不认识
        # "跟随提供方默认密钥 / key_mode=none"。**界面的权威口径是 API 层**
        # （api/models.py::_with_key_source 按 key_source 覆盖后的 has_secret）——
        # 别拿这里的值去判"未配置密钥"，否则会误报（实测 vision 那行）。
        cfg["has_secret"] = bool(cfg.get("secret_ref"))
        cfg["is_mock"] = cfg.get("provider") in ("mock", "hash", "heuristic")
        # 配了几段时段（列表只给条数：完整时段是详情那一份的事）
        cfg["price_rule_count"] = int(rule_counts.get(str(cfg.get("id")), 0))
        out.append(cfg)
    return out


def get_model(model_id: str) -> dict:
    cfg = _with_price(model_manager.get_config(model_id))
    # 详情带完整时段：编辑弹窗要一次拿全，不能再去请求一次 price-rules
    cfg["price_rules"] = price_rules.list_rules(str(cfg.get("id")))
    return cfg


def create_model(payload: dict) -> dict:
    # 单价 / 币种校验发生在 model_manager（唯一的写入点）：负数 / 非数字 / 怪币种
    # 会抛 ValidationError，这里透传，不自己再判一遍。
    return _with_price(model_manager.create(payload))


def update_model(model_id: str, payload: dict) -> dict:
    return _with_price(model_manager.update(model_id, payload))


def delete_model(model_id: str) -> dict:
    model_manager.delete(model_id)
    return {"deleted": model_id}


def test_model(model_id: str, key_id: str | None = None) -> dict:
    # key_id = "换一把密钥试算"（不落库，见 model_manager.test）
    return model_manager.test(model_id, key_id=key_id)


def set_default(model_id: str) -> dict:
    return _with_price(model_manager.set_default(model_id))


def types_summary() -> dict:
    return {"types": list(MODEL_TYPES), "defaults": model_manager.types_summary()}
