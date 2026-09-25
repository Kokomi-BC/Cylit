"""像素版面（扫描件专用）：把"图体在哪一块"从模型手里拿回来。

为什么需要它
------------
视觉模型给的 `bbox` 只是**语义锚点**（"Figure 3 大概在这一带"），实测它的精度是
"±20~50pt、偶尔整块偏掉"——同一页 p7 两次调用给出过两种差很远的框：

- 第一次 ``y 281~519``（基本盖住图，但不严丝合缝）；
- 第二次 ``y 228~402``（把上一段正文和 2.1 标题圈进来、却只盖住上面那张子图）。

拿这种框当裁剪窗口，轻则多截正文、重则把图裁掉一半。所以窗口必须由**页图像素**决定：
模型框只用来"认领"哪一块像素区域是这张图（图号关联 + 兜底），边界一律来自墨迹。

做法（三档，全部只读页图的灰度投影）
------------------------------------
1. **墨迹带**：行投影切出连续的墨迹带（``InkBand``，含该带内墨迹的左右边界）；
2. **分组**：间隔 ≤ ``GROUP_MAX_GAP`` 的相邻带并成一组（一张图内部的子图间距、坐标轴
   标签与图框的间距都在这个量级）；**排除"文字组"**（≥3 条高度相近的短带 = 一个段落，
   防止模型把框画在正文上时我们跟着裁一段正文出来）；
3. **认领**：与模型框重叠面积最大的那一组就是这张图。

图注切分：图注常常紧贴图体（p8 的 Figure 3 只隔 1.6pt，几何上根本分不开），
所以只用"**短行带**"来切：模型给的图注框落进哪一条带，若那条带足够矮（≤ ``CAPTION_BAND_MAX_H``
= 一行字的高度）就从它的上沿切开；若落进高大的图体带（模型框偏了十几点）就不切 ——
宁可留一行图注，也不能切掉图的一部分。
"""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf as fitz

from backend.core.logging import get_logger
from backend.parsing.models import Rect

log = get_logger(__name__)

PROBE_DPI = 110            # 只做版面判定，不需要高分辨率
INK_THRESHOLD = 250        # 灰度 < 它的算墨迹
GROUP_MAX_GAP = 10.0       # pt：组内相邻墨迹带的最大间隔
TEXT_BAND_MAX_H = 22.0     # pt：一行文字（含上下标）的高度上限
TEXT_GROUP_MIN_BANDS = 3   # 至少这么多条短带才算"一个段落"
TEXT_GROUP_MAX_CV = 0.65   # 文字带高度的变异系数上限（文字行高度本来就整齐）
SEED_MIN_COVER = 0.30      # 认领门槛：重叠面积 / min(模型框, 墨迹组) 面积
MIN_GROUP_AREA_RATIO = 0.03  # 小于页面这个比例的墨迹组不是插图（印章/水印/logo）
MERGE_GAP = 20.0           # pt：同一张图里"子图之间"的间隔上限（合并相邻组用）
CAPTION_BAND_MAX_H = 22.0  # pt：能当"图注行"来切的带高度上限
MAX_FIGURE_RATIO = 0.85    # 图体不该超过页面这个比例


@dataclass
class InkBand:
    """一条连续的墨迹带（页面上"有东西"的一段纵向区间）。"""

    y0: float
    y1: float
    x0: float
    x1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0


