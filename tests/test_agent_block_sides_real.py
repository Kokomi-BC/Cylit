r"""同一块的中英两面只能占一个 ref：译文预览是**截断**的（纯离线）。

用户复测（2026-12）：`paper_search("implementation algorithm")` 回来 ref 1（side en）与 ref 2
（side zh），`block_get([1,2])` 显示两条的 `text` / `translation` **逐字完全相同** —— 明明是
同一块，却各占一个位置；`deduped` 只记 1、hint 还说"这 4 条各是一处位置"（实际 3 处）。
日志/复现脚本打出来的两条差别：

1. **块键不同**：en 覆盖 `p020-13…14`、zh 覆盖 `p020-13…14` 再加 `p021-01` ——
   写侧给中英两路切 chunk 的边界本来就不一样，所以"落点 + 覆盖面"的键必然不同；
2. **文本长度不同**：en 的译文是**截断预览**（177 字 + `…`），zh 的 `content` 是完整那段
   （254 字），前 177 字逐字相同 —— 指纹相等**永远**对不上，文本兜底这道闸门从没生效过。

现在的规则：文本比"逐字相等**或**一边是另一边的截断预览"（|`_text_same`），仍然只认同页；
三条保护不许破：两页同文不并（第 5/6 页那对）、短于 12 字不并（「见图 3」）、
同一落点指向两条不同证据不并（chunk 类型不同 / 文本不同）。
"""

from __future__ import annotations

import asyncio

from backend.agent import tools as agent_tools

#: 线上那一块的两种长度：en 的译文是**预览**（截断 + `…`），zh 的 `content` 是完整那段
ZH_FULL = ("# E 实现\n\n如算法 1（面向 MSE 最优量化）与算法 2（面向内积量化）所详述，"
           "TURBOQUANT 算法在量化和反量化阶段都会对输入嵌入向量施加随机旋转矩阵。"
           "这一变换对算法的性能至关重要。在实践中，旋转矩阵被实现为结构化矩阵……")
ZH_PREVIEW = ZH_FULL[:60] + "…"
EN = ("# E IMPLEMENTATION\n\nThe TURBOQUANT algorithm, as detailed in Algorithm 1 (for "
      "MSE-optimal quantization) and Algorithm 2 (for inner product quantization), "
      "incorporates random rotation matrices applied to the input embedding vectors.")


def _hit(chunk_id: str, *, lang: str, content: str, translation: str | None = None,
         row_id: str = "p020-13", row_ids: list[str] | None = None,
         block_id: str | None = None, page_no: int = 20,
         chunk_type: str = "unit") -> dict:
    """一条检索命中（形状照宿主真实回执：行身份和块身份都在）。"""
    return {"chunk_id": chunk_id, "paper_id": "p1", "block_id": block_id,
            "row_id": row_id or None,
            "row_ids": list(row_ids if row_ids is not None else ([row_id] if row_id else [])),
            "page_no": page_no, "section": "E IMPLEMENTATION", "chunk_type": chunk_type,
            "lang": lang, "content": content, "translation": translation}


class FakeHost:
    """检索 + 按行读：两边都给同一份"这一块在哪几行"。"""

    def __init__(self, items: list[dict], rows: dict[str, dict] | None = None) -> None:
        self.items = list(items)
        self.rows = dict(rows or {})

    async def search(self, paper_id, query, *, top_k, chunk_types=None, mode=None):
        return {"page_total": 22, "items": [dict(one) for one in self.items]}

    async def row(self, paper_id, page, row_id):
        return self.rows.get(row_id)


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


def _search(host, **kwargs):
    return asyncio.run(agent_tools.paper_search(_Ctx(FakeDeps(host)), "implementation", **kwargs))


#: 线上那一对：同一个锚点行、覆盖面不同（zh 多一行 `p021-01`）、译文是截断预览
def _twins(**over) -> list[dict]:
    en = _hit("p1:a:unit:p020-13-p020-14:en", lang="en", content=EN,
              translation=ZH_PREVIEW, row_ids=["p020-13", "p020-14"])
    zh = _hit("p1:b:unit:p020-13-p021-01:zh", lang="zh", content=ZH_FULL,
              row_ids=["p020-13", "p020-14", "p021-01"])
    en.update(over.get("en") or {})
    zh.update(over.get("zh") or {})
    return [en, zh]


# --------------------------------------------------------------------------
# ① A：同一块的两面 → 一个 ref（另一面进 also_sides）
# --------------------------------------------------------------------------


