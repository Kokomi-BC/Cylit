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


# ---------------------------------------------------------------------------
# 正文行内公式的分数线 / 根号横杠 ≠ 图体墨迹（turboquant p7 图 1，2026-09-28）
#
# 实测事故：那一页图 1（上下两组直方图，全矢量绘制）上方 140pt 处是正文两条 bullet 的公式，
# 公式里的分式线（y 123.4 / 132.2 / 164.3，宽 4~39pt）也是矢量"墨迹" → 被算进"图体并集"，
# 裁图于是从正文第一条 bullet 一路裁到图（用户截图："figure 1 把上面的正文也裁进来了"）。
# 更隐蔽的一半：`_trim_top_at_heading` 的闸门是"标题之上还有没有真墨迹"—— 就是这几条分数线
# 让闸门一直关着。把它们认成**文字**之后，顶边自动收到 `2.1 EMPIRICAL VALIDATION` 之下。
# ---------------------------------------------------------------------------

def _math_bar(page_no: int, y: float, x0: float, x1: float) -> Drawing:
    """一条"细横杠"：分数线 / 根号横杠 / 下划线都长这样（stroke=True + 水平 + 零厚度）。"""
    return Drawing(kind="line", rect=Rect(x0, y, x1, y), p0=(x0, y), p1=(x1, y),
                   stroke=True, width=0.8)


def test_formula_bar_inside_a_text_line_is_not_figure_ink():
    """图体顶边不能被**正文公式的分数线**拽上去（turboquant p7 图 1：多裁了正文三条 bullet）。"""
    page = 1
    blocks = [
        # 正文：一条宽公式行，里面那根分数线就是"被误当墨迹"的东西
        RawBlock(page_no=page, block_no=0, lines=[
            _line("D_prod ≤ 3π²∥y∥²/d · 1/4^b", page, 120.0, 108.0, 504.0)]),
        # 图体上方的两个小节标题
        RawBlock(page_no=page, block_no=1, lines=[
            _line("2    EXPERIMENTS", page, 188.0, 108.0, 300.0)]),
        RawBlock(page_no=page, block_no=2, lines=[
            _line("2.1   EMPIRICAL VALIDATION", page, 263.0, 108.0, 340.0)]),
        _caption("Figure 1: Distribution of Inner Product error", page, 531.2,
                 x0=121.1, x1=475.1),
    ]
    drs = [
        _math_bar(page, 123.4, 188.5, 192.4),           # 正文公式的分数线（宽 4pt，在公式行里）
        _ink(page, 305.2, 524.1, x0=108.0, x1=504.0),   # 图体（两组直方图）
    ]
    cands = detect_figures(blocks, {}, {page: drs}, {page: PAGE})
    assert len(cands) == 1
    r = cands[0].rect
    # 顶边 = 图体墨迹上沿（305.2 - 3 余量），正文 bullet / 两个标题都在裁图外
    assert r.y0 == pytest.approx(302.2, abs=0.6)
    assert r.y1 == pytest.approx(527.1, abs=0.5)
    # 反向：这条分数线确实被判成"文字装饰"，而不是图体墨迹
    from backend.parsing.structure.figure_cut import _is_text_decoration
    assert _is_text_decoration(drs[0], blocks)
    assert not _is_text_decoration(drs[1], blocks)


def test_subfigure_label_just_above_the_ink_is_kept():
    """紧贴图体上沿的**子图标题**（文字）要留在裁图里（turboquant p7 图 1 的 `(a) TURBOQUANT_prod`）。

    只按墨迹裁会把子图标题切掉（"裁图不能缺内容"）；但再往上一行（正文段落）就必须停住 ——
    实测正文与图之间有 21.4pt 的空白，而子图标题只差 1.9pt。
    """
    page = 1
    blocks = [
        RawBlock(page_no=page, block_no=0, lines=[
            _line("2.1   EMPIRICAL VALIDATION", page, 263.0, 108.0, 340.0)]),
        RawBlock(page_no=page, block_no=1, lines=[
            _line("(a) TURBOQUANT_prod", page, 294.3, 264.5, 332.4)]),
        _caption("Figure 1: Distribution of Inner Product error", page, 531.2,
                 x0=121.1, x1=475.1),
    ]
    drs = [_ink(page, 305.2, 524.1, x0=108.0, x1=504.0)]
    cands = detect_figures(blocks, {}, {page: drs}, {page: PAGE})
    assert len(cands) == 1
    r = cands[0].rect
    assert r.y0 <= 294.3 - 3.0 + 0.5                 # 子图标题整行落在裁图内
    assert r.y0 > 272.9                              # 但正文/小节标题没有被吞进来


