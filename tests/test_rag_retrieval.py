"""检索内核测试（向量 + 词法 BM25 + RRF + 重排降级链 + /rag/similar 编排）。

**全部离线**：向量库与模型都用替身，不连 MySQL、不调远程模型。
真实链路（真向量 + 真重排模型）的实测见 docs/dev-log.md §17.35。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.errors import ValidationError
from backend.services.knowledge import knowledge_service, retrieval
from backend.services.knowledge.retrieval import (
    RRF_K, cap_sections, fused_size, lexical_search, recall_depth, rerank_hits,
    rerank_top_n, rrf_fuse, search, tokenize,
)


def _row(cid: str, content: str, *, paper: str = "p1", lang: str = "zh",
         ctype: str = "text", order: int = 1) -> dict:
    return {"chunk_id": cid, "paper_id": paper, "chunk_type": ctype, "lang": lang,
            "content": content, "page_no": 1, "block_id": f"b-{cid}",
            "section": "S1", "order_global": order, "metadata": {}}


# --------------------------------------------------------------------------
# 分词
# --------------------------------------------------------------------------


def test_tokenize_handles_chinese_and_english() -> None:
    zh = tokenize("在线向量量化不需要校准数据")
    assert "向量量化" in zh and "校准" in zh        # 词级（jieba）
    assert {"量", "化"} <= set(zh)                  # 字级兜底：查询"量化"靠它命中"向量量化"
    en = tokenize("TurboQuant: Online Vector Quantization (ICLR 2026)")
    assert {"turboquant", "quantization", "2026"} <= set(en)
    assert tokenize("") == []


def test_lang_filter_keeps_any_row() -> None:
    """`lang=any`（公式原子行）在中文、英文过滤下都必须留下 —— 否则勾了语言公式就没了。"""
    rows = [_row("zh-1", "中文正文"), _row("en-1", "english body", lang="en"),
            _row("f-1", "公式 f-1：D_{mse}", lang="any", ctype="formula")]
    assert {r["chunk_id"] for r in retrieval._filter_rows(
        rows, paper_ids=None, chunk_types=None, langs=["zh"],
        exclude_paper=None, exclude_chunks=None)} == {"zh-1", "f-1"}
    assert {r["chunk_id"] for r in retrieval._filter_rows(
        rows, paper_ids=None, chunk_types=None, langs=["en"],
        exclude_paper=None, exclude_chunks=None)} == {"en-1", "f-1"}
    assert len(retrieval._filter_rows(
        rows, paper_ids=None, chunk_types=None, langs=None,
        exclude_paper=None, exclude_chunks=None)) == 3


# --------------------------------------------------------------------------
# RRF
# --------------------------------------------------------------------------


def test_rrf_prefers_documents_found_by_both_retrievers() -> None:
    vec = [_row("a", "A"), _row("b", "B")]
    lex = [_row("b", "B"), _row("c", "C")]

    fused = rrf_fuse({"vector": vec, "lexical": lex})

    assert [f["chunk_id"] for f in fused] == ["b", "a", "c"]
    top = fused[0]
    assert top["sources"] == {"vector": 2, "lexical": 1}
    assert top["rrf"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert fused[0]["rrf_rank"] == 1
    assert top["content"] == "B" and top["lang"] == "zh"


def test_rrf_weights_and_top_k() -> None:
    vec = [_row("a", "A")]
    lex = [_row("b", "B")]
    fused = rrf_fuse({"vector": vec, "lexical": lex}, weights={"vector": 3.0})
    assert [f["chunk_id"] for f in fused] == ["a", "b"]      # 加权后向量路胜出
    assert len(rrf_fuse({"vector": vec, "lexical": lex}, top_k=1)) == 1


def test_rrf_carries_stage_scores_and_skips_idless() -> None:
    vec = [{**_row("a", "A"), "vector_score": 0.9}]
    lex = [{**_row("a", "A"), "lexical_score": 4.2}, {"content": "没有 id"}]
    fused = rrf_fuse({"vector": vec, "lexical": lex})
    assert len(fused) == 1
    assert fused[0]["vector_score"] == 0.9 and fused[0]["lexical_score"] == 4.2


def test_rrf_aggregates_bilingual_twins_into_one_slot() -> None:
    """双语索引：同一 chunk 的 zh / en 两行不能占两个位置（实测踩过）。"""
    zh = {**_row("c1:zh", "中文内容", lang="zh"),
          "metadata": {"pair_id": "c1"}}
    en = {**_row("c1:en", "english content", lang="en"),
          "metadata": {"pair_id": "c1"}}
    vec = [zh, _row("c2", "另一段")]
    lex = [en, _row("c3", "第三段")]

    fused = rrf_fuse({"vector": vec, "lexical": lex})

    assert [f["pair_id"] for f in fused].count("c1") == 1
    node = next(f for f in fused if f["pair_id"] == "c1")
    # 两行的 RRF 求和：向量路第 1、词法路第 1
    assert node["rrf"] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 1))
    assert node["sources"] == {"vector": 1, "lexical": 1}
    assert node["langs"] == ["zh", "en"]
    assert node["content"] == "中文内容"          # 展示名次最好的那一行
    assert node["chunk_id"] == "c1:zh"


# --------------------------------------------------------------------------
# 词法 BM25
# --------------------------------------------------------------------------


def test_lexical_search_ranks_term_matches_first(monkeypatch) -> None:
    rows = [
        _row("c1", "本文提出一种不需要校准数据的在线量化方法，量化误差更小。"),
        _row("c2", "今天天气不错，适合出门散步。"),
        _row("c3", "校准数据通常来自一小批代表性样本。", order=2),
    ]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: rows)

    # 查询含两个词：c1 两个都命中（且"量化"出现两次），必须排在只命中一个的 c3 前面
    hits = lexical_search("校准数据 量化", top_k=3)
    assert hits and hits[0]["chunk_id"] == "c1"
    assert hits[0]["lexical_score"] > 0
    assert {h["chunk_id"] for h in hits} == {"c1", "c3"}      # 无关文档不出现


def test_lexical_search_matches_query_inside_longer_word(monkeypatch) -> None:
    """jieba 把"向量量化"切成一个词 → 查询"量化"必须靠**单字**兜底才匹配得上。"""
    rows = [_row("c1", "在线向量量化方法"), _row("c2", "无关内容")]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: rows)
    hits = lexical_search("量化", top_k=3)
    assert [h["chunk_id"] for h in hits] == ["c1"]


def test_lexical_search_applies_filters(monkeypatch) -> None:
    rows = [
        _row("c1", "向量量化校准", paper="p1", lang="zh"),
        _row("c2", "向量量化校准", paper="p2", lang="zh"),
        _row("c3", "向量量化校准", paper="p1", lang="en"),
        _row("c4", "向量量化校准", paper="p1", lang="zh", ctype="formula"),
    ]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: rows)

    zh = lexical_search("向量量化", paper_ids=["p1"], langs=["zh"], top_k=9)
    assert {h["chunk_id"] for h in zh} == {"c1", "c4"}

    cross = lexical_search("向量量化", exclude_paper="p1", top_k=9)
    assert {h["chunk_id"] for h in cross} == {"c2"}

    only_text = lexical_search("向量量化", chunk_types=["text"], top_k=9)
    assert {h["chunk_id"] for h in only_text} == {"c1", "c2", "c3"}

    assert lexical_search("", top_k=3) == []


# --------------------------------------------------------------------------
# 重排（降级链）
# --------------------------------------------------------------------------


class _FakeReranker:
    def __init__(self, scores: dict[str, float] | None = None, boom: bool = False):
        self.model_name = "fake-rerank"
        self._scores = scores or {}
        self._boom = boom
        self.instruct_seen: str | None = None

    def rerank(self, query, documents, top_n=None, *, instruct=None):
        if self._boom:
            raise RuntimeError("模型 500")
        self.instruct_seen = instruct
        ranked = sorted(((i, self._scores.get(d, 0.1)) for i, d in enumerate(documents)),
                        key=lambda x: -x[1])
        return ranked[:top_n] if top_n else ranked


def _fake_reranker_env(monkeypatch, provider, *, kind: str = "dashscope") -> None:
    monkeypatch.setattr(retrieval.model_manager, "get_reranker", lambda model_id=None: provider)
    monkeypatch.setattr(retrieval.model_manager, "get_config",
                        lambda model_id=None, model_type="generation":
                        {"provider": kind, "model_name": getattr(provider, "model_name", "")})


def test_rerank_uses_model_and_passes_similarity_instruct(monkeypatch) -> None:
    fake = _FakeReranker({"B": 0.9, "A": 0.2})
    _fake_reranker_env(monkeypatch, fake)
    cands = [_row("a", "A"), _row("b", "B")]

    items, info = rerank_hits("查询", cands, top_n=2, mode="similar")

    assert [i["chunk_id"] for i in items] == ["b", "a"]
    assert items[0]["rerank_score"] == 0.9 and items[0]["rerank_rank"] == 1
    assert info["model"] == "fake-rerank" and info["degraded"] is False
    assert fake.instruct_seen == retrieval.SIMILAR_INSTRUCT


def test_rerank_falls_back_to_heuristic_on_failure(monkeypatch) -> None:
    _fake_reranker_env(monkeypatch, _FakeReranker(boom=True))
    cands = [_row("a", "向量量化"), _row("b", "今天天气不错")]

    items, info = rerank_hits("向量量化", cands, top_n=2)

    assert len(items) == 2                       # 降级也要给出结果
    assert info["degraded"] is True and "RuntimeError" in info["reason"]
    assert info["model"] == "heuristic-overlap"
    assert items[0]["chunk_id"] == "a"           # 词法启发式至少把重合的排前面


def test_rerank_marks_degraded_when_no_model_configured(monkeypatch) -> None:
    from backend.providers.embedding.providers import HeuristicReranker

    _fake_reranker_env(monkeypatch, HeuristicReranker({}, None), kind="heuristic")
    items, info = rerank_hits("量化", [_row("a", "量化误差")], top_n=1)
    assert items and info["degraded"] is True
    assert "未配置重排模型" in info["reason"]


def test_rerank_empty_candidates() -> None:
    assert rerank_hits("q", []) == ([], {"model": None, "degraded": False,
                                         "reason": "", "candidates": 0})


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------


def test_search_pipeline_metadata(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "vector_search",
                        lambda q, **kw: [_row("a", "A"), _row("b", "B")])
    monkeypatch.setattr(retrieval, "lexical_search",
                        lambda q, **kw: [_row("b", "B"), _row("c", "C")])
    monkeypatch.setattr(retrieval, "rerank_hits",
                        lambda q, cands, top_n=None, mode="similar":
                        (cands[:2], {"model": "fake", "degraded": False, "reason": ""}))

    out = search("查询", mode="similar", scope="cross", paper_id="p9", top_k=2)

    assert [i["chunk_id"] for i in out["items"]] == ["b", "a"]
    meta = out["retrieval"]
    assert (meta["vector_hits"], meta["lexical_hits"], meta["fused"]) == (2, 2, 3)
    assert meta["scope"] == "cross" and meta["rrf_k"] == RRF_K
    assert search("   ")["items"] == []


# --------------------------------------------------------------------------
# /rag/similar 服务层
# --------------------------------------------------------------------------


def test_similar_requires_query_or_chunk() -> None:
    with pytest.raises(ValidationError):
        knowledge_service.similar({})


def test_similar_by_text_decorates_items(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "search", lambda q, **kw: {
        "items": [{**_row("c1", "向量量化误差" * 20, paper="p2"), "lang": "en",
                   "vector_score": 0.8, "lexical_score": 3.1, "rrf": 0.03,
                   "rerank_score": 0.71, "rrf_rank": 1, "rerank_rank": 1,
                   "sources": {"vector": 1, "lexical": 2}}],
        "retrieval": {"vector_hits": 1, "lexical_hits": 1, "fused": 1,
                      "returned": 1, "rrf_k": RRF_K},
    })
    monkeypatch.setattr(knowledge_service, "_paper_titles",
                        lambda ids: {"p2": "另一篇论文"})

    out = knowledge_service.similar({"query": "如何降低量化误差", "scope": "all"})

    assert out["implemented"] is True and out["scope"] == "all"
    item = out["items"][0]
    assert item["paper_title"] == "另一篇论文" and item["lang"] == "en"
    assert item["langs"] == ["en"]
    assert item["scores"] == {"vector": 0.8, "lexical": 3.1,
                              "rrf": 0.03, "rerank": 0.71}
    assert item["ranks"]["vector_rank"] == 1
    assert len(item["snippet"]) <= 260
    assert out["retrieval"]["returned"] == 1


def test_similar_by_chunk_defaults_to_cross_paper(monkeypatch) -> None:
    monkeypatch.setattr(retrieval.vector_store, "get_rows",
                        lambda ids: [_row(ids[0], "以这一段为查询", paper="p1")])
    seen: dict = {}

    def fake_search(q, **kw):
        seen.update(kw, query=q)
        return {"items": [], "retrieval": {}}

    monkeypatch.setattr(retrieval, "search", fake_search)

    out = knowledge_service.similar({"chunk_id": "c-src"})

    assert seen["scope"] == "cross" and seen["paper_id"] == "p1"
    assert seen["exclude_chunks"] == ["c-src"] and seen["query"] == "以这一段为查询"
    assert out["source"]["chunk_id"] == "c-src"
    assert out["scope"] == "cross"


def test_similar_rejects_unknown_chunk(monkeypatch) -> None:
    from backend.core.errors import NotFoundError

    monkeypatch.setattr(retrieval.vector_store, "get_rows", lambda ids: [])
    with pytest.raises(NotFoundError):
        knowledge_service.similar({"chunk_id": "nope"})
    with pytest.raises(ValidationError):
        knowledge_service.similar({"query": "x", "scope": "wat"})


def test_rag_similar_endpoint_contract(monkeypatch) -> None:
    from backend.api import knowledge as api_knowledge

    monkeypatch.setattr(api_knowledge.knowledge_service, "similar",
                        lambda payload: {"implemented": True, "items": [],
                                         "query": payload.get("query")})
    res = asyncio.run(api_knowledge.rag_similar(None, {"query": "量化"}))
    assert res["data"]["implemented"] is True
    assert res["data"]["query"] == "量化" and res["error"] is None


# --------------------------------------------------------------------------
# 融合池大小：top_k 要的条数不许被池子上限静默截掉
# --------------------------------------------------------------------------


def test_fused_pool_never_smaller_than_requested_top_k() -> None:
    """线上默认池 12，工具传 top_k=20 只回 12 条 —— 于是「多要几条」完全无效。

    实测（turboquant 篇，`TURBOQUANT_mse`）：池 12 时回 12 条、p6/p18 一条都不在；
    池 ≥20 时回 19 条，p6（1.2 INNER-PRODUCT）与 p18（C.1）才进得来。
    """
    assert fused_size(12, 20) == 20        # 要得比池子多 → 池子跟着放大
    assert fused_size(12, 5) == 12         # 要得比池子少 → 池子仍是下限（多留候选给重排）
    assert fused_size(8, 30) == 30
    assert fused_size(None, 20) == max(retrieval.rag_k("fused"), 20)
    assert fused_size(0, 0) >= 1           # 坏值不许把池子变成 0（否则必然空手）


# --------------------------------------------------------------------------
# 节内名额上限：同一节不许把 top_k 占满
# --------------------------------------------------------------------------


def test_section_cap_keeps_at_most_per_items() -> None:
    items = [
        {"paper_id": "p1", "section": "S1", "page_no": 3, "chunk_id": "a"},
        {"paper_id": "p1", "section": "S1", "page_no": 3, "chunk_id": "b"},
        {"paper_id": "p1", "section": "S1", "page_no": 4, "chunk_id": "c"},
        {"paper_id": "p1", "section": "S2", "page_no": 5, "chunk_id": "d"},
    ]
    assert [i["chunk_id"] for i in cap_sections(items)] == ["a", "b", "d"]


def test_section_cap_does_not_group_sectionless_blocks_together() -> None:
    items = [
        {"paper_id": "p1", "section": "", "page_no": 3, "chunk_id": "fig"},
        {"paper_id": "p1", "section": "", "page_no": 4, "chunk_id": "tbl"},
        {"paper_id": "p1", "section": "", "page_no": 5, "chunk_id": "eq"},
    ]
    assert len(cap_sections(items)) == 3


def test_section_cap_is_per_paper() -> None:
    items = [
        {"paper_id": "p1", "section": "ABSTRACT", "page_no": 1, "chunk_id": "x1"},
        {"paper_id": "p2", "section": "ABSTRACT", "page_no": 1, "chunk_id": "x2"},
    ]
    assert len(cap_sections(items)) == 2


def test_recall_depth_doubles_only_when_top_k_is_given() -> None:
    assert recall_depth(None) == 0
    assert recall_depth(20) == 40
    assert recall_depth(16) == 32          # 2×16 已在下限之上，按下限算是错的口径


def test_rerank_sees_whole_pool_only_when_section_cap_is_on() -> None:
    assert rerank_top_n(40, 20, None) == 20        # 不限名额：排这么多就够了（老口径）
    assert rerank_top_n(40, None, None) is None    # 没要条数：交给 rag_top_n_rerank 的设置值
    assert rerank_top_n(40, 20, 2) == 40           # 限名额：整池排一次，砍掉的名额才有替补


def test_recall_depth_has_a_floor_for_small_k() -> None:
    """k=8 时 2×8=16 装不下 B.2 的四个位置（Definition 1 那块排在 16 名外）：池子要有下限。"""
    assert recall_depth(8) == retrieval.POOL_MIN
    assert recall_depth(12) == retrieval.POOL_MIN
    assert recall_depth(20) == 40          # 大 k 仍按 2×k
    assert recall_depth(None) == 0         # 没要条数就不放大（RAG 问答那条链路）


# --------------------------------------------------------------------------
# 编码截止时间：卡住就快速失败，不陪 provider 等满 60s
# --------------------------------------------------------------------------


def test_query_vector_gives_up_at_the_deadline(monkeypatch) -> None:
    """线上实测一条 total 60860ms / vector 60372ms：provider 自己的超时也是 60s，双撞。

    这里只钉「到点就抛 EmbedTimeout」——检索侧**不做兜底**（用户口径：直接返回调用结果）。
    """
    import time as _time

    class _Slow:
        def embed(self, texts, **kwargs):
            _time.sleep(0.5)
            return [[0.0] * 4]

    monkeypatch.setattr(retrieval, "EMBED_TIMEOUT_S", 0.05)
    monkeypatch.setattr(retrieval.model_manager, "get_embedding", lambda model_id=None: _Slow())
    started = _time.perf_counter()
    with pytest.raises(retrieval.EmbedTimeout):
        retrieval.query_vector("SLB", mode="qa")
    assert _time.perf_counter() - started < 0.5      # 没等 provider 睡完


# --------------------------------------------------------------------------
# 只有标题的块：不进候选（`content` 只有一个章节名，拿去引用等于交白卷）
# --------------------------------------------------------------------------


def test_heading_only_detection() -> None:
    # 章节号里的点不算「正文」：以前用「整段没有句号」判，`## 0.2 PRIOR WORK` 会漏掉
    assert retrieval.is_heading_only("## 0.2 PRIOR WORK")
    assert retrieval.is_heading_only("# ABSTRACT")
    assert retrieval.is_heading_only("# A EXTENDED PRIOR WORK")
    assert not retrieval.is_heading_only(
        "# ABSTRACT  Vector quantization, a problem rooted in Shannon source coding, aims to "
        "quantize high-dimensional Euclidean vectors while minimizing their geometric structure.")
    assert not retrieval.is_heading_only("## 0.1 PROBLEM DEFINITION  Formally, our goal is … .")
    assert not retrieval.is_heading_only("普通正文，没有井号")


def test_heading_only_rows_are_dropped_from_lexical(monkeypatch) -> None:
    rows = [
        {"chunk_id": "h1", "paper_id": "p1", "chunk_type": "unit", "lang": "en",
         "content": "## 0.2 PRIOR WORK", "page_no": 3, "block_id": "b1",
         "section": "0.2 PRIOR WORK", "order_global": 1, "metadata": {}},
        {"chunk_id": "b1", "paper_id": "p1", "chunk_type": "unit", "lang": "en",
         "content": "prior work body about rotation quantization", "page_no": 3,
         "block_id": "b2", "section": "0.2 PRIOR WORK", "order_global": 2, "metadata": {}},
    ]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: list(rows))
    hits = retrieval.lexical_search("PRIOR WORK", paper_ids=["p1"], top_k=5)
    assert [h["chunk_id"] for h in hits] == ["b1"]
