"""模型提供方目录 / 端点守卫 / 远程模型列表（改造计划 §2.1、§7）。

这几条是纯逻辑 + 一个本地假 HTTP 服务，不联网、不依赖 MySQL：

· `endpoint_guard`：协议、userinfo、元数据地址、DNS 解析结果、内网放行口径；
· `providers_catalog`：官方目录的 kind/icon 由后端算出来，且不含任何密钥字段；
· `remote_models`：能解析三种常见形状，错误里不出现密钥。
"""

from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.services.model import endpoint_guard


def test_normalize_rejects_bad_shapes():
    for bad in ("", "   ", "ftp://x.com/v1", "api.deepseek.com/v1",
                "https://u:p@api.deepseek.com/v1", "https://api.deepseek.com/v1?a=1",
                "https://api.deepseek.com/v1#x"):
        with pytest.raises(ValidationError):
            endpoint_guard.normalize_base_url(bad)


def test_normalize_strips_trailing_slash_and_lowercases_scheme():
    assert endpoint_guard.normalize_base_url("HTTPS://API.DeepSeek.com/v1/") == \
        "https://api.deepseek.com/v1"


def test_guard_blocks_cloud_metadata_even_when_private_allowed():
    for url in ("http://169.254.169.254/latest", "http://100.100.100.200/latest/meta-data"):
        with pytest.raises(ValidationError):
            endpoint_guard.guard_url_sync(url, allow_private=True)


def test_guard_blocks_loopback_unless_allowed():
    with pytest.raises(ValidationError):
        endpoint_guard.guard_url_sync("http://127.0.0.1:11434/v1", allow_private=False)
    assert endpoint_guard.guard_url_sync("http://127.0.0.1:11434/v1", allow_private=True)


def test_guard_blocks_private_ranges():
    for url in ("http://10.0.0.5/v1", "http://192.168.1.9/v1", "http://172.16.3.4/v1"):
        with pytest.raises(ValidationError):
            endpoint_guard.guard_url_sync(url, allow_private=False)


def test_allow_private_for_stored_vs_fresh(monkeypatch):
    monkeypatch.delenv(endpoint_guard.ALLOW_PRIVATE_ENV, raising=False)
    assert endpoint_guard.allow_private_for(stored_config=False) is False
    assert endpoint_guard.allow_private_for(stored_config=True) is True
    monkeypatch.setenv(endpoint_guard.ALLOW_PRIVATE_ENV, "0")
    assert endpoint_guard.allow_private_for(stored_config=True) is False
    monkeypatch.setenv(endpoint_guard.ALLOW_PRIVATE_ENV, "1")
    assert endpoint_guard.allow_private_for(stored_config=False) is True


def test_pick_ids_accepts_three_shapes():
    from backend.services.model import remote_models

    a = remote_models._pick_ids({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]})
    assert [x["id"] for x in a] == ["a", "b"]
    b = remote_models._pick_ids({"models": ["x", "y"]})
    assert [x["id"] for x in b] == ["x", "y"]
    c = remote_models._pick_ids([{"id": "m", "owned_by": "deepseek"}])
    assert c == [{"id": "m", "owned_by": "deepseek"}]
    with pytest.raises(ValidationError):
        remote_models._pick_ids({"nope": 1})


def test_embedding_protocol_is_model_level_not_a_provider():
    """「多模态向量」是**模型级协议**，不是一家提供方（已折叠进 dashscope）。

    以前 ``provider=dashscope_multimodal`` 指向 DashScopeMultimodalEmbedding（原生多模态
    端点，不是 OpenAI 兼容的 /embeddings）；现在模型自带 ``parameters.protocol``，
    provider 一律是 dashscope —— 同一家阿里云不再被拆成两个厂商。
    """
    from backend.providers.embedding.providers import (
        DashScopeMultimodalEmbedding, OpenAICompatibleEmbedding,
    )
    from backend.providers.factory import build_embedding_provider

    multi = build_embedding_provider(
        {"provider": "dashscope", "parameters": {"protocol": "dashscope_multimodal"}})
    assert isinstance(multi, DashScopeMultimodalEmbedding)

    plain = build_embedding_provider({"provider": "dashscope",
                                      "parameters": {"dim": 1024}})
    assert isinstance(plain, OpenAICompatibleEmbedding)

    # 老配置（provider 直接还是多模态家）行为不变：照样落到多模态实现
    legacy = build_embedding_provider({"provider": "dashscope_multimodal"})
    assert isinstance(legacy, DashScopeMultimodalEmbedding)

    # 协议写错不静默降级成 mock：按 provider 回落
    unknown = build_embedding_provider({"provider": "dashscope",
                                        "parameters": {"protocol": "no-such-protocol"}})
    assert isinstance(unknown, OpenAICompatibleEmbedding)


def test_provider_catalog_is_written_by_backend_and_no_secret_leak(monkeypatch):
    """目录是后端**写死的清单**（PROVIDER_META），不再从"一键导入预设"派生。

    目录里每一条都必须是真实 provider id（= model_config.provider 可能取到的值）：
    三家本地档（ollama / local / local_bge）与 dashscope_rag（RAG 模板的归类名，
    从来不是 provider id）都标 hidden；dashscope_multimodal 保留 —— 库里有模型在用它。
    """
    from backend.services.model import model_service, providers_catalog

    # 目录只向库里要 configured 计数，把这次调用桩掉就不碰数据库
    monkeypatch.setattr(model_service, "list_models", lambda model_type=None: [])
    data = providers_catalog.catalog()
    ids = [p["id"] for p in data["providers"]]
    assert sorted(ids) == ["anthropic", "dashscope", "deepseek", "doubao", "gemini",
                           "minimax", "moonshot", "openai", "zhipu"]
    assert "ollama" not in ids and "local" not in ids and "local_bge" not in ids
    assert "dashscope_rag" not in ids
    # 多模态向量已折叠进百炼：目录里不再有这一家
    assert "dashscope_multimodal" not in ids

    deep = next(p for p in data["providers"] if p["id"] == "deepseek")
    assert deep["kind"] == "official"
    assert deep["icon"] == "deepseek"
    assert deep["types"] == ["generation", "agent", "vision"]
    assert deep["base_url"] == "https://api.deepseek.com/v1"

    # 每家都得有默认端点与能力，且不再有「几套预设」这种字段
    for entry in data["providers"]:
        assert entry["base_url"].startswith("http"), entry["id"]
        assert entry["types"], entry["id"]
        # 字段集固定（前端照这个渲染）；「几套模板」那种派生字段已随模板下线
        assert set(entry) == {"id", "kind", "label", "icon", "note", "auth",
                              "base_url", "types", "configured"}, entry["id"]

    # 百炼一家同时能跑生成 / 向量 / 重排（多模态向量由模型级协议决定，不再单列一家）
    dashscope = next(p for p in data["providers"] if p["id"] == "dashscope")
    assert dashscope["icon"] == "qwen"
    assert "embedding" in dashscope["types"] and "reranker" in dashscope["types"]

    assert data["custom"]["kind"] == "custom"
    blob = repr(data)
    assert "deepseek.key" not in blob          # 凭据文件名不外泄
    assert "sk-" not in blob
