"""插图图体**按图注分段**的单元测试（纯函数，不依赖数据库/PDF）。

真实故障（9984-boomerang p33，用户 2026-09-27 报的"一页多图会漏掉"）：
一页上下堆叠三张图（图 28/29/30），三条图注各自"向上取所有墨迹的并集" →
三条 bbox 分别是 36~204 / 36~384 / 36~563（下面那张把上面全吞了），
而 `figure` 表主键是 `block_id`、三条候选又都撞到同一个宿主块 → `INSERT OR REPLACE`
静默覆盖，最后只剩两条，**图 29 整张消失**（PDF 图注 38 个 / 产出 23 张）。

这里锁住"按同栏上一张图注的底边切段、每段各自取墨迹外接框"的规则。
"""

from __future__ import annotations

import pytest

from backend.parsing.extract.vector_layer import Drawing
from backend.parsing.models import RawBlock, Rect, Span, TextLine
from backend.parsing.structure.figure_cut import (
    _caption_anchor, _looks_like_caption_line, _segment_floor, detect_figures,
)

PAGE = Rect(0, 0, 612, 792)


def _line(text: str, page_no: int, y: float, x0: float, x1: float,
          line_no: int = 0) -> TextLine:
    sp = Span(text=text, x0=x0, y0=y, x1=x1, y1=y + 10.0, font="Times", size=9.0)
    return TextLine(spans=[sp], page_no=page_no, block_no=0, line_no=line_no)


def _caption(text: str, page_no: int, y: float, x0: float = 108.0, x1: float = 504.0) -> RawBlock:
    return RawBlock(page_no=page_no, block_no=0,
                    lines=[_line(text, page_no, y, x0, x1)])


def _ink(page_no: int, y0: float, y1: float, x0: float = 207.0, x1: float = 405.0) -> Drawing:
    return Drawing(kind="curve", rect=Rect(x0, y0, x1, y1),
                   p0=(x0, y0), p1=(x1, y1))


def test_stacked_figures_are_segmented_by_caption():
    """一页三张堆叠图 → 三段互不吞并（图体只在"上一张图注底边 ~ 本图注顶边"这一带里）。"""
    blocks = [
        _caption("Figure 28: Boomerang distillation creates models", 1, 210.6),
        _caption("Figure 29: Interpolated models produced using boomerang", 1, 390.9),
        _caption("Figure 30: Boomerang distillation performs significantly", 1, 570.0),
    ]
    drs = [
        _ink(1, 39.2, 39.2, x0=108.0, x1=504.0),      # 页眉下那条通栏细线（图 28 的）
        _ink(1, 81.9, 200.6),                          # 图 28 的墨迹
        _ink(1, 253.2, 380.9),                         # 图 29 的墨迹
        _ink(1, 433.5, 560.0),                         # 图 30 的墨迹
    ]
    cands = detect_figures(blocks, {}, {1: drs}, {1: PAGE})
    assert len(cands) == 3
    y0s = [c.rect.y0 for c in cands]
    y1s = [c.rect.y1 for c in cands]
    # 三段首尾相接、互不重叠（修复前三条的 y0 都是 36.2、y1 一路涨到 563）
    assert y1s[0] < y0s[1] and y1s[1] < y0s[2]
    assert y0s[1] == pytest.approx(250.2, abs=0.5)     # 图 29 只吃自己那一段（253.2 - 3 余量）
    assert y1s[1] == pytest.approx(383.9, abs=0.5)
    assert y0s[2] == pytest.approx(430.5, abs=0.5)
    assert y1s[2] == pytest.approx(563.0, abs=0.5)
    # 页眉带里那条通栏细线不算图体（整条落在页眉带内 → 排除），所以顶边是墨迹自己的上沿
    assert cands[0].rect.y0 == pytest.approx(78.9, abs=0.5)


def test_captionless_band_is_still_cut():
    """整页没有图注时也要把墨迹带裁出来（用户："实在没有图注也行"），只是不挂图注。"""
    drs = [_ink(1, 100.0 + i * 3.0, 108.0 + i * 3.0) for i in range(24)]   # 高 60pt+ 的墨迹带
    cands = detect_figures([], {}, {1: drs}, {1: PAGE})
    assert len(cands) == 1
    assert cands[0].caption == "" and cands[0].reason == "no_caption"
    assert cands[0].rect.y0 == pytest.approx(97.0, abs=0.5)


