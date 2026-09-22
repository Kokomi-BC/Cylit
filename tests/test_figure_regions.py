"""插图区域收紧逻辑的单元测试（纯函数，不依赖数据库）。

真实故障（TurboQuant p8）：解析阶段给出的 Figure 3 bbox 从 y=95.8 一直到 y=535.0，
把上面的 Figure 2 与中间的 ``2.2 NEEDLE-IN-A-HAYSTACK`` 标题一起框了进来；
前端因此出现"同一张图显示两次、图里夹着正文"。这里锁住修正后的边界规则。
"""

from __future__ import annotations

import pytest

from backend.services.paper.figure_regions import (
    SEP_PAD, _number_of, _tighten, inject_images,
)


def test_tighten_figure_excludes_heading_above():
    """图注在下：上边界 = 图注上方最近的标题底边（再让开 SEP_PAD），下边界 = 图注顶边。"""
    bbox = [112.84, 95.80, 499.15, 535.01]          # 解析阶段给的重叠 bbox
    caption = {"y0": 541.6, "y1": 584.9}            # Figure 3 的图注
    structs = [{"y0": 231.0, "y1": 241.0},          # Figure 2 的图注
               {"y0": 294.7, "y1": 304.6}]          # 2.2 NEEDLE-IN-A-HAYSTACK 标题
    x0, y0, x1, y1 = _tighten(bbox, caption, structs, 842.0, below=True)
    assert y0 == pytest.approx(304.6 + SEP_PAD)
    assert y1 == pytest.approx(541.6)               # 收到图注顶边（图注在图下方）
    assert (x0, x1) == (112.84, 499.15)


def test_tighten_figure_without_separator_keeps_bbox_top():
    """图注上方没有标题/图注时（页首的图）：顶边保持 bbox，底边收到图注顶边。"""
    bbox = [117.57, 95.80, 494.42, 220.63]
    caption = {"y0": 231.0, "y1": 241.0}
    x0, y0, x1, y1 = _tighten(bbox, caption, [], 842.0, below=True)
    assert (x0, y0, x1) == (117.57, 95.80, 494.42)
    assert y1 == pytest.approx(231.0)


def test_tighten_table_uses_caption_below():
    """题注在表上方：上边界 = 题注底边，下边界 = 表块自身底边与下方标题顶边取小。"""
    bbox = [104.0, 228.71, 508.0, 700.0]
    caption = {"y0": 228.71, "y1": 246.67}
    structs = [{"y0": 720.0, "y1": 730.0}]
    _x0, y0, _x1, y1 = _tighten(bbox, caption, structs, 842.0, below=False)
    assert y0 == pytest.approx(246.67)
    assert y1 == pytest.approx(700.0)      # 表块底边比下一个标题更紧，取表块底边


def test_tighten_table_clipped_by_next_heading():
    """表块 bbox 过长、下方紧接着标题时，下边界收到标题顶边（让开 SEP_PAD）。"""
    bbox = [104.0, 228.71, 508.0, 800.0]
    caption = {"y0": 228.71, "y1": 246.67}
    structs = [{"y0": 600.0, "y1": 610.0}]
    _x0, _y0, _x1, y1 = _tighten(bbox, caption, structs, 842.0, below=False)
    assert y1 == pytest.approx(600.0 - SEP_PAD)


def test_tighten_bails_out_when_band_too_small():
    """收紧后高度过小（会裁空）时退回原始 bbox。"""
    bbox = [104.0, 300.0, 508.0, 320.0]
    caption = {"y0": 302.0, "y1": 310.0}
    structs = [{"y0": 298.0, "y1": 300.0}]
    assert _tighten(bbox, caption, structs, 842.0, below=True) == bbox


def test_number_of_reads_figure_and_table_numbers():
    assert _number_of("Figure 3: Evaluation of Llama") == "3"
    assert _number_of("**Table 12**: results") == "12"
    assert _number_of("图 5：方差") == "5"
    assert _number_of("no number here") == ""


# --------------------------------------------------------------------------
# Markdown 插图注入（页级产物的 Markdown 本体里原本没有任何图片语法）
# --------------------------------------------------------------------------


def test_inject_images_puts_ref_above_caption():
    md = "Body paragraph.\n\n**Figure 3: Evaluation**\n\nMore text."
    out = inject_images(md, {"3": "![Figure 3](/api/x.png)"})
    assert out.index("![Figure 3](/api/x.png)") < out.index("**Figure 3: Evaluation**")
    assert "Body paragraph." in out and "More text." in out


def test_inject_images_appends_unmatched_at_tail():
    md = "**Figure 1: cap**\n\ntext"
    out = inject_images(md, {"1": "![Figure 1](a.png)", "9": "![Figure 9](b.png)"})
    assert out.startswith("![Figure 1](a.png)")
    assert out.rstrip().endswith("![Figure 9](b.png)")


def test_inject_images_respects_existing_image_syntax():
    md = "![模型自己给的图](a.png)\n\n**Figure 2: cap**"
    assert inject_images(md, {"2": "![Figure 2](b.png)"}) == md


def test_inject_images_noop_without_refs():
    assert inject_images("text", {}) == "text"
    assert inject_images("", {"1": "![Figure 1](a.png)"}) == ""
