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
# 取编号专用（见 caption_number）：容忍 Markdown 标记与中英文写法
_CAPTION_NUM = re.compile(r"^\s*[*#>\s]*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})",
                          re.IGNORECASE)
#: 图注与图体的**最大间隔**（pt）。正文里的引用句（"…as shown in Figure 3. From Case 1, …"）
#: 也会以 `Figure N` 开头，于是上方很远的旧图会被当成它的"图体" —— 同一图号就多出一条
#: 裁到正文的区域、还把那句正文当图注（实测 DTDBD p11：假图注离图体 162pt，真图注 6.9pt）。
CAPTION_MAX_GAP = 120.0


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
            and _usable_image(i, page_rect)
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


def _usable_image(img: ImageInfo, page_rect: Rect,
                  max_area_ratio: float = 0.8) -> bool:
    """这张位图能不能当"图体"候选。

    排除两类在**扫描件/影印件**上必然出现、但几何法会误当成图体的位图：

    - **满页位图**：扫描件每页就是一张铺满整页的图，任何图注都能"落在它上方"，
      直接参与并集会裁出整页；
    - **过小位图**：水印、印章、出版社 logo（``ImageInfo.is_small`` 已经算过）。

    文本层论文不受影响（插图位图本来就远小于整页）。
    """
    if img.is_small:
        return False
    return img.rect.area <= max_area_ratio * max(1.0, page_rect.area)


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
        # 图注必须**贴着**图体（见 `CAPTION_MAX_GAP`）：正文引用句不算图注
        gap = (body.y0 - ab.rect.y1) if body.y0 >= ab.rect.y1 else (ab.rect.y0 - body.y1)
        if gap > CAPTION_MAX_GAP:
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
            and _usable_image(i, page_rect)
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
        if not _looks_like_table_caption(ab.text):
            # 正文里"Table 2 summarizes our results and …"这种**句子**不该被当成表注
            # （实测 attention p8 就多出一个假的"表 2"，知识库里跟着多一条空表）
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
            page_no=page_no,
            # ⚠️ 表体 = 表注 + **表格本体**（线框并集，没有线框就吃表注下方连续的同栏文本行）。
            # 以前这里直接写 `ab.rect.expand(4)`（= 表注那一行），于是所有表格区域都只有 ~18pt：
            # 知识库里的表格缩略图只有一行表注、放大也看不到数值（2026-09-27 用户报的）。
            rect=_table_body_rect(ab, blocks, local_h + local_v, page_rect).expand(4),
            caption_block_id=None,
            caption=ab.text.strip(),
            row_count=max(0, len(rows) - 1), col_count=max(0, len(cols) - 1),
            has_borders=has_borders,
            grid=[round(v, 1) for v in rows + cols],
        ))
    return out


#: 表注必须"像表注"：`Table 1: …` / `表 1：…` 这类**带标点**的，
#: 或足够短的行；否则正文里的句子（"Table 2 summarizes our results and …"，实测 200+ 字）
#: 会被当成表注，凭空多出一条"表"。
_TABLE_CAPTION_DELIM = re.compile(r"^\s*[:：.．—–\-|]")
_TABLE_CAPTION_SANE = re.compile(r"^\s*(?:Table|表)\s*\d{1,3}", re.IGNORECASE)


def _looks_like_table_caption(text: str) -> bool:
    t = (text or "").strip()
    if not _TABLE_CAPTION_SANE.match(t):
        return False
    rest = _TABLE_CAPTION_SANE.sub("", t, count=1)
    if _TABLE_CAPTION_DELIM.match(rest):
        return True
    return len(t) <= 60          # 短行且不写标点的排版也认；长句一律不认


def _table_body_rect(caption: RawBlock, blocks: list[RawBlock],
                     bars: list[Drawing], page_rect: Rect) -> Rect:
    """表格本体：优先**线框并集**，没有线框就吃表注下方**连续的同栏文本块**。

    两种排版都要管：attention 的 booktabs 表只有几条横线（`has_borders=False`），
    而 3360 的表有线框。都没有时退回"表注下方到第一个明显空档"的文本行 ——
    那正是表格的数值行，比"只剩表注"强得多。
    """
    cap = caption.rect
    body: Rect | None = None
    for above in (False, True):        # 先看表注**下方**（多数排版），再看上方
        picked = [d.rect for d in bars
                  if (d.rect.y0 >= cap.y1 - 2 if not above else d.rect.y1 <= cap.y0 + 2)]
        if not picked:
            continue
        x0 = min(r.x0 for r in picked)
        x1 = max(r.x1 for r in picked)
        y0 = min(r.y0 for r in picked)
        y1 = max(r.y1 for r in picked)
        body = cap.union(Rect(x0, y0, x1, y1))
        break
    if body is None:
        # 无框表：表注下方、与表注横向重叠、且**垂直连续**的块（间隔 > 28pt 视为表格结束）
        limit = min(page_rect.y1, cap.y1 + max(120.0, 0.45 * page_rect.height))
        rows = sorted([b.rect for b in blocks
                       if b.rect.y0 >= cap.y1 - 2 and b.rect.y0 <= limit
                       and b.rect.x1 > cap.x0 + 1 and b.rect.x0 < cap.x1 - 1],
                      key=lambda r: r.y0)
        body = cap
        bottom = cap.y1
        for r in rows:
            if r.y0 - bottom > 28.0:
                break
            body = body.union(r)
            bottom = max(bottom, r.y1)
    return body


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


def caption_number(text: str) -> str:
    """从图注/表注文本里取编号（``"Figure 3: ..."`` → ``"3"``；取不到返回空串）。

    扫描页按**图号**把插图挂回图注块（不能用 bbox：那些块的 rect 是等分虚拟横带），
    所以这个口径必须与阅读器一致（前端也是按图号把图挂到图注行上）。
    容忍 Markdown 标记（``**Figure 3**``）与中英文两种写法（``图 5``/``表 2``）。
    """
    m = _CAPTION_NUM.match(text or "")
    return m.group(1) if m else ""
