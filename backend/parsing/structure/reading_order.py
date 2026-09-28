"""阅读顺序与栏结构检测（规划 §13.2 阅读顺序、风险 R3）。

算法（对应规划 §13.2 四条）：
1. 用「行覆盖区间合并」在页面中部寻找栏间隙（gutter），要求：
   - 间隙宽度 ≥ ``min_gutter_ratio`` × 页宽（默认 0.03，约 18pt）
   - 间隙两侧都有足够多的行文本（否则是缩进造成的假间隙）
   - 跨越间隙的"宽元素"占比低于阈值（否则是单栏带通栏标题）
2. 栏内按 y 排序；同栏内按行距聚类成块（在 structure.paragraph 里做）。
3. 跨栏元素（宽表格/大图/通栏标题）特判：宽度 > 0.62×文本区宽 → 当作跨栏块，
   顺序上置于其覆盖行区间的顶部之前，避免被拆成左右两半。
4. 与 ``order_global`` 自检：返回每页栏数、gutter 位置、交叉行数量，供 QA 输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.parsing.models import RawBlock, Rect, TextLine

# 跨栏判定：宽度超过文本区宽度的比例
CROSS_COLUMN_WIDTH_RATIO = 0.62
# 栏间隙最小宽度（相对页宽）
MIN_GUTTER_RATIO = 0.028
MIN_GUTTER_PT = 10.0


@dataclass
class ColumnLayout:
    """单页栏结构。"""

    page_no: int
    page_width: float
    columns: int = 1
    gutter: tuple[float, float] | None = None   # (x_left_end, x_right_start)
    text_left: float = 0.0
    text_right: float = 0.0
    cross_rows: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def gutter_x(self) -> float | None:
        if not self.gutter:
            return None
        return (self.gutter[0] + self.gutter[1]) / 2

    def column_of(self, rect: Rect, width_ratio: float = CROSS_COLUMN_WIDTH_RATIO) -> int:
        """判定一个块的栏号：0=左栏/单栏，1=右栏，-1=跨栏。"""
        if self.columns == 1 or not self.gutter:
            return 0
        text_width = max(1.0, self.text_right - self.text_left)
        if rect.width >= width_ratio * text_width:
            return -1
        g0, g1 = self.gutter
        cx = rect.center_x
        if cx <= g0:
            return 0
        if cx >= g1:
            return 1
        # 跨越间隙但不够宽：按重叠更多的侧归类
        left_overlap = max(0.0, min(rect.x1, g0) - rect.x0)
        right_overlap = max(0.0, rect.x1 - max(rect.x0, g1))
        return 0 if left_overlap >= right_overlap else 1

    def sort_key(self, rect: Rect, col: int) -> tuple[int, float, float]:
        if col == -1:
            # 跨栏元素：排在其覆盖区间顶部（用较小的有效 y），保证不被拆散
            return (0, rect.y0 - 0.01, rect.x0)
        return (1, rect.y0, col)


def _merge_intervals(intervals: list[tuple[float, float]], gap: float = 2.0
                     ) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ivs = sorted(intervals)
    out = [list(ivs[0])]
    for a, b in ivs[1:]:
        if a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


#: "左右两栏被并成**同一条 line**"时，中间那道缝的最小宽度（pt）。
#: 与 `extract.text_layer.COLUMN_SPLIT_GAP` 同源：词间空格只有 3~5pt，栏缝是 12~27pt。
MERGE_GAP_PT = 12.0


def _line_segments(lines: list[TextLine], mid_x: float) -> list[tuple[float, float]]:
    """把每条行转成 **1~2 段 x 区间**：跨过中线的内部大缝要挖掉。

    ⚠️ 实测事故（sniffer p8，2026-09-28 用户截图）：那一页有一条 line 是**左右两栏被并在一起**
    的（`News400 and TamperedNews [32]. In these datasets, img` + `trieval methods. …`，
    缝 27pt、正好跨过文本区中线 297.6）。它的 `rect` 横跨整页 → 把整页唯一的栏缝
    （295.8~306.0）盖住 → `counts[i] == 0` 永不成立 → **整页被判成单栏**，
    接着段落把两栏逐行交叉并块、图注块横跨整页、图 7/8/9 的墨迹并集变成通栏
    （用户看到的就是"一张卡片里并排两张图、图里夹着另一栏的正文"）。

    这样的行**本来就不属于同一栏**，让它去给"栏缝在哪"投票只会指错方向：
    这里只把它的两段分别计入覆盖，**不改行本身**（行的切分在 `text_layer` 里做，
    动它会牵到公式/表格的成块）。
    """
    out: list[tuple[float, float]] = []
    for ln in lines:
        r = ln.rect
        if r.width <= 0:
            continue
        spans = sorted(ln.spans, key=lambda s: s.x0)
        best: tuple[float, float, float] | None = None      # (缝宽, 缝左, 缝右)
        for prev, cur in zip(spans, spans[1:]):
            gap = cur.x0 - prev.x1
            if gap >= MERGE_GAP_PT and prev.x1 <= mid_x <= cur.x0:
                if best is None or gap > best[0]:
                    best = (gap, prev.x1, cur.x0)
        if best is None:
            out.append((r.x0, r.x1))
        else:
            out.append((r.x0, best[1]))
            out.append((best[2], r.x1))
    return out


def _line_span_coverage(lines: list[TextLine], page_width: float, bands: int = 40
                        ) -> list[float]:
    """x 方向的"行覆盖计数"直方图（用于找 gutter）。"""
    if page_width <= 0:
        return []
    mid_x = (min(ln.rect.x0 for ln in lines) + max(ln.rect.x1 for ln in lines)) / 2 if lines else 0.0
    counts = [0] * bands
    for a, b in _line_segments(lines, mid_x):
        if b <= a:
            continue
        i0 = max(0, int(a / page_width * bands))
        i1 = min(bands - 1, int(b / page_width * bands))
        for i in range(i0, i1 + 1):
            counts[i] += 1
    return counts


def detect_layout(lines: list[TextLine], page_no: int, page_width: float,
                  page_height: float, min_rows_per_column: int = 8) -> ColumnLayout:
    """检测单页栏结构。"""
    body = [ln for ln in lines if ln.spans and ln.text.strip()]
    layout = ColumnLayout(page_no=page_no, page_width=page_width)
    if not body:
        return layout

    xs = [ln.rect.x0 for ln in body]
    xe = [ln.rect.x1 for ln in body]
    layout.text_left = min(xs)
    layout.text_right = max(xe)
    text_width = layout.text_right - layout.text_left
    if text_width <= 0:
        return layout

    # 垂直覆盖：用于排除页眉页脚（只取页面主体 12%~88% 区域参与栏检测）
    y_top = min(ln.rect.y0 for ln in body)
    y_bot = max(ln.rect.y1 for ln in body)
    body_lines = [ln for ln in body
                  if y_top + 0.10 * (y_bot - y_top) <= ln.rect.center_y
                  <= y_top + 0.95 * (y_bot - y_top)]

    bands = 60
    counts = _line_span_coverage(body_lines, page_width, bands=bands)
    if not counts:
        return layout

    band_w = page_width / bands
    # 只在页面水平中部 30%~70% 区间找间隙（双栏论文的典型 gutter 位置）
    lo = int(bands * 0.28)
    hi = int(bands * 0.72)
    min_gap_bands = max(1, int(max(MIN_GUTTER_PT, MIN_GUTTER_RATIO * page_width) / band_w))

    best: tuple[int, int, float] | None = None  # (start_band, end_band, width)
    i = lo
    while i < hi:
        if counts[i] == 0:
            j = i
            while j < hi and counts[j] == 0:
                j += 1
            width_bands = j - i
            if width_bands >= min_gap_bands:
                if best is None or width_bands > best[1] - best[0]:
                    best = (i, j, width_bands)
            i = j
        else:
            i += 1

    if best is None:
        return layout

    g0 = best[0] * band_w
    g1 = best[1] * band_w

    # 统计跨间隙的行数（真正的双栏不应有大量跨间隙行）
    cross = 0
    left_rows = right_rows = 0
    for ln in body_lines:
        r = ln.rect
        if r.x0 < g0 and r.x1 > g1:
            cross += 1
        elif r.center_x < g0:
            left_rows += 1
        elif r.center_x > g1:
            right_rows += 1
    total = max(1, len(body_lines))
    if left_rows < min_rows_per_column or right_rows < min_rows_per_column:
        layout.notes.append(f"栏内行数不足(left={left_rows},right={right_rows})，按单栏处理")
        return layout
    if cross / total > 0.18:
        layout.notes.append(f"跨间隙行占比 {cross / total:.2f} 过高，按单栏处理")
        return layout

    layout.columns = 2
    layout.gutter = (g0, g1)
    layout.cross_rows = cross
    return layout


def order_page_lines(blocks: list[RawBlock], layout: ColumnLayout
                     ) -> list[tuple[TextLine, int]]:
    """按阅读顺序排列一页的**行**。

    采用「**块优先、行其次**」的两级排序：先按 PyMuPDF 文本块的位置排块，
    再在块内按基线排 `PyMuPDF` 的原始行顺序。原因：如果只做全局按 y 排序，
    多行展示公式的各行会互相穿插（分数、根号、上下标在不同 y 上），
    段落与公式都会被切碎。

    仅当块内部明显横跨两栏时才拆块（返回多段）。
    """
    if layout.columns == 1 or not layout.gutter:
        ordered_blocks = sorted(
            blocks,
            key=lambda ab: (round(ab.rect.y0, 1), round(ab.rect.x0, 1)),
        )
        out: list[tuple[TextLine, int]] = []
        for ab in ordered_blocks:
            for ln in ab.lines:
                if ln.text.strip():
                    out.append((ln, 0))
        return out

    # 双栏：先按栏分组（块的左右归属），再在栏内按 (y, x) 排块
    g0, g1 = layout.gutter
    text_width = max(1.0, layout.text_right - layout.text_left)
    left: list[RawBlock] = []
    right: list[RawBlock] = []
    span: list[RawBlock] = []
    for ab in blocks:
        r = ab.rect
        if r.width >= CROSS_COLUMN_WIDTH_RATIO * text_width:
            span.append(ab)
        elif r.center_x < g0:
            left.append(ab)
        elif r.center_x > g1:
            right.append(ab)
        else:
            (left if r.center_x < (g0 + g1) / 2 else right).append(ab)

    def key(ab: RawBlock):
        return (round(ab.rect.y0, 1), round(ab.rect.x0, 1))

    left.sort(key=key)
    right.sort(key=key)
    span.sort(key=key)

    merged: list[tuple[RawBlock, int]] = [(ab, 0) for ab in left]
    for ab in span:
        pos = len(merged)
        for i, (other, _c) in enumerate(merged):
            if other.rect.y0 > ab.rect.y1 - 1:
                pos = i
                break
        merged.insert(pos, (ab, 0))
    merged.extend((ab, 1) for ab in right)

    out = []
    for ab, col in merged:
        for ln in ab.lines:
            if ln.text.strip():
                out.append((ln, col))
    return out


def order_page_blocks(blocks: list[RawBlock], layout: ColumnLayout
                      ) -> list[tuple[RawBlock, int]]:
    """对一页的块做阅读顺序排序，返回 ``[(block, col)]``。

    先按栏切分（PyMuPDF 的文本块有可能把左右栏的行并进同一个块，必须拆开），
    再排序：跨栏元素排在覆盖区间的顶部，栏内元素按 (col, y, x) 排。
    """
    pieces = split_blocks_by_column(blocks, layout)
    decorated: list[tuple[tuple, RawBlock, int]] = []
    for ab, col in pieces:
        decorated.append((layout.sort_key(ab.rect, col), ab, col))
    decorated.sort(key=lambda t: t[0])
    return [(ab, col) for _, ab, col in decorated]


def split_blocks_by_column(blocks: list[RawBlock], layout: ColumnLayout
                           ) -> list[tuple[RawBlock, int]]:
    """把跨栏的原始文本块按栏切开（保证段落不被左右栏污染）。"""
    out: list[tuple[RawBlock, int]] = []
    if layout.columns == 1 or not layout.gutter:
        return [(b, 0) for b in blocks]

    g0, g1 = layout.gutter
    for ab in blocks:
        rect = ab.rect
        if rect.width >= CROSS_COLUMN_WIDTH_RATIO * max(1.0, layout.text_right - layout.text_left):
            out.append((ab, -1))
            continue
        # 行级分栏
        buckets: dict[int, list[TextLine]] = {-1: [], 0: [], 1: []}
        for ln in ab.lines:
            r = ln.rect
            if r.x0 < g0 - 1 and r.x1 > g1 + 1:
                buckets[-1].append(ln)
            elif r.center_x < g0:
                buckets[0].append(ln)
            elif r.center_x > g1:
                buckets[1].append(ln)
            else:  # 落在间隙里：并入最近一侧
                buckets[0 if r.center_x < layout.gutter_x else 1].append(ln)

        non_empty = [c for c in (-1, 0, 1) if buckets[c]]
        if len(non_empty) <= 1:
            col = non_empty[0] if non_empty else layout.column_of(rect)
            out.append((ab, col))
            continue
        for col in non_empty:
            lines = buckets[col]
            nb = RawBlock(page_no=ab.page_no, block_no=ab.block_no,
                          lines=lines, kind=ab.kind)
            nb.split_index = len([1 for o, _ in out if o.page_no == ab.page_no
                                  and o.block_no == ab.block_no])
            out.append((nb, col))
    return out


def self_check_order(ordered: list[tuple[RawBlock, int]], layout: ColumnLayout
                     ) -> list[str]:
    """order_global 自检（规划 §13.2 第 4 条）。"""
    issues: list[str] = []
    if layout.columns == 2:
        seen_right_at_y = None
        for ab, col in ordered:
            r = ab.rect
            if col == 1:
                seen_right_at_y = r.y0
            elif col == 0 and seen_right_at_y is not None and r.y0 < seen_right_at_y - 60:
                issues.append(
                    f"左栏块 {ab.block_no} (y={r.y0:.0f}) 出现在右栏块 (y={seen_right_at_y:.0f}) 之后"
                )
                break
    # y 单调性检查（允许跨栏元素造成的轻微回退）
    prev_y = -1e9
    regressions = 0
    for ab, col in ordered:
        y = ab.rect.y0
        if y < prev_y - 40:
            regressions += 1
        prev_y = max(prev_y, y)
    if regressions > max(2, len(ordered) // 10):
        issues.append(f"阅读顺序 y 回退次数过多：{regressions}")
    return issues