def test_axis_line_wider_than_the_tick_label_is_not_a_formula_bar():
    """图里的**坐标轴横线**（比刻度文字行宽得多）不能被当成行内公式线 —— 否则图会被切。"""
    from backend.parsing.structure.figure_cut import _is_text_decoration
    page = 1
    axis = _math_bar(page, 499.1, 108.0, 205.1)                 # 轴：73pt
    tick = RawBlock(page_no=page, block_no=0, lines=[
        _line("0.0", page, 500.1, 118.0, 126.1)])               # 刻度文字行：8pt
    assert not _is_text_decoration(axis, [tick])
    # 同一位置、比刻度行窄的短线（真要出现在这里也只能是文字里的分数线）才认
    assert _is_text_decoration(_math_bar(page, 500.1, 118.5, 124.0), [tick])


# ---------------------------------------------------------------------------
# KAT p3（2026-09-28）：**跨栏居中的题注**也要当界标，否则下一张图会把上一张整幅吞进来
# ---------------------------------------------------------------------------

def test_centered_caption_segments_both_columns():
    """居中/通栏的题注对**每一栏**都是分段界标（KAT p3 的 Fig. 2 题注排在页面正中）。

    实测事故：Fig. 2（整幅模型框架，上半页）与 Fig. 3（句子树，下半页左栏）上下堆叠，
    Fig. 2 的题注 x 237~358 跨过中线 297.6、Fig. 3 的题注在左栏 x 106~220。
    只按"同栏"判 → Fig. 2 的题注不算界标 → Fig. 3 的墨迹带从**页首**开始，
    把整幅 Fig. 2 吞进来（再被切成"上段/下段"，用户看到的就是"图 3 裁错了"）。
    """
    page = Rect(0, 0, 595.276, 793.701)
    mid = 595.276 / 2
    blocks = [
        # Fig. 2 的题注：排在页面正中（跨过中线），它就是 Fig. 3 的界标
        RawBlock(page_no=1, block_no=0, lines=[
            _line("Fig. 2. The proposed model framework.", 1, 300.7, 237.0, 358.2)]),
        # Fig. 3 的题注：左栏
        RawBlock(page_no=1, block_no=1, lines=[
            _line("Fig. 3. Structure of the sentence tree.", 1, 413.5, 106.0, 220.3)]),
    ]
    drs = [
        _ink(1, 67.7, 290.7, x0=128.3, x1=469.3),      # Fig. 2 的墨迹（上半页居中那块）
        _ink(1, 328.5, 403.5, x0=42.7, x1=283.7),      # Fig. 3 的墨迹（下半页**只在左栏**）
    ]
    cands = detect_figures(blocks, {}, {1: drs}, {1: page})
    assert len(cands) == 2
    f2, f3 = sorted(cands, key=lambda c: c.rect.y0)
    assert f2.caption.startswith("Fig. 2") and f3.caption.startswith("Fig. 3")
    # Fig. 3 的顶边落在 Fig. 2 题注（底边 307.2）之下 —— 不再从页首开始
    assert f3.rect.y0 == pytest.approx(325.5, abs=1.0)
    # 图 3 也不可能越过中线把右栏包进来
    assert f3.rect.x1 < mid
    assert f3.rect.y1 == pytest.approx(406.5, abs=1.0)