def test_captionless_fallback_ignores_a_lone_formula():
    """独立公式只有几条大括号路径 —— 不能被当成"无图注的图"（LMF：4 张图截成 6 张）。"""
    drs = [_ink(1, 576.7 + i * 12.0, 590.0 + i * 12.0) for i in range(6)]  # 实测那页公式是 6 条
    assert detect_figures([], {}, {1: drs}, {1: PAGE}) == []


def test_captionless_fallback_is_off_when_asked():
    """导入开关「深度图片搜索」关掉（默认）时，整页无图注**不产出任何插图**。

    同一页同一份墨迹，`allow_captionless=False` 必须一张都不给 —— 这正是用户口径
    "关 = 只出图注锚定的图"。
    """
    drs = [_ink(1, 100.0 + i * 3.0, 108.0 + i * 3.0) for i in range(24)]
    assert detect_figures([], {}, {1: drs}, {1: PAGE}) != []          # 开（库层默认）
    assert detect_figures([], {}, {1: drs}, {1: PAGE},
                          allow_captionless=False) == []             # 关
    # 有图注的页不受这个开关影响：图注锚定的图照旧出
    blocks = [_caption("Figure 4: T-SNE visualizations", 1, 250.0)]
    cands = detect_figures(blocks, {}, {1: drs}, {1: PAGE}, allow_captionless=False)
    assert len(cands) == 1 and cands[0].reason == "caption_anchor"


def test_captionless_band_is_not_double_produced():
    """有图注的墨迹带不再走兜底（同一块墨迹不重复产出）。"""
    blocks = [_caption("Figure 4: T-SNE visualizations", 1, 250.0)]
    drs = [_ink(1, 100.0 + i * 3.0, 108.0 + i * 3.0) for i in range(24)]
    cands = detect_figures(blocks, {}, {1: drs}, {1: PAGE})
    assert len(cands) == 1
    assert cands[0].caption.startswith("Figure 4") and cands[0].reason == "caption_anchor"


def test_captionless_band_ignores_page_furniture():
    """页眉细线自己成不了"图"（太矮、元素太少），不会被兜底当成插图。"""
    rule = _ink(1, 39.2, 39.2, x0=108.0, x1=504.0)
    cands = detect_figures([], {}, {1: [rule]}, {1: PAGE})
    assert cands == []


def test_segment_floor_is_per_column():
    """双栏页：分段界标只认**同一栏**上一张图注，另一栏的图注不算。"""
    caps = [
        _caption("Figure 1: left", 1, 400.0, x0=50.0, x1=290.0),
        _caption("Figure 2: left", 1, 700.0, x0=50.0, x1=290.0),
        _caption("Figure 3: right", 1, 500.0, x0=320.0, x1=560.0),
    ]
    marks = [(c.page_no, _caption_anchor(c)[0]) for c in caps]
    floor = _segment_floor(marks[1][1], 1, marks, PAGE)
    assert floor == pytest.approx(412.0)      # 只被同栏的 Figure 1 挡住（400+10+2）
    assert _segment_floor(marks[2][1], 1, marks, PAGE) == pytest.approx(
        PAGE.y0 + 0.02 * PAGE.height)


def test_segment_floor_defaults_to_page_top():
    cap = _caption("Figure 7: only one on page", 3, 300.0)
    rect = _caption_anchor(cap)[0]
    assert _segment_floor(rect, 3, [(3, rect)], PAGE) == pytest.approx(
        PAGE.y0 + 0.02 * PAGE.height)


def test_caption_anchor_uses_caption_column_only():
    """**一个文本块装了两栏的行**时（转换器产出的 PDF），锚点只取图注那一栏。

    实测 efficient-low-rank-multimodal-fusion p8：图注块 bbox x 72~527（横跨整页），
    里面是「左栏的图注行」+「右栏的参考文献行」。拿整块当锚点 → 右栏墨迹也算进图体，
    裁出来是"图 + 右栏一整片正文"（用户截图）；整块文本当图注 → 图注尾巴拼一条文献。
    """
    ab = RawBlock(page_no=8, block_no=0, lines=[
        _line("Figure 4: The Impact of different rank settings on", 8, 719.9, 72.0, 290.3, 0),
        _line("Kazemzadeh, Emily Mower, Samuel Kim, Jeanette Chang", 8, 719.9, 307.3, 527.2, 1),
    ])
    rect, text = _caption_anchor(ab)
    assert (rect.x0, rect.x1) == (72.0, 290.3)          # 只留左栏那一行
    assert text == "Figure 4: The Impact of different rank settings on"
    assert "Kazemzadeh" not in text


