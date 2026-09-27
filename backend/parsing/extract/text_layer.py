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
            lines.append(TextLine(spans=current, page_no=page_no))
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
    out: list[TextLine] = []
    for ln in lines:
        out.extend(_split_line_by_gaps(ln))
    return out


def _split_line_by_gaps(line: TextLine) -> list[TextLine]:
    """同一基线上的 span 按水平间隙切分：左右两栏的内容不能算一行。

    ⚠️ 第二个判据是 **"这一段 span 以图注开头"**（2026-09-27 加的，实测事故）：
    只按间隙切是不够的 —— 栏间距 23pt 而 `SPAN_GAP_FACTOR=3.5`（约 32pt），
    于是**左栏正文与右栏图注被聚成了同一行**：
    `and fusion is performed by concatenating features from different` +
    `Figure 4: T-SNE visualizations display the prediction results`
    （line.rect 跨满 x 54→558）。图注从此不再是"独立的一行"，块级/段级那两条
    "图注自成一块"的规则都碰不到它，`Figure 4` 整张图静默消失
    （`scripts/figure_caption_audit.py` 现在会把这种缺图对账出来）。
    """
    spans = sorted(line.spans, key=lambda s: s.x0)
    size = max((s.size for s in spans), default=10.0)
    gap_limit = SPAN_GAP_FACTOR * max(size, 1.0)
    groups: list[list[Span]] = [[spans[0]]]
    for prev, cur in zip(spans, spans[1:]):
        if cur.x0 - prev.x1 > gap_limit or _is_caption_line(cur.text):
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
        same_column = not (cur.rect.x1 < prev.rect.x0 - size * 6
                           or cur.rect.x0 > prev.rect.x1 + size * 6)
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