def test_raster_straddling_its_caption_is_not_lost():
    """整幅图是**一张位图**、题注烧在位图里面（文字层盖在图上）时，图不能被漏掉。

    实测 2409-06226v1 p25 的 Figure 8：位图 y 128.1~669.5，题注文字层 y 650.2~662.1
    （**落在位图内部**）。原来的 `above()` 要求"墨迹底边在题注上方"→ 这张位图一条都不算
    → **图 8 整张消失**（用户："漏了 figure8"）。修好之后还要把题注那一小条**切掉**，
    免得图里带一遍题注、下面又显示一遍。
    """
    from backend.parsing.extract.image_layer import ImageInfo

    page = Rect(0, 0, 612.0, 792.0)
    cap = RawBlock(page_no=1, block_no=0, lines=[
        _line("Figure 8: Comparison between reference clusters and CyLit clusters",
              1, 650.2, 132.0, 480.0)])
    raster = ImageInfo(page_no=1, xref=7, rect=Rect(72.0, 128.1, 540.0, 669.5),
                       width_px=1162, height_px=1344)
    cands = detect_figures([cap], {1: [raster]}, {}, {1: page})
    assert len(cands) == 1
    r = cands[0].rect
    assert r.y0 == pytest.approx(125.1, abs=0.5)       # 从位图顶边开始（含 3pt padding）
    assert r.y1 == pytest.approx(651.2, abs=0.5)       # 底边收到**题注顶边**之下，不把题注裁进来
    assert cands[0].caption.startswith("Figure 8")

    # 反向：**满页位图**（扫描件那种）照旧被 `_usable_image` 挡住，不能被这条判据放进来
    full = ImageInfo(page_no=1, xref=8, rect=Rect(0.0, 0.0, 612.0, 792.0),
                     width_px=1200, height_px=1600)
    assert detect_figures([cap], {1: [full]}, {}, {1: page}) == []


# ---------------------------------------------------------------------------
# sniffer p4/p8（2026-09-28）：**纯白填充的背景板**不是墨迹
# ---------------------------------------------------------------------------

def _white_box(page_no: int, r: Rect) -> Drawing:
    """排版工具垫在图下的那块白底（`type=f`、`fill=(1,1,1)`、无描边）。"""
    return Drawing(kind="rect", rect=r, p0=(r.x0, r.y0), p1=(r.x1, r.y1),
                   stroke=False, fill=True, fill_rgb=(1.0, 1.0, 1.0))


def test_white_background_box_is_not_figure_ink():
    """通栏白底不能把图撑成通栏（sniffer p4 图 3 / p8 图 7、8、9 的真机故障）。

    实测 p8：白底 [1.9, 37.4, 483.8, 266.8] 盖住图 7 + 图 9 + 图 8 的上半，于是三张图的
    并集都变成"左右两栏拼在一起"（用户截图：一张卡片里两张图并排、图里夹着正文）。
    """
    page = Rect(0.0, 0.0, 612.0, 792.0)
    blocks = [_caption("Figure 1: chart", 1, 300.0, x0=50.0, x1=290.0)]
    box = _white_box(1, Rect(1.9, 37.4, 545.0, 295.0))          # 通栏白底
    chart = _ink(1, 100.0, 250.0, x0=60.0, x1=280.0)            # 图自己的墨迹（本栏内）
    cands = detect_figures(blocks, {}, {1: [box, chart]}, {1: page})
    assert len(cands) == 1
    assert cands[0].rect.x0 == pytest.approx(57.0, abs=0.5)     # 只剩图体（-3pt 余量）
    assert cands[0].rect.x1 == pytest.approx(283.0, abs=0.5)
    assert cands[0].rect.y0 == pytest.approx(97.0, abs=0.5)
    assert cands[0].rect.y1 == pytest.approx(253.0, abs=0.5)


def test_colored_or_stroked_box_is_still_ink():
    """彩色填充、或**有描边**的框照样是墨迹 —— 白底这条判据不能误杀真图形。"""
    page = Rect(0.0, 0.0, 612.0, 792.0)
    cap = [_caption("Figure 1: chart", 1, 300.0, x0=50.0, x1=290.0)]
    colored = Drawing(kind="rect", rect=Rect(40.0, 90.0, 300.0, 260.0),
                      p0=(40.0, 90.0), p1=(300.0, 260.0),
                      stroke=False, fill=True, fill_rgb=(0.2, 0.4, 0.9))
    stroked = Drawing(kind="rect", rect=Rect(40.0, 90.0, 300.0, 260.0),
                      p0=(40.0, 90.0), p1=(300.0, 260.0),
                      stroke=True, fill=True, fill_rgb=(1.0, 1.0, 1.0))
    for box in (colored, stroked):
        cands = detect_figures(cap, {}, {1: [box]}, {1: page})
        assert len(cands) == 1 and cands[0].rect.x1 == pytest.approx(303.0, abs=0.5)


