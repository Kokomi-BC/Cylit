"""图注的长度上限：不能被 200 的片段上限截断（纯离线，不碰真库）。

线上实测（真实数据，2026-12）：`figure_regions.media_of_page` 给的图注**本身是完整的** ——
第 8 页图 2 = 234 字符、图 3 = 313 字符、第 9 页表 1 = 113 字符。但 `figure_list` 用的是
`_clip(fig.get("caption") or "")` 的**默认** `SNIPPET_CHARS = 200`，于是 234 那条被切成
"…Speedup is measured relat…"、313 那条被切成 "…While some methods struggle with recall, T…"。
模型拿着断尾的图注描述图，丢掉的就是整句结论（"speedup 相对 PyTorch einsum 基线"这种）。

现在 `figure_list` 的 `caption`（回执 items）与引用 `snippet`（registry.assign）都按
`CAPTION_CHARS = 800` 裁（与 `backend/services/agent/project.py` 的 `CAPTION_CHARS` 同口径）；
`SNIPPET_CHARS` 的默认值**没有**动 —— 正文片段几十处都靠它。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agent import tools as agent_tools
from backend.agent.citations import RefRegistry

#: 第 8 页图 2（线上实测 234 字符，逐字）
CAPTION_234 = ("Figure 2: (a) Inner-product error, (b) MSE, (c) recall, and (d) query time against "
               "Product Quantization (PQ) and its variants on the GloVe and OpenAI benchmarks. "
               "Measured on one GPU; speedup is relative to the PyTorch einsum baseline.")
#: 第 8 页图 3（线上实测 313 字符，逐字）
CAPTION_313 = ("Figure 3: (a) Inner-product error and (b) recall versus the bit-width for TurboQuant, "
               "PQ, and RaBitQ on the OpenAI benchmarks. TurboQuant keeps a low error at every "
               "bit-width, while some methods struggle with recall. Timing excludes index "
               "construction; speedup is measured relative to the PyTorch einsum baseline.")


class _FakeHost:
    """只回这一页的图 / 表（不动主后端）。"""

    def __init__(self, captions: list[str]) -> None:
        self.captions = captions
        self.calls: list[int] = []

    async def figures(self, paper_id, page=None):
        self.calls.append(page)
        return {"page": page, "items": [
            {"index": i, "kind": "table" if i == 1 else "figure",
             "number": f"Figure {i + 1}" if i != 1 else "Table 1",
             "caption": text,
             "url": f"/api/v1/papers/{paper_id}/figures/region/{page}/{i}?v=abc"}
            for i, text in enumerate(self.captions)]}


class _Deps:
    def __init__(self, host) -> None:
        self.host = host
        self.paper_id = "p1"
        self.registry = RefRegistry("p1")
        self.query_cache: dict = {}
        self.row_spans: dict = {}
        self.page_total = 12

    def count_search(self) -> None:
        return None

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _figure_list(monkeypatch, captions: list[str]):
    """跑一遍 `figure_list`；`_caption_rows` 打桩（引用行锚点要查页产物，这里有真库也不需要）。"""
    monkeypatch.setattr(agent_tools, "_caption_rows", lambda paper_id, page: {})
    ctx = _Ctx(_Deps(_FakeHost(captions)))
    return ctx, asyncio.run(agent_tools.figure_list(ctx, page=8))


def test_measured_captions_are_exactly_the_measured_length() -> None:
    """先钉住"线上那两条图注到底多长"——下面两条断言才有意义。"""
    assert (len(CAPTION_234), len(CAPTION_313)) == (234, 313)


def test_caption_and_snippet_use_the_caption_limit(monkeypatch) -> None:
    """234 / 313 字符的图注要**原样**给出去（以前走 SNIPPET_CHARS → 截在 200）。"""
    ctx, out = _figure_list(monkeypatch, [CAPTION_234, CAPTION_313])

    assert [item["caption"] for item in out["items"]] == [CAPTION_234, CAPTION_313]
    assert out["items"][0]["caption"].endswith("PyTorch einsum baseline."), "整句结论必须在"
    assert "…" not in out["items"][0]["caption"]
    # 引用片段（前端引用表 + 模型看到的 snippet）同样按图注上限
    snippets = [c.snippet for c in ctx.deps.registry.all()]
    assert snippets == [CAPTION_234, CAPTION_313]


def test_overlong_caption_is_still_capped(monkeypatch) -> None:
    """超长图注仍然有闸（800），别让"放宽到 800"变成"不设上限"。"""
    _, out = _figure_list(monkeypatch, ["超" * 1200])
    caption = out["items"][0]["caption"]
    assert len(caption) == agent_tools.CAPTION_CHARS == 800
    assert caption.endswith("…")


def test_snippet_chars_default_is_untouched(monkeypatch) -> None:
    """`SNIPPET_CHARS` 是正文片段的默认值（几十处在用），这次只该多出 `CAPTION_CHARS`。"""
    assert agent_tools.SNIPPET_CHARS == 200
    assert agent_tools._clip("字" * 500) == "字" * 199 + "…"
