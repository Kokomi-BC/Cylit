r"""`paper_search` 的**同源去重**与 `side`（纯离线）。

用户实测（2026-12-07，turboquant 第 42 号 ref）：

1. `paper_search(mode="rag", top_k=4)` 回 `returned: 4`，但 **ref 42 出现两次** —— 同一块
   的中英两路命中各占一行，实际只有 3 个不同位置；
2. 回执里**没有 `side`**，用户只能靠"片段里有没有汉字"反推是原文还是译文；纯公式片段
   （`$D_{\\text{mse}} \\leq …$`）里一个汉字都没有，于是被当成原文引了出去。

这一组用例钉住三件事：
- `top_k` 数的是**不同块**（先按 ref 去重再截断，所以会向后端多要一点原始命中）；
- 同一块的另一面不丢，记在该条的 `also_sides` 里；
- `side` 默认 `both`（跨语言召回有价值），且它是**召回口径**、不是排序。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent import tools as agent_tools

EN = "We quantize the rotated coordinates independently."
ZH = "我们对旋转后的坐标逐坐标独立量化。"


def _hit(chunk_id: str, *, lang: str, content: str, block_id: str,
         translation: str | None = None, page: int = 5,
         chunk_type: str = "unit", row_ids: list[str] | None = None) -> dict:
    return {"chunk_id": chunk_id, "paper_id": "p1", "block_id": block_id,
            "row_ids": list(row_ids or []),
            "page_no": page, "section": "2", "chunk_type": chunk_type,
            "lang": lang, "content": content, "translation": translation}


class FakeHost:
    """按真实回执的形状回命中；记录每次调用要了几条。"""

    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)
        self.calls: list[dict] = []

    async def search(self, paper_id, query, *, top_k, chunk_types=None, mode=None):
        self.calls.append({"query": query, "top_k": top_k, "mode": mode})
        return {"page_total": 22, "items": [dict(one) for one in self.items]}


class FakeDeps:
    def __init__(self, host) -> None:
        from backend.agent.citations import RefRegistry

        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.row_spans: dict = {}
        self.page_total = 0

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _call(ctx, **kwargs):
    return asyncio.run(agent_tools.paper_search(ctx, "quantize", **kwargs))


def _twins(page: int = 5, block: str = "p005-03") -> list[dict]:
    """同一块的**中英两条命中**：英文那条带中文预览，中文那条 content 就是那段中文。

    这正是线上撞 ref 的形状 —— 两条的"落点 + 展示片段"完全相同，只有 chunk id 不同
    （road a:…:en 与 road b:…:zh）。`block_id` 在真实索引里就是**首行 id**（`p003-13`），
    `_register_chunk` 会把它剥成"行身份"，所以两条的落点才是同一个。
    """
    rows = [f"p{page:03d}-13", f"p{page:03d}-14"]
    return [
        _hit(f"p1:a:unit:{block}:en", lang="en", content=EN, block_id=rows[0],
             translation=ZH, page=page, row_ids=rows),
        _hit(f"p1:b:unit:{block}:zh", lang="zh", content=ZH, block_id=rows[0],
             page=page, row_ids=rows),
    ]


# --------------------------------------------------------------------------
# ① 同源重复不再吃掉 top_k 预算
# --------------------------------------------------------------------------


def test_same_block_en_zh_pair_counts_once_and_refs_are_unique() -> None:
    host = FakeHost([*_twins(), *_twins(page=6, block="p006-02")])
    out = _call(_Ctx(FakeDeps(host)), top_k=2, mode="rag")
    refs = [one["ref"] for one in out["items"]]
    assert out["returned"] == 2, out
    assert len(set(refs)) == 2, f"ref 不能重复：{refs}"
    assert out["deduped"] == 2, "两条重复要说出来，不能静默丢"


def test_top_k_counts_distinct_blocks_not_raw_hits() -> None:
    """4 条原始命中里有一对孪生 → 仍要回 4 个**不同位置**（向后端多要一点）。"""
    host = FakeHost([
        *_twins(block="p005-03"),
        _hit("c-2", lang="en", content="second block", block_id="p006-01", page=6),
        _hit("c-3", lang="en", content="third block", block_id="p007-01", page=7),
        _hit("c-4", lang="en", content="fourth block", block_id="p008-01", page=8),
    ])
    out = _call(_Ctx(FakeDeps(host)), top_k=4)
    assert host.calls[0]["top_k"] > 4, "去重会吃掉条数，必须多要原始命中"
    pages = [one["page"] for one in out["items"]]
    assert pages == [5, 6, 7, 8], pages
    assert out["returned"] == 4


def test_merged_twin_keeps_the_other_face_in_also_sides() -> None:
    """同一块的另一面**不丢掉**：留在 also_sides 里，用户仍知道有中文那一份。"""
    host = FakeHost(_twins())
    out = _call(_Ctx(FakeDeps(host)))
    assert out["returned"] == 1
    one = out["items"][0]
    assert one["side"] == "en" and one["also_sides"] == ["zh"]
    assert "also_sides" in (out.get("hint") or ""), "回执里要说明合并了哪一面"


def test_relevance_order_is_preserved() -> None:
    """去重不排序：命中顺序仍是后端给的相关度顺序。"""
    host = FakeHost([
        _hit("c-1", lang="en", content="first", block_id="b1"),
        *_twins(block="b2"),
        _hit("c-3", lang="en", content="third", block_id="b3"),
    ])
    out = _call(_Ctx(FakeDeps(host)), top_k=3)
    # 片段仍按"先原文、后译文"给（老口径不动）；合并只影响条数与 also_sides
    assert [one["snippet"] for one in out["items"]] == ["first", EN, "third"]


def test_old_receipt_fields_are_all_still_there() -> None:
    host = FakeHost([_hit("c-1", lang="en", content="only", block_id="b1")])
    out = _call(_Ctx(FakeDeps(host)))
    one = out["items"][0]
    assert set(one) == {"ref", "page", "section", "type", "side", "snippet"}
    assert out["returned"] == 1 and out["page_total"] == 22
    assert out["items"][0]["type"] == "unit"


# --------------------------------------------------------------------------
# ② 每条命中带 `side`，并按 `side` 过滤
# --------------------------------------------------------------------------


def test_side_is_read_from_the_hit_itself() -> None:
    assert agent_tools._hit_side({"lang": "en", "chunk_type": "unit"}) == "en"
    assert agent_tools._hit_side({"lang": "zh", "chunk_type": "unit"}) == "zh"
    assert agent_tools._hit_side({"lang": "any", "chunk_type": "formula"}) == "formula"
    # 图 / 表没有译文孪生，`lang` 是 any → 按原文面算
    assert agent_tools._hit_side({"lang": "any", "chunk_type": "figure"}) == "en"


def test_default_is_both_and_every_hit_carries_its_side() -> None:
    host = FakeHost([
        _hit("c-en", lang="en", content="english block", block_id="b1"),
        _hit("c-zh", lang="zh", content="中文那一段", block_id="b2"),
        _hit("c-f", lang="any", content="$x$", block_id="b3", chunk_type="formula"),
    ])
    out = _call(_Ctx(FakeDeps(host)))
    assert [one["side"] for one in out["items"]] == ["en", "zh", "formula"]


@pytest.mark.parametrize("want,expected", [("zh", ["zh"]), ("en", ["en"]),
                                           ("formula", ["formula"])])
def test_side_filter_narrows_recall(want, expected) -> None:
    host = FakeHost([
        _hit("c-en", lang="en", content="english block", block_id="b1"),
        _hit("c-zh", lang="zh", content="中文那一段", block_id="b2"),
        _hit("c-f", lang="any", content="$x$", block_id="b3", chunk_type="formula"),
    ])
    out = _call(_Ctx(FakeDeps(host)), side=want)
    assert [one["side"] for one in out["items"]] == expected


def test_side_filter_keeps_a_block_available_on_the_other_face() -> None:
    """合并过的块：要 zh 那一面时它仍然算命中（另一面确实存在）。"""
    host = FakeHost(_twins())
    out = _call(_Ctx(FakeDeps(host)), side="zh")
    assert out["returned"] == 1 and out["items"][0]["also_sides"] == ["zh"]


def test_side_filter_that_drops_everything_says_so() -> None:
    host = FakeHost([_hit("c-en", lang="en", content="english only", block_id="b1")])
    out = _call(_Ctx(FakeDeps(host)), side="zh")
    assert out["returned"] == 0 and "side='both'" in (out.get("hint") or "")


def test_unknown_side_is_rejected_loudly() -> None:
    """拼错就报错：静默降级成 both 会让模型以为"按面筛过了"，接着断言"译文里没这句"。"""
    host = FakeHost([_hit("c-en", lang="en", content="x", block_id="b1")])
    out = _call(_Ctx(FakeDeps(host)), side="zh-CN")
    assert out["ok"] is False and "both" in out["error"] and "formula" in out["error"]
    assert host.calls == [], "取值不合法时不该打后端"


def test_side_has_its_own_cache_slot() -> None:
    """"先查 both、再查 zh"必须真的再打一次后端，否则模型拿到的仍是全集。"""
    host = FakeHost([_hit("c-en", lang="en", content="x", block_id="b1")])
    ctx = _Ctx(FakeDeps(host))
    _call(ctx)
    _call(ctx, side="zh")
    assert len(host.calls) == 2
    _call(ctx, side="zh")            # 同一面重复调用才命中缓存
    assert len(host.calls) == 2
