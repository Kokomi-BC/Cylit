"""文本层提取：``get_text("dict")`` → span 级 bbox 与字体（规划 §13.1）。

**为什么不用 PyMuPDF 的文本块边界**：``get_text("dict")`` 的一个 "block"
常常只覆盖一两行，甚至把一个展示公式拆成 ``h`` / ``x−Q⁻¹(Q(x))`` / ``2``
等多个块，导致段落被切碎、公式无法正确识别。因此本模块
**只用 span（字符级 bbox + 字体）作为原始证据**，自行做：

1. ``cluster_spans_into_lines``：按基线 + 字号把 span 聚成"行"；
2. ``group_lines_into_blocks``：把行按纵向连续性聚成"块"（块只作为
   公式/插图检测的几何容器，段落切分在 structure 层按行做）。

输出 ``RawBlock`` 列表，保留：span 级 bbox/font/size/flags、行的 span 集合、
页面旋转与尺寸、是否有文本层（扫描件判定）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz

from backend.core.logging import get_logger
from backend.parsing.models import RawBlock, Span, TextLine

log = get_logger(__name__)

# 同一行的基线容差（pt）
BASELINE_TOL = 2.2
# 同一块内相邻行的最大间距（× 字号）
BLOCK_GAP_FACTOR = 2.4
# 行内 span 的水平间隙上限（× 字号），超过则视为两段（如同行的左右栏）
SPAN_GAP_FACTOR = 3.5


def open_pdf(path: str | Path) -> "fitz.Document":
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"PDF 不存在：{p}")
    return fitz.open(str(p))


def _span_from_dict(d: dict) -> Span:
    x0, y0, x1, y1 = d.get("bbox", (0, 0, 0, 0))
    origin = d.get("origin", (0, 0))
    return Span(
        text=d.get("text", ""),
        x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1),
        font=d.get("font", "") or "",
        size=float(d.get("size", 0.0) or 0.0),
        flags=int(d.get("flags", 0) or 0),
        color=int(d.get("color", 0) or 0),
        origin_y=float(origin[1]) if len(origin) > 1 else float(y1),
    )


def page_spans(page: "fitz.Page") -> list[Span]:
    """提取页面全部文本 span（字符级证据）。"""
    raw = page.get_text("dict", sort=False)
    spans: list[Span] = []
    for b in raw.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for ln in b.get("lines", []):
            for s in ln.get("spans", []):
                if not s.get("text"):
                    continue
                sp = _span_from_dict(s)
                if sp.text.strip() or " " in sp.text:
                    spans.append(sp)
    return spans


def cluster_spans_into_lines(spans: list[Span], page_no: int) -> list[TextLine]:
    """按基线 + 字号把 span 聚成行。"""
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: (round(s.origin_y, 1), s.x0))
    lines: list[TextLine] = []
    current: list[Span] = []
    current_base = None
    current_size = 0.0

    def flush() -> None:
        nonlocal current, current_base
        if current:
            current.sort(key=lambda s: s.x0)
            # ⚠️ **近似重复的 span 只留一份**：matplotlib 画的图为了"加粗"会把同一段文字
            # 画两遍（位置差 <2pt，实测 9984-boomerang p5 有 19 对）—— 两份都留下，
            # 行文本就成了 `LayerLayer droppingdropping strategyInterpolation…`
            # （用户报的"图例文字重复/拼接"）。同文本 + 位置差 ≤2pt 视为同一份。
            kept: list[Span] = []
            for sp in current:
                text = sp.text.strip()
                if text and any(
                        text == k.text.strip()
                        and abs(sp.x0 - k.x0) <= 2.0
                        and abs(sp.origin_y - k.origin_y) <= 2.0
                        for k in kept):
                    continue
                kept.append(sp)
            lines.append(TextLine(spans=kept, page_no=page_no))
            current = []
        current_base = None

    for sp in ordered:
        if current_base is None:
            current = [sp]
            current_base = sp.origin_y
            current_size = sp.size
            continue
        tol = max(BASELINE_TOL, 0.22 * max(current_size, 1.0))
        size_ok = abs(sp.size - current_size) <= max(1.2, 0.28 * current_size)
        if abs(sp.origin_y - current_base) <= tol and size_ok:
            current.append(sp)
            current_size = max(current_size, sp.size)
        else:
            flush()
            current = [sp]
            current_base = sp.origin_y
            current_size = sp.size
    flush()

    # 行内按水平间隙再切分（同一基线上的左右两栏内容不能算一行）
    # ⚠️ 除了"间隙 > 阈值"，再按**页面栏间空白带**（gutter）精确切一刀：
    # 实测 3360-mmdfnd 的栏间距只有 23pt，而阈值 `SPAN_GAP_FACTOR=3.5`（字号 9.1 → 32pt）
    # 大于它 → 左右两栏被聚成同一行（该篇 84 个跨栏宽行）。而把阈值降到 2.0 虽然也能切开，
    # 却会在 attention / turboquant 上多切 6~9% 的行（表格单元格、同行标签被切碎）——
    # 所以只认"整页都空着的那条竖带"（按 **span** 算覆盖：合并过的行会盖住栏间距，span 不会），
    # 切在带上、别处不动。
    gutter = _detect_gutter_band(spans)
    out: list[TextLine] = []
    for ln in lines:
        out.extend(_split_line_by_gaps(ln, gutter=gutter))
    return out


def _detect_gutter_band(spans: list[Span], *,
                        min_width: float = 12.0,
                        margin_ratio: float = 0.10,
                        cover_ratio: float = 0.02) -> tuple[float, float] | None:
    """在页面上找**栏间空白带**（几乎没有 span 覆盖的那条竖带）。

    为什么不能用 `detect_layout` 的结果，也不能用"合并后的行"：两者都在**已经合并过的行**上
    找栏间距，而合并出来的行正好横跨栏间距（3360 p7 有 46/68 行跨栏）→ 空白带永远"被覆盖"。
    span 是原子宽度，左右两栏的 span 各自止于/起于栏边，谁都不盖住那条带 → 判定稳定。
    """
    if len(spans) < 20:
        return None
    x0 = min(s.x0 for s in spans)
    x1 = max(s.x1 for s in spans)
    width = x1 - x0
    if width < 200.0:
        return None
    n = int(width)
    counts = [0] * n
    for s in spans:
        a = max(0, int(s.x0 - x0))
        b = min(n, int(s.x1 - x0))
        for i in range(a, b):
            counts[i] += 1
    limit = max(2.0, cover_ratio * len(spans))
    lo, hi = int(margin_ratio * n), int((1.0 - margin_ratio) * n)
    best: tuple[int, int, int] | None = None      # (start, end, width)
    i = lo
    while i < hi:
        if counts[i] > limit:
            i += 1
            continue
        j = i
        while j < hi and counts[j] <= limit:
            j += 1
        if j - i >= min_width:
            left = max(counts[lo:i] or [0])
            right = max(counts[j:hi] or [0])
            # 两侧都要有"实质成栏"的内容，避免把页边空白/图表空洞当成栏间
            if left >= 0.10 * len(spans) and right >= 0.10 * len(spans):
                if best is None or (j - i) > best[2]:
                    best = (i, j, j - i)
        i = j
    if best is None:
        return None
    return (x0 + best[0], x0 + best[1])


def _split_line_by_gaps(line: TextLine,
                        gutter: tuple[float, float] | None = None) -> list[TextLine]:
    """同一基线上的 span 按水平间隙切分：左右两栏的内容不能算一行。

    两个判据：
    1. **栏间空白带**（`gutter`）：前一段止于带左侧、后一段起于带右侧 → 切（跨栏的分栏依据）；
    2. **跨度 > `SPAN_GAP_FACTOR` × 字号**，或**这一段 span 以图注开头** ——
       2026-09-27 加的后半条是实测事故：左栏正文与右栏图注被聚成同一行，
       `Figure 4` 整张图因此静默消失（`scripts/figure_caption_audit.py` 现在会把它对账出来）。
    """
    spans = sorted(line.spans, key=lambda s: s.x0)
    size = max((s.size for s in spans), default=10.0)
    gap_limit = SPAN_GAP_FACTOR * max(size, 1.0)
    groups: list[list[Span]] = [[spans[0]]]
    for prev, cur in zip(spans, spans[1:]):
        crosses_gutter = bool(gutter) and prev.x1 <= gutter[0] + 1.0 and cur.x0 >= gutter[1] - 1.0
        if cur.x0 - prev.x1 > gap_limit or _is_caption_line(cur.text) or crosses_gutter:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    if len(groups) == 1:
        return [line]
    return [TextLine(spans=g, page_no=line.page_no) for g in groups
            if "".join(s.text for s in g).strip()]


# 图注 / 表注开头（这类行必须独立成块，否则图体定位会失败）
_CAPTION_START = re.compile(
    r"^\s*(?:Figure|Fig\.?|Table|Algorithm|Listing|Equation|Eq\.?)\s*\d+",
    re.IGNORECASE,
)


def _is_caption_line(text: str) -> bool:
    return bool(_CAPTION_START.match(text))


def group_lines_into_blocks(lines: list[TextLine], page_no: int,
                            block_start: int = 0) -> list[RawBlock]:
    """把行按纵向连续性聚成块（仅作为公式/插图/表格检测的几何容器）。

    图注/表注行强制独立成块（规划 §13.2 插图切分依赖"图注定位"）。
    """
    if not lines:
        return []
    ordered = sorted(lines, key=lambda ln: (round(ln.rect.y0, 1), ln.rect.x0))
    blocks: list[RawBlock] = []
    current: list[TextLine] = [ordered[0]]

    def flush() -> None:
        nonlocal current
        if current and "".join(ln.text for ln in current).strip():
            blocks.append(RawBlock(page_no=page_no,
                                   block_no=block_start + len(blocks),
                                   lines=current, kind="text"))
        current = []

    for prev, cur in zip(ordered, ordered[1:]):
        size = max(prev.size, cur.size, 1.0)
        gap = cur.rect.y0 - prev.rect.y1
        lead = cur.rect.y0 - prev.rect.y0
        # 同一栏 = 横向**真的重叠**。以前这里是 `±6×字号` 的宽松判据（约 ±55pt），双栏页上
        # 左栏的表格行与右栏正文正好落在这个容差里 → 被并进同一个块，块文本变成
        # `'DatasetMethodAccuracy 5.4Visualization'` 这种左右两栏粘一起的句子
        # （实测 3360 p8）。同一段的相邻行必然横向重叠，所以收紧不会切碎正常段落。
        same_column = cur.rect.x1 > prev.rect.x0 + 1.0 and cur.rect.x0 < prev.rect.x1 - 1.0
        caption_break = _is_caption_line(prev.text) or _is_caption_line(cur.text)
        if same_column and not caption_break \
                and -size * 1.2 <= gap <= BLOCK_GAP_FACTOR * size \
                and lead < BLOCK_GAP_FACTOR * size * 1.4:
            current.append(cur)
        else:
            flush()
            current = [cur]
    flush()
    return blocks


def extract_page_blocks(page: "fitz.Page", page_no: int) -> list[RawBlock]:
    """提取单页的原始文本块（按自研行聚类 + 纵向连续性分块）。"""
    spans = page_spans(page)
    lines = cluster_spans_into_lines(spans, page_no)
    return group_lines_into_blocks(lines, page_no)


def extract_text_blocks(doc: "fitz.Document", page_numbers: list[int] | None = None
                        ) -> dict[int, list[RawBlock]]:
    """按页提取文本块；返回 ``{page_no(1-based): [RawBlock]}``。"""
    out: dict[int, list[RawBlock]] = {}
    nums = page_numbers or list(range(1, doc.page_count + 1))
    for page_no in nums:
        if page_no < 1 or page_no > doc.page_count:
            continue
        out[page_no] = extract_page_blocks(doc[page_no - 1], page_no)
    return out


def page_has_text_layer(page: "fitz.Page", min_chars: int = 20) -> bool:
    text = page.get_text("text") or ""
    return len(text.strip()) >= min_chars
