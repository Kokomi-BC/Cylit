"""单页**栏结构检测**（`reading_order.detect_layout`）的单元测试。

真实故障（sniffer，2026-09-28 用户截图）：p8 明明双栏（左栏 x 50.1~286.4、右栏 x 308.9~545.1），
却被判成**单栏** —— 后续每一环都跟着错：段落把左右两栏**逐行交叉**并成一个块
（`When trained with 25% of the data, SNIFFER- surpasses tion, prov`）、图注块横跨整页、
两张图的墨迹并集变成"通栏"（一张插图卡片里并排两张图 + 中间夹着另一栏的正文）。

根因只有一条：找栏缝要求"这一条竖带的**行覆盖数恒为 0**"，而 p8 有一条 line 是
**左右两栏被并在一起**的（`…In these datasets, img` + `trieval methods. …`，缝 27pt、
正好跨过文本区中线）—— 它的 rect 横跨整页，把整页唯一的栏缝盖住。
所以"并栏行"必须按**两段**分别计入覆盖，而不是按整条 rect 计入。
"""

from __future__ import annotations

import pytest

from backend.parsing.models import Span, TextLine
from backend.parsing.structure.reading_order import detect_layout

PAGE_W = 612.0
PAGE_H = 792.0
#: 实测几何：左栏 50.1~286.4、右栏 308.9~545.1，栏缝 295.8~306.0
LEFT = (50.1, 286.4)
RIGHT = (308.9, 545.1)
GUTTER = (295.8, 306.0)


def _text_line(text: str, y: float, x0: float, x1: float, page_no: int = 1) -> TextLine:
    sp = Span(text=text, x0=x0, y0=y, x1=x1, y1=y + 10.0, font="Times", size=9.0)
    return TextLine(spans=[sp], page_no=page_no)


def _merged_line(left_text: str, right_text: str, y: float, page_no: int = 1) -> TextLine:
    """左右两栏被并成**同一条 line**（两个 span + 中间 27pt 的缝，跨过中线 297.6）。"""
    a = Span(text=left_text, x0=LEFT[0], y0=y, x1=281.9, y1=y + 10.0, font="Times", size=9.0)
    b = Span(text=right_text, x0=RIGHT[0], y0=y, x1=RIGHT[1], y1=y + 10.0, font="Times", size=9.0)
    return TextLine(spans=[a, b], page_no=page_no)


def _two_column_lines(rows: int = 12, *, merged: int = 0) -> list[TextLine]:
    lines: list[TextLine] = []
    for i in range(rows):
        y = 100.0 + i * 14.0
        lines.append(_text_line("left column body text " + "x" * 20, y, *LEFT))
        lines.append(_text_line("right column body text " + "y" * 20, y, *RIGHT))
    for i in range(merged):
        lines.append(_merged_line("…In these datasets, img", "trieval methods. …",
                                  150.0 + i * 20.0))
    return lines


def test_merged_two_column_line_does_not_hide_the_gutter():
    """并栏的 line 不能把整页的栏缝"抹掉"（sniffer p8：就差这一条）。"""
    lay = detect_layout(_two_column_lines(merged=1), 8, PAGE_W, PAGE_H)
    assert lay.columns == 2
    assert lay.gutter == pytest.approx(GUTTER, abs=0.5)
    assert lay.cross_rows == 1


def test_a_real_full_width_line_still_blocks_the_gutter():
    """**真的**通栏元素（一条 span 横跨整页，中间没有缝）照旧挡住栏缝。

    反向保证：上面那条判据只挖"两个 span 之间的大缝"，不能把 sniffer p4 图 2 那种
    整幅图注（一条 span 从 50 到 545）也当成"并栏行" —— 那页的图注本来就是通栏的，
    判单栏是既有行为（图体边界靠 `figure_cut` 的图注锚点兜底）。
    """
    lines = _two_column_lines()
    for i in range(3):
        lines.append(_text_line("Figure 2. Architecture of the proposed framework " + "a" * 20,
                                150.0 + i * 11.0, *LEFT))
        lines[-1].spans[0].x1 = 545.1        # 一条 span 横跨整页
    lay = detect_layout(lines, 4, PAGE_W, PAGE_H)
    assert lay.columns == 1


def test_single_column_page_is_not_split():
    """单栏页（整页都是通栏行）不能凭空多出一条 gutter。"""
    lines = [_text_line("single column body text " + "x" * 60, 100.0 + i * 14.0, 108.0, 504.0)
             for i in range(20)]
    lay = detect_layout(lines, 3, PAGE_W, PAGE_H)
    assert lay.columns == 1
    assert lay.gutter is None


def test_one_column_is_not_split_when_text_is_offset():
    """单栏但文本区偏左（x 50~400）也不能凭空多出一条 gutter（两侧行数不足那道闸门）。"""
    lines = [_text_line("offset single column text " + "x" * 30, 100.0 + i * 14.0, 50.0, 400.0)
             for i in range(20)]
    lay = detect_layout(lines, 2, PAGE_W, PAGE_H)
    assert lay.columns == 1
    assert lay.notes


def test_too_many_crossing_lines_still_means_single_column():
    """跨缝行占比 > 18% 时照旧判单栏（这条老闸门不能绕过）。"""
    lines = _two_column_lines(rows=10)
    for i in range(12):
        lines.append(_text_line("full width heading " + "z" * 60, 150.0 + i * 13.0, *LEFT))
        lines[-1].spans[0].x1 = 545.1
    lay = detect_layout(lines, 5, PAGE_W, PAGE_H)
    assert lay.columns == 1