class PageInk:
    """一页的墨迹投影（同页多张图复用同一份，只渲染一次）。"""

    def __init__(self, doc, page_no: int, page_rect: Rect, *,
                 dpi: int = PROBE_DPI, threshold: int = INK_THRESHOLD) -> None:
        import numpy as np

        self.page_rect = page_rect
        self.page_no = page_no
        page = doc[page_no - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                              colorspace=fitz.csGRAY, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        arr = arr.reshape(pix.height, pix.stride)[:, :pix.width]
        self._mask = arr < threshold
        self._sx = page_rect.width / max(1, pix.width)
        self._sy = page_rect.height / max(1, pix.height)
        self._bands: list[InkBand] | None = None

    # ------------------------------------------------------------------
    def bands(self) -> list[InkBand]:
        if self._bands is None:
            self._bands = self._compute_bands()
        return self._bands

    def _compute_bands(self) -> list[InkBand]:
        import numpy as np

        mask = self._mask
        rows = mask.any(axis=1)
        if not rows.any():
            return []
        idx = np.flatnonzero(rows)
        # 连续行（允许中间断 1 行，抗扫描噪点）
        splits = np.flatnonzero(np.diff(idx) > 2)
        out: list[InkBand] = []
        for start, end in zip(np.r_[0, splits + 1], np.r_[splits, len(idx) - 1]):
            r0, r1 = int(idx[start]), int(idx[end]) + 1
            cols = mask[r0:r1].any(axis=0)
            c = np.flatnonzero(cols)
            if not len(c):
                continue
            out.append(InkBand(
                y0=self.page_rect.y0 + r0 * self._sy,
                y1=self.page_rect.y0 + r1 * self._sy,
                x0=self.page_rect.x0 + int(c[0]) * self._sx,
                x1=self.page_rect.x0 + (int(c[-1]) + 1) * self._sx,
            ))
        return out


# --------------------------------------------------------------------------
def is_text_group(bands: list[InkBand]) -> bool:
    """这一组是不是"一个文字段落"（而不是一张图）。

    文字段落的特征非常稳：许多条**高度相近的短带**、间距也相近。图体带则要么很高
    （图框、热力图），要么高矮混杂（图框 + 刻度 + 轴标签）。
    """
    if len(bands) < TEXT_GROUP_MIN_BANDS:
        return False
    heights = [b.height for b in bands]
    if max(heights) > TEXT_BAND_MAX_H:
        return False
    mean = sum(heights) / len(heights)
    if mean <= 0:
        return False
    var = sum((h - mean) ** 2 for h in heights) / len(heights)
    return (var ** 0.5) / mean <= TEXT_GROUP_MAX_CV


def _group_bands(bands: list[InkBand], max_gap: float = GROUP_MAX_GAP
                 ) -> list[list[InkBand]]:
    groups: list[list[InkBand]] = []
    for b in bands:
        if groups and b.y0 - groups[-1][-1].y1 <= max_gap:
            groups[-1].append(b)
        else:
            groups.append([b])
    return groups


def _rect_of(bands: list[InkBand]) -> Rect:
    return Rect(min(b.x0 for b in bands), min(b.y0 for b in bands),
                max(b.x1 for b in bands), max(b.y1 for b in bands))


def _overlap_area(a: Rect, b: Rect) -> float:
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return ix * iy


def figure_window(ink: PageInk, seed: Rect) -> tuple[Rect, list[InkBand]] | None:
    """用像素投影定出 seed（模型框）所指的那张图的真实边界与它的墨迹带。

    认领规则（都踩过坑）：
    - **排除文字组**：模型框画在正文上时，不能让正文段"认领"成功；
    - **排除过小的组**：印章/水印/logo 也在页面上，但它们占不到页面的 3%；
    - **重叠判据用 min(...)**：模型框既可能比图大（多圈了正文）也可能比图小
      （只盖住一半），所以要求"与两者中较小的那个有 30% 以上的重叠"；
    - **相邻组要合并**：一张图上下两排子图之间的间隔（实测 p7 约 14pt）会把它切成两组，
      不合并就只裁到上半张；合并只发生在**两组都被模型框认领、且间隔 ≤ 20pt** 时，
      所以不会把两张不同的图并成一张（p8 的 Figure 2 与 Figure 3 相隔 29pt）。
    """
    page_area = max(1.0, ink.page_rect.area)
    accepted: list[list[InkBand]] = []
    for bands in _group_bands(ink.bands()):
        if is_text_group(bands):
            continue
        rect = _rect_of(bands)
        if rect.area < MIN_GROUP_AREA_RATIO * page_area:
            continue
        inter = _overlap_area(rect, seed)
        if inter < SEED_MIN_COVER * min(seed.area, rect.area):
            continue
        accepted.append(bands)

    merged: list[list[InkBand]] = []
    for bands in accepted:                                   # 已按 y 升序
        if merged and _rect_of(bands).y0 - _rect_of(merged[-1]).y1 <= MERGE_GAP:
            merged[-1] = merged[-1] + bands
        else:
            merged.append(bands)

    best: tuple[float, list[InkBand]] | None = None
    for bands in merged:
        rect = _rect_of(bands)
        if rect.height > MAX_FIGURE_RATIO * ink.page_rect.height:
            continue
        inter = _overlap_area(rect, seed)
        if best is None or inter > best[0]:
            best = (inter, bands)
    if best is None:
        return None
    return _rect_of(best[1]), best[1]


def caption_cut_band(bands: list[InkBand], caption_box: Rect | None,
                     window: Rect | None = None) -> float | None:
    """模型说图注在这儿 → 返回应该切下去的那条**图注段**的上沿（pt），否则 ``None``。

    两条判据缺一不可（都是实测踩出来的）：

    1. **只认短行带**：图注框落在高大图体带里说明模型框偏了十几点（p8 的 Figure 2
       就是这样），此时返回 ``None`` 不切 —— 切掉图比多留一行图注糟糕得多；
    2. **要么是多行成段、要么尾部很薄**：多行左对齐、行距相近的连续短带 = 图注段
       （p8 的 Figure 3 是 3~4 行，整段切掉）；如果只匹配到孤零零一行、且它下面还压着
       一百多点图体（p7 实测：模型把子图标题 ``(b) TURBOQUANT_mse`` 当成了图注），
       那就不能切，否则会把下半张图裁掉。
    """
    if caption_box is None:
        return None
    idx = [i for i, b in enumerate(bands)
           if b.height <= CAPTION_BAND_MAX_H
           and b.y1 > caption_box.y0 and b.y0 < caption_box.y1]
    if not idx:
        return None
    first, last = idx[0], idx[-1]
    # 往上并进同一段图注的其余行：左对齐、行距相近、行高相近
    while first > 0:
        prev, cur = bands[first - 1], bands[first]
        if (prev.height <= CAPTION_BAND_MAX_H
                and 0.0 <= cur.y0 - prev.y1 <= 6.0
                and abs(prev.x0 - cur.x0) <= 12.0
                and abs(prev.height - cur.height) <= 0.5 * cur.height):
            first -= 1
        else:
            break
    top = bands[first].y0
    lines = last - first + 1
    if lines >= 2:
        return top
    if window is not None and window.y1 - top <= 2.0 * CAPTION_BAND_MAX_H:
        return top                       # 只有一行，但尾巴确实很薄 → 还是切
    return None


def locate_figure(doc, page_no: int, page_rect: Rect, seed: Rect,
                  caption_box: Rect | None, *, ink: PageInk | None = None,
                  pad: float = 1.5) -> Rect | None:
    """一步到位：模型框 → 像素窗口 → 切掉图注段 → 四边贴墨迹。"""
    probe = ink if ink is not None else PageInk(doc, page_no, page_rect)
    found = figure_window(probe, seed)
    if found is None:
        return None
    win, bands = found
    cut = caption_cut_band(bands, caption_box, win)
    if cut is not None and cut - win.y0 >= 2.0 * CAPTION_BAND_MAX_H:
        win = Rect(win.x0, win.y0, win.x1, cut)
    from backend.parsing.extract.render import ink_bbox

    try:
        tight = ink_bbox(doc, page_no, win, pad=pad)
    except Exception as exc:  # noqa: BLE001
        log.warning("p%d 墨迹贴边失败：%s", page_no, exc)
        tight = win
    return tight
