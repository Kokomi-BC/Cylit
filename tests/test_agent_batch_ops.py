r"""批量：同一串文字一次改多页/两侧；高亮与笔记一次建一批（用户口径 2026-12-07）。

用户的实测痛点：定理 2 在第 4 页正文、第 7 页 1.2 节、第 19 页附录各陈述一遍，改一处就要
3 页 × 2 侧 = 6 次调用；12 条高亮 + 12 条笔记 = 24 次往返。批量之后一次调用搞定，但**逐处/逐条
回报**：一处失败不能拖累别处，模型据此只重试失败的那几条。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.deps import Deps
from backend.agent.tools import ANNOTATE_BATCH_MAX, highlight_manage, note_manage, page_markdown_edit

FIND = r"\sqrt{3\pi^2}"
REPLACE = r"\sqrt{3}\,\pi^2"


class _EditHost:
    def __init__(self, broken: set[int] | None = None) -> None:
        self.calls: list[tuple] = []
        self.broken = broken or set()

    async def page_product(self, paper_id, page, side):
        return {"page": page, "side": side, "lines": "   1| x", "chars": 1}

    async def page_edit(self, paper_id, page, side, find, replace, *, reason="", reindex=True):
        self.calls.append((page, side, find, replace))
        if page in self.broken:
            return {"ok": False, "page": page, "error": f"第 {page} 页里找不到这一段"}
        return {"ok": True, "page": page, "side": side, "line": 4,
                "before": find, "after": replace,
                "summary": f"第 {page} 页 · 第 4 行 → 「{replace}」"}


def _ctx(host) -> SimpleNamespace:
    deps = Deps(cfg=SimpleNamespace(), host=host, paper_id="p", registry=None)
    return SimpleNamespace(deps=deps)


def _read(ctx, page: int, side: str = "original") -> None:
    asyncio.run(page_markdown_edit(ctx, op="view", page=page, side=side))


def test_replace_all_covers_every_read_page_and_both_sides() -> None:
    host = _EditHost()
    ctx = _ctx(host)
    _read(ctx, 4)
    out = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=4, side="both",
                                         pages="4,7", find=FIND, replace=REPLACE))
    assert out["ok"] is True and len(out["done"]) == 2      # 第 4 页两侧（读过那一页就够）
    assert [(d["page"], d["side"]) for d in out["done"]] == [(4, "original"), (4, "translation")]
    assert {s["page"] for s in out["skipped"]} == {7}, "没读过的页不许动"
    assert all("先读后写" in s["reason"] for s in out["skipped"])
    assert out["summary"].startswith("4 处里改了 2 处")


def test_replace_all_keeps_going_when_one_page_fails() -> None:
    host = _EditHost(broken={7})
    ctx = _ctx(host)
    _read(ctx, 4)
    _read(ctx, 7)
    out = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=4, side="original",
                                         pages="4,7", find=FIND, replace=REPLACE))
    assert out["ok"] is True and len(out["done"]) == 1 and len(out["skipped"]) == 1
    assert out["skipped"][0]["page"] == 7 and "找不到" in out["skipped"][0]["reason"]


def test_replace_all_refuses_when_nothing_changed() -> None:
    host = _EditHost(broken={4})
    ctx = _ctx(host)
    _read(ctx, 4)
    out = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=4, side="original",
                                         find=FIND, replace=REPLACE))
    assert out["ok"] is False and "一处都没改成" in out["error"]


def test_replace_all_caps_the_batch_size() -> None:
    """60 处（页 × 侧）是上限：一篇 30 页的论文「全文两侧」正好装得下，再多要求分批。"""
    ctx = _ctx(_EditHost())
    out = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=1, side="both",
                                         pages="1-40", find=FIND, replace=REPLACE))
    assert out["ok"] is False and "最多" in out["error"]


def test_replace_all_does_not_truncate_pages_like_reading_does() -> None:
    """读正文上限 20 页**不能**套在批量改写上：那会静默漏掉第 21 页往后。"""
    ctx = _ctx(_EditHost())
    ctx.deps.page_total = 22                      # 页数拿得到，`all` 才摊得开
    for page in range(1, 23):
        _read(ctx, page)
    out = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=1, side="original",
                                         pages="all", find=FIND, replace=REPLACE))
    assert out["truncated"] is False and 22 in out["pages"]


def test_replace_all_rejects_unknown_side_and_empty_find() -> None:
    ctx = _ctx(_EditHost())
    bad_side = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=1, side="左侧",
                                             pages="1", find=FIND, replace=REPLACE))
    assert bad_side["ok"] is False and "side" in bad_side["error"]
    no_find = asyncio.run(page_markdown_edit(ctx, op="replace_all", page=1, side="original",
                                            pages="1", find="  ", replace=REPLACE))
    assert no_find["ok"] is False and "find" in no_find["error"]


class _AnnHost:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def highlight_create(self, paper_id, payload):
        self.calls.append(payload)
        if "找不到" in payload["text"]:
            return {"ok": False, "error": "这段文字找不到", "near_miss": ["原文一"]}
        return {"ok": True, "page": 7, "scope": "row",
                "highlight": {"id": "h-" + str(len(self.calls)), "text": payload["text"],
                              "color": payload.get("color"), "side": "zh"},
                "summary": "第 7 页 · 译文面 · 「" + str(payload.get("text")) + "」"}

    async def note_create(self, paper_id, payload):
        self.calls.append(payload)
        return {"ok": True, "note": {"id": "n-" + str(len(self.calls)), "page": 7,
                                     "scope": "row", "selected_text": "图 1 的图注"}}


def test_highlight_batch_reports_each_item() -> None:
    host = _AnnHost()
    out = asyncio.run(highlight_manage(_ctx(host), op="create", items=[
        {"anchor": "md:p7", "text": "图 1 的图注"},
        {"anchor": "md:p7", "text": "找不到的这一段"},
        {"anchor": "md:p9", "text": "表 1 的图注", "color": "#123456"},
    ]))
    assert out["ok"] is True and len(out["created"]) == 2 and len(out["failed"]) == 1
    assert out["failed"][0]["index"] == 1 and out["failed"][0]["near_miss"] == ["原文一"]
    assert out["summary"] == "建了 2 条高亮，1 条没建成"
    assert host.calls[2]["color"] == "#123456", "每条自己的颜色要透传"


def test_highlight_batch_caps_and_rejects_bad_shape() -> None:
    host = _AnnHost()
    too_many = asyncio.run(highlight_manage(_ctx(host), op="create", items=[
        {"anchor": "md:p7", "text": "t" + str(i)} for i in range(ANNOTATE_BATCH_MAX + 1)]))
    assert too_many["ok"] is False and "最多" in too_many["error"]
    assert host.calls == []
    bad = asyncio.run(highlight_manage(_ctx(host), op="create", items=["not-a-dict"]))
    assert bad["ok"] is False and "items" in bad["error"]


def test_note_batch_writes_each_note() -> None:
    host = _AnnHost()
    out = asyncio.run(note_manage(_ctx(host), op="create", items=[
        {"highlight_id": "h-1", "content": "第一条"},
        {"highlight_id": "h-2", "content": "第二条"},
    ]))
    assert out["ok"] is True and [c["note_id"] for c in out["created"]] == ["n-1", "n-2"]
    assert out["summary"] == "写了 2 条笔记"
    assert host.calls[0]["highlight_id"] == "h-1"


def test_note_batch_reports_the_missing_highlight() -> None:
    host = _AnnHost()
    out = asyncio.run(note_manage(_ctx(host), op="create", items=[
        {"highlight_id": "", "content": "没挂上任何高亮"},
    ]))
    assert out["ok"] is False and "高亮" in out["failed"][0]["error"]

def test_single_edit_without_page_says_what_to_do() -> None:
    """`page` 有了默认值（批量不需要它）：漏给时要说人话，别掉进「第 0 页」。"""
    out = asyncio.run(page_markdown_edit(_ctx(_EditHost()), op="replace", side="original",
                                         find="a", replace="b"))
    assert out["ok"] is False and "page" in out["error"]


def test_replace_all_does_not_need_page() -> None:
    host = _EditHost()
    ctx = _ctx(host)
    _read(ctx, 4)
    out = asyncio.run(page_markdown_edit(ctx, op="replace_all", side="original", pages="4",
                                         find=FIND, replace=REPLACE))
    assert out["ok"] is True and out["done"][0]["page"] == 4