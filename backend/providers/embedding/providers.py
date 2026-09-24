"""Embedding / Rerank Provider（规划 §12.1）。

三档实现，按可用性自动选择（用户的方案：本地高级模型（可选安装）+ 兼容远程 API）：
1. ``LocalSentenceTransformerEmbedding``：sentence-transformers 本地模型（推荐 bge 系列）
2. ``OpenAICompatibleEmbedding``：``/embeddings`` 远程接口
3. ``MockEmbedding``：确定性哈希向量（离线降级，保证链路可跑）

多模态向量与 DashScope 原生排序各有一份实现（见文件后半段）：
4. ``DashScopeMultimodalEmbedding``：``qwen3-vl-embedding``（文本/图片同一语义空间）
5. ``DashScopeReranker``：``qwen3-vl-rerank`` / ``qwen3.7-text-rerank`` / ``qwen3-rerank``
   / ``gte-rerank-v2``（同一个 native 端点）

维度变化必须被上层拒绝（规划 §12.2 兼容性校验），因此每个实现都暴露 ``dim``。
"""

from __future__ import annotations

import base64
import time
from typing import Any

from backend.core.errors import ProviderError, RateLimitError
from backend.core.logging import get_logger
from backend.providers.base import EmbeddingProvider, ModelInfo

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# DashScope（阿里云百炼）原生端点
# ---------------------------------------------------------------------------
# 注意：这两个能力**不在** OpenAI 兼容层上，必须走 DashScope 原生 HTTP：
#   · 多模态向量：POST {host}/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding
#   · 文本排序  ：POST {host}/api/v1/services/rerank/text-rerank/text-rerank
DASHSCOPE_HOST = "https://dashscope.aliyuncs.com"
MULTIMODAL_EMBEDDING_PATH = ("/api/v1/services/embeddings/multimodal-embedding/"
                             "multimodal-embedding")
TEXT_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
# qwen3-vl-embedding 单次请求 contents 元素总数上限（官方文档：不超过 20）
MAX_CONTENTS_PER_REQUEST = 20


def _endpoint(config: dict, default_path: str) -> str:
    """解析端点：``base_url`` 若已含路径就整体采用，否则拼默认路径（可切地域）。"""
    base = (config.get("base_url") or "").strip().rstrip("/")
    if not base:
        return DASHSCOPE_HOST + default_path
    if "/api/v1" in base:
        return base
    return base + default_path


def _image_to_data_uri(image: Any) -> str:
    """bytes → ``data:image/...;base64,...``；已是 URL / data URI 的字符串原样返回。"""
    if isinstance(image, str):
        return image
    raw = bytes(image)
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


