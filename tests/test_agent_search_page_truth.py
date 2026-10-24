r"""`paper_search` 的**页码真相源**：行 id，不是索引里的 `page_no`（纯离线）。

用户自检实测（TurboQuant，同一轮）：
- `paper_search` 说 ref 43 / ref 44 在第 **20** 页，`block_get([43,44])` 说同两条在第 **21** 页；
- 那两条的 `text` 与 `translation` **逐字完全相同**（同一块的中英两面），却各占一个位置 ——
  于是 hint 断言"这 4 条是 4 个**不同位置**"，实际只有 3 处。

根因是同一份"这是第几页"写了两遍：`_chunk_identity` 读索引给的 `page_no`，而
`block_get`（以及高亮锚点、跳页）读**行 id**（`p021-06` → 第 21 页）。索引那条差了一页，于是
(a) 对外报的页与 `block_get` 打架，(b) 文本指纹兜底"只认同页"→ 拒绝合并 → 去重漏。

现在的规则：页码一律走 `_hit_page`（行 id 优先，拿不到才退回索引的 `page_no` / `page`），
兜底去重比较的也是**这同一个数**。"只认同页"的策略不变 —— 相邻页文本完全相同的两条实测
确实是两处不同位置。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent import tools as agent_tools
from backend.agent.deps import Deps
from backend.agent.tools import highlight_manage

#: 线上那两条的块文本（markdown 里带 `#` 标题记号）
BLOCK = ("# E IMPLEMENTATION\n\nThe TURBOQUANT algorithm rotates the input vector by a "
         "random orthogonal matrix and then quantizes each coordinate independently.")
OTHER = ("# 4 EXPERIMENTS\n\nWe compare against product quantization and report the "
         "mean squared error of the inner product estimate on four datasets.")


def _hit(chunk_id: str, *, lang: str, content: str = BLOCK, row_id: str = "",
         page_no: int = 0, block_id: str | None = None) -> dict:
    """一条检索命中；`row_id` 与索引的 `page_no` **故意可以互相矛盾**（实测就这样）。"""
    return {"chunk_id": chunk_id, "paper_id": "p1", "block_id": block_id,
            "row_id": row_id or None, "row_ids": [row_id] if row_id else [],
            "page_no": page_no or None, "page": page_no or None,
            "section": "3", "chunk_type": "unit", "lang": lang,
            "content": content, "translation": None}


class FakeHost:
    def __init__(self, items: list[dict]) -> None:
        self.items = list(items)

    async def search(self, paper_id, query, *, top_k, chunk_types=None, mode=None):
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


def _call(host, **kwargs):
    return asyncio.run(agent_tools.paper_search(_Ctx(FakeDeps(host)), "quantize", **kwargs))


# --------------------------------------------------------------------------
# A + B：同一块的两面 —— 索引页码差一页也不能拆成两处，对外只报行 id 那一页
# --------------------------------------------------------------------------


def test_two_sides_of_one_block_merge_even_when_index_pages_disagree() -> None:
    """行 id 都在第 21 页（`p021-*`）、索引 `page_no` 一个 20 一个 21 → 仍然是一处。

    这就是实测的那一对：文本逐字相同、页码差一页。合并靠指纹兜底，而兜底比较的页码
    必须是**行 id 解析出来的**（同为 21），不是索引那两个自相矛盾的值。
    """
    host = FakeHost([_hit("a1", lang="en", row_id="p021-06", page_no=20),
                     _hit("b1", lang="zh", row_id="p021-09", page_no=21)])
    out = _call(host, top_k=4)
    assert out["returned"] == 1, out
    assert out["deduped"] == 1, "同一块的两面合并了就要说出来"
    one = out["items"][0]
    assert one["page"] == 21, "页码按行 id 报（与 block_get 同一口径），不是索引的 20"
    assert one["side"] == "en" and one["also_sides"] == ["zh"]


def test_page_does_not_come_from_the_index_when_a_row_id_exists() -> None:
    """单条命中就能看出页码来源：`row_id=p021-06` + `page_no=20` → 报 21。"""
    host = FakeHost([_hit("a1", lang="en", row_id="p021-06", page_no=20)])
    out = _call(host, top_k=4)
    assert out["items"][0]["page"] == 21


# --------------------------------------------------------------------------
# 兜底仍然"只认同页"，但比的是解析后的页
# --------------------------------------------------------------------------


def test_identical_text_on_two_real_pages_stays_two_positions() -> None:
    """`p005-*` 与 `p006-*` 是真的两页 → 不合并，页码各自正确。

    索引 `page_no` 这里**故意都写 6**：如果去重比的是索引值，这两条会被并成一条
    （实测里出事的就是这一层）。比行 id 解析出来的 5 / 6 才不会。
    """
    host = FakeHost([_hit("a1", lang="en", row_id="p005-03", page_no=6),
                     _hit("b1", lang="zh", row_id="p006-09", page_no=6)])
    out = _call(host, top_k=4)
    assert out["returned"] == 2 and "deduped" not in out
    assert [one["page"] for one in out["items"]] == [5, 6]
    assert len({one["ref"] for one in out["items"]}) == 2


def test_index_page_is_the_fallback_when_no_row_id() -> None:
    """老投影只给块 id 与索引页码（没有行 id）→ 退回索引值，行为与今天一致。"""
    host = FakeHost([_hit("a1", lang="en", block_id="blk-17", page_no=20),
                     _hit("b1", lang="zh", block_id="blk-18", page_no=21)])
    out = _call(host, top_k=4)
    assert [one["page"] for one in out["items"]] == [20, 21]
    assert out["returned"] == 2 and "deduped" not in out


# --------------------------------------------------------------------------
# hint 不许替错误背书
# --------------------------------------------------------------------------


def test_hint_does_not_claim_the_merged_count_are_distinct_positions() -> None:
    """合并之后不许再说"这 N 条是 N 个**不同位置**"（实测那句话是错的）。"""
    host = FakeHost([_hit("a1", lang="en", row_id="p003-13", page_no=3),
                     _hit("b1", lang="zh", row_id="p003-15", page_no=3),
                     _hit("c1", lang="en", content=OTHER, row_id="p009-01", page_no=9),
                     _hit("d1", lang="zh", content=OTHER, row_id="p009-04", page_no=9)])
    out = _call(host, top_k=4)
    hint = out.get("hint") or ""
    assert out["deduped"] == 2 and out["returned"] == 2
    assert "个**不同位置**" not in hint, hint
    assert "各是一处位置" in hint, hint
    assert "中英两路" in hint and "also_sides" in hint, hint


# --------------------------------------------------------------------------
# 顺带钉住的两处文档级修正（字段含义，不改老字段）
# --------------------------------------------------------------------------


def test_group_receipt_names_the_per_group_count() -> None:
    """顶层 `returned` 是各组之和、组内 `group_returned` 才是该组条数（同名不同义）。"""
    host = FakeHost([_hit("a1", lang="en", row_id="p004-03", page_no=4)])
    out = _call(host, queries=["rotation", "quantize"], top_k=2)
    assert len(out["groups"]) == 2
    assert out["returned"] == sum(g["group_returned"] for g in out["groups"])
    assert all(g["returned"] == g["group_returned"] for g in out["groups"])


class _HlHost:
    async def highlights(self, paper_id, **kw):
        return {"ok": True, "items": [
            {"id": "h-1", "block_id": "p002-04", "text": r"$\frac{a}{b}$ 随机旋转是有效的",
             "start": 0, "end": 5, "color": "yellow", "side": "zh",
             "scope": "row", "page": 2}]}


def test_highlight_list_renders_length_in_render_coordinates() -> None:
    """`start`/`end` 是**渲染坐标**（公式只占 1 个字符），别拿 `len(text)` 去切。"""
    host = _HlHost()
    out = asyncio.run(highlight_manage(
        SimpleNamespace(deps=Deps(cfg=SimpleNamespace(), host=host, paper_id="p",
                                 registry=None)), op="list"))
    row = out["items"][0]
    assert row["start"] == 0 and row["end"] == 5, "老字段一个都不动"
    assert row["rendered_length"] == 5
    assert row["rendered_length"] != len(row["text"]), "text 是 LaTeX 原文，长度本就不是一回事"
