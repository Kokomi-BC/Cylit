"""插图切分（规划 §13.2）：图注定位 → 上下找图体 bbox（矢量+位图并集）→ 裁剪 + 余量。

同时输出「子图数」估计（按内部矢量框/位图数量），供 qa 层体检使用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.parsing.extract.image_layer import ImageInfo
from backend.parsing.extract.vector_layer import Drawing
from backend.parsing.models import RawBlock, Rect, rects_span

_CAPTION = re.compile(r"^\s*(?:Figure|Fig\.?)\s*(\d+[a-z]?)", re.IGNORECASE)
_TABLE_CAPTION = re.compile(r"^\s*(?:Table)\s*(\d+[a-z]?)", re.IGNORECASE)
_CAPTION_ANY = re.compile(r"^\s*(?:Figure|Fig\.?|Table)\s*\d+", re.IGNORECASE)


@dataclass
class FigureCandidate:
    page_no: int
    rect: Rect
    caption_block_id: str | None
    caption: str
    image_infos: list[ImageInfo]
    drawings: list[Drawing]
    subfigure_count: int = 0
    confidence: float = 0.0
    reason: str = ""


@dataclass
class TableCandidate:
    page_no: int
    rect: Rect
    caption_block_id: str | None
    caption: str
    row_count: int = 0
    col_count: int = 0
    has_borders: bool = False
    grid: list[float] | None = None


def find_caption_blocks(blocks: list[RawBlock]) -> list[tuple[RawBlock, str, str]]:
    """返回 ``[(caption_block, kind, label)]``，kind ∈ {figure, table}。"""
    out: list[tuple[RawBlock, str, str]] = []
    for ab in blocks:
        text = ab.text.strip()
        m_fig = _CAPTION.match(text)
        m_tab = _TABLE_CAPTION.match(text)
        if m_fig:
            out.append((ab, "figure", m_fig.group(1)))
        elif m_tab:
            out.append((ab, "table", m_tab.group(1)))
    return out


def _body_rect_above_caption(
    caption: RawBlock,
    page_images: list[ImageInfo],
    page_drawings: list[Drawing],
    page_rect: Rect,
    max_height_ratio: float = 0.72,
) -> tuple[Rect | None, list[ImageInfo], list[Drawing], str]:
    """在图注上方寻找图体区域。"""
    cap_rect = caption.rect
    top_limit = page_rect.y0 + 0.02 * page_rect.height
    bottom = cap_rect.y0 - 2
    max_top = bottom - 0.04 * page_rect.height

    # 候选元素：图注上方、与图注水平有重叠的位图与矢量路径
    def above(r: Rect) -> bool:
        return r.y1 <= cap_rect.y0 + 3 and r.y0 >= top_limit

    imgs = [i for i in page_images if above(i.rect)
            and _h_overlap(i.rect, cap_rect) > 0.10]
    drs = [d for d in page_drawings if above(d.rect)
           and _h_overlap(d.rect, cap_rect) > 0.10
           and d.rect.area > 25
           and not d.is_thin_bar(3.0)]

    if not imgs and not drs:
        return None, [], [], "no_body_found"

    candidates = [i.rect for i in imgs] + [d.rect for d in drs]
    union = rects_span(candidates)

    # 从图注向上"连续块"扩展：只要存在与之接近的元素就继续
    changed = True
    while changed:
        changed = False
        for r in candidates:
            if union.y0 - r.y0 <= 0.06 * page_rect.height and r.y0 < union.y0:
                union = union.union(r)
                changed = True
            if r.y1 - union.y1 <= 0.12 * page_rect.height and r.y1 > union.y1:
                union = union.union(r)
                changed = True

    # 高度限制：图体不应超过页面的 max_height_ratio
    if union.height > max_height_ratio * page_rect.height:
        union = Rect(union.x0, max(max_top, union.y1 - max_height_ratio * page_rect.height),
                     union.x1, union.y1)

    used_imgs = [i for i in imgs if union.intersects(i.rect)]
    used_drs = [d for d in drs if union.intersects(d.rect)]
    return union, used_imgs, used_drs, "caption_anchor"


def _h_overlap(a: Rect, b: Rect) -> float:
    ix = min(a.x1, b.x1) - max(a.x0, b.x0)
    if ix <= 0:
        return 0.0
    return ix / max(1.0, min(a.width, b.width))


def estimate_subfigures(drawings: list[Drawing], images: list[ImageInfo]) -> int:
    """子图数估计：内部独立矩形框与（较大）位图的数量。"""
    boxes = 0
    for d in drawings:
        if d.kind in ("rect", "quad") and d.rect.area > 400 and not d.is_thin_bar(3.0):
            boxes += 1
    big_imgs = [i for i in images if i.width_px >= 60 and i.height_px >= 60]
    est = max(len(big_imgs), boxes)
    # 单个图通常有 1~2 个外层框
    return max(1, min(est, 12))


def detect_figures(
    blocks: list[RawBlock],
    images_by_page: dict[int, list[ImageInfo]],
    drawings_by_page: dict[int, list[Drawing]],
    page_rects: dict[int, Rect],
    padding: float = 3.0,
) -> list[FigureCandidate]:
    out: list[FigureCandidate] = []
    for ab, kind, _label in find_caption_blocks(blocks):
        if kind != "figure":
            continue
        page_no = ab.page_no
        page_rect = page_rects.get(page_no) or Rect(0, 0, 612, 792)
        body, imgs, drs, reason = _body_rect_above_caption(
            ab, images_by_page.get(page_no, []), drawings_by_page.get(page_no, []), page_rect
        )
        if body is None:
            # 图注下方找图体（部分排版图注在图上方）
            body, imgs, drs, reason = _body_rect_below_caption(
                ab, images_by_page.get(page_no, []), drawings_by_page.get(page_no, []),
                page_rect, blocks
            )
        if body is None:
            continue
        rect = body.expand(padding)
        conf = 0.85 if imgs else 0.6
        out.append(FigureCandidate(
            page_no=page_no, rect=rect, caption_block_id=None,
            caption=ab.text.strip(), image_infos=imgs, drawings=drs,
            subfigure_count=estimate_subfigures(drs, imgs), confidence=conf, reason=reason,
        ))
    return out


def _body_rect_below_caption(
    caption: RawBlock, page_images: list[ImageInfo], page_drawings: list[Drawing],
    page_rect: Rect, all_blocks: list[RawBlock], max_height_ratio: float = 0.6,
) -> tuple[Rect | None, list[ImageInfo], list[Drawing], str]:
    cap_bottom = caption.rect.y1 + 2
    # 下一个文本块的顶部作为下边界
    next_top = page_rect.y1
    for other in all_blocks:
        if other is caption:
            continue
        if other.rect.y0 > cap_bottom and other.rect.y0 < next_top:
            next_top = other.rect.y0
    limit = min(next_top, cap_bottom + max_height_ratio * page_rect.height)

    imgs = [i for i in page_images
            if i.rect.y0 >= cap_bottom - 3 and i.rect.y0 < limit
            and _h_overlap(i.rect, caption.rect) > 0.10]
    drs = [d for d in page_drawings
           if d.rect.y0 >= cap_bottom - 3 and d.rect.y0 < limit
           and _h_overlap(d.rect, caption.rect) > 0.10
           and d.rect.area > 25 and not d.is_thin_bar(3.0)]
    if not imgs and not drs:
        return None, [], [], "no_body_below"
    union = rects_span([i.rect for i in imgs] + [d.rect for d in drs])
    return union, imgs, drs, "caption_anchor_below"


# --------------------------------------------------------------------------
# 表格（规划 §13.2：矢量线框 → 行列网格；无框表降级为"保留原图 + 双语文表注"）
# --------------------------------------------------------------------------


def detect_tables(
    blocks: list[RawBlock],
    drawings_by_page: dict[int, list[Drawing]],
    page_rects: dict[int, Rect],
) -> list[TableCandidate]:
    out: list[TableCandidate] = []
    for ab, kind, _label in find_caption_blocks(blocks):
        if kind != "table":
            continue
        page_no = ab.page_no
        page_rect = page_rects.get(page_no) or Rect(0, 0, 612, 792)
        drs = drawings_by_page.get(page_no, [])
        h_bars = [d for d in drs if d.is_thin_bar(3.0) and d.length > 20]
        v_bars = [d for d in drs if d.is_vertical and d.length > 20]

        # 表体：图注附近 0.6 页高内的线框集合
        window = Rect(page_rect.x0, max(page_rect.y0, ab.rect.y0 - 0.6 * page_rect.height),
                      page_rect.x1, min(page_rect.y1, ab.rect.y1 + 0.25 * page_rect.height))
        local_h = [d for d in h_bars if window.intersects(d.rect)]
        local_v = [d for d in v_bars if window.intersects(d.rect)]
        has_borders = len(local_h) >= 3 and len(local_v) >= 2
        rows = _cluster_positions([d.y for d in local_h])
        cols = _cluster_positions([d.rect.center_x for d in local_v])
        out.append(TableCandidate(
            page_no=page_no, rect=ab.rect.expand(4), caption_block_id=None,
            caption=ab.text.strip(),
            row_count=max(0, len(rows) - 1), col_count=max(0, len(cols) - 1),
            has_borders=has_borders,
            grid=[round(v, 1) for v in rows + cols],
        ))
    return out


def _cluster_positions(values: list[float], tol: float = 3.0) -> list[float]:
    if not values:
        return []
    vals = sorted(values)
    clusters = [vals[0]]
    for v in vals[1:]:
        if v - clusters[-1] > tol:
            clusters.append(v)
        else:
            clusters[-1] = (clusters[-1] + v) / 2
    return clusters


def is_caption_text(text: str) -> bool:
    return bool(_CAPTION_ANY.match(text.strip()))