class DashScopeMultimodalEmbedding(EmbeddingProvider):
    """``qwen3-vl-embedding``：文本与图片落在**同一语义空间**（规划 §12.1 多模态档）。

    实测（2026-09，真实调用，非文档抄写）：
    - **模型名是 ``qwen3-vl-embedding``**：``qwen3-vl-embedding-8b`` 返回
      ``Model not exist``（``-8b`` 是开源权重名，不是百炼模型名）；``-2b`` 存在但
      当前账号 ``Access denied``；
    - 默认 **2560** 维，可用 ``parameters.dim`` 降到 2048/1536/1024/768/512/256；
    - 端点不是 OpenAI 兼容的 ``/embeddings``，而是上面那个原生多模态端点；
    - 同一次请求里文本与图片各得一个向量（``type`` 均为 ``vl``），
      图片走 base64 data URI（本机裁图无需公网可访问）；
    - 单次请求 contents ≤ 20，本类按 ``parameters.batch``（默认 10）自动切片。

    ``parameters.instruct`` 可加一句任务说明（官方称通常 +1%~5%），
    但**入库与查询必须用同一句**，否则两侧语义偏移。
    """

    def __init__(self, config: dict | None = None, api_key: str | None = None,
                 client: Any = None) -> None:
        super().__init__(config, api_key)
        self._client = client
        self._dim: int | None = None

    # ------------------------------------------------------------------
    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ProviderError(
                "DashScope 多模态向量缺少 API Key（data/secrets/dashscope.key）",
                retryable=False,
            )
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("未安装 httpx：pip install httpx", retryable=False) from exc
        self._client = httpx.Client(
            timeout=float(self.config.get("timeout") or 60.0),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        return self._client

    def _params(self, instruct: str | None) -> dict:
        cfg = dict(self.config.get("parameters") or {})
        out: dict[str, Any] = {}
        if cfg.get("dim"):
            out["dimension"] = int(cfg["dim"])
        text = instruct if instruct is not None else cfg.get("instruct")
        if text:
            out["instruct"] = str(text)
        return out

    def _post(self, contents: list[dict], instruct: str | None = None) -> dict:
        client = self._ensure_client()
        body: dict[str, Any] = {"model": self.model_name, "input": {"contents": contents}}
        params = self._params(instruct)
        if params:
            body["parameters"] = params
        url = _endpoint(self.config, MULTIMODAL_EMBEDDING_PATH)
        try:
            resp = client.post(url, json=body)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"多模态向量请求失败：{str(exc)[:200]}") from exc
        return _parse_dashscope(resp, "多模态向量")

    # ------------------------------------------------------------------
    def embed_mixed(self, items: list[dict], *, instruct: str | None = None
                    ) -> list[list[float]]:
        """``items`` 形如 ``{"text": ...}`` / ``{"image": bytes|str}``，按序返回向量。

        ``instruct`` 是**查询侧**参数（"Given a web search query, retrieve relevant
        passages…" 之类）：入库时不要传，检索时传，两侧才会落在同一个空间。
        """
        if not items:
            return []
        cfg = dict(self.config.get("parameters") or {})
        batch = max(1, min(int(cfg.get("batch") or 10), MAX_CONTENTS_PER_REQUEST))
        contents: list[dict] = []
        for it in items:
            if it.get("image") is not None:
                contents.append({"image": _image_to_data_uri(it["image"])})
            else:
                contents.append({"text": str(it.get("text") or "")})

        vectors: list[list[float]] = []
        for i in range(0, len(contents), batch):
            part = contents[i:i + batch]
            data = self._post(part, instruct)
            rows = (data.get("output") or {}).get("embeddings") or []
            if len(rows) != len(part):
                raise ProviderError(
                    f"多模态向量返回条数不符（请求 {len(part)}，返回 {len(rows)}）",
                    retryable=False)
            ordered = sorted(rows, key=lambda r: int(r.get("index") or 0))
            vectors.extend([list(map(float, r.get("embedding") or [])) for r in ordered])
        if vectors and vectors[0]:
            self._dim = len(vectors[0])
        return vectors

    def embed(self, texts: list[str], *, instruct: str | None = None) -> list[list[float]]:
        return self.embed_mixed([{"text": t} for t in texts], instruct=instruct)

    def embed_images(self, images: list[Any], *, instruct: str | None = None
                     ) -> list[list[float]]:
        return self.embed_mixed([{"image": img} for img in images], instruct=instruct)

    @property
    def dim(self) -> int | None:
        return self._dim or (self.config.get("parameters") or {}).get("dim")

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        try:
            v = self.embed(["ping"])[0]
            return ModelInfo(ok=True, dimension=len(v), model_name=self.model_name,
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        except ProviderError as exc:
            return ModelInfo(ok=False, model_name=self.model_name, detail=exc.message[:200])


class DashScopeReranker:
    """DashScope 原生文本排序：``qwen3-vl-rerank`` / ``qwen3.7-text-rerank`` /
    ``qwen3-rerank`` / ``gte-rerank-v2`` 共用同一个端点（规划 §12.1 重排档）。

    实测（2026-09，真实调用）：四个模型名在同一 native 端点上都可用；
    ``qwen3-rerank`` 另有 OpenAI 风格的 ``/compatible-api/v1/reranks``，但没必要分两条路。

    **分数是本次请求内的相对分**（官方明确），跨请求不可比 —— 上层不要拿它做
    绝对阈值过滤，只能用于"这一次取前几名"。

    ``qwen3-vl-rerank`` 额外支持**图片文档**：``images`` 给出与 ``documents``
    等长的图片（bytes/URL，``None`` 表示该项是纯文本），即可让查询与公式/插图裁图
    在同一次排序里比较（跨模态）。
    """

    def __init__(self, config: dict | None = None, api_key: str | None = None,
                 client: Any = None) -> None:
        self.config = config or {}
        self.api_key = api_key
        self._client = client

    @property
    def model_name(self) -> str:
        return str(self.config.get("model_name") or "qwen3-vl-rerank")

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise ProviderError("DashScope 排序模型缺少 API Key（data/secrets/dashscope.key）",
                                retryable=False)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("未安装 httpx：pip install httpx", retryable=False) from exc
        self._client = httpx.Client(
            timeout=float(self.config.get("timeout") or 60.0),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        return self._client

    def rerank(self, query: str, documents: list[str], top_n: int | None = None,
               *, instruct: str | None = None, images: list[Any] | None = None,
               query_image: Any = None) -> list[tuple[int, float]]:
        if not documents:
            return []
        cfg = dict(self.config.get("parameters") or {})
        instruct = instruct or cfg.get("instruct")
        has_image = any(img is not None for img in (images or []))
        docs: list[Any] = []
        for i, d in enumerate(documents):
            img = images[i] if images and i < len(images) else None
            if img is not None:
                docs.append({"image": _image_to_data_uri(img)})
            elif has_image:
                # 实测：documents 必须**同质** —— 一旦有图片项，文本项也得是
                # ``{"text": ...}`` 字典，否则 400 InvalidParameter（把字符串和字典混着传）。
                docs.append({"text": str(d)})
            else:
                docs.append(str(d))
        q: Any = {"image": _image_to_data_uri(query_image)} if query_image is not None else (
            {"text": query} if has_image else query)
        params: dict[str, Any] = {}
        if top_n:
            params["top_n"] = int(top_n)
        if instruct:
            params["instruct"] = str(instruct)
        body: dict[str, Any] = {"model": self.model_name,
                                "input": {"query": q, "documents": docs}}
        if params:
            body["parameters"] = params
        client = self._ensure_client()
        url = _endpoint(self.config, TEXT_RERANK_PATH)
        try:
            resp = client.post(url, json=body)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"排序请求失败：{str(exc)[:200]}") from exc
        data = _parse_dashscope(resp, "排序")
        results = data.get("results") or (data.get("output") or {}).get("results") or []
        ranked = sorted(((int(r.get("index") or 0), float(r.get("relevance_score") or 0.0))
                         for r in results), key=lambda x: x[1], reverse=True)
        return ranked[:top_n] if top_n else ranked

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        try:
            self.rerank("ping", ["pong"])
            return ModelInfo(ok=True, model_name=self.model_name,
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        except ProviderError as exc:
            return ModelInfo(ok=False, model_name=self.model_name, detail=exc.message[:200])


def _parse_dashscope(resp, what: str) -> dict:
    """统一解析 DashScope 原生响应：HTTP 状态 + 体内 ``code`` 都算失败。"""
    status = getattr(resp, "status_code", 0)
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"{what}返回非 JSON（HTTP {status}）", retryable=status >= 500) from exc
    code = data.get("code")
    if status == 429 or code in ("Throttling", "Throttling.RateQuota"):
        raise RateLimitError(f"{what}限流：{data.get('message') or code}")
    if status != 200 or code:
        retryable = status >= 500
        raise ProviderError(
            f"{what}失败（HTTP {status} {code or ''}）：{str(data.get('message'))[:200]}",
            retryable=retryable)
    return data


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