def _span_block(page_no: int, *spans: Span) -> RawBlock:
    return RawBlock(page_no=page_no, block_no=0,
                    lines=[TextLine(spans=list(spans), page_no=page_no)])


def test_caption_anchor_splits_a_line_that_contains_two_columns():
    """PyMuPDF 会把**两栏同一基线**的文字并成一条 line（LMF p8 的图注行就是这样）。"""
    ab = _span_block(
        8,
        Span(text="Figure 4: The Impact of different rank settings on",
             x0=72.0, y0=719.9, x1=290.3, y1=730.9, font="Times", size=9.0),
        Span(text="Kazemzadeh, Emily Mower, Samuel Kim",
             x0=307.3, y0=719.9, x1=527.2, y1=730.9, font="Times", size=9.0),
    )
    rect, text = _caption_anchor(ab, page_rect=Rect(0, 0, 595.276, 841.89))
    assert (rect.x0, rect.x1) == (72.0, 290.3)
    assert text == "Figure 4: The Impact of different rank settings on"


def test_caption_anchor_keeps_a_tab_gap_inside_the_caption():
    """`Figure 1:    Overview …` 这种 tab 式宽空格**不能**当成栏缝（否则图注被截断）。"""
    ab = _span_block(
        1,
        Span(text="Figure 1:", x0=72.0, y0=100.0, x1=110.0, y1=111.0,
             font="Times", size=9.0),
        Span(text="Overview of our method", x0=130.0, y0=100.0, x1=300.0, y1=111.0,
             font="Times", size=9.0),
    )
    rect, text = _caption_anchor(ab, page_rect=Rect(0, 0, 595.276, 841.89))
    assert text == "Figure 1:Overview of our method"
    assert rect.x1 == 300.0


def test_merged_block_caption_does_not_pull_in_other_column_ink():
    ab = RawBlock(page_no=8, block_no=0, lines=[
        _line("Figure 4: The Impact of different rank settings on", 8, 719.9, 72.0, 290.3, 0),
        _line("Kazemzadeh, Emily Mower, Samuel Kim, Jeanette Chang", 8, 719.9, 307.3, 527.2, 1),
    ])
    table_cap = _caption("Table 3: Comparison of the training and testing", 8, 252.0,
                         x0=71.7, x1=290.3)
    drs = [
        _ink(8, 215.5, 245.9, x0=72.0, x1=289.4),        # 左栏表格线（在 Table 3 表注之上）
        _ink(8, 321.2, 371.7, x0=307.3, x1=527.4),       # 右栏墨迹（标题下划线之类）
        _ink(8, 566.5, 708.8, x0=72.0, x1=301.2),        # 图 4 本体（左栏）
    ]
    cands = detect_figures([ab, table_cap], {8: []}, {8: drs}, {8: PAGE})
    assert len(cands) == 1
    r = cands[0].rect
    assert (r.x0, r.x1) == (69.0, 304.2)                 # 只有左栏（72-3 ~ 301.2+3）
    assert (r.y0, r.y1) == (pytest.approx(563.5), pytest.approx(711.8))
    assert cands[0].caption.startswith("Figure 4: The Impact")
    assert "Kazemzadeh" not in cands[0].caption


def test_looks_like_caption_line_filters_body_sentences():
    assert _looks_like_caption_line("Figure 29: Interpolated models produced using boomerang")
    assert _looks_like_caption_line("Fig. 2. Per-layer loss yields stable interpolation")
    assert _looks_like_caption_line("Table 3: Results on the benchmark suite")
    assert _looks_like_caption_line("图 5：双栏排版下的插图")
    # 长引用句（编号后没有标点、且明显是句子）不能当分段的界标
    assert not _looks_like_caption_line(
        "Figure 2 shows that boomerang distillation creates models whose size and performance")
    # 短句按"短行图注"认（真有 `Figure 3 Overview of the proposed method` 这种不写标点的排法）：
    # 放宽是安全的 —— 界标只界定**墨迹**（矢量/位图）的上界，正文文字根本不参与并集，
    # 而漏掉一条真界标的代价是把上一张图吞进这一张（用户报的"一页多图会漏掉"）。
    assert _looks_like_caption_line("Table 2 summarizes our results and shows the trend")
