"""「重排条数 = 0」= 关闭重排：一条**静默失效**的回归测试。

界面（`KbIndexPanel` / `RagConfigCard`）的说明写着「0 = 不重排」，用户确实会去关它；
但后端有三处 `or` 会把 0 悄悄抬回默认值：

1. `rag_k("rerank")` 写成 `setting(...) or DEFAULT_K[...]` → 0 变 6；
2. `rerank_hits` 里 `top_n = top_n or rag_k("rerank")` → 0 变 6（第二次抬）；
3. 调用点无条件下 `top_n=top_k or rag_k("rerank")` → 恒走重排。

三处都不报错，只是「用户关不掉重排」—— 所以这里逐个钉死。顺手钉住三个召回键：
它们的 0 在语义上**无意义**（召回 0 条 = 结果必然为空），必须夹到下限 1，
且返回值保持 `int`。

全部离线：`retrieval._setting_raw` 是唯一的数据库入口，把它替成字典即可。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge import retrieval
from backend.services.knowledge.retrieval import DEFAULT_K, rag_k, rerank_hits, search


def _fake_store(monkeypatch, values: dict) -> None:
    """把唯一的数据库入口 `_setting_raw` 换成字典查询（键不存在 → `_MISSING`）。"""
    monkeypatch.setattr(retrieval, "_setting_raw",
                        lambda key: values.get(key, retrieval._MISSING))


def _row(cid: str, content: str, *, lang: str = "zh", ctype: str = "unit") -> dict:
    return {"chunk_id": cid, "paper_id": "p1", "chunk_type": ctype, "lang": lang,
            "content": content, "page_no": 1, "block_id": f"b-{cid}",
            "section": "S1", "order_global": 1, "metadata": {}}


def _no_reranker(monkeypatch) -> None:
    """任何一次"去拿重排模型"都算失败：关闭重排后根本不该走到那一步。"""
    def boom(*args, **kwargs):
        raise AssertionError("关闭重排后仍然去调用重排模型")

    monkeypatch.setattr(retrieval.model_manager, "get_reranker", boom)
    monkeypatch.setattr(retrieval.model_manager, "get_config", boom)


# --------------------------------------------------------------------------
# 1. rag_k：0 的两种语义（rerank 保留 0；召回键夹到 1）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["kb.rag_top_n_rerank", "rag_top_n_rerank"])
def test_rag_k_keeps_rerank_zero(monkeypatch, key) -> None:
    """0 必须原样返回 —— `or` 兜底会把它抬成 DEFAULT_K["rerank"]。"""
    _fake_store(monkeypatch, {key: 0})
    assert rag_k("rerank") == 0


def test_rag_k_rerank_zero_written_as_string(monkeypatch) -> None:
    """脚本/手改写的是文本 "0"：一样是关闭，不能被当成坏值回落默认。"""
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": "0"})
    assert rag_k("rerank") == 0
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": " 0 "})
    assert rag_k("rerank") == 0


@pytest.mark.parametrize("raw", ["不是数字", "", None, "3.5.7"])
def test_rag_k_rerank_bad_value_falls_back_to_default(monkeypatch, raw) -> None:
    """解析失败 ≠ 关闭：坏值回落默认（照常重排），只有明确的 0 才表示关闭。"""
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": raw})
    assert rag_k("rerank") == DEFAULT_K["rerank"]


def test_rag_k_rerank_missing_uses_default(monkeypatch) -> None:
    _fake_store(monkeypatch, {})
    assert rag_k("rerank") == DEFAULT_K["rerank"]


@pytest.mark.parametrize("name", ["vector", "lexical", "fused"])
def test_rag_k_recall_zero_is_clamped_to_one(monkeypatch, name) -> None:
    """召回键的 0 没有意义（召回 0 条 = 永远空结果）→ 夹到 1，但仍是 int。"""
    _fake_store(monkeypatch, {f"kb.rag_top_k_{name}": 0})
    value = rag_k(name)
    assert value == 1 and isinstance(value, int) and not isinstance(value, bool)
    _fake_store(monkeypatch, {f"kb.rag_top_k_{name}": -5})       # 负数同样夹回 1
    assert rag_k(name) == 1


@pytest.mark.parametrize("name", ["vector", "lexical", "fused", "rerank"])
def test_rag_k_missing_key_uses_default(monkeypatch, name) -> None:
    _fake_store(monkeypatch, {})
    value = rag_k(name)
    assert value == DEFAULT_K[name] and isinstance(value, int)


def test_rag_k_reads_positive_values_verbatim(monkeypatch) -> None:
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 3, "kb.rag_top_k_vector": 7,
                              "kb.rag_top_k_fused": "9"})
    assert (rag_k("rerank"), rag_k("vector"), rag_k("fused")) == (3, 7, 9)


# --------------------------------------------------------------------------
# 2. rerank_hits：关闭 = 原样返回候选（顺序仍是 RRF 融合序）
# --------------------------------------------------------------------------


def test_rerank_off_returns_candidates_untouched(monkeypatch) -> None:
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 0})
    _no_reranker(monkeypatch)
    cands = [_row("c1", "甲"), _row("c2", "乙"), _row("c3", "丙")]

    items, info = rerank_hits("查询", cands, mode="similar")

    assert [i["chunk_id"] for i in items] == ["c1", "c2", "c3"]   # 融合序原样透传
    assert all("rerank_score" not in i and "rerank_rank" not in i for i in items)
    assert info == {"model": None, "provider": None, "degraded": False,
                    "reason": "已关闭重排（重排条数 = 0）", "candidates": 3,
                    "reranked": 0, "skipped": True}


def test_rerank_off_ignores_caller_top_n(monkeypatch) -> None:
    """判据来自**设置**：调用方传一个正数 top_n 也不能把重排复活。"""
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 0})
    _no_reranker(monkeypatch)

    items, info = rerank_hits("查询", [_row("c1", "甲"), _row("c2", "乙")], top_n=5)

    assert [i["chunk_id"] for i in items] == ["c1", "c2"]
    assert info["skipped"] is True and info["reranked"] == 0


def test_rerank_off_is_not_marked_degraded(monkeypatch) -> None:
    """关闭不是降级：不能混进「未配置重排模型」那句提示里。"""
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 0})
    _no_reranker(monkeypatch)
    _, info = rerank_hits("查询", [_row("c1", "甲")])
    assert info["degraded"] is False
    assert "未配置重排模型" not in info["reason"]


def test_rerank_off_with_empty_candidates(monkeypatch) -> None:
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 0})
    _no_reranker(monkeypatch)
    items, info = rerank_hits("查询", [])
    assert items == [] and info["skipped"] is True and info["candidates"] == 0


def test_rerank_skipped_when_caller_asks_for_zero(monkeypatch) -> None:
    """重排开着（默认 6）但调用方显式要 0 条 → 同样跳过，不拿 0 去调模型。"""
    _fake_store(monkeypatch, {})
    _no_reranker(monkeypatch)
    items, info = rerank_hits("查询", [_row("c1", "甲")], top_n=0)
    assert [i["chunk_id"] for i in items] == ["c1"] and info["skipped"] is True


class _FakeReranker:
    model_name = "fake-rerank"

    def __init__(self) -> None:
        self.seen_top_n = None

    def rerank(self, query, documents, top_n=None, *, instruct=None):
        self.seen_top_n = top_n
        return [(i, 1.0 - i * 0.1) for i in range(len(documents))]


def test_rerank_still_runs_when_setting_is_positive(monkeypatch) -> None:
    """关掉的分支不能顺手把"开着重排"也关掉：默认（未设）照常重排。"""
    _fake_store(monkeypatch, {})
    fake = _FakeReranker()
    monkeypatch.setattr(retrieval.model_manager, "get_reranker",
                        lambda model_id=None: fake)
    monkeypatch.setattr(retrieval.model_manager, "get_config",
                        lambda model_id=None, model_type="generation":
                        {"provider": "dashscope", "model_name": "fake-rerank"})
    cands = [_row("c1", "甲"), _row("c2", "乙"), _row("c3", "丙")]

    items, info = rerank_hits("查询", cands)

    assert fake.seen_top_n == DEFAULT_K["rerank"]      # 没传 top_n → 用设置里的 6
    assert "skipped" not in info and len(items) == 3
    assert items[0]["rerank_rank"] == 1


def test_rerank_top_n_still_caps_the_returned_list(monkeypatch) -> None:
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 6})
    fake = _FakeReranker()
    monkeypatch.setattr(retrieval.model_manager, "get_reranker",
                        lambda model_id=None: fake)
    monkeypatch.setattr(retrieval.model_manager, "get_config",
                        lambda model_id=None, model_type="generation":
                        {"provider": "dashscope", "model_name": "fake-rerank"})

    items, _ = rerank_hits("查询", [_row(f"c{i}", "甲") for i in range(5)], top_n=2)

    assert fake.seen_top_n == 2 and len(items) == 2


# --------------------------------------------------------------------------
# 3. 编排：search 不再"恒走重排"
# --------------------------------------------------------------------------


def test_search_skips_rerank_when_setting_is_zero(monkeypatch) -> None:
    _fake_store(monkeypatch, {"kb.rag_top_n_rerank": 0})
    _no_reranker(monkeypatch)
    vec = [_row("v1", "A"), _row("v2", "B")]
    lex = [_row("l1", "C")]
    monkeypatch.setattr(retrieval, "vector_search", lambda q, **kw: list(vec))
    monkeypatch.setattr(retrieval, "lexical_search", lambda q, **kw: list(lex))
    monkeypatch.setattr(retrieval, "default_search_types", lambda: ["unit"])
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda chunk_type: True)
    expected = [n["chunk_id"] for n in retrieval.rrf_fuse(
        {"vector": vec, "lexical": lex}, top_k=DEFAULT_K["fused"])]

    out = search("查询", mode="similar")

    assert [i["chunk_id"] for i in out["items"]] == expected     # 就是 RRF 融合序
    assert all("rerank_score" not in i for i in out["items"])
    assert out["retrieval"]["rerank"]["skipped"] is True
    assert out["retrieval"]["returned"] == len(expected)


def test_search_still_reranks_by_default(monkeypatch) -> None:
    """未设值时行为不变（既有测试依赖它）：仍拿 rag_top_n_rerank 当候选上限。"""
    _fake_store(monkeypatch, {})
    fake = _FakeReranker()
    monkeypatch.setattr(retrieval.model_manager, "get_reranker",
                        lambda model_id=None: fake)
    monkeypatch.setattr(retrieval.model_manager, "get_config",
                        lambda model_id=None, model_type="generation":
                        {"provider": "dashscope", "model_name": "fake-rerank"})
    monkeypatch.setattr(retrieval, "vector_search",
                        lambda q, **kw: [_row("v1", "A"), _row("v2", "B")])
    monkeypatch.setattr(retrieval, "lexical_search", lambda q, **kw: [_row("l1", "C")])
    monkeypatch.setattr(retrieval, "default_search_types", lambda: ["unit"])
    monkeypatch.setattr(retrieval, "_has_chunk_type", lambda chunk_type: True)

    out = search("查询", mode="similar")

    assert fake.seen_top_n == DEFAULT_K["rerank"]
    assert out["retrieval"]["rerank"].get("skipped") is None
    assert all("rerank_score" in i for i in out["items"])
