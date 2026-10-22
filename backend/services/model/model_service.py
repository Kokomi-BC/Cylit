"""模型服务：把 ModelManager 的字典结果整理为 API 契约（规划 §20.7）。"""

from __future__ import annotations

from backend.services.model.model_manager import MODEL_TYPES, model_manager


def list_models(model_type: str | None = None) -> list[dict]:
    out = []
    for cfg in model_manager.list_configs(model_type):
        cfg = dict(cfg)
        cfg["has_secret"] = bool(cfg.get("secret_ref"))
        cfg["is_mock"] = cfg.get("provider") in ("mock", "hash", "heuristic")
        out.append(cfg)
    return out


def get_model(model_id: str) -> dict:
    return model_manager.get_config(model_id)


def create_model(payload: dict) -> dict:
    return model_manager.create(payload)


def update_model(model_id: str, payload: dict) -> dict:
    return model_manager.update(model_id, payload)


def delete_model(model_id: str) -> dict:
    model_manager.delete(model_id)
    return {"deleted": model_id}


def test_model(model_id: str, key_id: str | None = None) -> dict:
    # key_id = "换一把密钥试算"（不落库，见 model_manager.test）
    return model_manager.test(model_id, key_id=key_id)


def set_default(model_id: str) -> dict:
    return model_manager.set_default(model_id)


def types_summary() -> dict:
    return {"types": list(MODEL_TYPES), "defaults": model_manager.types_summary()}
