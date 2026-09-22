"""providers/：模型抽象（规划 §12.1 五类 Provider）。

只负责"调用模型"，**不感知业务语义**（规划 §6.1）。
所有实现返回统一错误（``ProviderError`` / ``RateLimitError``），
由 services 层的调度器决定重试与降级。
"""

from backend.providers.base import (
    EmbeddingProvider, GenerationProvider, OCRProvider, ProviderBase,
    RerankerProvider, VisionProvider,
)
from backend.providers.factory import (
    build_embedding_provider, build_generation_provider, build_reranker_provider,
)

__all__ = [
    "ProviderBase", "GenerationProvider", "EmbeddingProvider", "RerankerProvider",
    "VisionProvider", "OCRProvider",
    "build_generation_provider", "build_embedding_provider", "build_reranker_provider",
]
