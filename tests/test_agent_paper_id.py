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
from types import SimpleNamespace

from backend.agent.citations import RefRegistry
from backend.agent.deps import Deps
from backend.agent.host import HostError
from backend.agent.tools import (TEXT_PAGES_MAX, block_get, page_markdown_get,
                                 paper_search, translate_get)

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
    # 回执形状不变（没有新增字段）
    assert set(one) == {"page_total", "truncated", "pages"}
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
    assert set(out) == {"query", "page_total", "returned", "items", "hint"}
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