def test_a_lone_white_box_is_not_a_figure():
    """只有白底、没有任何真墨迹的页不能产出插图（白底本身看不见）。"""
    page = Rect(0.0, 0.0, 612.0, 792.0)
    cap = [_caption("Figure 1: chart", 1, 300.0, x0=50.0, x1=290.0)]
    box = _white_box(1, Rect(1.9, 37.4, 545.0, 295.0))
    assert detect_figures(cap, {}, {1: [box]}, {1: page}) == []


# ---------------------------------------------------------------------------
# 白底不再算墨迹之后必须补回来的两类内容（2026-09-28）
# ---------------------------------------------------------------------------

def test_side_labels_hugging_the_body_are_absorbed():
    """紧贴图体左右的**坐标轴标题/刻度**要收进图体（白底曾顺手把它们包进来）。

    实测 9984-boomerang p25 图 13：刻度 `0.45`（x 116.7~128.7）与图体左边界 128.4
    只差 −0.3pt、y 轴标题 `Accuracy`（x 108.6~115.9）紧挨着刻度 —— 两条都得收进来。
    """
    from backend.parsing.structure.figure_cut import _absorb_labels_beside

    blocks = [
        RawBlock(page_no=1, block_no=0, lines=[_line("0.45", 1, 132.0, 116.7, 128.7)]),
        RawBlock(page_no=1, block_no=1, lines=[_line("Accuracy", 1, 151.0, 108.6, 115.9)]),
        # 另一栏的正文（离得远，绝不能收）
        RawBlock(page_no=1, block_no=2, lines=[_line("body text", 1, 150.0, 320.0, 560.0)]),
    ]
    body = Rect(128.4, 121.0, 502.9, 243.8)
    out = _absorb_labels_beside(body, blocks)
    assert out.x0 == pytest.approx(108.6, abs=0.1)
    assert out.x1 == pytest.approx(502.9, abs=0.1)


def test_side_labels_far_away_are_not_absorbed():
    """离得远的文字（另一栏正文 / 相邻表格）不能被吃进图体。"""
    from backend.parsing.structure.figure_cut import _absorb_labels_beside

    blocks = [RawBlock(page_no=1, block_no=0,
                       lines=[_line("another column", 1, 150.0, 330.0, 560.0)])]
    body = Rect(128.4, 121.0, 300.0, 243.8)
    assert _absorb_labels_beside(body, blocks) == body


def test_label_straddling_the_body_top_is_absorbed():
    """子图标题压在图的**顶边**上时要把整行收进来（boomerang p6 图 4 的少截）。

    实测那页的 `Classification Accuracy (↑)` bbox 是 y 82.7~94.5，而图的墨迹上沿在 90.8。
    """
    from backend.parsing.structure.figure_cut import _absorb_labels_above

    blocks = [RawBlock(page_no=1, block_no=0,
                       lines=[_line("Classification Accuracy (↑)", 1, 82.7, 166.4, 238.5)])]
    # `_line` 的行高固定 10pt，这里手工把标题行做成 11.8pt 的实测高度
    blocks[0].lines[0].spans[0].y1 = 94.5
    body = Rect(147.0, 90.8, 482.0, 205.9)
    assert _absorb_labels_above(body, blocks).y0 == pytest.approx(82.7, abs=0.1)


def test_panels_are_grown_but_the_next_figure_is_not():
    """并排面板（缝 ≤ 30pt）要补进图体；**另一张图**（缝 54pt）不能补。

    实测 turboquant p7 图 1 的 `Bitwidth = 4` 那一栏离前三个面板 27.8pt（要补）；
    sniffer p8 图 7 与图 9 之间是 54pt（两张不同的图，绝不能并）。
    """
    from backend.parsing.structure.figure_cut import _grow_adjacent_panels

    def above(_r):
        return True

    union = Rect(100.0, 100.0, 200.0, 200.0)
    near = Rect(227.8, 100.0, 300.0, 200.0)      # 缝 27.8pt
    far = Rect(254.0, 100.0, 320.0, 200.0)       # 缝 54pt
    assert _grow_adjacent_panels(union, [near], above=above).x1 == pytest.approx(300.0)
    assert _grow_adjacent_panels(union, [far], above=above).x1 == pytest.approx(200.0)
    # 上下堆叠的另一张图（y 不重叠）也不补
    stacked = Rect(100.0, 300.0, 200.0, 400.0)
    assert _grow_adjacent_panels(union, [stacked], above=above) == union
