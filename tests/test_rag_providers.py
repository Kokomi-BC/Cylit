"""RAG 接入层测试：DashScope 多模态向量 / 排序 provider 与工厂、预设接线。

**全部离线**：用 ``httpx.MockTransport`` 顶掉真实网络（真实调用的实测结论写在
provider 的 docstring 与 docs/dev-log.md，不放进单测，避免 CI 花钱/联网）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.core.errors import ProviderError, RateLimitError
from backend.providers.embedding.providers import (
    MULTIMODAL_EMBEDDING_PATH, TEXT_RERANK_PATH,
    DashScopeMultimodalEmbedding, DashScopeReranker,
)
from backend.providers.factory import build_embedding_provider, build_reranker_provider

KEY = "sk-test-key"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _emb_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(200, json={
        "output": {"embeddings": [
            {"index": i, "embedding": v, "type": "vl"} for i, v in enumerate(vectors)]},
        "usage": {"input_tokens": 10, "total_tokens": 10},
    })


def _emb_provider(handler, **params) -> DashScopeMultimodalEmbedding:
    cfg = {"model_name": "qwen3-vl-embedding", "provider": "dashscope_multimodal",
           "parameters": params}
    return DashScopeMultimodalEmbedding(cfg, KEY, client=_client(handler))


# --------------------------------------------------------------------------
# 多模态向量
# --------------------------------------------------------------------------


def test_multimodal_embedding_uses_native_endpoint_and_dimension() -> None:
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return _emb_response([[0.1, 0.2, 0.3]] * 2)

    p = _emb_provider(handler, dim=1024, batch=10)
    out = p.embed(["向量量化", "online quantization"])

    assert len(out) == 2 and out[0] == [0.1, 0.2, 0.3]
    url, body = seen[0]
    assert url == "https://dashscope.aliyuncs.com" + MULTIMODAL_EMBEDDING_PATH
    assert body["model"] == "qwen3-vl-embedding"
    assert body["input"]["contents"] == [{"text": "向量量化"}, {"text": "online quantization"}]
    assert body["parameters"] == {"dimension": 1024}      # 入库不带 instruct
    assert p.dim == 3                                      # 实测维度覆盖配置值


def test_multimodal_embedding_reorders_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # 故意乱序返回（索引 2、0、1），provider 必须按 index 重排回输入顺序
        return httpx.Response(200, json={"output": {"embeddings": [
            {"index": 2, "embedding": [5.0]},
            {"index": 0, "embedding": [9.0]},
            {"index": 1, "embedding": [1.0]}]}})

    p = _emb_provider(handler)
    assert p.embed(["a", "b", "c"]) == [[9.0], [1.0], [5.0]]


def test_multimodal_embedding_batches_at_20_contents() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(body["input"]["contents"])
        calls.append(n)
        return _emb_response([[0.0]] * n)

    p = _emb_provider(handler, batch=50)                   # 想开大也不行：硬上限 20
    assert len(p.embed([f"t{i}" for i in range(45)])) == 45
    assert calls == [20, 20, 5]


def test_multimodal_embedding_query_side_instruct() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _emb_response([[0.0]])

    p = _emb_provider(handler)
    p.embed(["问题"], instruct="Given a web search query, retrieve relevant passages.")
    assert bodies[0]["parameters"]["instruct"].startswith("Given a web search query")


def test_multimodal_embedding_image_becomes_data_uri() -> None:
    seen: list[dict] = []
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 8

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _emb_response([[0.0], [0.0]])

    p = _emb_provider(handler)
    p.embed_mixed([{"text": "这张公式图在讲什么"}, {"image": png}])

    contents = seen[0]["input"]["contents"]
    assert contents[0] == {"text": "这张公式图在讲什么"}
    assert contents[1]["image"].startswith("data:image/png;base64,")


def test_multimodal_embedding_error_mapping() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "InvalidApiKey", "message": "Invalid API-key"})

    def throttled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"code": "Throttling", "message": "slow down"})

    with pytest.raises(ProviderError) as e1:
        _emb_provider(unauthorized).embed(["x"])
    assert e1.value.retryable is False                    # 鉴权错不重试

    with pytest.raises(RateLimitError):
        _emb_provider(throttled).embed(["x"])


def test_multimodal_embedding_count_mismatch_is_fatal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _emb_response([[0.0]])                     # 请求 2 条只回 1 条

    with pytest.raises(ProviderError) as exc:
        _emb_provider(handler).embed(["a", "b"])
    assert "条数不符" in exc.value.message


# --------------------------------------------------------------------------
# 排序
# --------------------------------------------------------------------------


def _rerank_provider(handler, **params) -> DashScopeReranker:
    cfg = {"model_name": "qwen3-vl-rerank", "provider": "dashscope", "parameters": params}
    return DashScopeReranker(cfg, KEY, client=_client(handler))


def test_reranker_body_and_order() -> None:
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"output": {"results": [
            {"index": 2, "relevance_score": 0.1},
            {"index": 0, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.5}]}})

    p = _rerank_provider(handler)
    ranked = p.rerank("如何降低量化误差", ["doc-a", "doc-b", "doc-c"], top_n=2)

    assert ranked == [(0, 0.9), (1, 0.5)]                  # 降序 + top_n 截断
    url, body = seen[0]
    assert url == "https://dashscope.aliyuncs.com" + TEXT_RERANK_PATH
    assert body["model"] == "qwen3-vl-rerank"
    assert body["input"]["query"] == "如何降低量化误差"      # 纯文本查询用字符串形式
    assert body["input"]["documents"] == ["doc-a", "doc-b", "doc-c"]
    assert body["parameters"]["top_n"] == 2


def test_reranker_image_documents_switch_to_object_form() -> None:
    seen: list[dict] = []
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 8

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"output": {"results": [
            {"index": 1, "relevance_score": 0.7}, {"index": 0, "relevance_score": 0.2}]}})

    p = _rerank_provider(handler)
    ranked = p.rerank("这个公式在说什么", ["caption", "formula"], images=[None, png])

    assert ranked == [(1, 0.7), (0, 0.2)]
    body = seen[0]
    # 混模态时 documents 必须同质：文本项也走 {"text": ...} 字典（实测否则 400）
    assert body["input"]["documents"][0] == {"text": "caption"}
    assert body["input"]["documents"][1]["image"].startswith("data:image/png;base64,")
    assert body["input"]["query"] == {"text": "这个公式在说什么"}   # 混模态时查询走对象形式


def test_reranker_instruct_passthrough_and_empty_docs() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"output": {"results": []}})

    p = _rerank_provider(handler, instruct="Retrieve semantically similar text.")
    p.rerank("q", ["d"], instruct="Retrieve semantically similar text.")
    assert seen[0]["parameters"]["instruct"] == "Retrieve semantically similar text."
    assert p.rerank("q", []) == []                          # 空文档不请求
    assert len(seen) == 1


# --------------------------------------------------------------------------
# 工厂与预设接线
# --------------------------------------------------------------------------


def test_factory_routes_dashscope_models() -> None:
    emb = build_embedding_provider({"provider": "dashscope_multimodal",
                                    "model_name": "qwen3-vl-embedding"})
    assert isinstance(emb, DashScopeMultimodalEmbedding)

    rr = build_reranker_provider({"provider": "dashscope", "model_name": "qwen3-vl-rerank"})
    assert isinstance(rr, DashScopeReranker)

    # 旧行为不变：dashscope 的 embedding 仍是 OpenAI 兼容的 /embeddings（text-embedding-v3）
    from backend.providers.embedding.providers import OpenAICompatibleEmbedding

    assert isinstance(build_embedding_provider({"provider": "dashscope",
                                                "model_name": "text-embedding-v3"}),
                      OpenAICompatibleEmbedding)
    # 未配置时仍回落词法重排（离线可跑）
    assert build_reranker_provider({}).__class__.__name__ == "HeuristicReranker"


def test_preset_declares_reranker_and_multimodal_embedding() -> None:
    from backend.services.model.presets import PRESETS, list_presets

    preset = PRESETS["dashscope_rag"]
    assert preset["embedding"]["provider"] == "dashscope_multimodal"
    assert preset["embedding"]["model_name"] == "qwen3-vl-embedding"
    assert preset["embedding"]["parameters"]["dim"] == 2560
    assert preset["reranker"]["model_name"] == "qwen3-vl-rerank"
    assert preset["embedding"]["secret_ref"] == "dashscope.key"
    assert "instruct" not in preset["embedding"]["parameters"]   # 查询侧参数，不入库

    listed = {p["id"]: p for p in list_presets()}
    assert "reranker" in listed["dashscope_rag"]
    assert listed["deepseek"].get("reranker") is None


# --------------------------------------------------------------------------
# 向量库：换 embedding 模型（维度变化）必须重建而不是写坏索引
# --------------------------------------------------------------------------


def _chunk(cid: str, lang: str = "en") -> dict:
    return {"id": cid, "paper_id": "p1", "chunk_type": "text", "page_no": 1,
            "order_global": 1, "block_id": f"b-{cid}", "section": "S1",
            "lang": lang, "content": f"content {cid}", "content_hash": f"h-{cid}",
            "metadata": {"lang": lang}}


def test_vector_store_rebuilds_when_dimension_changes(tmp_path) -> None:
    pytest.importorskip("lancedb")
    from backend.repositories.vector.lancedb_store import LanceDBStore

    store = LanceDBStore(tmp_path / "lancedb")
    chunks = [_chunk("c1"), _chunk("c2")]

    # 1) mock 向量（3 维）先入库
    store.upsert_chunks(chunks, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], 1,
                        embedding_model_id="mock-embedding")
    assert store.count() == 2

    # 2) 换成真实模型（5 维）：旧表定长向量列放不下 → 丢表重建，不报错、不残留旧向量
    store.upsert_chunks(chunks, [[1.0, 0.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0, 0.0]], 2,
                        embedding_model_id="qwen3-vl-embedding")
    assert store.count() == 2
    hits = store.search([1.0, 0.0, 0.0, 0.0, 0.0], top_k=1)
    assert hits and hits[0]["chunk_id"] == "c1"
    assert hits[0]["metadata"] == {"lang": "en"}
    assert hits[0]["lang"] == "en"


def test_vector_store_filters_by_lang(tmp_path) -> None:
    pytest.importorskip("lancedb")
    from backend.repositories.vector.lancedb_store import LanceDBStore

    store = LanceDBStore(tmp_path / "lancedb")
    chunks = [_chunk("c1:zh", "zh"), _chunk("c1:en", "en")]
    store.upsert_chunks(chunks, [[1.0, 0.0], [1.0, 0.0]], 1)
    assert store.count() == 2

    assert [r["chunk_id"] for r in store.search([1.0, 0.0], top_k=5)] == ["c1:zh", "c1:en"]
    only_zh = store.search([1.0, 0.0], top_k=5, langs=["zh"])
    assert [r["chunk_id"] for r in only_zh] == ["c1:zh"]
    assert only_zh[0]["lang"] == "zh"
    assert {r["chunk_id"] for r in store.all_rows()} == {"c1:zh", "c1:en"}
    assert all("content" in r for r in store.all_rows())


def test_vector_store_reads_legacy_table_without_lang_column(tmp_path) -> None:
    """旧索引（本轮之前建的）没有 lang 列：读取与"语言过滤"都不能因此报错/变空。

    ★ 真实踩坑：`select([... "lang" ...])` 在旧表上直接抛错，而调用方语义是
    "失败 → 返回空列表"，于是**整条词法检索静默失效**（查询词明明在正文里，
    lexical_hits 仍是 0）。这里把旧表结构固化下来当回归。
    """
    lancedb = pytest.importorskip("lancedb")
    import pyarrow as pa

    path = tmp_path / "lancedb"
    path.mkdir()
    db = lancedb.connect(str(path))
    db.create_table("paper_chunks", data=pa.Table.from_pylist([{
        "chunk_id": "c1", "paper_id": "p1", "chunk_type": "text", "page_no": 1,
        "order_global": 1, "block_id": "b1", "section": "S1",
        "content": "TurboQuant online vector quantization",
        "content_hash": "h1", "index_version": 1, "embedding_model_id": "mock-embedding",
        "metadata_json": "{}", "vector": [1.0, 0.0],
    }]))

    from backend.repositories.vector.lancedb_store import LanceDBStore

    store = LanceDBStore(path)
    assert store.has_lang() is False
    rows = store.all_rows()
    assert rows and rows[0]["lang"] == "en"          # 旧行按 en 处理
    assert store.get_rows(["c1"])[0]["content"].startswith("TurboQuant")
    hits = store.search([1.0, 0.0], top_k=5, langs=["zh"])   # 旧表忽略过滤，而不是返回空
    assert [h["chunk_id"] for h in hits] == ["c1"]
