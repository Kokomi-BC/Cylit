r"""读**别的论文**（可选 `paper_id`）的三条读通路：检索 / 正文 / 译文。

需求（「paper_id 阅读」）钉在这里：

1. 不传 `paper_id` → 与改造前**逐字段相同**：回执形状、ref 编号顺序、不多打一次后端；
2. 传了 → 读**那一篇**：页码按那篇的页数解析，ref 属于那篇（`Citation.paper_id`）；
3. 指名一篇不存在的论文 → 明确报错，**绝不静默退回当前论文**；
4. 两篇论文的块**不共用一个 ref**，`block_get` 也只从 ref 自己那篇取正文（不串读）。

全部离线：假宿主只实现 tools 真会调的那几个接口（archive / pages / search / block / row）。
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.host import HostError
from backend.agent.tools import (TEXT_PAGES_MAX, block_get, figure_image_get,
                                 figure_list, formula_get, page_image_get,
                                 page_markdown_get, paper_search, translate_get)

#: 当前论文（会话打开的那一篇）
CURRENT = "pA"
#: 另一篇论文：篇幅比当前论文短得多（页数口径靠它验）
OTHER = "pB"
#: 当前论文的页数
CURRENT_TOTAL = 22


def _paper(pid: str, pages: int, *, hits: list[dict] | None = None,
           texts: dict[str, str] | None = None) -> dict:
    """造一篇假论文：每页两行正文 + 译文，行 id 与真机同形（`p003-00`）。"""
    blocks = {}
    for n in range(1, pages + 1):
        blocks[n] = [(f"{pid} 第 {n} 页第 0 行", f"{pid} 第 {n} 页译文 0"),
                     (f"{pid} 第 {n} 页第 1 行", f"{pid} 第 {n} 页译文 1")]
    return {"page_total": pages, "title": f"论文 {pid}", "blocks": blocks,
            "hits": hits or [], "texts": texts or {}}


class FakeHost:
    """只实现这次改动会用到的接口（`archive` 是新增的存在性 / 页数入口）。"""

    def __init__(self, papers: dict[str, dict]) -> None:
        self.papers = papers
        self.archive_calls: list[str] = []
        self.pages_calls: list[tuple[str, str, str]] = []
        self.search_calls: list[tuple[str, str]] = []
        self.block_calls: list[tuple[str, str]] = []
        self.row_calls: list[tuple[str, int, str]] = []
        self.page_image_calls: list[tuple[str, int, int]] = []
        self.figure_calls: list[tuple[str, int]] = []
        self.figure_image_calls: list[tuple[str, int, int]] = []
        self.formula_calls: list[tuple[str, int | None]] = []

    async def archive(self, paper_id: str) -> dict:
        self.archive_calls.append(paper_id)
        paper = self.papers.get(paper_id)
        if paper is None:
            raise HostError(f"论文不存在：{paper_id}", status=404, code="NOT_FOUND")
        return {"paper_id": paper_id, "title": paper["title"],
                "page_total": paper["page_total"]}

    async def pages(self, paper_id: str, pages: str, lang: str = "both") -> dict:
        self.pages_calls.append((paper_id, pages, lang))
        paper = self.papers[paper_id]
        out = []
        for raw in str(pages).split(","):
            n = int(raw)
            rows = paper["blocks"].get(n) or []
            blocks = []
            for idx, (text, zh) in enumerate(rows):
                block = {"id": f"p{n:03d}-{idx:02d}", "type": "text",
                         "section": "1", "text": text, "source": "parsed"}
                if lang != "en":
                    block["translation"] = zh
                blocks.append(block)
            out.append({"page": n, "blocks": blocks})
        return {"paper_id": paper_id, "page_total": paper["page_total"], "pages": out}

    async def search(self, paper_id: str, query: str, *, top_k: int,
                     chunk_types: list[str] | None = None,
                     mode: str | None = None) -> dict:
        self.search_calls.append((paper_id, query))
        paper = self.papers[paper_id]
        items = [{**hit, "chunk_type": "text", "lang": "en"}
                 for hit in paper["hits"]]
        return {"paper_id": paper_id, "page_total": paper["page_total"],
                "query": query, "items": items[:top_k]}

    async def block(self, paper_id: str, block_id: str) -> dict:
        self.block_calls.append((paper_id, block_id))
        text = (self.papers[paper_id]["texts"] or {}).get(block_id)
        if text is None:
            raise HostError("主后端 HTTP 404", status=404, code="NOT_FOUND")
        return {"page": int(block_id[1:4]), "type": "text", "text": text}

    async def page_image(self, paper_id: str, page_no: int, dpi: int = 150):
        """页面图：按论文造字节（回执里那张图就是它，串篇一眼看得出来）。"""
        self.page_image_calls.append((paper_id, int(page_no), int(dpi)))
        if int(page_no) > self.papers[paper_id]["page_total"]:
            return None, None
        return f"{paper_id}:p{page_no}@{dpi}".encode(), "image/png"

    async def figures(self, paper_id: str, page: int | None = None) -> dict:
        self.figure_calls.append((paper_id, page))
        if not page or int(page) > self.papers[paper_id]["page_total"]:
            return {"items": []}
        return {"items": [{"index": 0, "kind": "figure", "number": "1",
                           "caption": f"{paper_id} 第 {page} 页的图注",
                           "url": f"https://img/{paper_id}/{page}.png"}]}

    async def figure_image(self, paper_id: str, page_no: int, index: int, **kw):
        self.figure_image_calls.append((paper_id, int(page_no), int(index)))
        return f"{paper_id}:fig{index}".encode(), "image/png"

    async def formulas(self, paper_id: str, *, page: int | None = None,
                       query: str | None = None, include_inline: bool = False,
                       offset: int = 0, limit: int = 40) -> dict:
        self.formula_calls.append((paper_id, page))
        items = []
        for n in range(1, self.papers[paper_id]["page_total"] + 1):
            if page is not None and int(page) != n:
                continue
            items.append({"page": n, "row_id": f"p{n:03d}-00", "display": True,
                          "latex": f"{paper_id} 第 {n} 页的公式", "grade": None,
                          "partial": False, "source": "markdown",
                          "context": f"{paper_id} 第 {n} 页的上下文"})
        return {"items": items, "display_total": len(items), "inline_total": 0,
                "source": "markdown"}

    async def row(self, paper_id: str, page: int, row_id: str) -> dict:
        self.row_calls.append((paper_id, page, row_id))
        rows = self.papers[paper_id]["blocks"].get(page) or []
        idx = int(str(row_id).split("-")[-1])
        if idx >= len(rows):
            return {}
        text, zh = rows[idx]
        return {"page": page, "type": "text", "text": text, "translation": zh}


def _deps(host, paper_id: str = CURRENT, page_total: int = CURRENT_TOTAL) -> Deps:
    cfg = SimpleNamespace(evidence_limit=12, evidence_chars=100000,
                          image_max_pages=3, image_max_dpi=400)
    return Deps(cfg=cfg, host=host, paper_id=paper_id,
                registry=RefRegistry(paper_id), page_total=page_total)


def _ctx(deps: Deps) -> SimpleNamespace:
    return SimpleNamespace(deps=deps)


def _two_papers() -> FakeHost:
    """两篇论文：**同名行 id / 同名块 id**（真机就是这样），正文不同。"""
    a = _paper(CURRENT, CURRENT_TOTAL,
               hits=[{"chunk_id": "pA:en", "block_id": None, "row_id": "p003-00",
                      "row_ids": ["p003-00"], "content": "A 号论文的检索片段",
                      "translation": "A 号论文的检索译文", "page_no": 3}],
               texts={"p003-00": "A 号论文的块正文"})
    b = _paper(OTHER, 4,
               hits=[{"chunk_id": "pB:en", "block_id": None, "row_id": "p003-00",
                      "row_ids": ["p003-00", "p004-00"], "content": "B 号论文的检索片段",
                      "translation": "B 号论文的检索译文", "page_no": 3}],
               texts={"p003-00": "B 号论文的块正文"})
    return FakeHost({CURRENT: a, OTHER: b})


# --------------------------------------------------------------------------
# 1. 不传 paper_id：与改造前逐字段相同
# --------------------------------------------------------------------------


def test_default_call_is_identical_and_asks_for_no_archive() -> None:
    """同一工具、同一 pages：不传 与 显式传本论文 id 的回执逐字段相等。

    顺带钉住"不多打一次后端"：不传时连档案都不去取（多一次往返也是行为变化）。
    """
    host = FakeHost({CURRENT: _paper(CURRENT, CURRENT_TOTAL)})
    plain = _deps(host)
    explicit = _deps(host)
    one = asyncio.run(page_markdown_get(_ctx(plain), "1"))
    two = asyncio.run(page_markdown_get(_ctx(explicit), "1", paper_id=CURRENT))
    assert one == two
    # 回执形状不变：只在末尾多出"这一份来自哪一篇"的两个键（见第 9 节）
    assert set(one) == {"page_total", "truncated", "pages", "paper_id", "scope"}
    assert set(one["pages"][0]) == {"page", "blocks"}
    assert set(one["pages"][0]["blocks"][0]) == {"ref", "type", "text", "translation"}
    # ref 仍是 1、2（分配顺序不变）
    assert [b["ref"] for b in one["pages"][0]["blocks"]] == [1, 2]
    assert host.archive_calls == [], "不传 paper_id 不该多一次档案请求"
    # 轨迹摘要里也没有多出来的键
    assert "paper_id" not in plain.trace[-1]["args"]


def test_paper_search_default_receipt_is_unchanged() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(paper_search(_ctx(deps), query="检索"))
    assert set(out) == {"query", "page_total", "returned", "items", "hint",
                        "paper_id", "scope"}
    assert out["query"] == "检索" and out["returned"] == 1
    item = out["items"][0]
    assert set(item) == {"ref", "page", "section", "type", "side", "snippet"}
    assert item["ref"] == 1 and item["page"] == 3 and item["side"] == "en"
    assert host.search_calls == [(CURRENT, "检索")]
    assert host.archive_calls == []


# --------------------------------------------------------------------------
# 2~3. 传了就读那一篇（正文 / 译文 / 语言模式）
# --------------------------------------------------------------------------


def test_page_markdown_get_reads_the_named_paper() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(page_markdown_get(_ctx(deps), "2", paper_id=OTHER))
    texts = [b["text"] for b in out["pages"][0]["blocks"]]
    assert texts == [f"{OTHER} 第 2 页第 0 行", f"{OTHER} 第 2 页第 1 行"]
    assert all(CURRENT not in t for t in texts), "绝不能混进当前论文的正文"
    assert host.pages_calls == [(OTHER, "2", "both")]
    assert {c.paper_id for c in deps.registry.all()} == {OTHER}


def test_translate_get_reads_the_named_paper_and_asks_for_zh() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(translate_get(_ctx(deps), "1", paper_id=OTHER))
    assert host.pages_calls == [(OTHER, "1", "zh")], "译文那条路仍要 lang=zh"
    assert [b["translation"] for b in out["pages"][0]["blocks"]] == [
        f"{OTHER} 第 1 页译文 0", f"{OTHER} 第 1 页译文 1"]
    assert set(out["pages"][0]["blocks"][0]) == {"ref", "translation"}


def test_both_sides_mode_works_for_the_named_paper() -> None:
    """`both` 面：原文 + 译文并给（与当前论文同一条路径，只是换了论文）。"""
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(page_markdown_get(_ctx(deps), "3", paper_id=OTHER))
    block = out["pages"][0]["blocks"][0]
    assert block["text"] == f"{OTHER} 第 3 页第 0 行"
    assert block["translation"] == f"{OTHER} 第 3 页译文 0"


# --------------------------------------------------------------------------
# 4. 不存在的 id：明确报错，绝不退回当前论文
# --------------------------------------------------------------------------


def test_unknown_paper_id_errors_and_never_falls_back() -> None:
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    for call in (lambda: page_markdown_get(ctx, "1", paper_id="ghost"),
                 lambda: translate_get(ctx, "1", paper_id="ghost"),
                 lambda: paper_search(ctx, query="检索", paper_id="ghost")):
        out = asyncio.run(call())
        assert out["ok"] is False
        assert "ghost" in out["error"] and "不存在" in out["error"]
        assert "pages" not in out and "items" not in out, "错误回执不许带正文"
    # 没有退回当前论文：一次正文 / 检索都没打，引用表仍是空的
    assert host.pages_calls == [] and host.search_calls == []
    assert len(deps.registry) == 0
    assert host.archive_calls == ["ghost", "ghost", "ghost"]


# --------------------------------------------------------------------------
# 5. 两篇之间不串读
# --------------------------------------------------------------------------


def test_refs_of_two_papers_never_collide_and_block_get_reads_the_right_one() -> None:
    """同一句话在两篇论文里各命中一次（行 id 还一模一样）→ 两个 ref，各读各的正文。"""
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    a = asyncio.run(paper_search(ctx, query="检索", paper_id=CURRENT))
    b = asyncio.run(paper_search(ctx, query="检索", paper_id=OTHER))
    ref_a, ref_b = a["items"][0]["ref"], b["items"][0]["ref"]
    assert ref_a != ref_b, "两篇论文的块不能共用一个编号"
    assert a["items"][0]["snippet"].startswith("A 号")
    assert b["items"][0]["snippet"].startswith("B 号")
    assert {c.paper_id for c in deps.registry.all()} == {CURRENT, OTHER}

    # 按 ref 展开（行路径）：A 的编号只读 A 的行，B 的编号只读 B 的行
    got_a = asyncio.run(block_get(ctx, ref=ref_a))
    got_b = asyncio.run(block_get(ctx, ref=ref_b))
    assert got_a["text"] == "pA 第 3 页第 0 行"
    assert got_b["text"].startswith("pB 第 3 页第 0 行")
    assert "pB 第 4 页第 0 行" in got_b["text"], "B 自己的两行照拼"
    assert host.row_calls == [(CURRENT, 3, "p003-00"), (OTHER, 3, "p003-00"),
                             (OTHER, 4, "p004-00")]

    # 按 ref 展开（块路径）：同样只从 ref 自己那篇取块
    page_b = asyncio.run(page_markdown_get(ctx, "3", paper_id=OTHER))
    ref_block = page_b["pages"][0]["blocks"][0]["ref"]
    block_got = asyncio.run(block_get(ctx, ref=ref_block))
    assert block_got["text"] == "B 号论文的块正文"
    assert host.block_calls[-1] == (OTHER, "p003-00")


def test_row_span_from_another_paper_never_joins_this_one() -> None:
    """行 id 每篇都长得一样：另一篇登记的行跨度不能拿来拼这一篇的段。"""
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    # B 的命中跨两行（p003-00 起），A 的命中只有一行 —— 跨度表里 p003-00 指向 B 的两行
    b = asyncio.run(paper_search(ctx, query="检索", paper_id=OTHER))
    assert deps.row_spans["p003-00"] == ["p003-00", "p004-00"]
    assert deps.row_span_paper["p003-00"] == OTHER
    a = asyncio.run(paper_search(ctx, query="检索", paper_id=CURRENT))
    got = asyncio.run(block_get(ctx, ref=a["items"][0]["ref"]))
    assert got["text"] == "pA 第 3 页第 0 行", "只读 A 自己的那一行"
    assert [rid for _, _, rid in host.row_calls] == ["p003-00"]
    # B 的编号仍能正常展开（该拼的两行照拼）
    got_b = asyncio.run(block_get(ctx, ref=b["items"][0]["ref"]))
    assert "pB 第 4 页第 0 行" in got_b["text"]


def test_search_cache_does_not_leak_between_papers() -> None:
    """同一个词在两篇论文里各查一次：不许命中另一篇的缓存回执。"""
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    first = asyncio.run(paper_search(ctx, query="同一句话", paper_id=OTHER))
    second = asyncio.run(paper_search(ctx, query="同一句话", paper_id=CURRENT))
    assert host.search_calls == [(OTHER, "同一句话"), (CURRENT, "同一句话")]
    assert first["items"][0]["snippet"].startswith("B 号")
    assert second["items"][0]["snippet"].startswith("A 号")
    assert first["items"][0]["ref"] != second["items"][0]["ref"]


# --------------------------------------------------------------------------
# 6. 回执仍带 ref / 定位信息
# --------------------------------------------------------------------------


def test_receipts_keep_ref_and_locate() -> None:
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    out = asyncio.run(page_markdown_get(ctx, "3", paper_id=OTHER))
    refs = [b["ref"] for b in out["pages"][0]["blocks"]]
    assert refs == [1, 2]
    cites = {c.ref: c for c in deps.registry.all()}
    assert cites[1].label.startswith("第 3 页"), "定位串仍按目标论文的页码算"
    assert cites[1].paper_id == OTHER
    assert cites[1].model_view() == {"ref": 1, "locate": cites[1].label,
                                     "snippet": cites[1].snippet}
    searched = asyncio.run(paper_search(ctx, query="检索", paper_id=OTHER))
    assert searched["items"][0]["page"] == 3
    assert "ref" in searched["items"][0] and "snippet" in searched["items"][0]


# --------------------------------------------------------------------------
# 7. 页数按目标论文算
# --------------------------------------------------------------------------


def test_page_total_comes_from_the_target_paper() -> None:
    """B 只有 4 页、当前论文 22 页：`all` / 越界页都必须按 B 算。"""
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    whole = asyncio.run(page_markdown_get(ctx, "all", paper_id=OTHER))
    assert [p["page"] for p in whole["pages"]] == [1, 2, 3, 4]
    assert whole["page_total"] == 4

    beyond = asyncio.run(page_markdown_get(ctx, "5", paper_id=OTHER))
    assert beyond["ok"] is False
    assert "共 4 页" in beyond["error"], "越界要用**目标论文**的页数说"

    # 当前论文自己的页数一点没被改写；它自己的 all 仍按 22 页（截到上限 20）
    assert deps.page_total == CURRENT_TOTAL
    mine = asyncio.run(page_markdown_get(ctx, "all"))
    assert mine["page_total"] == CURRENT_TOTAL
    assert len(mine["pages"]) == TEXT_PAGES_MAX and mine["truncated"] is True
    assert deps.page_total == CURRENT_TOTAL


# --------------------------------------------------------------------------
# 8. 页图 / 图形区域 / 公式：同一套 paper_id 语义（本轮补齐的四个读工具）
# --------------------------------------------------------------------------


def test_page_image_get_default_and_own_id_are_identical() -> None:
    """不传 与 显式传本论文 id：过程行、附上的图、编号全一样，且不多打一次档案。"""
    host = FakeHost({CURRENT: _paper(CURRENT, CURRENT_TOTAL)})
    plain, explicit = _deps(host), _deps(host)
    one = asyncio.run(page_image_get(_ctx(plain), "4", dpi=300))
    two = asyncio.run(page_image_get(_ctx(explicit), "4", dpi=300, paper_id=CURRENT))
    assert one.return_value == two.return_value
    assert [getattr(c, "data", c) for c in one.content] == [getattr(c, "data", c) for c in two.content]
    assert host.page_image_calls == [(CURRENT, 4, 300), (CURRENT, 4, 300)]
    assert host.archive_calls == [], "不传 paper_id 不该多一次档案请求"
    assert "paper_id" not in plain.trace[-1]["args"], "轨迹摘要里也不多这个键"


def test_page_image_get_reads_the_named_paper() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(page_image_get(_ctx(deps), "2", paper_id=OTHER))
    assert host.page_image_calls == [(OTHER, 2, 200)]
    assert out.content[1].data.startswith(b"pB"), "附上的必须是那一篇的那一页"
    assert {c.paper_id for c in deps.registry.all()} == {OTHER}
    assert deps.pages_read == [], "读了别篇的第 2 页不算读过这篇的"


def test_page_image_get_parses_pages_by_the_target_paper() -> None:
    """B 只有 4 页：第 9 页按**它**的页数报错，不按当前论文的 22 页去读。"""
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(page_image_get(_ctx(deps), "9", paper_id=OTHER))
    assert out["ok"] is False and "共 4 页" in out["error"]
    assert host.page_image_calls == []


def test_figure_list_default_and_own_id_are_identical() -> None:
    host = FakeHost({CURRENT: _paper(CURRENT, CURRENT_TOTAL)})
    plain, explicit = _deps(host), _deps(host)
    one = asyncio.run(figure_list(_ctx(plain), page=4))
    two = asyncio.run(figure_list(_ctx(explicit), page=4, paper_id=CURRENT))
    assert one == two
    assert set(one) == {"page", "returned", "items", "hint",
                        "paper_id", "scope"}, "老用法回执结构一个字不变"
    assert one["items"][0]["ref"] == 1
    assert host.archive_calls == []


def test_figure_list_reads_the_named_paper() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(figure_list(_ctx(deps), pages=[1, 2], paper_id=OTHER))
    assert host.figure_calls == [(OTHER, 1), (OTHER, 2)]
    captions = [g["items"][0]["caption"] for g in out["pages"]]
    assert captions == [f"{OTHER} 第 1 页的图注", f"{OTHER} 第 2 页的图注"]
    assert {c.paper_id for c in deps.registry.all()} == {OTHER}


def test_figure_image_get_default_and_own_id_are_identical() -> None:
    host = FakeHost({CURRENT: _paper(CURRENT, CURRENT_TOTAL)})
    plain, explicit = _deps(host), _deps(host)
    one = asyncio.run(figure_image_get(_ctx(plain), page=4, index=0))
    two = asyncio.run(figure_image_get(_ctx(explicit), page=4, index=0, paper_id=CURRENT))
    assert one.return_value == two.return_value
    assert [getattr(c, "data", c) for c in one.content] == [getattr(c, "data", c) for c in two.content]
    assert host.archive_calls == []


def test_figure_image_get_reads_the_named_paper() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(figure_image_get(_ctx(deps), items=[{"page": 3, "index": 0}],
                                       paper_id=OTHER))
    assert host.figure_image_calls == [(OTHER, 3, 0)]
    assert out.content[1].data.startswith(b"pB")
    cite, = deps.registry.all()
    assert cite.paper_id == OTHER and cite.label == "第 3 页 · 图 1"


def test_formula_get_default_and_own_id_are_identical() -> None:
    host = FakeHost({CURRENT: _paper(CURRENT, CURRENT_TOTAL)})
    plain, explicit = _deps(host), _deps(host)
    one = asyncio.run(formula_get(_ctx(plain), page=3))
    two = asyncio.run(formula_get(_ctx(explicit), page=3, paper_id=CURRENT))
    assert one == two
    assert one["items"][0]["ref"] == 1 and one["returned"] == 1
    assert host.formula_calls == [(CURRENT, 3), (CURRENT, 3)]
    assert host.archive_calls == []


def test_formula_get_reads_the_named_paper() -> None:
    host = _two_papers()
    deps = _deps(host)
    out = asyncio.run(formula_get(_ctx(deps), paper_id=OTHER))
    assert host.formula_calls == [(OTHER, None)], "整篇那份列表只取那篇的一次"
    assert [one["page"] for one in out["items"]] == [1, 2, 3, 4]
    assert out["items"][0]["latex"] == f"{OTHER} 第 1 页的公式"
    assert {c.paper_id for c in deps.registry.all()} == {OTHER}


def test_unknown_paper_id_errors_for_the_four_new_tools() -> None:
    """指名一篇不存在的论文：四个工具都明确报错，一次数都不去打那篇的内容。"""
    host = _two_papers()
    deps = _deps(host)
    ctx = _ctx(deps)
    for call in (lambda: page_image_get(ctx, "1", paper_id="ghost"),
                 lambda: figure_list(ctx, page=1, paper_id="ghost"),
                 lambda: figure_image_get(ctx, page=1, index=0, paper_id="ghost"),
                 lambda: formula_get(ctx, page=1, paper_id="ghost")):
        out = asyncio.run(call())
        assert out["ok"] is False, out
        assert "ghost" in out["error"] and "不存在" in out["error"]
    assert host.page_image_calls == [] and host.figure_calls == []
    assert host.figure_image_calls == [] and host.formula_calls == []
    assert len(deps.registry) == 0, "错误回执不许留下引用"
    assert host.archive_calls == ["ghost"] * 4


# --------------------------------------------------------------------------
# 9. 每份读回执都回显**这一趟实际读的是哪一篇**：`paper_id` + `scope`
#
# 为什么：同一会话里先后读两篇论文时，两次回执原先唯一能区分来源的线索可能只是
# `page_total`（22 vs 15）—— 那是间接信号；两篇同领域时连它都失效，模型会把另一篇
# 的块当成"这篇"的证据（静默拿错证据）。所以每个读工具都把目标论文写在回执里。
# --------------------------------------------------------------------------

#: 本轮被改的八个读工具（`kb_search` 每条命中本来就带 paper_id；`notes_search` 不是正文）
SCOPED_TOOLS = ("paper_search", "page_markdown_get", "translate_get", "block_get",
                "page_image_get", "figure_list", "figure_image_get", "formula_get")

#: 图片工具回给模型的是**说明文字**（ToolReturn）：来源写在那一行里，不在 dict 键上
#: 论文 id 里不会有 `；`，所以按它切开就够
_NOTE_SCOPE_RE = re.compile(r"paper_id=([^；\s]+)；scope=(current|foreign)")

#: 老口径的键集：新键只能追加在末尾，一个都不许删、不许改名
OLD_KEYS = {
    "paper_search": {"query", "page_total", "returned", "items", "hint"},
    "page_markdown_get": {"page_total", "truncated", "pages"},
    "translate_get": {"page_total", "truncated", "pages", "note"},
    "block_get": {"ref", "page", "type", "text", "translation"},
    "figure_list": {"page", "returned", "items", "hint"},
    "formula_get": {"total", "returned", "offset", "limit", "next_offset",
                    "has_more", "source", "truncated", "display_total",
                    "inline_total", "omitted_inline", "missing_pages", "items",
                    "hint"},
}


def _one_call(name: str, ctx, paper_id: str | None = None):
    """按名字调一次那个工具，拿回它的回执。

    `block_get` 没有 `paper_id` 参数：它的目标由 **ref 自己的论文身份**决定 —— 所以先在
    那一篇里检索一次，再按回来的 ref 展开（这正是"拿到谁的 ref 就展开谁"的真实姿势）。
    """
    kw = {} if paper_id is None else {"paper_id": paper_id}
    if name == "block_get":
        found = asyncio.run(paper_search(ctx, query="检索", paper_id=paper_id))
        return asyncio.run(block_get(ctx, ref=found["items"][0]["ref"]))
    calls = {
        "paper_search": lambda: paper_search(ctx, query="检索", **kw),
        "page_markdown_get": lambda: page_markdown_get(ctx, "1", **kw),
        "translate_get": lambda: translate_get(ctx, "1", **kw),
        "page_image_get": lambda: page_image_get(ctx, "1", **kw),
        "figure_list": lambda: figure_list(ctx, page=1, **kw),
        "figure_image_get": lambda: figure_image_get(ctx, page=1, index=0, **kw),
        "formula_get": lambda: formula_get(ctx, page=1, **kw),
    }
    return asyncio.run(calls[name]())


def _scope_in(out) -> tuple[str | None, str | None, str]:
    """取（paper_id, scope, scope_note）：dict 读键，图片工具读说明文字那一行。"""
    if isinstance(out, dict):
        return out.get("paper_id"), out.get("scope"), str(out.get("scope_note") or "")
    note = str(out.return_value)
    hit = _NOTE_SCOPE_RE.search(note)
    assert hit, f"说明文字里没写出来源：{note!r}"
    pid, scope = hit.group(1), hit.group(2)
    return pid, scope, note if scope == "foreign" else ""


def test_a_default_call_says_current_and_carries_no_scope_note() -> None:
    """a) 不传 paper_id：`paper_id` == 当前论文、`scope` == "current"、**没有** scope_note。"""
    assert len(SCOPED_TOOLS) == 8, "八个读工具一个都不能漏"
    for name in SCOPED_TOOLS:
        deps = _deps(_two_papers())
        out = _one_call(name, _ctx(deps))
        pid, scope, note = _scope_in(out)
        assert pid == CURRENT, f"{name}：回执没回显当前论文（拿到 {pid}）"
        assert scope == "current", f"{name}：不传就是当前论文"
        assert note == "", f"{name}：current 时不许带 scope_note（省 token）"
        if isinstance(out, dict):
            assert "scope_note" not in out, f"{name}：current 的回执里不许有这个键"


def test_b_explicit_own_id_is_field_for_field_identical() -> None:
    """b) 显式传当前论文 id：与 a) **逐字段相同**（把老口径钉死）。"""
    for name in SCOPED_TOOLS:
        one = _one_call(name, _ctx(_deps(_two_papers())))
        two = _one_call(name, _ctx(_deps(_two_papers())), CURRENT)
        assert _scope_in(one) == _scope_in(two), f"{name}：来源字段不一致"
        if isinstance(one, dict):
            assert one == two, f"{name}：不传 与 显式传本篇 必须逐字段相同"
        else:   # 图片工具：说明文字与附图逐项相同
            assert one.return_value == two.return_value, f"{name}：说明文字不一致"
            assert [getattr(c, "data", c) for c in one.content] == [
                getattr(c, "data", c) for c in two.content], f"{name}：附图不一致"


def test_c_foreign_call_names_both_papers() -> None:
    """c) 传别的论文 id：== 那篇、`scope` == "foreign"、scope_note 里有当前论文 id。"""
    for name in SCOPED_TOOLS:
        deps = _deps(_two_papers())
        out = _one_call(name, _ctx(deps), OTHER)
        pid, scope, note = _scope_in(out)
        assert pid == OTHER, f"{name}：回显的必须是**目标那篇**，不是当前论文"
        assert scope == "foreign", f"{name}：读了另一篇就该是 foreign"
        assert note and CURRENT in note, f"{name}：scope_note 要说清当前是哪一篇"


def test_d_scoped_tools_append_the_new_keys_at_the_end() -> None:
    """d) 原有键一个不少、不改名：新键只追加在末尾。"""
    for name in SCOPED_TOOLS:
        out = _one_call(name, _ctx(_deps(_two_papers())))
        if not isinstance(out, dict):
            assert _NOTE_SCOPE_RE.search(str(out.return_value)), f"{name}：说明里没有来源"
            continue
        assert list(out)[-2:] == ["paper_id", "scope"], f"{name}：新键要追加在末尾"
        assert set(out) - {"paper_id", "scope"} == OLD_KEYS[name], \
            f"{name}：原有键被改了（现在是 {sorted(out)}）"


# --------------------------------------------------------------------------
# 10. `block_get` 批量：**每条 item 带自己的 paper_id**；混批时顶层不许再报单篇
#
# 为什么：顶层原是单值 `paper_id` / `scope`，三条 ref 来自两篇时它只能报一个 ——
# 当前论文那条也会被别篇的 `scope="foreign"` 包住（用户实测：requested=3、scope=foreign，
# 可 [3] 明明是当前论文的摘要）。缺失时人会警惕，误标时人会直接信，所以这比"字段缺失"更严重。
# --------------------------------------------------------------------------


def _page_refs(deps: Deps, paper_id: str | None = None) -> list[int]:
    """读那一篇的第 2 页，拿回它自己的那些 ref（顺序与页内一致）。"""
    out = asyncio.run(page_markdown_get(_ctx(deps), "2", paper_id=paper_id))
    return [b["ref"] for b in out["pages"][0]["blocks"]]


def test_block_get_single_ref_item_carries_the_same_paper_id() -> None:
    """a) 单条 ref：item 带 paper_id，且与顶层一致（current 与 foreign 各一次）。"""
    for target, scope in ((CURRENT, "current"), (OTHER, "foreign")):
        deps = _deps(_two_papers())
        ctx = _ctx(deps)
        ref = _page_refs(deps, None if target == CURRENT else OTHER)[0]
        assert deps.registry.by_ref(ref).paper_id == target, "前提：ref 属于那一篇"
        out = asyncio.run(block_get(ctx, ref=ref))
        # 老回执结构不变：原有键一个不少、没有新面孔（`note` 是照旧的按行兜底提示），
        # 来源键仍追加在末尾
        extra = {"paper_id", "scope"} | ({"scope_note"} if scope == "foreign" else set())
        assert OLD_KEYS["block_get"] <= set(out), sorted(out)
        assert set(out) <= OLD_KEYS["block_get"] | {"note"} | extra, sorted(out)
        assert extra <= set(out), sorted(out)
        tail = ["paper_id", "scope"] + (["scope_note"] if scope == "foreign" else [])
        assert list(out)[-len(tail):] == tail, list(out)
        assert "items" not in out, "单条调用不该被改成批量结构"
        assert (out["paper_id"], out["scope"]) == (target, scope)
        assert out["paper_id"] == deps.registry.by_ref(ref).paper_id, "取 ref 自己那篇，不是硬填"

        # 批量形态（哪怕只有一条）里那一项也带自己的 paper_id
        one = asyncio.run(block_get(ctx, refs=[ref]))
        assert one["items"][0]["paper_id"] == target
        assert one["paper_id"] == target and one["scope"] == scope


def test_block_get_mixed_batch_names_each_source_and_the_top_level() -> None:
    """b) 混批（别篇两条 + 当前论文一条）：每条 item 各对各的，顶层 mixed 且点名。"""
    deps = _deps(_two_papers())
    ctx = _ctx(deps)
    mine = _page_refs(deps)
    theirs = _page_refs(deps, OTHER)
    out = asyncio.run(block_get(ctx, refs=[theirs[0], theirs[1], mine[0]]))
    assert out["returned"] == 3 and out["failed"] == 0
    # 条目层：每条只报自己的来源（当前论文那条**不再**被 foreign 包住）
    assert [row["paper_id"] for row in out["items"]] == [OTHER, OTHER, CURRENT]
    # 顶层：不再给单值，改报 mixed + 全部来源
    assert out["scope"] == "mixed"
    assert "paper_id" not in out, "混批时顶层给单值就一定会误标其中一条"
    assert out["paper_ids"] == [OTHER, CURRENT], "按出现顺序列出全部来源"
    note = out["scope_note"]
    assert "2 篇" in note and OTHER in note and CURRENT in note, note
    assert "paper_id" in note, "要说清来源看条目自己的 paper_id"


def test_block_get_many_refs_from_one_foreign_paper_is_still_foreign() -> None:
    """c) 全部来自同一篇别篇：仍是 foreign（不因为"不止一条"就变 mixed）。"""
    deps = _deps(_two_papers())
    out = asyncio.run(block_get(_ctx(deps), refs=_page_refs(deps, OTHER)))
    assert out["returned"] == 2
    assert out["scope"] == "foreign" and out["paper_id"] == OTHER
    assert "paper_ids" not in out, "只有一篇时不给列表"
    assert out["scope_note"] and CURRENT in out["scope_note"]
    assert {row["paper_id"] for row in out["items"]} == {OTHER}


def test_block_get_failed_items_also_carry_their_own_paper_id() -> None:
    """d) 失败条目（ok=false）也带 paper_id —— 否则它会跟着顶层的来源被误标。"""
    deps = _deps(_two_papers())
    ctx = _ctx(deps)
    # 行路径读不到：那篇没有这一行
    bad_row = deps.registry.assign(key="k:row", kind="block", row_id="p999-00",
                                   page=9, snippet="…", paper_id=OTHER).ref
    # 块路径 404：当前论文里没有这个块（"blk404" 也不像行 id，不会按行兜底）
    bad_block = deps.registry.assign(key="k:blk", kind="block", block_id="blk404",
                                     page=4, snippet="…").ref
    good = _page_refs(deps)[0]
    out = asyncio.run(block_get(ctx, refs=[bad_row, bad_block, good]))
    rows = {row["ref"]: row for row in out["items"]}
    assert out["failed"] == 2 and out["returned"] == 1
    assert rows[bad_row]["ok"] is False and rows[bad_row]["paper_id"] == OTHER
    assert rows[bad_block]["ok"] is False and rows[bad_block]["paper_id"] == CURRENT
    assert rows[good]["paper_id"] == CURRENT
    assert out["scope"] == "mixed", "失败条目也算来源"
    # 引用表里根本没有这个 ref：纸面形状仍在，值是 None（来源不明，不猜成当前论文）
    ghost = asyncio.run(block_get(ctx, refs=[999]))
    assert "paper_id" in ghost["items"][0] and ghost["items"][0]["paper_id"] is None

