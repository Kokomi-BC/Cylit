"""Embedding Provider（规划 §12.1）。

三档实现，按可用性自动选择（用户的方案：本地高级模型（可选安装）+ 兼容远程 API）：
1. ``LocalSentenceTransformerEmbedding``：sentence-transformers 本地模型（推荐 bge 系列）
2. ``OpenAICompatibleEmbedding``：``/embeddings`` 远程接口
3. ``MockEmbedding``：确定性哈希向量（离线降级，保证链路可跑）

维度变化必须被上层拒绝（规划 §12.2 兼容性校验），因此每个实现都暴露 ``dim``。
"""

from __future__ import annotations

import time
from typing import Any

from backend.core.errors import ProviderError, RateLimitError
from backend.core.logging import get_logger
from backend.providers.base import EmbeddingProvider, ModelInfo

log = get_logger(__name__)


class OpenAICompatibleEmbedding(EmbeddingProvider):
    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self._client = None
        self._dim: int | None = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ProviderError("Embedding 模型缺少 API Key", retryable=False)
        from openai import OpenAI

        self._client = OpenAI(
            api_key=self.api_key,
            base_url=(self.config.get("base_url") or "").strip() or None,
            timeout=float(self.config.get("timeout") or 60.0),
            max_retries=0,
        )
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        client = self._ensure_client()
        params: dict[str, Any] = {"model": self.model_name, "input": texts}
        cfg = dict(self.config.get("parameters") or {})
        if cfg.get("dim"):
            try:
                params["dimensions"] = int(cfg["dim"])
            except (TypeError, ValueError):
                pass
        try:
            resp = client.embeddings.create(**params)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            if status == 429:
                raise RateLimitError(f"限流：{exc}") from exc
            raise ProviderError(f"Embedding 调用失败：{str(exc)[:200]}",
                                retryable=status not in (400, 401, 403)) from exc
        vectors = [d.embedding for d in resp.data]
        if vectors:
            self._dim = len(vectors[0])
        return vectors

    @property
    def dim(self) -> int | None:
        return self._dim or (self.config.get("parameters") or {}).get("dim")

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        try:
            v = self.embed(["ping"])[0]
            return ModelInfo(ok=True, dimension=len(v), latency_ms=round(
                (time.perf_counter() - t0) * 1000, 1), model_name=self.model_name)
        except ProviderError as exc:
            return ModelInfo(ok=False, detail=exc.message[:200])


class LocalSentenceTransformerEmbedding(EmbeddingProvider):
    """本地 sentence-transformers（可选安装；模型按需下载）。"""

    _cache: dict[str, Any] = {}

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        self._model = None
        self._dim: int | None = None

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        name = self.model_name or "BAAI/bge-small-zh-v1.5"
        if name in self._cache:
            self._model = self._cache[name]
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ProviderError(
                "未安装 sentence-transformers，本地 Embedding 不可用。"
                "安装：pip install sentence-transformers（或改用远程 API / mock）",
                retryable=False,
            ) from exc
        device = (self.config.get("device") or "").strip() or None
        log.info("加载本地 Embedding 模型：%s（device=%s）", name, device or "auto")
        self._model = SentenceTransformer(name, device=device)
        self._cache[name] = self._model
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        cfg = dict(self.config.get("parameters") or {})
        batch = int(cfg.get("batch") or 32)
        try:
            vecs = model.encode(texts, batch_size=batch, normalize_embeddings=True,
                                show_progress_bar=False)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"本地 Embedding 失败：{str(exc)[:200]}") from exc
        out = [list(map(float, v)) for v in vecs]
        if out:
            self._dim = len(out[0])
        return out

    @property
    def dim(self) -> int | None:
        if self._dim is None:
            try:
                self._dim = int(self._ensure_model().get_sentence_embedding_dimension())
            except Exception:  # noqa: BLE001
                return None
        return self._dim

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        try:
            v = self.embed(["ping"])[0]
            return ModelInfo(ok=True, dimension=len(v), model_name=self.model_name,
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        except ProviderError as exc:
            return ModelInfo(ok=False, detail=exc.message[:200])


class LocalCrossEncoderReranker:
    """本地 cross-encoder 重排（规划 §12.1；不可用时自动降级，规划 §19.1）。"""

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        self.config = config or {}
        self.api_key = api_key
        self._model = None

    @property
    def model_name(self) -> str:
        return str(self.config.get("model_name") or "BAAI/bge-reranker-base")

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            raise ProviderError("未安装 sentence-transformers，无法本地重排",
                                retryable=False) from exc
        self._model = CrossEncoder(self.model_name,
                                   device=(self.config.get("device") or None))
        return self._model

    def rerank(self, query: str, documents: list[str], top_n: int | None = None
               ) -> list[tuple[int, float]]:
        if not documents:
            return []
        model = self._ensure_model()
        pairs = [(query, d) for d in documents]
        try:
            scores = model.predict(pairs)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"重排失败：{str(exc)[:200]}") from exc
        ranked = sorted(enumerate(map(float, scores)), key=lambda x: x[1], reverse=True)
        return ranked[:top_n] if top_n else ranked

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        try:
            self.rerank("ping", ["pong"])
            return ModelInfo(ok=True, model_name=self.model_name,
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        except ProviderError as exc:
            return ModelInfo(ok=False, detail=exc.message[:200])


class HeuristicReranker:
    """无模型时的词法启发式重排（保证 RAG 链路可用，规划 §19.1 降级）。"""

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        self.config = config or {}
        self.api_key = api_key

    @property
    def model_name(self) -> str:
        return "heuristic-overlap"

    def rerank(self, query: str, documents: list[str], top_n: int | None = None
               ) -> list[tuple[int, float]]:
        from backend.core.utils import sha256_text  # noqa: F401  (保持依赖显式)

        q = {t for t in _terms(query)}
        scored: list[tuple[int, float]] = []
        for i, doc in enumerate(documents):
            d = {t for t in _terms(doc)}
            if not d:
                scored.append((i, 0.0))
                continue
            inter = len(q & d)
            scored.append((i, inter / (len(q) or 1)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n] if top_n else scored

    def test(self) -> ModelInfo:
        return ModelInfo(ok=True, model_name="heuristic-overlap",
                         detail="词法启发式重排（无模型降级）")


def _terms(text: str) -> list[str]:
    import re

    if any("\u4e00" <= c <= "\u9fff" for c in (text or "")):
        try:
            import jieba

            return [w for w in jieba.lcut(text or "") if w.strip()]
        except ImportError:
            return list(text or "")
    return re.findall(r"[a-zA-Z0-9_]+", (text or "").lower())
