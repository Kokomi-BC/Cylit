"""插图区域收紧逻辑的单元测试（纯函数，不依赖数据库）。

真实故障（TurboQuant p8）：解析阶段给出的 Figure 3 bbox 从 y=95.8 一直到 y=535.0，
把上面的 Figure 2 与中间的 ``2.2 NEEDLE-IN-A-HAYSTACK`` 标题一起框了进来；
前端因此出现"同一张图显示两次、图里夹着正文"。这里锁住修正后的边界规则。
"""

from __future__ import annotations

import pytest

from backend.services.paper.figure_regions import (
    SEP_PAD, _number_of, _parts_of, _snap_edges, _tighten, inject_images,
)
from backend.parsing.extract.render import ink_bbox
from backend.parsing.models import Rect


PAGE = Rect(0, 0, 612, 792)


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
# 贴边：边界不能"离文字太近"（多截）也不能"离文字太远留白缝"（少截）
# 真实故障 1（attention p4）：图块 bbox 的 y0=79.7，图题文字块是 [71.2, 81.2]，
#   出图余量把图题切掉半行 —— 图顶上多一条"被切一半的字"。
# 真实故障 2（TurboQuant p8）：图 2 的 bbox 底边 229.8，图注顶边 231.0，
#   只差 1.2pt，裁出来图注顶部被切掉一条。
# --------------------------------------------------------------------------


def test_snap_edges_pushes_top_below_intruding_title():
    """边界切进**图注/题注块**里 → 推到它下方（题注是论文文字，不该被裁进图里）。

    ⚠️ 必须把该块放进 `avoid`：`_snap_edges` 对"图注块"与"图内标注块"是**两种处置**
    （2026-09-27 实测分出来的）：图注块推开，图内小标注（子图标题/刻度）反而要**收进来**，
    否则就会出现用户报的"少裁"（turboquant p8 图 2 的三个子图标题被切掉 7.8pt，
    该 x 切片 99.6% 的墨迹没进裁图）。见 `test_snap_edges_includes_in_figure_label_when_cut`。
    """
    title = Rect(147.78, 71.2, 450.2, 81.16)
    out = _snap_edges([171.96, 81.0, 470.01, 273.19], [title], PAGE, avoid=[title])
    assert out[1] == pytest.approx(81.16 + 1.2)
    assert (out[0], out[2], out[3]) == (171.96, 470.01, 273.19)


def test_snap_edges_includes_in_figure_label_when_cut():
    """**图内**的单行标注被边界切到时，边界要往外让开、把整行收进来（少裁修复）。"""
    label = Rect(147.78, 71.2, 450.2, 81.16)          # 图内子图标题行（不在 avoid 里）
    out = _snap_edges([171.96, 79.69, 470.01, 273.19], [label], PAGE)
    assert out[1] == pytest.approx(71.2 - 1.2)        # 上边界往上让开，整行进图
    assert out[0] == pytest.approx(147.78 - 1.2)      # 左边界也被这行字穿过 → 同样让开
    assert (out[2], out[3]) == (470.01, 273.19)


def test_snap_edges_keeps_top_when_gap_is_safe():
    """边界在**图注块**下方、且已经贴到安全距离之外时不动它 —— 免得把图体上沿裁掉。"""
    title = Rect(147.78, 71.2, 450.2, 81.16)          # 文字块底边 81.16
    rect = [171.96, 79.69, 470.01, 273.19]            # 79.69 已经比 81.16+TEXT_PAD 低
    assert _snap_edges(rect, [title], PAGE, avoid=[title]) == rect


def test_snap_edges_pushes_bottom_above_caption():
    """边界离下方图注不足 TEXT_PAD → 推到图注上方（图注顶部不再被切）。"""
    caption = Rect(108.0, 231.0, 504.0, 240.96)
    out = _snap_edges([117.57, 95.8, 494.42, 229.8], [caption], PAGE)
    assert out[3] == pytest.approx(231.0 - 1.2)


def test_snap_edges_ignores_distant_text():
    """远处正文不该动边界（否则图体 bbox 会被平白推掉一大截）。"""
    body_para = Rect(108.0, 465.09, 505.66, 540.74)
    rect = [193.56, 69.0, 418.44, 404.75]
    assert _snap_edges(rect, [body_para], PAGE) == rect


def test_snap_edges_bails_out_when_region_would_collapse():
    """贴到文字边上会把区域收到最小高度以下 → 保留原边界（宁可多裁一点也不要裁空）。"""
    caption = Rect(108.0, 60.0, 504.0, 100.5)         # 边界正好切进图注里
    rect = [104.0, 40.0, 508.0, 100.5]
    assert _snap_edges(rect, [caption], PAGE) == rect  # 收到 98.8 后只剩 58.8 高 → 放弃贴边


# --------------------------------------------------------------------------
# 一张图截成两份：区域内部夹着别的图注（TurboQuant p8 的 Figure 3 框住了 Figure 2 的图注）
# --------------------------------------------------------------------------


def test_parts_of_splits_at_inner_caption():
    inner = [{"id": "b02", "type": "caption", "y0": 231.0, "y1": 240.96}]
    pieces = _parts_of([112.84, 95.8, 499.15, 535.01], inner, PAGE)
    assert len(pieces) == 2
    assert pieces[0][0][3] == pytest.approx(231.0 - 1.2)      # 上段止于图注上方
    assert pieces[1][0][1] == pytest.approx(240.96 + 1.2)     # 下段起于图注下方
    assert pieces[0][1] is None and pieces[1][1] is None


def test_parts_of_keeps_single_piece_without_inner_caption():
    pieces = _parts_of([117.57, 95.8, 494.42, 229.8],
                       [{"id": "x", "type": "para", "y0": 231.0, "y1": 241.0}], PAGE)
    assert pieces == [([117.57, 95.8, 494.42, 229.8], None)]


def test_parts_of_marks_tail_touching_page_footer():
    """图体画到页脚 → 最后一段标记 continues（跨页续图由调用方补下一页的条目）。"""
    pieces = _parts_of([104.0, 100.0, 508.0, 790.0], [], PAGE)
    assert len(pieces) == 1 and pieces[0][1] == {"continues": True}


# --------------------------------------------------------------------------
# 墨迹裁白
# --------------------------------------------------------------------------


def test_ink_bbox_trims_white_margin():
    """按 bbox 裁出来四周的白边要被量掉（TurboQuant p8 右侧近 40% 是空白）。"""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(50, 60, 150, 120))
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0))
    shape.commit()
    got = ink_bbox(doc, 1, Rect(0, 0, 200, 200), pad=0.0)
    assert got.x0 == pytest.approx(50, abs=1.5)
    assert got.y0 == pytest.approx(60, abs=1.5)
    assert got.x1 == pytest.approx(150, abs=1.5)
    assert got.y1 == pytest.approx(120, abs=1.5)
    doc.close()


def test_ink_bbox_returns_input_for_blank_region():
    import pymupdf as fitz

    doc = fitz.open()
    doc.new_page(width=200, height=200)
    rect = Rect(10, 10, 100, 100)
    assert ink_bbox(doc, 1, rect, pad=0.0) == rect
    doc.close()


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
