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
from backend.providers.generation.anthropic import AnthropicGeneration
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
    # 下面几家都提供 **OpenAI 兼容**端点（不是原生协议）：
    #   · OpenAI / Gemini：官方文档明确的 /chat/completions 兼容层；
    #   · Anthropic：官方 OpenAI SDK 兼容层（beta），model 名仍用 claude-*；
    #   · 豆包 = 火山方舟 Ark，model 填「推理接入点 ID」（ep-… 或 doubao-…）。
    # 别名多写几个：用户手填 provider 时写 anthropic / claude 都能落到同一条路。
    # Claude 走**原生 Messages API**（OpenAI 兼容层只是 beta，字段语义不同）
    "anthropic": AnthropicGeneration,
    "claude": AnthropicGeneration,
    "gemini": OpenAICompatibleGeneration,
    "google": OpenAICompatibleGeneration,
    "doubao": OpenAICompatibleGeneration,
    "volcengine": OpenAICompatibleGeneration,
    "ark": OpenAICompatibleGeneration,
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


def embedding_protocol(config: dict) -> str:
    """模型自己声明的向量「协议」（``parameters.protocol``），没有就是空串。

    为什么要模型级协议：能力本来是挂在 ``provider`` 上的，于是"多模态向量"被迫变成
    一家提供方（dashscope_multimodal）—— 同一家阿里云被拆成两个厂商，密钥、图标、
    目录全是重复的。协议下沉到模型参数后，一家厂商可以同时有多种向量协议。
    """
    params = config.get("parameters") or {}
    return str(params.get("protocol") or "").strip().lower()


def build_embedding_provider(config: dict, api_key: str | None = None,
                             *, allow_mock_fallback: bool = True) -> EmbeddingProvider:
    """按 ``protocol``（优先）或 ``provider`` 选向量实现。

    ``parameters.protocol`` 命中已知实现就用它（``dashscope_multimodal`` = qwen3-vl 原生
    多模态端点，**不是** OpenAI 兼容的 /embeddings）；没写 protocol 或协议不认识时，
    仍按 provider 查表 —— 老配置（provider=dashscope_multimodal）因此照样能用。
    """
    protocol = embedding_protocol(config)
    name = protocol or (config.get("provider") or "mock").lower()
    cls = EMBEDDING_PROVIDERS.get(name)
    if cls is None and protocol:
        # 协议写了个不认识的：别静默降级成 mock，回落按 provider 查表（行为与改造前一致）
        log.warning("未知 embedding protocol %r，按 provider 回落", protocol)
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
