"""插图区域：**按图注/标题锚定**收紧解析阶段给出的插图 bbox，并决定裁图的真实边界。

为什么需要它
------------
解析阶段检测插图区域时会"合并"：TurboQuant p8 的 Figure 3 记录 bbox 从 y=95.8 一直到
y=535.0，把**上面的 Figure 2 和中间的小标题**都吃了进来，裁出来就是"同一张图出现两次、
图里夹着正文"，完全不像论文里的插图。

规则（论文自己的排法）
----------------------
- 图注在图的**下方**（Figure）→ 下边界 = 图注顶边；上边界取以下三者的最大值：
  图块自身 y0、同页上一张图的已用下边界、图注上方最近的**标题/图注**的底边。
  中间夹着的标题（如 ``2.2 NEEDLE-IN-A-HAYSTACK``）因此被排除在图外。
- 题注在表的**上方**（Table）→ 上边界 = 题注底边；下边界取表块 y1 与下方最近标题/图注的顶边。
- 收紧后若高度过小（< 24pt），退回原始 bbox（宁可多裁一点也不要裁空）。

在此之上还有三道"多截/少截"的闸门（用户报过"一张图被切掉标题或图注"）：

1. **贴边闸门**（``_snap_edges``）：排版软件给文字块的 bbox 比字形墨迹宽出 1~2pt，
   解析阶段给的插图 bbox 又是"位图 + 矢量"的并集 —— 两个边界差一点点的结果，就是
   图注顶部被切掉一条、或者图体底部一小条留在裁图外。这一步把四条边统一贴到相邻文字的
   边上（既不让边界从字里穿过，也不留会被切掉的白缝）。
2. **墨迹裁白闸门**（``render_crop_png(ink_trim=True)``，见 ``parsing.extract.render``）：
   按 bbox 裁出来四周常有一圈白边（attention p3 右侧近 40% 是空白），先低倍渲染量出真实
   墨迹范围，再按墨迹范围出图 —— 白边被裁掉，图在栏里也更大更清楚。
3. **一份不够就切两份**（``_parts_of`` + 跨页续图）：一张图覆盖了别的图注（TurboQuant p8 的
   Figure 3 框住了 Figure 2 的图注）或跨到页脚（图体延续到下一页）时，硬裁成一张必然
   既多截又少截。此时把它拆成**同页两段**或**跨页两段**，每段各自贴边收紧；两段共享同一个
   region 下标，用 ``part`` / ``parts`` 区分，接口 URL 与落盘缓存都按 part 命名。

裁图按需渲染（600dpi，只渲染 clip 区域）并落盘缓存；缓存文件名带 bbox 哈希，
重解析后 bbox 变了会重新裁。
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

from backend.core.errors import NotFoundError
from backend.core.logging import get_logger
from backend.parsing.models import Rect

log = get_logger(__name__)

NUMBER_RE = re.compile(r"(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})", re.IGNORECASE)
STRUCT_TYPES = ("caption", "heading")     # 结构分隔元素：图注与标题
SEP_PAD = 6.0        # 分隔元素（标题/图注）要往外让开一点：字形墨迹会超出文本框
TEXT_PAD = 1.2       # 边界与文字的"安全距离"：小于它就会被出图余量切到字形
TOL = 1.0            # 边界"正好贴着文字"的容差
MIN_VISIBLE = 1.5    # 区域至少要有这么宽，否则放弃贴边
SNAP_MAX = 16.0      # 一次最多把边界挪多少 pt（文字带高度量级）——防止"一挪就挪掉半个图"
MIN_HEIGHT = 24.0    # 区域至少要有这么高，否则放弃贴边
EDGE_MARGIN = 0.02   # 图体贴到页脚/页首这个比例以内 → 认为它跨到了下一页
CROP_DPI = 600
INK_PAD = 1.5        # 墨迹裁白后四周留的呼吸位（pt）

_lock = threading.Lock()
# {paper_id: (ir_mtime, {page: [regions]})}
_cache: dict[str, tuple[float, dict[int, list[dict]]]] = {}


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bbox(row: dict) -> list[float]:
    return [_num(row.get("x0")), _num(row.get("y0")),
            _num(row.get("x1")), _num(row.get("y1"))]


def _number_of(text: str) -> str:
    m = NUMBER_RE.search(text or "")
    return m.group(1) if m else ""


def _tighten(bbox: list[float], caption: dict | None, structs: list[dict],
             page_bottom: float, *, below: bool) -> list[float]:
    """把插图 bbox 收到"图注 + 结构分隔"之间。"""
    x0, y0, x1, y1 = bbox
    if caption is None:
        return bbox
    cy0, cy1 = _num(caption.get("y0")), _num(caption.get("y1"))

    if below:      # 图注在图下方：下边界 = 图注顶边；上边界排除夹在中间的标题/图注
        boundary = cy0 if cy0 - y0 >= MIN_HEIGHT else y1
        seps = [_num(s.get("y1")) + SEP_PAD for s in structs
                if _num(s.get("y1")) <= cy0 - TOL]
        top = max([y0] + seps[-1:])
        if boundary - top < MIN_HEIGHT:
            return bbox
        return [x0, top, x1, boundary]

    # 题注在表上方：上边界 = 题注底边；下边界 = 下方最近的标题/图注顶边
    boundary = cy1 if y1 - cy1 >= MIN_HEIGHT else y0
    top = max(y0, boundary)
    seps = [_num(s.get("y0")) - SEP_PAD for s in structs
            if _num(s.get("y0")) >= cy1 + TOL]
    bottom = min([y1] + seps[:1])
    if bottom - top < MIN_HEIGHT:
        return bbox
    return [x0, top, x1, bottom]


def _text_rects(blocks_by_page: dict[int, list[dict]], page_no: int) -> list[Rect]:
    """该页**文字层**块的矩形（插图区域不该把文字圈进来）。"""
    out: list[Rect] = []
    for b in blocks_by_page.get(int(page_no), []):
        if (_num(b.get("char_count")) or 0) <= 0 and not str(b.get("text") or "").strip():
            continue
        out.append(Rect(_num(b.get("x0")), _num(b.get("y0")),
                        _num(b.get("x1")), _num(b.get("y1"))))
    return out


def _rect_of(row: dict) -> Rect:
    return Rect(_num(row.get("x0")), _num(row.get("y0")),
                _num(row.get("x1")), _num(row.get("y1")))


def _snap_y(edge: float, blocks: list[Rect], *, top: bool,
            lo: float, hi: float) -> float:
    """把一条水平边界推到相邻文字的边上（不动就返回原值）。

    判据只有一条：**边界离某个文字块太近**
    （离文字块还不到 ``TOL`` 就当作已经贴住；到文字块上/下沿不足 ``TEXT_PAD``
    则说明出图余量会切到字形）。此时统一把边界推到文字的边上 —— 问题一次消掉。

    实测两个正例：attention p4 的图题（文字块底边 81.2，区域顶边 79.7 → 推到 82.4）、
    TurboQuant p8 图 2 的图注（文字块顶边 231.0，区域底边 229.8 → 推到 229.8 之上）。
    再远一点就不该动边界：否则远处的正文也会被算进来（实测 p3 的图体 bbox 会被平白推掉一大截）。
    """
    for t in blocks:
        if top:
            if edge >= t.y1 + TOL or edge <= t.y1 - TEXT_PAD:
                continue
            new = t.y1 + TEXT_PAD
            if new <= lo + TOL or new - edge > SNAP_MAX or (hi - new) < MIN_HEIGHT:
                continue
            edge = new
        else:
            if edge <= t.y0 - TOL or edge >= t.y0 + TEXT_PAD:
                continue
            new = t.y0 - TEXT_PAD
            if new >= hi - TOL or edge - new > SNAP_MAX or (new - lo) < MIN_HEIGHT:
                continue
            edge = new
    return edge


def _snap_x(edge: float, blocks: list[Rect], *, left: bool, lo: float, hi: float) -> float:
    """把一条垂直边界推到相邻文字的边上（与 :func:`_snap_y` 同构）。"""
    for t in blocks:
        if left:
            if edge >= t.x1 + TOL or edge <= t.x1 - TEXT_PAD:
                continue
            new = t.x1 + TEXT_PAD
            if new <= lo + TOL or new - edge > SNAP_MAX or (hi - new) < MIN_VISIBLE:
                continue
            edge = new
        else:
            if edge <= t.x0 - TOL or edge >= t.x0 + TEXT_PAD:
                continue
            new = t.x0 - TEXT_PAD
            if new >= hi - TOL or edge - new > SNAP_MAX or (new - lo) < MIN_VISIBLE:
                continue
            edge = new
    return edge


def _snap_edges(rect: list[float], texts: list[Rect], page_rect: Rect) -> list[float]:
    """把区域的四条边**贴到相邻文字的边上**：既不从文字中间穿过，也不留一条会被切掉的白缝。

    为什么必须有这一步：排版软件给文字块的 bbox 比字形墨迹宽出 1~2pt，解析阶段的插图
    bbox 又是"位图 + 矢量"的并集，两个边界几乎不可能正好落在同一处。差一点点的结果就是
    **图注顶部被切掉一条**（多截）或**图体底部一小条留在裁图外**（少截）—— 用户报的
    "多截和少截"就是它。

    迭代四轮：一次贴边可能让另一条边落进新的危险带（尤其是上下都紧邻文字时）。
    """
    x0, y0, x1, y1 = rect
    order = sorted(texts, key=lambda t: t.y0)
    for _ in range(4):
        before = (x0, y0, x1, y1)
        col = [t for t in order if t.x1 > x0 + TOL and t.x0 < x1 - TOL]
        y0 = _snap_y(y0, col, top=True, lo=y0, hi=y1)
        y1 = _snap_y(y1, col, top=False, lo=y0, hi=y1)
        row = [t for t in order if t.y1 > y0 + TOL and t.y0 < y1 - TOL]
        x0 = _snap_x(x0, row, left=True, lo=x0, hi=x1)
        x1 = _snap_x(x1, row, left=False, lo=x0, hi=x1)
        if (x0, y0, x1, y1) == before:
            break
    if (y1 - y0) < MIN_HEIGHT or (x1 - x0) < MIN_VISIBLE:
        return rect                                       # 收得太过 → 保留原样，宁可多裁
    return [max(x0, page_rect.x0), max(y0, page_rect.y0),
            min(x1, page_rect.x1), min(y1, page_rect.y1)]


def _snap_x(edge: float, blocks: list[Rect], *, left: bool, lo: float, hi: float) -> float:
    """把一条垂直边界贴到相邻文字的边上（与 :func:`_snap_y` 同构）。"""
    for t in blocks:
        if left:
            if edge < t.x0 - TEXT_PAD or edge > t.x1 + TEXT_PAD:
                continue
            new = t.x1 + TEXT_PAD
            if new <= lo + TOL or new - edge > SNAP_MAX or (hi - new) < MIN_VISIBLE:
                continue
            edge = new
        else:
            if edge < t.x0 - TEXT_PAD or edge > t.x1 + TEXT_PAD:
                continue
            new = t.x0 - TEXT_PAD
            if new >= hi - TOL or edge - new > SNAP_MAX or (new - lo) < MIN_VISIBLE:
                continue
            edge = new
    return edge


def _normalise(rect: list[float], page_rect: Rect) -> list[float]:
    """保证矩形有效且落在页面内。"""
    x0, y0, x1, y1 = rect
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return [max(x0, page_rect.x0), max(y0, page_rect.y0),
            min(x1, page_rect.x1), min(y1, page_rect.y1)]


def _parts_of(rect: list[float], inner: list[dict], page_rect: Rect
              ) -> list[tuple[list[float], dict | None]]:
    """把区域切成若干份：内部夹着别的图注、或图体跨到页脚时，一份裁不干净。

    ``inner`` 是**严格落在区域内**的其他图注（y0/y1 都在区域内、且高度不大）。
    切分点取图注的上下边（各让开 ``TEXT_PAD``），保证两段都不含图注文字：
    图注本身在正文里有自己的行，重复裁进图里就会"同一句话出现两次"。
    """
    x0, y0, x1, y1 = rect
    cuts: list[tuple[float, float]] = []
    for cap in inner:
        cy0, cy1 = _num(cap.get("y0")), _num(cap.get("y1"))
        if not (y0 + MIN_HEIGHT <= cy0 and cy1 <= y1 - MIN_HEIGHT):
            continue
        if cy1 - cy0 > 0.2 * max(1.0, y1 - y0):        # 太"厚"的不是图注，是正文
            continue
        cuts.append((cy0, cy1))
    cuts = sorted(cuts)

    pieces: list[tuple[list[float], dict | None]] = []
    cursor = y0
    for cy0, cy1 in cuts:
        if cy0 - cursor >= MIN_HEIGHT:
            pieces.append(([x0, cursor, x1, cy0 - TEXT_PAD], None))
        cursor = max(cursor, cy1 + TEXT_PAD)
    if y1 - cursor >= MIN_HEIGHT:
        pieces.append(([x0, cursor, x1, y1], None))

    # 图体压到页脚：最后一段其实是"下一页的续图"，这里先给出跨页标记
    if len(pieces) >= 1 and y1 >= page_rect.y1 - EDGE_MARGIN * page_rect.height:
        pieces[-1] = (pieces[-1][0], {"continues": True})
    if not pieces:                                      # 全被切没了 → 不切
        return [([x0, y0, x1, y1], None)]
    if len(pieces) == 1 and not pieces[0][1]:
        return [([x0, y0, x1, y1], None)]
    return pieces


def _looks_like_continuation(fig: dict, page_h: float) -> bool:
    """上一页那张图的 bbox 是否"被页脚切开"（图体一直画到页面下沿）。"""
    bottom = _num(fig.get("y1"))
    return bool(page_h) and bottom >= page_h - EDGE_MARGIN * page_h


def _continuation_page(paper_id: str, src_page: int, src: dict,
                       pages: dict[int, dict], ir) -> int | None:
    """图体跨页时，判断"下一页页首是否接着这张图"，是就返回下一页页码。

    判据很保守（宁可不切也不要乱切）：源图必须画到页脚，下一页页首必须有内容
    （文字块或矢量图形）与源图**横向重叠**。
    """
    from backend.parsing.extract.vector_layer import extract_drawings
    from backend.services.paper import paper_service

    nxt = src_page + 1
    page = pages.get(nxt)
    if page is None:
        return None
    page_h = _num(page.get("height")) or 792.0
    band = EDGE_MARGIN * page_h
    x0, x1 = _num(src.get("x0")), _num(src.get("x1"))

    def in_band(r: Rect) -> bool:
        return r.y0 <= band and not (r.x1 <= x0 or r.x0 >= x1)

    for b in ir.blocks():
        if int(b["page_no"]) != nxt:
            continue
        if (_num(b.get("char_count")) or 0) <= 0 and not str(b.get("text") or "").strip():
            continue
        if in_band(_rect_of(b)):
            return nxt
    try:
        import pymupdf as fitz

        with fitz.open(str(paper_service.resolve_pdf(paper_id))) as doc:
            for d in extract_drawings(doc[nxt - 1]):
                if d.rect.area > 25 and in_band(d.rect):
                    return nxt
    except Exception as exc:                            # noqa: BLE001
        log.warning("跨页插图探测失败（p%d）：%s", nxt, exc)
    return None


def _merge(regions: list[dict], extra: dict) -> list[dict]:
    """把续图并进同页列表，并按文档顺序（y0, y1）重排，保证下标稳定。"""
    out = regions + [extra]
    out.sort(key=lambda r: (_num(r.get("y0")), _num(r.get("y1"))))
    return out


def _compute_all(paper_id: str) -> dict[int, list[dict]]:
    """一次读库算完整篇（按页分组，顺序 = 文档顺序）。"""
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return {}
    pages = {int(p["page_no"]): p for p in ir.pages()}
    blocks_by_page: dict[int, list[dict]] = {}
    for b in ir.blocks():
        blocks_by_page.setdefault(int(b["page_no"]), []).append(b)
    figures_by_page: dict[int, list[dict]] = {}
    for f in ir.figures():
        figures_by_page.setdefault(int(f["page_number"]), []).append({**f, "_kind": "figure"})
    tables_by_page: dict[int, list[dict]] = {}
    for t in ir.tables():
        tables_by_page.setdefault(int(t["page_number"]), []).append({**t, "_kind": "table"})

    text_cache: dict[int, list[Rect]] = {}
    out: dict[int, list[dict]] = {}

    def page_rect(page_no: int) -> Rect:
        p = pages.get(page_no) or {}
        return Rect(0.0, 0.0, _num(p.get("width")) or 612.0,
                    _num(p.get("height")) or 792.0)

    def entry(page_no: int, kind: str, rect: list[float], raw: list[float],
              caption: str, number: str, caption_block_id: str | None,
              part: dict | None = None) -> dict:
        tight = [round(v, 2) for v in _normalise(rect, page_rect(page_no))]
        row = {
            "page": page_no, "kind": kind,
            "number": number, "caption": caption, "caption_block_id": caption_block_id,
            "bbox": tight, "raw_bbox": [round(v, 2) for v in raw],
            "hash": hashlib.sha256(
                repr([round(v, 1) for v in tight]).encode()).hexdigest()[:8],
        }
        if part:
            row.update(part)
        return row

    for page_no, page in pages.items():
        blocks = blocks_by_page.get(page_no, [])
        if not blocks:
            continue
        rect = page_rect(page_no)
        by_id = {b["id"]: b for b in blocks}
        structs = sorted([b for b in blocks
                          if (b.get("type") or "") in STRUCT_TYPES],
                         key=lambda b: _num(b.get("y0")))
        texts = text_cache.get(page_no)
        if texts is None:
            texts = text_cache[page_no] = _text_rects(blocks_by_page, page_no)
        rows = sorted(figures_by_page.get(page_no, []) + tables_by_page.get(page_no, []),
                      key=lambda r: _num(r.get("y0")))
        regions: list[dict] = []
        prev_bottom: float | None = None
        for row in rows:
            caption = by_id.get(row.get("caption_block_id") or "")
            if caption is None and row.get("caption"):
                head = str(row["caption"])[:40]
                caption = next((b for b in blocks
                                if str(b.get("text_clean") or b.get("text") or "")
                                .strip().startswith(head)), None)
            raw = _bbox(row)
            below = row["_kind"] == "figure"
            seps = list(structs)
            if below and prev_bottom is not None:
                seps = seps + [{"y0": prev_bottom, "y1": prev_bottom}]
            tight = _tighten(raw, caption, sorted(seps, key=lambda b: _num(b.get("y0"))),
                             rect.y1, below=below)
            tight = _snap_edges(tight, texts, rect)
            cap_text = str(row.get("caption") or (caption or {}).get("text_clean") or "").strip()
            number = _number_of(cap_text)
            cap_id = row.get("caption_block_id") or (caption or {}).get("id") or None
            inner = [b for b in structs
                     if (b.get("type") or "") == "caption" and b.get("id") != cap_id
                     and _num(b.get("y0")) > tight[1] + MIN_HEIGHT
                     and _num(b.get("y1")) < tight[3] - MIN_HEIGHT]
            pieces = _parts_of(tight, inner, rect)
            for i, (piece, part) in enumerate(pieces):
                # 同一张图被切成两段时给个看得懂的标签（前端在侧栏里显示）
                label = "" if len(pieces) == 1 else ("上段" if i == 0 else "下段")
                regions.append(entry(
                    page_no, row["_kind"], piece, raw,
                    cap_text, number if i == 0 else "", cap_id,
                    ({"part": i, "parts": len(pieces), "part_label": label}
                     if label else None),
                ))
            if below:
                prev_bottom = max(_num(regions[-1]["bbox"][3]) - 3.0, 0.0)
        out[page_no] = regions

    # ---- 跨页续图：图体画到页脚、下一页页首接着它 → 下一页补一条"续图"条目 ----
    for page_no in sorted(out):
        for reg in list(out[page_no]):
            if reg.get("kind") != "figure" or reg.get("part", 0) != 0:
                continue
            if not _looks_like_continuation(reg, page_rect(page_no).height):
                continue
            nxt = _continuation_page(paper_id, page_no, reg, pages, ir)
            if nxt is None:
                continue
            rect = page_rect(nxt)
            top = next((_num(b.get("y0")) for b in blocks_by_page.get(nxt, [])
                        if (_num(b.get("char_count")) or 0) > 0
                        and _num(b.get("y0")) > rect.y0 + MIN_HEIGHT), None)
            body = [reg["bbox"][0], rect.y0 + 1.0, reg["bbox"][2],
                    (top - TEXT_PAD) if top else rect.y0 + 0.5 * rect.height]
            body = _normalise(body, rect)
            if body[3] - body[1] < MIN_HEIGHT:
                continue
            out.setdefault(nxt, [])
            out[nxt] = _merge(out[nxt], entry(
                nxt, "figure", body, body, reg.get("caption") or "", "",
                None, {"part": 1, "parts": 2, "part_label": "跨页续图",
                       "continues_from": page_no}))
    return out


def all_regions(paper_id: str, *, force: bool = False) -> dict[int, list[dict]]:
    """全篇插图区域（按 IR 文件 mtime 缓存，避免每个请求反复读库）。"""
    from backend.services.paper import paper_service

    ir_path: Path = paper_service.paper_paths(paper_id)["ir"]
    if not ir_path.is_file():
        return {}
    mtime = ir_path.stat().st_mtime
    with _lock:
        cached = _cache.get(paper_id)
        if cached and not force and abs(cached[0] - mtime) < 1e-6:
            return cached[1]
    data = _compute_all(paper_id)
    prune_crops(paper_id, data)
    with _lock:
        _cache[paper_id] = (mtime, data)
    return data


def prune_crops(paper_id: str, regions: dict[int, list[dict]] | None = None) -> int:
    """删掉不再被任何区域引用的裁图缓存（重解析/换算法后旧文件不会一直堆在磁盘上）。

    文件名自带 bbox 哈希，所以每次算法调整都会产生一批新文件；这里按"当前该有的文件名
    集合"反选。表格裁图与公式裁图不归本模块管，只清理 ``region-p*.png``。
    """
    from backend.services.paper import paper_service

    try:
        figures_dir: Path = paper_service.paper_paths(paper_id)["figures"]
    except Exception:                                   # noqa: BLE001
        return 0
    if not figures_dir.is_dir():
        return 0
    data = regions if regions is not None else all_regions(paper_id)
    keep = {f"region-p{int(page):04d}-{i:02d}-p{int(r.get('part', 0))}-{r['hash']}.png"
            for page, rows in data.items() for i, r in enumerate(rows)}
    keep |= {name[:-4] + "-card.png" for name in keep}     # 图表卡片缓存一起保留
    removed = 0
    for path in figures_dir.glob("region-p*.png"):
        if path.name in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:                          # noqa: PERF203
            log.warning("旧裁图删除失败：%s（%s）", path.name, exc)
    if removed:
        log.info("清理旧插图裁图 %d 个：%s", removed, paper_id)
    return removed


def page_regions(paper_id: str, page_no: int) -> list[dict]:
    """该页插图区域（按文档顺序；表格也返回，kind=table）。"""
    return all_regions(paper_id).get(int(page_no), [])


def invalidate_cache(paper_id: str | None = None) -> None:
    with _lock:
        if paper_id is None:
            _cache.clear()
        else:
            _cache.pop(paper_id, None)


def region_crop_path(paper_id: str, page_no: int, index: int, part: int = 0,
                     *, card: bool = False) -> Path:
    """缓存文件路径：``figures/region-p0008-00-p1-ab12cd34[-card].png``（含 bbox 哈希）。

    ``-card`` 后缀 = 图 + 图注合成的"图表卡片"（与纯图裁图分开缓存）。
    """
    from backend.services.paper import paper_service

    regions = page_regions(paper_id, page_no)
    if index < 0 or index >= len(regions):
        raise NotFoundError(f"插图区域不存在：p{page_no} #{index}")
    figures_dir: Path = paper_service.paper_paths(paper_id)["figures"]
    suffix = "-card" if card else ""
    return figures_dir / (f"region-p{int(page_no):04d}-{index:02d}-p{int(part)}"
                          f"-{regions[index]['hash']}{suffix}.png")


def _resolve_part(paper_id: str, page_no: int, index: int, part: int | None) -> tuple[dict, int]:
    """定位到具体那一段：``part=None`` 取 entry 自己声明的 part（默认 0）。"""
    regions = page_regions(paper_id, page_no)
    if index < 0 or index >= len(regions):
        raise NotFoundError(f"插图区域不存在：p{page_no} #{index}")
    region = regions[index]
    want = int(region.get("part", 0) if part is None else part)
    if want == int(region.get("part", 0)):
        return region, want
    # 兼容旧链接：同一张图被切成多段时，允许只给 index（取第一段）
    return region, int(region.get("part", 0))


def caption_rects(paper_id: str, page_no: int, region: dict) -> list[Rect]:
    """该插图对应的图注矩形（可能跨多块，比如通栏长图注被拆成两段）。"""
    from backend.parsing.ir.ir_reader import IRReader
    from backend.services.paper import paper_service

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    if not ir.exists:
        return []
    number = str(region.get("number") or "")
    if not number:
        return []

    def matches(text: str) -> bool:
        head = re.match(r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})",
                        text or "", re.IGNORECASE)
        return bool(head) and head.group(1) == number

    out: list[Rect] = []
    for b in ir.blocks():
        if int(b["page_no"]) != int(page_no) or (b.get("type") or "") != "caption":
            continue
        if not matches(str(b.get("text_clean") or b.get("text") or "")):
            continue
        rect = _rect_of(b)
        if rect.width > 2 and rect.height > 2:
            out.append(rect)
    return sorted(out, key=lambda r: (r.y0, r.x0))


def ensure_region_crop(paper_id: str, page_no: int, index: int,
                       part: int | None = None, *, card: bool = False) -> Path:
    """按需渲染并缓存该区域的裁图（只渲染 clip 区域，600dpi，先墨迹裁白）。

    注意这里是 ``padding=0``：余量由"墨迹裁白"在**量出真实墨迹之后**再加，
    不能先用 padding 把裁剪框撑大 —— 撑出去的正是旁边图注/图题的半行字
    （用户报的"图注被切掉一条"就是这么来的）。

    ``card=True`` 出的是**图表卡片**（图 + 图注合成一张，见 ``figure_card_png``），
    用于放大看大图与导出；正文里的插图仍是纯图（图注在正文里有自己的一行）。
    """
    from backend.core.utils import write_bytes_atomic
    from backend.parsing.extract.render import figure_card_png, render_crop_png
    from backend.services.paper import paper_service

    region, part_no = _resolve_part(paper_id, page_no, index, part)
    target = region_crop_path(paper_id, page_no, index, part_no, card=card)
    if target.is_file() and target.stat().st_size > 0:
        return target
    x0, y0, x1, y1 = region["bbox"]
    caps = caption_rects(paper_id, page_no, region) if card else []
    pdf = paper_service.resolve_pdf(paper_id)
    with _lock:
        if target.is_file() and target.stat().st_size > 0:
            return target
        import pymupdf as fitz

        with fitz.open(str(pdf)) as doc:
            if card:
                data = figure_card_png(doc, int(page_no), Rect(x0, y0, x1, y1), caps,
                                       dpi=CROP_DPI)
            else:
                data = render_crop_png(doc, int(page_no), Rect(x0, y0, x1, y1),
                                       dpi=CROP_DPI, padding=INK_PAD, ink_trim=True)
        write_bytes_atomic(target, data)
    log.info("插图裁图已生成：p%d #%d part%d%s（%s %s）%d bytes",
             page_no, index, part_no, "·卡片" if card else "",
             region["kind"], region["number"], target.stat().st_size)
    return target


def media_of_page(paper_id: str, page_no: int, *, figures_only: bool = True) -> list[dict]:
    """阅读器/侧栏用的插图条目（url 指向按需裁图接口）。

    被切成多段（同页夹着别的图注 / 跨页续图）时，每段是一条条目：
    ``part`` 是段号、``parts`` 是总段数、``part_label`` 给前端显示"续"。
    """
    out: list[dict] = []
    for i, r in enumerate(page_regions(paper_id, page_no)):
        if figures_only and r["kind"] != "figure":
            continue
        part = int(r.get("part", 0))
        base = (f"/api/v1/papers/{paper_id}/figures/region/{int(page_no)}/{i}"
                + (f"/{part}" if part else ""))
        out.append({
            "kind": r["kind"], "number": r["number"], "caption": r["caption"],
            "bbox": r["bbox"],
            "part": part, "parts": int(r.get("parts", 1)),
            "part_label": r.get("part_label") or "",
            "url": base,
            "card_url": f"{base}?card=1",          # 图 + 图注合成（放大看大图/导出用）
            "status": "ok",
        })
    return out


# --------------------------------------------------------------------------
# Markdown 图片引用：让插图**出现在 Markdown 本体里**
# --------------------------------------------------------------------------

# 图注行：整行就是图注（可带 ** 加粗）
_MD_CAPTION = re.compile(
    r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})\b", re.IGNORECASE)


def markdown_refs(paper_id: str, page_no: int, *, prefix: str) -> dict[str, str]:
    """该页每个图号对应的 Markdown 图片行，并**确保裁图已落盘**。

    ``prefix`` 决定引用形式：
    - 以 ``/`` 开头（如 ``/api/v1``）→ ``/api/v1/papers/<id>/figures/region/<page>/<i>``，
      应用内渲染用（浏览器能取到）；
    - 其它（如 ``../figures``）→ 相对路径，落盘 Markdown 用（在 md/ 目录下打开可用）。
    """
    refs: dict[str, str] = {}
    for i, r in enumerate(page_regions(paper_id, page_no)):
        if r["kind"] != "figure" or not r["number"]:
            continue
        try:
            path = ensure_region_crop(paper_id, page_no, i)
        except Exception as exc:  # noqa: BLE001
            log.warning("裁图渲染失败，Markdown 里跳过该图：p%d #%d %s", page_no, i, exc)
            continue
        if prefix.startswith("/"):
            base = f"{prefix.rstrip('/')}/papers/{paper_id}/figures/region/{int(page_no)}/{i}"
        else:
            base = f"{prefix.rstrip('/')}/{path.name}"
        refs[r["number"]] = f"![{r['caption'][:80] or f'Figure {r[number]}'}]({base})"
    return refs


def inject_images(md: str, refs: dict[str, str]) -> str:
    """把图片行插到**对应图号的图注行上方**（论文排法：图在上、图注在下）。

    已经带 ``![`` 的行不再重复插入；匹配不到的图放到末尾，绝不丢图。
    """
    text = (md or "").strip()
    if not text or not refs:
        return text
    if "![" in text:                    # 模型自己给了图片引用就尊重它
        return text
    blocks = re.split(r"\n\s*\n", text)
    used: set[str] = set()
    out: list[str] = []
    for block in blocks:
        m = _MD_CAPTION.match(block.strip())
        if m and m.group(1) in refs and m.group(1) not in used:
            used.add(m.group(1))
            out.append(refs[m.group(1)])      # 图在上
        out.append(block)                     # 图注在下
    for number, ref in refs.items():
        if number not in used:
            out.append(ref)
    return "\n\n".join(out).strip()
