"""检索变体的写入层：只改检索、不改译法，而且**批量**（一次给几组）。

钉四件事：

1. 入参容错：variants 数组 / variant 单数 / 空串 / 与 term 相同 —— 该跳过的说清原因，不静默丢；
2. 别名表**两个方向**都存（term→variant、variant→term），键与 expand.expand_terms 同口径；
3. 词法检索真的因此多带回块（manual_aliases=False 与 True 的差别）；
4. 回执**按 term 分组**，并带上「加上之前/之后分别带回哪些页」的证据。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.tools import term_manage
from backend.services.knowledge import expand, retrieval, term_variants


def _row(cid: str, content: str, *, paper: str = "p1", page: int = 1) -> dict:
    return {"chunk_id": cid, "paper_id": paper, "chunk_type": "unit", "lang": "en",
            "content": content, "page_no": page, "block_id": "b-" + cid,
            "section": "S1", "order_global": page, "metadata": {}}


# --------------------------------------------------------------------------
# 1. 入参：批量 + 容错
# --------------------------------------------------------------------------


def test_pairs_accepts_list_and_single_and_skips_bad_input() -> None:
    pairs, skipped = term_variants.pairs_from([
        {"term": "TurboQuant_mse", "variants": ["TurboQuant MSE", "  "]},
        {"term": "SLB", "variant": "Shannon lower bound"},      # 单数写法也认
        {"term": "MSE", "variants": ["MSE"]},                    # 与 term 相同
        {"term": "", "variants": ["x"]},                         # term 空
        {"term": "QJL"},                                         # 没给 variants
        "not-a-dict",
    ])
    assert pairs == [{"term": "TurboQuant_mse", "variant": "TurboQuant MSE"},
                     {"term": "SLB", "variant": "Shannon lower bound"}]
    reasons = [item.get("reason") for item in skipped]
    assert len(reasons) == 4 and all(reasons)


def test_pairs_caps_one_call() -> None:
    items = [{"term": f"term{i}", "variants": [f"variant{i}"]}
             for i in range(term_variants.MAX_ITEMS + 5)]
    pairs, _ = term_variants.pairs_from(items)
    assert len(pairs) == term_variants.MAX_ITEMS


# --------------------------------------------------------------------------
# 2. 别名表：两个方向、键与 expand 同口径
# --------------------------------------------------------------------------


def test_alias_map_is_bidirectional() -> None:
    table = term_variants.alias_map([
        {"term": "SLB", "variant": "Shannon lower bound"},
    ])
    assert table["slb"] == ("shannon", "lower", "bound")
    assert table["shannon lower bound"] == ("slb",)


def test_alias_map_skips_identical_and_empty() -> None:
    assert term_variants.alias_map([{"term": "MSE", "variant": "MSE"},
                                    {"term": "", "variant": "x"}]) == {}


# --------------------------------------------------------------------------
# 3. 词法检索：manual_aliases 真的改结果
# --------------------------------------------------------------------------


def test_manual_variant_changes_lexical_hits(monkeypatch) -> None:
    """正文里没有 turboquant / mse 这两个词：只有登记了变体才搜得到。"""
    rows = [_row("hit", "Algorithm 1 with the distortion-optimal quantizer"),
            _row("noise", "unrelated sentence about weather")]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: list(rows))
    expand._CACHE.clear()
    monkeypatch.setattr(term_variants, "aliases", lambda paper_id: term_variants.alias_map([
        {"term": "TurboQuant_mse", "variant": "distortion-optimal quantizer"}]))
    plain = retrieval.lexical_search("TurboQuant_mse", paper_ids=["p1"], top_k=8,
                                     manual_aliases=False)
    with_variant = retrieval.lexical_search("TurboQuant_mse", paper_ids=["p1"],
                                            top_k=8, manual_aliases=True)
    assert plain == []
    assert [h["chunk_id"] for h in with_variant] == ["hit"]


def test_variants_do_not_leak_across_papers(monkeypatch) -> None:
    """跨库检索（paper_ids 为空或几篇）时不吃单篇变体：同一串写法在不同篇是不同概念。"""
    rows = [_row("hit", "Algorithm 1 with the distortion-optimal quantizer")]
    monkeypatch.setattr(retrieval.vector_store, "all_rows", lambda **kw: list(rows))
    monkeypatch.setattr(term_variants, "aliases",
                        lambda paper_id: term_variants.alias_map([
                            {"term": "TurboQuant_mse",
                             "variant": "distortion-optimal quantizer"}]))
    assert retrieval.lexical_search("TurboQuant_mse", top_k=8) == []


# --------------------------------------------------------------------------
# 4. 工具回执：按 term 分组 + 证据
# --------------------------------------------------------------------------


class _Host:
    def __init__(self) -> None:
        self.added: list[dict] = []
        self.removed: list[dict] = []

    async def term_variants(self, paper_id: str) -> dict:
        return {"paper_id": paper_id, "items": [], "count": 0}

    async def term_variants_add(self, paper_id: str, items: list[dict]) -> dict:
        self.added = items
        return {
            "added": [{"id": "tv1", "term": "TurboQuant_mse",
                       "variant": "TurboQuant MSE"},
                      {"id": "tv2", "term": "SLB", "variant": "Shannon lower bound"}],
            "skipped": [{"term": "SLB", "variant": "shannon lb", "reason": "已经有了"}],
            "evidence": [
                {"term": "TurboQuant_mse", "pages_before": [7],
                 "pages_after": [7, 5, 6], "gained": [5, 6]},
                {"term": "SLB", "pages_before": [16], "pages_after": [16, 19],
                 "gained": [19]},
            ],
            "count": 3,
        }

    async def term_variants_remove(self, paper_id: str, *, ids=None, terms=None) -> dict:
        self.removed = [{"id": i, "term": "TurboQuant_mse", "variant": "TurboQuant MSE"}
                        for i in (ids or [])]
        return {"removed": self.removed, "count": 1}


def _deps(host: _Host) -> Deps:
    cfg = SimpleNamespace(evidence_limit=12, evidence_chars=100000,
                          image_max_pages=3, image_max_dpi=400)
    return Deps(cfg=cfg, host=host, paper_id="p", registry=RefRegistry("p"))


def test_add_receipt_groups_by_term_with_evidence() -> None:
    host = _Host()
    out = asyncio.run(term_manage(
        SimpleNamespace(deps=_deps(host)), op="add",
        items=[{"term": "TurboQuant_mse", "variants": ["TurboQuant MSE"]},
               {"term": "SLB", "variants": ["Shannon lower bound", "shannon lb"]}]))
    assert out["ok"] is True and out["added"] == 2 and out["count"] == 3
    terms = [g["term"] for g in out["groups"]]
    assert terms == ["TurboQuant_mse", "SLB"], "按入参顺序分组"
    first = out["groups"][0]
    assert first["gained"] == [5, 6] and first["pages_before"] == [7]
    second = out["groups"][1]
    assert second["skipped"] == [{"variant": "shannon lb", "reason": "已经有了"}]
    assert second["gained"] == [19]


def test_remove_is_undo() -> None:
    host = _Host()
    out = asyncio.run(term_manage(SimpleNamespace(deps=_deps(host)), op="remove", ids=["tv1"]))
    assert out["ok"] is True and out["removed"] == 1 and out["count"] == 1
    assert out["items"][0]["id"] == "tv1"


def test_unknown_op_is_reported() -> None:
    out = asyncio.run(term_manage(SimpleNamespace(deps=_deps(_Host())), op="merge"))
    assert out["ok"] is False and "list / add / remove" in out["error"]
