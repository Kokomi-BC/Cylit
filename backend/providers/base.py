"""Provider 基类与能力接口（规划 §12.1）。

设计约束：
- 业务代码只引用 ``model_type``，不写模型名（规划 §12.2 默认路由）；
- Provider 只做「一次调用」，重试/退避/并发在调度器里（规划 §3.1 A3）；
- 错误必须区分**可重试**（429/超时/连接）与**不可重试**（鉴权/参数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class ModelInfo:
    """Provider 自描述信息（写回 ``model_config.status``）。"""

    ok: bool = False
    dimension: int | None = None
    latency_ms: float | None = None
    model_name: str = ""
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "dimension": self.dimension,
                "latency_ms": self.latency_ms, "model_name": self.model_name,
                "detail": self.detail}


@dataclass
class GenerationResult:
    """一次生成的结果（含成本记录所需的 token 用量，规划 §3.1 A3）。"""

    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # 思考模式相关的附加信息（DeepSeek 等）：思维链 token 也计入 completion_tokens
    reasoning_tokens: int = 0
    reasoning_content: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        return {"text": self.text, "model": self.model,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "latency_ms": self.latency_ms}


class ProviderBase:
    """所有 Provider 的基类。"""

    model_type: str = "base"

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        self.config = config or {}
        self.api_key = api_key

    @property
    def model_name(self) -> str:
        return str(self.config.get("model_name") or "")

    @property
    def provider_name(self) -> str:
        return str(self.config.get("provider") or "")

    def test(self) -> ModelInfo:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def close(self) -> None:
        pass


class GenerationProvider(ProviderBase):
    """文本生成：``generate`` / ``stream`` / ``structured``。"""

    model_type = "generation"

    def generate(self, prompt: str, *, system: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> GenerationResult:
        raise NotImplementedError

    def generate_vision(self, prompt: str, images: list[bytes], *,
                        system: str | None = None,
                        max_tokens: int | None = None,
                        **kwargs: Any) -> GenerationResult:
        """带图片的生成（多模态）。``images`` 是 PNG/JPEG 字节。

        扫描件论文没有文本层，正文只能由这个能力读出来（规划 §13.4 扫描件兜底）。
        默认不支持，子类按需实现。
        """
        raise NotImplementedError("该 provider 不支持图片输入")

    @property
    def supports_vision(self) -> bool:
        return type(self).generate_vision is not GenerationProvider.generate_vision

    def stream(self, prompt: str, *, system: str | None = None,
               **kwargs: Any) -> Iterator[str]:
        yield self.generate(prompt, system=system, **kwargs).text

    def structured(self, prompt: str, schema: dict, *, system: str | None = None,
                   **kwargs: Any) -> dict:
        """要求模型输出符合 ``schema`` 的 JSON（由子类/包装器实现）。"""
        raise NotImplementedError


class EmbeddingProvider(ProviderBase):
    """向量化：``embed`` / ``dim``。"""

    model_type = "embedding"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_one(self, text: str) -> list[float]:
        out = self.embed([text])
        return out[0] if out else []

    @property
    def dim(self) -> int | None:
        return None


class RerankerProvider(ProviderBase):
    """重排：``rerank``。"""

    model_type = "reranker"

    def rerank(self, query: str, documents: list[str], top_n: int | None = None
               ) -> list[tuple[int, float]]:
        """返回 ``[(原索引, 分数)]``，按分数降序。"""
        raise NotImplementedError


class VisionProvider(ProviderBase):
    """图像理解（P2，规划 §12.1）。"""

    model_type = "vision"

    def describe(self, image_b64: str, prompt: str) -> str:
        raise NotImplementedError

    def extract_table(self, image_b64: str) -> str:
        raise NotImplementedError


class OCRProvider(ProviderBase):
    """OCR / 公式识别（P2）。"""

    model_type = "ocr"

    def recognize(self, image_b64: str) -> str:
        raise NotImplementedError
