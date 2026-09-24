"""Provider 工厂（规划 §12.2：业务只写 ``model_type``，不写模型名）。

根据 ``model_config`` 的 ``provider`` 字段实例化对应实现；
未配置任何模型时回落到 mock（保证离线可跑，规划 §1.2）。
"""

from __future__ import annotations

from backend.core.errors import ModelNotConfiguredError
from backend.core.logging import get_logger
from backend.providers.base import (
    EmbeddingProvider, GenerationProvider, RerankerProvider,
)
from backend.providers.embedding.providers import (
    DashScopeMultimodalEmbedding, DashScopeReranker,
    HeuristicReranker, LocalCrossEncoderReranker,
    LocalSentenceTransformerEmbedding, OpenAICompatibleEmbedding,
)
from backend.providers.generation.mock import MockEmbedding, MockGeneration
from backend.providers.generation.openai_compatible import (
    OpenAICompatibleGeneration,
)

log = get_logger(__name__)

GENERATION_PROVIDERS = {
    "mock": MockGeneration,
    "openai_compatible": OpenAICompatibleGeneration,
    "openai": OpenAICompatibleGeneration,
    "deepseek": OpenAICompatibleGeneration,
    "dashscope": OpenAICompatibleGeneration,
    "qwen": OpenAICompatibleGeneration,
    "zhipu": OpenAICompatibleGeneration,
    "moonshot": OpenAICompatibleGeneration,
    "ollama": OpenAICompatibleGeneration,     # Ollama 提供 OpenAI 兼容端点
    "vllm": OpenAICompatibleGeneration,
    "local": MockGeneration,                  # 本地未接入时回落 mock
}

EMBEDDING_PROVIDERS = {
    "mock": MockEmbedding,
    "hash": MockEmbedding,
    "local": LocalSentenceTransformerEmbedding,
    "sentence_transformers": LocalSentenceTransformerEmbedding,
    "openai_compatible": OpenAICompatibleEmbedding,
    "openai": OpenAICompatibleEmbedding,
    "dashscope": OpenAICompatibleEmbedding,
    "qwen": OpenAICompatibleEmbedding,
    "zhipu": OpenAICompatibleEmbedding,
    # 多模态向量（qwen3-vl-embedding）：**不是** OpenAI 兼容端点，单独一条路
    "dashscope_multimodal": DashScopeMultimodalEmbedding,
    "dashscope_vl": DashScopeMultimodalEmbedding,
    "qwen_vl": DashScopeMultimodalEmbedding,
}

RERANKER_PROVIDERS = {
    "mock": HeuristicReranker,
    "heuristic": HeuristicReranker,
    "local": LocalCrossEncoderReranker,
    "cross_encoder": LocalCrossEncoderReranker,
    "sentence_transformers": LocalCrossEncoderReranker,
    # DashScope 原生排序端点（qwen3-vl-rerank / qwen3.7-text-rerank / …）
    "dashscope": DashScopeReranker,
    "qwen": DashScopeReranker,
    "dashscope_rerank": DashScopeReranker,
}


def build_generation_provider(config: dict, api_key: str | None = None,
                              *, allow_mock_fallback: bool = True
                              ) -> GenerationProvider:
    name = (config.get("provider") or "mock").lower()
    cls = GENERATION_PROVIDERS.get(name)
    if cls is None:
        if not allow_mock_fallback:
            raise ModelNotConfiguredError("generation")
        log.warning("未知 generation provider %r，回落到 mock", name)
        cls = MockGeneration
    return cls(config, api_key)


def build_embedding_provider(config: dict, api_key: str | None = None,
                             *, allow_mock_fallback: bool = True) -> EmbeddingProvider:
    name = (config.get("provider") or "mock").lower()
    cls = EMBEDDING_PROVIDERS.get(name)
    if cls is None:
        if not allow_mock_fallback:
            raise ModelNotConfiguredError("embedding")
        log.warning("未知 embedding provider %r，回落到 mock", name)
        cls = MockEmbedding
    return cls(config, api_key)


def build_reranker_provider(config: dict, api_key: str | None = None
                            ) -> RerankerProvider:
    name = (config.get("provider") or "heuristic").lower()
    cls = RERANKER_PROVIDERS.get(name, HeuristicReranker)
    return cls(config, api_key)
