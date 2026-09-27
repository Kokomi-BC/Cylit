"""插图切分（规划 §13.2）：图注定位 → 上下找图体 bbox（矢量+位图并集）→ 裁剪 + 余量。

同时输出「子图数」估计（按内部矢量框/位图数量），供 qa 层体检使用。

图体按**图注分段**取（2026-09-27，用户报的"一页多图会漏掉"）
------------------------------------------------------------
一张图注的图体 = 从**同栏上一张图注的底边**到**本图注顶边**这一条横带内的墨迹外接框。
不这么切的话，"图注上方所有墨迹取并集"会让同一页上下堆叠的每张图得到几乎相同的 bbox
（下面那张的并集把上面全吞了），而 `figure` 表的主键是 `block_id`、两条候选又都会撞到
同一个宿主块 → `INSERT OR REPLACE` 静默覆盖，一页三张图最后只剩一张。
实测 9984-boomerang p33：三条候选并集分别 36~204 / 36~384 / 36~563，落库只剩两条，图 29 整张丢失。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.parsing.extract.image_layer import ImageInfo
from backend.parsing.extract.vector_layer import Drawing
from backend.parsing.models import RawBlock, Rect, rects_span
from backend.parsing.structure.paragraph import compute_margins

_CAPTION = re.compile(r"^\s*(?:Figure|Fig\.?)\s*(\d+[a-z]?)", re.IGNORECASE)
_TABLE_CAPTION = re.compile(r"^\s*(?:Table)\s*(\d+[a-z]?)", re.IGNORECASE)
_CAPTION_ANY = re.compile(r"^\s*(?:Figure|Fig\.?|Table)\s*\d+", re.IGNORECASE)
# 取编号专用（见 caption_number）：容忍 Markdown 标记与中英文写法
_CAPTION_NUM = re.compile(r"^\s*[*#>\s]*(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})",
                          re.IGNORECASE)
# ⚠️ 这里曾有一个 `CAPTION_MAX_GAP = 120pt`（图注离图体太远就不认）：它本是为压掉
# DTDBD p11 那条"正文引用句当图注"的假图（离图体 162pt），但那条**已由区域层的"同一图号
# 只留离图体最近的那条"解决**（`figure_regions._compute_all`），留在解析层只会误杀真图 ——
# 实测 9984-boomerang p33 的图 29 与上一张图的墨迹相隔 187pt，整张图被丢。
# 现在由 `_segment_floor` 的**分段**保证"每张图只吃自己那一段的墨迹"，不需要距离阈值。


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
    #: 图注**本栏锚点**（见 `_caption_anchor`）：下游（"图 + 图注"卡片的图注条）用它把
    #: "横跨整页的图注块"裁回本栏，免得把另一栏的正文/参考文献拼进图注。
    caption_anchor: list[float] | None = None


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


def _same_column(a: Rect, b: Rect, *, min_overlap: float = 0.35) -> bool:
    """两个块是不是同一栏（横向重叠占较窄那方的比例足够大）。"""
    return _h_overlap(a, b) >= min_overlap


#: 一行里"栏间空隙"的下限（pt）：词间距只有 3~5pt，双栏页的栏间空隙 12~20pt。
COLUMN_GAP_SPLIT = 10.0


def _line_column_run(line, page_rect: Rect | None) -> tuple[Rect, str]:
    """一行里**靠左的那一段连续 span**（在栏间空隙处断开）。

    ⚠️ PyMuPDF 会把**左右两栏同一基线**的文字并成一条 ``line``：实测 LMF p8 的图注行
    ``text`` 就是 ``"Figure 4: The Impact … on" + "Kazemzadeh, Emily Mo…"``（左栏图注 + 右栏文献），
    而这一行的 rect 横跨整页。只能再按 span 之间的空隙切开。

    判据刻意收窄：断点必须出现在**页面中线右侧**（第二栏的起点），免得把"编号与图注之间
    那个 tab 式宽空格"（`Figure 1:    Overview …` 这种排法很常见）当成栏缝、把图注截断。
    """
    spans = [s for s in line.spans if s.text]
    if not spans:
        return line.rect, line.text
    mid = (page_rect.x0 + page_rect.width / 2) if page_rect is not None else None
    run = [spans[0]]
    for prev, cur in zip(spans, spans[1:]):
        if cur.x0 - prev.x1 > COLUMN_GAP_SPLIT and mid is not None and cur.x0 > mid:
            break
        run.append(cur)
    return rects_span([s.rect for s in run]), "".join(s.text for s in run)


def _caption_anchor(ab: RawBlock, page_rect: Rect | None = None) -> tuple[Rect, str]:
    """图注的**锚点矩形**与**锚点文本**：只取块里属于图注那一栏的行/半边。

    ⚠️ 实测 `efficient-low-rank-multimodal-fusion`（A4 双栏；转换器把左右两栏的行塞进了
    **同一个** PyMuPDF 文本块、甚至是**同一条 line**）：图注块 `…-p08-b30` 的 bbox 是 x 72~527
    —— 横跨整页，里面装的是「左栏的图注行」+「右栏的参考文献行」：

        Figure 4: The Impact of different rank settings on   |  Kazemzadeh, Emily Mower, …

    拿**整块**当锚点，右栏那些墨迹（标题下划线等）与锚点的横向重叠就是 1.0 → 全被算进图体，
    裁出来是"图 + 右栏一整片正文"（用户 2026-09-27 的截图）；整块文本当图注，
    图注尾巴上还会拼一条参考文献。所以以**第一条像图注的行**所在栏为准，只保留这一栏的内容。
    ``page_rect``：只有**比一栏宽**（> 半页）的行才做栏间切分。
    """
    lines = [ln for ln in ab.lines if ln.spans and ln.text.strip()]
    if not lines:
        return ab.rect, ab.text.strip()
    pieces: list[tuple[Rect, str]] = []
    for ln in lines:
        if page_rect is not None and ln.rect.width > 0.5 * page_rect.width:
            pieces.append(_line_column_run(ln, page_rect))
        else:
            pieces.append((ln.rect, ln.text))
    heads = [p for p in pieces
             if _CAPTION.match(p[1].strip()) or _TABLE_CAPTION.match(p[1].strip())]
    col = (heads[0] if heads else pieces[0])[0]
    keep = [p for p in pieces if _same_column(p[0], col, min_overlap=0.5)]
    if not keep:
        return ab.rect, ab.text.strip()
    return (rects_span([p[0] for p in keep]),
            "\n".join(p[1] for p in keep).strip())


#: 图注形态（"Figure 29: …"）：编号后紧跟标点，或整行本来就短。
#: 正文里的引用句（"Figure 2 shows that boomerang distillation creates models whose size…"）
#: 编号后没有标点、而且长 —— 不拿它当分段的界标，免得把上一张图的图体切掉一块。
_CAPTION_DELIM = re.compile(r"^\s*[:：.．—–\-|]")
_CAPTION_HEAD = re.compile(r"^\s*(?:\*\*)?\s*(?:Figure|Fig\.?|Table|图|表)\s*\d{1,3}",
                           re.IGNORECASE)


def _looks_like_caption_line(text: str) -> bool:
    """这行像不像"真图注"（而不是正文里的 `Figure 2 shows that …` 引用句）。

    编号后紧跟标点 → 认；否则只在**整行很短**时认（真有 `Figure 3 Overview of the proposed
    method` 这种不写标点的排法）。放宽是**安全**的：这个判据只用来定"分段的界标"，
    而界标界的是**墨迹**（矢量/位图）的上界 —— 正文文字不参与并集，
    反倒是漏掉一条真界标的代价更大（上一张图会被吞进这一张，见 `_segment_floor`）。
    """
    t = (text or "").strip()
    m = _CAPTION_HEAD.match(t)
    if not m:
        return False
    if _CAPTION_DELIM.match(t[m.end():]):
        return True
    return len(t) <= 60


def _segment_floor(cap_rect: Rect, page_no: int, captions: list[tuple[int, Rect]],
                   page_rect: Rect) -> float:
    """本图注所在竖段的**下边界**：同栏上一张图注的底边（没有就用页首）。

    这是"按图注切段"的界标（见模块 docstring）：只吃这一段里的墨迹，
    上下堆叠的多张图才不会互相吞并。``captions`` = ``[(页, 图注锚点矩形)]``。
    """
    cap = cap_rect
    floor = page_rect.y0 + 0.02 * page_rect.height
    for other_page, r in captions:
        if other_page != page_no:
            continue
        if r == cap:                               # 自己
            continue
        if r.y1 > cap.y0 - 2:                      # 只看**上方**的图注/题注
            continue
        if not _same_column(r, cap):               # 不同栏不算（双栏页里两栏各排各的）
            continue
        floor = max(floor, r.y1 + 2)
    return floor


# ---- 没有图注的图体兜底（用户 2026-09-27："实在没有图注也行"）------------------
# 图注被并进正文、或排版本来就没写图注时，不能整张图都不要 —— 但这种页**很少**
# （用户："图片一般都有图注"），所以兜底必须收得很紧，否则公式页/表格页的墨迹会被当成插图
# 批量产出（实测第一版阈值太松：整篇 39 页产出 800+ 张"图"，每张还要按 600dpi 渲染）。
#
# 两道闸门：
# 1. **只认整页一条图注都没有的页**（图注或表注都算）—— 有图注的页交给"按图注分段"，
#    兜底只负责真正没有锚点的页面；
# 2. 墨迹带本身要够大、够密、且含"成块的图形"（面积 ≥ NO_CAPTION_MIN_SHAPE 的路径或位图）——
#    公式页的分数线/括号是零散细线，凑不出这种带。
NO_CAPTION_GAP = 24.0          # 带内元素允许的最大空档（pt）：图内子图/图例间隔就是这个量级
NO_CAPTION_MIN_HEIGHT = 60.0   # 带的最小高度（pt）：再矮的多半是页眉细线、公式下划线
NO_CAPTION_MIN_WIDTH = 0.25    # 带的最小宽度（页宽占比）
#: 带内最少元素数。实测（efficient-low-rank-multimodal-fusion）：真插图 34~147 条路径，
#: 而**独立公式**（带大括号的 `w_m = [w, …]`）只有 6 条 —— 阈值放在 5 会把公式裁成一张
#: "无图注的图"（用户：那篇只有 4 张图，却截出 6 张）。位图不数元素（下面 big_img 直接放行）。
NO_CAPTION_MIN_ELEMENTS = 20
NO_CAPTION_MIN_SHAPE = 900.0   # 带内至少要有这么大面积的图形（一块子图/一张位图）
NO_CAPTION_MAX_PER_PAGE = 3    # 一页最多认几张（保险丝）


def _cover_ratio(band: Rect, other: Rect) -> float:
    """``other`` 覆盖了 ``band`` 的多少（按 band 面积算）。"""
    if band.area <= 0:
        return 0.0
    ix = min(band.x1, other.x1) - max(band.x0, other.x0)
    iy = min(band.y1, other.y1) - max(band.y0, other.y0)
    if ix <= 0 or iy <= 0:
        return 0.0
    return (ix * iy) / band.area


def _captionless_bands(
    page_drawings: list[Drawing], page_images: list[ImageInfo],
    page_rect: Rect, claimed: list[Rect],
) -> list[tuple[Rect, list[Drawing], list[ImageInfo]]]:
    """按"墨迹带"找没有被图注认领的图体（返回 ``[(bbox, 矢量, 位图)]``）。"""
    els: list[tuple[Rect, object, str]] = [(d.rect, d, "d") for d in page_drawings]
    els += [(i.rect, i, "i") for i in page_images if _usable_image(i, page_rect)]
    els.sort(key=lambda t: (t[0].y0, t[0].x0))

    bands: list[list[tuple[Rect, object, str]]] = []
    cur: list[tuple[Rect, object, str]] = []
    bottom = 0.0
    for item in els:
        if cur and item[0].y0 - bottom <= NO_CAPTION_GAP:
            cur.append(item)
            bottom = max(bottom, item[0].y1)
        else:
            if cur:
                bands.append(cur)
            cur = [item]
            bottom = item[0].y1
    if cur:
        bands.append(cur)

    out: list[tuple[Rect, list[Drawing], list[ImageInfo]]] = []
    for band in bands:
        span = rects_span([r for r, _o, _k in band])
        if span.height < NO_CAPTION_MIN_HEIGHT:
            continue
        if span.width < NO_CAPTION_MIN_WIDTH * max(1.0, page_rect.width):
            continue
        big_img = any(k == "i" for _r, _o, k in band)
        if len(band) < NO_CAPTION_MIN_ELEMENTS and not big_img:
            continue
        # 带里得有"成块的图形"：一块子图（几平方英寸的 rect）或一张位图。
        # 只有细线/小刻度的带（公式、表格线框、坐标轴）不算插图。
        if not big_img and not any(r.area >= NO_CAPTION_MIN_SHAPE
                                   for r, _o, k in band if k == "d"):
            continue
        if any(_cover_ratio(span, c) > 0.35 for c in claimed):
            continue                                        # 已经被某条图注/表认领
        out.append((span,
                    [o for _r, o, k in band if k == "d"],      # type: ignore[misc]
                    [o for _r, o, k in band if k == "i"]))     # type: ignore[misc]
        if len(out) >= NO_CAPTION_MAX_PER_PAGE:
            break
    return out


def _body_rect_above_caption(
    cap_rect: Rect,
    page_images: list[ImageInfo],
    page_drawings: list[Drawing],
    page_rect: Rect,
    max_height_ratio: float = 0.95,
    floor: float | None = None,
) -> tuple[Rect | None, list[ImageInfo], list[Drawing], str]:
    """在图注上方寻找图体区域（**按墨迹外接框**，且只取本图注那一段）。

    ``cap_rect`` 是图注的**锚点矩形**（只含图注那一栏，见 :func:`_caption_anchor`），
    不是一个可能横跨整页的文本块。
    ``floor`` = 本段的下边界（同栏上一张图注的底边，见 :func:`_segment_floor`）：
    只收 ``y1 > floor`` 的元素，并把并集裁到 ``floor`` 之上。默认（None）用页首。
    """
    top_limit = page_rect.y0 + 0.02 * page_rect.height
    if floor is not None:
        top_limit = max(top_limit, floor)
    # 页眉带里的墨迹是**页眉装饰**（与 `paragraph.compute_margins` 同一个口径）：
    # 实测这篇每页在 y=39.2 有一条通栏细线（页眉下沿的分隔线），它落进并集之后，
    # 每张"页首图"的裁图顶部都会多出一条横线 + 一大片空白 —— `render_crop_png(ink_trim=True)`
    # 也去不掉，因为它本身就是墨迹。整条落在页眉带里的元素一律不算图体。
    header_bottom = page_rect.y0 + compute_margins(page_rect.height).header_band
    bottom = cap_rect.y0 - 2
    max_top = bottom - 0.04 * page_rect.height

    # 候选元素：图注上方、**伸进本段**、与图注水平有重叠、且不整条落在页眉带里的位图与矢量路径
    def above(r: Rect) -> bool:
        return (r.y1 <= cap_rect.y0 + 3 and r.y1 > top_limit
                and r.y1 > header_bottom and r.y0 >= page_rect.y0)

    imgs = [i for i in page_images if above(i.rect)
            and _usable_image(i, page_rect)
            and _h_overlap(i.rect, cap_rect) > 0.10]
    # ⚠️ 矢量元素**不再按"面积 > 25 / 不是细线"过滤**（2026-09-27，用户报的"裁少了"）：
    # matplotlib 画的多子图网格，最上面那条往往是坐标轴细线或极小的刻度（面积 < 25）——
    # 被过滤掉之后，图体上沿就掉到下一个"大元素"上，于是**裁图只剩下半截**
    # （实测 9984-boomerang 的高图：裁出来只有图例那一小条）。改成"按墨迹外接框取并集"，
    # 由 ``floor``（上一张图注的底边）负责不把上面那张图吞进来。
    drs = [d for d in page_drawings if above(d.rect)
           and _h_overlap(d.rect, cap_rect) > 0.10]
    if not imgs and not drs:
        return None, [], [], "no_body_found"

    union = rects_span([i.rect for i in imgs] + [d.rect for d in drs])
    # 跨段元素只取伸进本段的那部分（段界就是上一张图注的底边）
    if union.y0 < top_limit:
        union = Rect(union.x0, top_limit, union.x1, union.y1)
    # 段内元素已经全部算进并集，原先那段"连续块扩展"是在已选集合里再取并集 —— 恒等于上面这行，
    # 一并删掉（它从来没能挡住任何东西，只是让人以为有"连续性"这道闸门）。

    # 高度上限只作"别把整页吞进来"的护栏（0.95 页高）：图注+图体正常都在一页内
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


def _is_text_decoration(d: Drawing, page_blocks: list[RawBlock]) -> bool:
    """这条细横线是不是**文字的下划线**（而不是图里的线）。

    实测 DeepSeek-V3.2 附录页（`12382-beyond-speedup-utilizing-2` p6）：参考文献里的 URL 带下划线，
    下划线也是"墨迹" → 被算进图 4 的图体并集 → 裁出来顶上是 5 条参考文献 + `Appendices` 标题
    （用户截图）。判据：细横线 + 紧贴在某个文字行的**下沿**（间隔 -2~5pt）+ 与它有横向重叠。
    只用于**排除图体候选**，不动任何真实图形（图里的线不会贴着文字下沿走）。
    """
    if not d.is_thin_bar(3.0) or d.length < 20:
        return False
    for ab in page_blocks:
        for ln in ab.lines:
            if not ln.spans:
                continue
            r = ln.rect
            if _h_overlap(r, d.rect) < 0.5:
                continue
            if -2.0 <= d.rect.y0 - r.y1 <= 5.0:
                return True
    return False


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
    pages: set[int] | None = None,
) -> list[FigureCandidate]:
    """插图检测。

    ``pages``：**本次调用负责的页**。管道按页调用（``pages={pno}``）时必须传 ——
    两个 page 映射是全篇的，不传就会把其他页的墨迹也兜底产出（见下面兜底那段注释）。
    默认 ``None`` = 认为调用方一次负责整篇。
    """
    out: list[FigureCandidate] = []
    all_caps = find_caption_blocks(blocks)
    # 文字下划线（参考文献里的 URL 都带）不算图体墨迹：实测 DeepSeek-V3.2 附录页的图 4
    # 因此把顶上 5 条参考文献 + `Appendices` 标题一起裁了进来（用户截图）。
    deco: dict[int, list[Drawing]] = {}
    for pno, drs in drawings_by_page.items():
        here = [b for b in blocks if b.page_no == pno]
        deco[pno] = [d for d in drs if not _is_text_decoration(d, here)]
    drawings_by_page = deco
    # 图注锚点：块里**本栏的那些行**（见 `_caption_anchor`：转换器产出的 PDF 会把左右两栏的行
    # 塞进同一个文本块，整块 bbox 横跨整页）。锚点同时用于"分段界标"和"图体并集"。
    anchors: dict[int, tuple[Rect, str]] = {}
    for ab, _kind, _label in all_caps:
        pr = page_rects.get(ab.page_no) or Rect(0, 0, 612, 792)
        # 超过半页宽的行才可能是"两栏被并成一条 line"（见 `_caption_anchor`）
        anchors[id(ab)] = _caption_anchor(ab, page_rect=pr)
    # 分段的界标 = **所有**图注/题注（真图注形态的那些）：图与表都是"结构对象"，
    # 一张图的下边界不该越过上一张图注/题注的底边。
    floors: list[tuple[int, Rect]] = [
        (ab.page_no, anchors[id(ab)][0]) for ab, _kind, _label in all_caps
        if _looks_like_caption_line(anchors[id(ab)][1])
    ]
    # ⚠️ 界标还要包含**表格本体**：实测 12382 p7 的 Table 2，表注在表格**下方**、而且只有右半栏，
    # 光靠"上一张图注"挡不住 → 图 3 的图体把整张表格吞进来，下游还把图 3 的一块当成了表格
    #（用户："figure3 变成表格了"）。
    try:
        floors += [(t.page_no, t.rect) for t in detect_tables(blocks, drawings_by_page, page_rects)]
    except Exception as exc:  # noqa: BLE001 —— 界标拿不到不该影响插图检测
        log.warning("表格界标计算失败（只用图注当界标）：%s", exc)

    for ab, kind, _label in all_caps:
        if kind != "figure":
            continue
        page_no = ab.page_no
        page_rect = page_rects.get(page_no) or Rect(0, 0, 612, 792)
        cap_rect, cap_text = anchors[id(ab)]
        floor = _segment_floor(cap_rect, page_no, floors, page_rect)
        body, imgs, drs, reason = _body_rect_above_caption(
            cap_rect, images_by_page.get(page_no, []), drawings_by_page.get(page_no, []),
            page_rect, floor=floor,
        )
        if body is None:
            # 图注下方找图体（部分排版图注在图上方）
            body, imgs, drs, reason = _body_rect_below_caption(
                cap_rect, page_no, images_by_page.get(page_no, []),
                drawings_by_page.get(page_no, []), page_rect, blocks,
            )
        if body is None:
            continue
        # ⚠️ 这里**不再**用旧的 `CAPTION_MAX_GAP` 挡"图注离图体太远"（已删，理由见文件头）。
        rect = body.expand(padding)
        conf = 0.85 if imgs else 0.6
        out.append(FigureCandidate(
            page_no=page_no, rect=rect, caption_block_id=None,
            caption=cap_text, image_infos=imgs, drawings=drs,
            subfigure_count=estimate_subfigures(drs, imgs), confidence=conf, reason=reason,
            caption_anchor=[round(v, 2) for v in (cap_rect.x0, cap_rect.y0,
                                                  cap_rect.x1, cap_rect.y1)],
        ))

    # ---- 兜底：整页**一条图注都没有**时，把墨迹带也裁出来（无图注；用户 2026-09-27）----
    # 图注被并进正文（实测 3360 的 Figure 4）、或排版本来就没写图注时，不能整张图都不要。
    # 只对"没有任何图注/表注的页"生效（用户："图片一般都有图注"）：有图注的页有锚点，
    # 交给上面的分段逻辑；放宽到"有图注的页也兜底"会立刻产出几百张假图
    #（实测第一版：39 页 800+ 张，公式页的分数线全被当成插图）。
    pages_with_caps = {ab.page_no for ab, _k, _l in all_caps if _looks_like_caption_line(ab.text)}
    # ⚠️ `pages` = **本次调用负责的页**，必须由调用方说清楚（管道按页调用：`pages={pno}`）。
    # `images_by_page`/`drawings_by_page` 传的是**全篇**映射，不收窄的话每次调用都会把
    # "其他页"当成本页、把它们的墨迹带全部兜底产出（实测：39 页 × 每页几十条 → 700+ 张假图，
    # 还要按 600dpi 逐张渲染，解析从 36 秒变成十几分钟）。默认 None = 整篇一次调用。
    my_pages = (set(images_by_page) | set(drawings_by_page)) if pages is None else set(pages)
    for page_no in sorted(my_pages):
        if page_no in pages_with_caps:
            continue
        page_rect = page_rects.get(page_no) or Rect(0, 0, 612, 792)
        for span, drs, imgs in _captionless_bands(
            drawings_by_page.get(page_no, []), images_by_page.get(page_no, []),
            page_rect, [],
        ):
            out.append(FigureCandidate(
                page_no=page_no, rect=span.expand(padding), caption_block_id=None,
                caption="", image_infos=imgs, drawings=drs,
                subfigure_count=estimate_subfigures(drs, imgs),
                confidence=0.5 if imgs else 0.4, reason="no_caption",
            ))
    return out


def _body_rect_below_caption(
    cap_rect: Rect, page_no: int, page_images: list[ImageInfo],
    page_drawings: list[Drawing], page_rect: Rect, all_blocks: list[RawBlock],
    max_height_ratio: float = 0.6,
) -> tuple[Rect | None, list[ImageInfo], list[Drawing], str]:
    cap_bottom = cap_rect.y1 + 2
    # 下一个文本块的顶部作为下边界（⚠️ 必须**同页**：`all_blocks` 传进来的可能是全篇的块，
    # 不筛页的话会被别的页面上靠上的块拖小窗口，图体凭空短一截）
    next_top = page_rect.y1
    for other in all_blocks:
        if other.page_no != page_no:
            continue
        if other.rect.y0 > cap_bottom and other.rect.y0 < next_top:
            next_top = other.rect.y0
    limit = min(next_top, cap_bottom + max_height_ratio * page_rect.height)

    imgs = [i for i in page_images
            if i.rect.y0 >= cap_bottom - 3 and i.rect.y0 < limit
            and _usable_image(i, page_rect)
            and _h_overlap(i.rect, cap_rect) > 0.10]
    drs = [d for d in page_drawings
           if d.rect.y0 >= cap_bottom - 3 and d.rect.y0 < limit
           and _h_overlap(d.rect, cap_rect) > 0.10
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
        # 表注同样用"本栏锚点"（块/行可能横跨两栏，见 `_caption_anchor`）
        cap_rect, cap_text = _caption_anchor(ab, page_rect=(page_rects.get(ab.page_no) or Rect(0, 0, 612, 792)))
        if not _looks_like_table_caption(cap_text):
            # 正文里"Table 2 summarizes our results and …"这种**句子**不该被当成表注
            # （实测 attention p8 就多出一个假的"表 2"，知识库里跟着多一条空表）
            continue
        page_no = ab.page_no
        page_rect = page_rects.get(page_no) or Rect(0, 0, 612, 792)
        drs = drawings_by_page.get(page_no, [])
        h_bars = [d for d in drs if d.is_thin_bar(3.0) and d.length > 20]
        v_bars = [d for d in drs if d.is_vertical and d.length > 20]

        # 表体：图注附近 0.6 页高内的线框集合
        window = Rect(page_rect.x0, max(page_rect.y0, cap_rect.y0 - 0.6 * page_rect.height),
                      page_rect.x1, min(page_rect.y1, cap_rect.y1 + 0.25 * page_rect.height))
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
            rect=_table_body_rect(cap_rect, blocks, local_h + local_v, page_rect).expand(4),
            caption_block_id=None,
            caption=cap_text,
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


def _table_body_rect(cap: Rect, blocks: list[RawBlock],
                     bars: list[Drawing], page_rect: Rect) -> Rect:
    """表格本体（``cap`` = 表注**本栏**的锚点矩形）：优先**线框并集**，
    没有线框就吃表注下方**连续的同栏文本块**。

    两种排版都要管：attention 的 booktabs 表只有几条横线（`has_borders=False`），
    而 3360 的表有线框。都没有时退回"表注下方到第一个明显空档"的文本行 ——
    那正是表格的数值行，比"只剩表注"强得多。
    """
    body: Rect | None = None
    # ⚠️ 不能"先看表注下方、有就收"：实测 12382 p7 的 Table 2，**表注在表格下方**，
    # 而表注下面正好还有图（Vanilla CoE）的线框 → 表格本体被算到下面的图上
    #（IR 里 Table 2 = y 268~453，真正的表格在 94~259），连带把图 3 切成了"上段=表格"。
    # 判据：**哪一侧有更多"像表格行的文字块"（含数字）就取哪一侧** —— 表格行几乎必带数值，
    # 而正文段落/图的说明不带（实测：上侧 4 块带数字，下侧 2 块）。
    def _digit_blocks(below: bool) -> int:
        n = 0
        for b in blocks:
            r = b.rect
            if not _h_overlap(r, cap) > 0.10:
                continue
            gap = (r.y0 - cap.y1) if below else (cap.y0 - r.y1)
            if gap < -2 or gap > 200:
                continue
            if len(r and str(b.text or "")) and any(ch.isdigit() for ch in str(b.text or "")):
                n += 1
        return n

    above_bars = [d.rect for d in bars if d.rect.y1 <= cap.y0 + 2]
    below_bars = [d.rect for d in bars if d.rect.y0 >= cap.y1 - 2]
    prefer_above = False
    if above_bars and below_bars:
        prefer_above = (_digit_blocks(True) == 0 and _digit_blocks(False) > 0) or \
            _digit_blocks(False) > _digit_blocks(True)
    picked: list[Rect] = []
    if above_bars and (prefer_above or not below_bars):
        picked = above_bars
    elif below_bars:
        picked = below_bars
    if picked:
        x0 = min(r.x0 for r in picked)
        x1 = max(r.x1 for r in picked)
        y0 = min(r.y0 for r in picked)
        y1 = max(r.y1 for r in picked)
        body = cap.union(Rect(x0, y0, x1, y1))
    else:
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