def test_truncated_preview_and_wider_coverage_still_one_ref() -> None:
    """这条就是线上那条：块键不同（覆盖面差 `p021-01`）、译文是截断预览 → **仍然一处**。"""
    out = _search(FakeHost(_twins()), top_k=4)
    assert out["returned"] == 1, out
    assert out["deduped"] == 1, "同一块的两面并了就要说出来"
    one = out["items"][0]
    assert one["side"] == "en" and one["also_sides"] == ["zh"], one
    assert one["page"] == 20
    assert "不再把同一块数两遍" in out["hint"]


def test_no_row_ids_left_still_merges_by_text() -> None:
    """老投影只给块 id（没有行 id）：靠文本还是得合上 —— 否则 top_k 又被同一处吃掉。"""
    out = _search(FakeHost(_twins(en={"row_id": "", "row_ids": [], "block_id": "blk-9"},
                                  zh={"row_id": "", "row_ids": [], "block_id": "blk-10"})),
                  top_k=4)
    assert out["returned"] == 1, out
    assert out["items"][0]["also_sides"] == ["zh"]


def test_deduped_counts_every_merged_hit() -> None:
    """三面（en / zh / zh 的另一段）都落在这一处 → `deduped` 记真数，不是恒 1。"""
    third = _hit("p1:c:unit:p020-13-p020-14:zh", lang="zh", content=ZH_FULL,
                 row_ids=["p020-13", "p020-14"])
    out = _search(FakeHost([*_twins(), third]), top_k=4)
    assert out["returned"] == 1 and out["deduped"] == 2, out


# --------------------------------------------------------------------------
# ② 三条保护
# --------------------------------------------------------------------------


def test_same_text_on_two_real_pages_stays_two_positions() -> None:
    """真的两页（第 5 / 6 页）文本一样 → 两处不同的位置（不是重复陈述）。"""
    host = FakeHost([
        _hit("a", lang="en", content=EN, translation=ZH_PREVIEW, row_id="p005-03",
             row_ids=["p005-03"], page_no=5),
        _hit("b", lang="zh", content=ZH_FULL, row_id="p006-09", row_ids=["p006-09"],
             page_no=6),
    ])
    out = _search(host, top_k=4)
    assert out["returned"] == 2 and "deduped" not in out, out
    assert [one["page"] for one in out["items"]] == [5, 6]


def test_short_snippets_never_merge_by_text() -> None:
    """「见图 3」这种短句满篇都是：短于 12 字一律不凭文本合并。"""
    host = FakeHost([
        _hit("a", lang="en", content="见图 3", row_id="p020-01", row_ids=["p020-01"]),
        _hit("b", lang="zh", content="见图 3", row_id="p020-02", row_ids=["p020-02"]),
    ])
    out = _search(host, top_k=4)
    assert out["returned"] == 2 and "deduped" not in out, out


def test_same_anchor_but_different_evidence_stays_two() -> None:
    """同一行里指向两条不同证据（文本不同、chunk 类型也不同）→ 两条各自给号。"""
    host = FakeHost([
        _hit("a", lang="en", content=EN, translation=ZH_PREVIEW, row_id="p020-13",
             row_ids=["p020-13", "p020-14"]),
        _hit("b", lang="en", content="$D_{\\text{mse}} \\leq \\frac{1}{4^b}$ 的推导",
             row_id="p020-13", row_ids=["p020-13"], chunk_type="formula"),
    ])
    out = _search(host, top_k=4)
    assert out["returned"] == 2 and "deduped" not in out, out
    assert len({one["ref"] for one in out["items"]}) == 2


# --------------------------------------------------------------------------
# ③ B：同一块，`paper_search` 与 `block_get` 报同一个页码
# --------------------------------------------------------------------------


def test_block_get_reports_the_anchor_page_of_a_cross_page_span() -> None:
    """跨页的一段（`p020-13…14` + `p021-01`）：报**锚点行**那一页，不是最后一行那一页。

    实测分歧就是这么来的：循环里 `page = data.get("page")` 一路被最后一行覆盖 → `block_get`
    说第 21 页，同一块的 `paper_search` 说第 20 页。
    """
    host = FakeHost(_twins(), rows={
        "p020-13": {"page": 20, "type": "heading", "text": "# E IMPLEMENTATION"},
        "p020-14": {"page": 20, "type": "para", "text": "The TURBOQUANT algorithm…"},
        "p021-01": {"page": 21, "type": "para", "text": "input embedding vectors…"},
    })
    deps = FakeDeps(host)
    ctx = _Ctx(deps)
    search = asyncio.run(agent_tools.paper_search(ctx, "implementation", top_k=4))
    ref = search["items"][0]["ref"]
    deps.row_spans["p020-13"] = ["p020-13", "p020-14", "p021-01"]
    block = asyncio.run(agent_tools.block_get(ctx, ref=ref))
    assert block["page"] == search["items"][0]["page"] == 20, (block, search)
    assert block["text"].count("\n\n") == 2, "整段照样拼起来（跨页那半句不能丢）"
