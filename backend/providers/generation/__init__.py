"""生成类 Provider（规划 §12.1）。"""

from backend.providers.generation.mock import MockEmbedding, MockGeneration
from backend.providers.generation.openai_compatible import OpenAICompatibleGeneration

__all__ = ["OpenAICompatibleGeneration", "MockGeneration", "MockEmbedding"]
