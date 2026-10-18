"""问答（agent）类型模型必须能用生成 Provider 探测。

回归点：`ModelManager._get` 原来只认 generation / embedding / reranker / vision，
遇到 `model_type == "agent"` 直接抛 `ModelNotConfiguredError` —— 于是设置页点「测试」
永远失败，提示"未配置可用的 agent 模型"，可那一行明明存在、默认也设了。
"""

from __future__ import annotations

from backend.services.model.model_manager import ModelManager


def _agent_cfg() -> dict:
    return {
        "id": "agent-test",
        "name": "问答模型",
        "provider": "mock",
        "model_type": "agent",
        "model_name": "mock-chat",
        "base_url": "",
        "parameters": {},
        "secret_ref": "",
        "enabled": True,
        "is_default": True,
    }


def test_agent_config_builds_a_provider() -> None:
    mm = ModelManager()
    provider = mm._get(_agent_cfg())
    assert provider is not None
    assert hasattr(provider, "generate")


def test_agent_config_is_cached_by_id() -> None:
    mm = ModelManager()
    first = mm._get(_agent_cfg())
    second = mm._get(_agent_cfg())
    assert first is second
