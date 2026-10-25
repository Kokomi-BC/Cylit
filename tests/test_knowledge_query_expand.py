"""查询扩展：下标写法（X_mse）与缩写 ↔ 全称（实测见 expand 模块头）。

这两条以前都靠模型自己拆词，实测拆不动：
- 线上 turboquant 篇 TURBOQUANT_mse 词法 **0 条**，拆成 TurboQuant MSE 就是 20 条；
- SLB 与 Shannon lower bound 各回 8 条、只重叠 4 条。

所以规则钉在代码里，并在这里锁住三件事：
1. 分词把下划线当分隔符（查询侧与正文侧同一套）；
2. 别名**只从正文的 X (ABBR) 句式学**，同义并置（Online (data-oblivious)）不学；
3. 词法召回真的因此把两组结果并起来。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge import expand, retrieval
from backend.services.knowledge.retrieval import lexical_search, tokenize


def _row(cid: str, content: str, *, paper: str = "p1") -> dict:
    return {"chunk_id": cid, "paper_id": paper, "chunk_type": "unit", "lang": "en",
            "content": content, "page_no": 1, "block_id": "b-" + cid,
            "section": "S1", "order_global": 1, "metadata": {}}


DEFINITION = "The Shannon Lower Bound (SLB) is a powerful tool in information theory."
FULL_ONLY = "… the Shannon lower bound gives a distortion floor for any quantizer …"
ABBR_ONLY = "By Lemma 2 (SLB), the MSE distortion is bounded below."


# --------------------------------------------------------------------------
# 1. 分词：下划线是分隔符
# --------------------------------------------------------------------------


def test_subscript_underscore_is_split_into_two_tokens() -> None:
    assert tokenize("TURBOQUANT_mse") == ["turboquant", "mse"]
    assert tokenize("TURBOQUANT$_{\text{mse}}$")[:1] == ["turboquant"]
    assert "mse" in tokenize("TURBOQUANT$_{\text{mse}}$")


# --------------------------------------------------------------------------
# 2. 学别名：只认真缩写
# --------------------------------------------------------------------------


def test_learns_abbreviation_both_directions() -> None:
    aliases = expand.learn([DEFINITION])
    assert aliases["slb"] == ("shannon", "lower", "bound")
    assert aliases["shannon lower bound"] == ("slb",)


def test_ignores_synonym_juxtaposition_in_parentheses() -> None:
    """Online (data-oblivious) 是并置同义词，不是缩写：并进检索会带进整段无关正文。"""
    aliases = expand.learn(["Online (data-oblivious) quantization methods are cheap."])
    assert aliases == {}


def test_sentence_lead_in_is_not_part_of_the_full_form() -> None:
    """正则从最左的大写词起匹配，不能把 Using / As 也当成全称的一部分。"""
    aliases = expand.learn(["Using the Random Orthogonal Rotation (ROR) matrix …"])
    assert aliases["ror"] == ("random", "orthogonal", "rotation")
    aliases = expand.learn(["as shown in the Shannon Lower Bound (SLB) we have"])
    assert aliases["slb"] == ("shannon", "lower", "bound")


def test_drops_pairs_whose_initials_do_not_match() -> None:
    """首字母对不上就不是「缩写 ↔ 全称」：宁可少学一条，也不要往查询里塞噪声词。"""
    assert expand.learn(["The transformer big model (XYZ) is strong."]) == {}


def test_plural_abbreviation_still_matches() -> None:
    aliases = expand.learn(["Large Language Models (LLMs) are widely used."])
    assert aliases["llms"] == ("large", "language", "models")   # 补的是正文里那个词


def test_keeps_leading_article_out_of_expansion() -> None:
    aliases = expand.learn(["The Shannon Lower Bound (SLB) is a tool."])
    assert "the" not in aliases["slb"]


def test_expand_terms_adds_the_other_side_only() -> None:
    aliases = expand.learn([DEFINITION])
    assert expand.expand_terms(["slb"], "slb", aliases) == [
        "slb", "shannon", "lower", "bound"]
    assert expand.expand_terms(["shannon", "lower", "bound"],
                              "shannon lower bound", aliases) == [
        "shannon", "lower", "bound", "slb"]
    # 无关词不动：不许因为"反正有多几个词"就往查询里塞东西
    assert expand.expand_terms(["rotation"], "rotation", aliases) == ["rotation"]
    assert expand.expand_terms(["slb"], "slb", {}) == ["slb"]


# --------------------------------------------------------------------------
# 3. 词法召回：靠它们真的并起来
# --------------------------------------------------------------------------


@pytest.fixture
def rows(monkeypatch):
    data = [_row("def", DEFINITION), _row("full", FULL_ONLY), _row("abbr", ABBR_ONLY)]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: list(data))
    expand._CACHE.clear()
    return data


def test_subscript_query_is_recalled_at_all(rows, monkeypatch) -> None:
    """以前这里是 0 条：查询成了整 token turboquant_mse。"""
    data = rows + [_row("sub", "Algorithm 1 TURBOQUANT$_{\text{mse}}$: optimized for MSE.")]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: list(data))
    hits = [h["chunk_id"] for h in lexical_search("TURBOQUANT_mse", top_k=8)]
    assert hits[0] == "sub"                       # 下标那一块必须排第一
    assert "abbr" in hits                         # MSE 这个词本来就该命中它


def test_abbreviation_query_also_returns_the_full_form_block(rows) -> None:
    hits = {h["chunk_id"] for h in lexical_search("SLB", top_k=8)}
    assert {"abbr", "def", "full"} <= hits


def test_full_form_query_also_returns_the_abbreviation_block(rows) -> None:
    hits = {h["chunk_id"] for h in lexical_search("Shannon lower bound", top_k=8)}
    assert {"full", "def", "abbr"} <= hits


def test_expansion_does_not_leak_between_papers(rows, monkeypatch) -> None:
    """别名只从**作用域内**的行学：别的论文的定义句不许改这篇的检索。"""
    expand._CACHE.clear()
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: [
        _row("other", DEFINITION, paper="p2"), _row("mine", "rotation only", paper="p1")])
    hits = lexical_search("SLB", paper_ids=["p1"], top_k=8)
    assert [h["chunk_id"] for h in hits] == []
