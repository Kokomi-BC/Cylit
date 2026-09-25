"""页面渲染：整页 PNG（复核与降级用）+ 局部高倍裁图（公式/插图）。

规划 §13.1 ``render_pages(pdf, dpi=150)``；规划 §14.1 ③ 位图/页面裁图。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from backend.core.logging import get_logger
from backend.core.utils import write_bytes_atomic
from backend.parsing.models import Rect

log = get_logger(__name__)


def render_page_png(doc: "fitz.Document", page_no: int, dpi: int = 150) -> bytes:
    page = doc[page_no - 1]
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return pix.tobytes("png")


def ink_bbox(doc: "fitz.Document", page_no: int, rect: Rect, *, dpi: int = 110,
             pad: float = 2.0, threshold: int = 250) -> Rect:
    """量出 ``rect`` 内**真实墨迹**的范围（去掉四周白边），失败时原样返回。

    插图裁图专用：按 bbox 裁出来的图四周常有一圈白边（TurboQuant p8 右侧近 40% 是空白），
    先低倍灰度渲染量出非白像素的包围盒，再按它出图 —— 白边没了，图在栏里也更大。

    实现用 ``Pixmap.samples`` 自己扫一遍：PyMuPDF 1.28 已经删掉了 ``Pixmap.get_bbox``
    （老写法会 AttributeError），而 ``samples`` 是稳定的公开属性。
    返回值保证落在 ``rect`` 之内、且不小于 4pt，因此调用方可以无条件采用。
    """
    page = doc[page_no - 1]
    clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1) & page.rect
    if clip.is_empty or clip.width < 2 or clip.height < 2:
        return rect
    zoom = dpi / 72.0
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip,
                              colorspace=fitz.csGRAY, alpha=False)
        box = _ink_pixel_bbox(pix, threshold)
    except Exception as exc:  # noqa: BLE001
        log.warning("墨迹裁白失败（p%d）：%s", page_no, exc)
        return rect
    if box is None:
        return rect
    px0, py0, px1, py1 = box
    sx = clip.width / max(1, pix.width)
    sy = clip.height / max(1, pix.height)
    out = fitz.Rect(clip.x0 + px0 * sx - pad, clip.y0 + py0 * sy - pad,
                    clip.x0 + px1 * sx + pad, clip.y0 + py1 * sy + pad)
    out = out & clip
    if out.is_empty or out.width < 4 or out.height < 4:
        return rect
    return Rect(out.x0, out.y0, out.x1, out.y1)


def _ink_pixel_bbox(pix, threshold: int) -> tuple[int, int, int, int] | None:
    """灰度 Pixmap 里非白像素的像素级包围盒（``None`` = 整幅全白）。"""
    import numpy as np

    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    arr = arr.reshape(pix.height, pix.stride)[:, :pix.width]
    mask = arr < threshold
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if not len(rows) or not len(cols):
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def ink_fraction(doc: "fitz.Document", page_no: int, rect: Rect, *, dpi: int = 110,
                 threshold: int = 250) -> float:
    """``rect`` 内非白像素占比（0~1）；渲染失败返回 ``-1.0``。

    用途：:meth:`ink_bbox` 在**整幅全白**时会原样返回入参，所以"磨迹收紧成功"不等于
    "这里有内容"。扫描件靠视觉模型给粗框，就必须能识别出"模型把框画在空白处"这种提案。
    """
    page = doc[page_no - 1]
    clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1) & page.rect
    if clip.is_empty or clip.width < 2 or clip.height < 2:
        return -1.0
    try:
        import numpy as np

        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip,
                              colorspace=fitz.csGRAY, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8)
        arr = arr.reshape(pix.height, pix.stride)[:, :pix.width]
        return float((arr < threshold).mean())
    except Exception as exc:  # noqa: BLE001
        log.warning("墨迹占比量测失败（p%d）：%s", page_no, exc)
        return -1.0


def _content_span(pix, threshold: int) -> tuple[int, int]:
    """渲染结果的行方向内容范围，用来去掉图注图上下多余的整行白边。"""
    box = _ink_pixel_bbox(pix, threshold)
    return (box[1], box[3]) if box else (0, pix.height)


def figure_card_png(doc: "fitz.Document", page_no: int, rect: Rect, caption_rects: list[Rect],
                    *, dpi: int = 600, margin: int = 12, gap: int = 18,
                    max_pixels: int = 40_000_000) -> bytes:
    """把插图本体与它的图注合成**一张** PNG（图在上、图注在下，白底）。

    为什么要它：阅读器正文里图注本来就有自己的一行，所以插图裁图不带图注；
    但**导出/放大看单张图**时缺了图注就不知道这图画的是什么。这里把两者拼成一张"图表卡片"，
    图注直接用 PDF 里的文字渲染（不做 OCR、不改写），因此和论文原样一致。

    ``caption_rects`` 可能有多个矩形（图注跨栏/多段时），按给定顺序竖排。
    """
    import io

    from PIL import Image

    page = doc[page_no - 1]
    fig_clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1) & page.rect
    if fig_clip.is_empty or fig_clip.width < 2 or fig_clip.height < 2:
        return render_crop_png(doc, page_no, rect, dpi=dpi, padding=0.0)

    def render_region(clip: "fitz.Rect") -> "Image.Image":
        zoom = dpi / 72.0
        est = (clip.width * zoom) * (clip.height * zoom)
        if est > max_pixels:                            # 防超大图：按面积上限回落
            zoom *= (max_pixels / est) ** 0.5
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip,
                              colorspace=fitz.csRGB, alpha=False)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as im:
            return im.convert("RGB")

    # 图体：沿用"墨迹裁白"，把 bbox 自带的白边去掉
    body = ink_bbox(doc, page_no, rect, pad=0.0)
    im_body = render_region(fitz.Rect(body.x0, body.y0, body.x1, body.y1) & page.rect)

    im_caps: list["Image.Image"] = []
    for cap in caption_rects:
        clip = fitz.Rect(cap.x0, cap.y0, cap.x1, cap.y1) & page.rect
        if clip.is_empty or clip.width < 2 or clip.height < 2:
            continue
        # 图注也是文字：先用 96dpi 量出真实墨迹行范围，免得裁出一条"半行字"
        try:
            probe = page.get_pixmap(matrix=fitz.Matrix(96 / 72.0, 96 / 72.0), clip=clip,
                                    colorspace=fitz.csGRAY, alpha=False)
            top, bottom = _content_span(probe, 250)
            sy = clip.height / max(1, probe.height)
            clip = fitz.Rect(clip.x0, clip.y0 + top * sy - 1.0,
                             clip.x1, clip.y0 + bottom * sy + 1.0) & page.rect
        except Exception as exc:  # noqa: BLE001
            log.warning("图注墨迹量测失败（p%d）：%s", page_no, exc)
        im_caps.append(render_region(clip))

    if not im_caps:
        buf = io.BytesIO()
        im_body.save(buf, format="PNG")
        return buf.getvalue()

    width = max([im_body.width] + [im.width for im in im_caps]) + 2 * margin
    height = im_body.height + 2 * margin + sum(im.height for im in im_caps) + gap * len(im_caps)
    canvas = Image.new("RGB", (max(1, width), max(1, height)), "white")
    y = margin
    canvas.paste(im_body, ((width - im_body.width) // 2, y))
    y += im_body.height + gap
    for im in im_caps:
        canvas.paste(im, ((width - im.width) // 2, y))
        y += im.height + gap
    canvas = canvas.crop((0, 0, width, max(1, y - gap + margin)))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_crop_png(doc: "fitz.Document", page_no: int, rect: Rect, dpi: int = 600,
                    padding: float = 2.0, max_pixels: int = 40_000_000,
                    ink_trim: bool = False) -> bytes:
    """局部高倍裁图（公式卡 / 插图裁图用）。

    ``ink_trim=True`` 时先量真实墨迹范围（见 :func:`ink_bbox`）再出图，把 bbox 自带的白边裁掉。
    """
    if ink_trim:
        probed = ink_bbox(doc, page_no, rect, pad=max(padding, 1.5))
        return render_crop_png(doc, page_no, probed, dpi=dpi, padding=0.0,
                               max_pixels=max_pixels)
    page = doc[page_no - 1]
    clip = fitz.Rect(rect.x0 - padding, rect.y0 - padding,
                     rect.x1 + padding, rect.y1 + padding)
    clip = clip & page.rect  # 与页面求交，防越界报错
    if clip.is_empty:
        clip = page.rect
    zoom = dpi / 72.0
    # 防超大图：按面积上限回落 dpi
    est = (clip.width * zoom) * (clip.height * zoom)
    if est > max_pixels:
        zoom *= (max_pixels / est) ** 0.5
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    return pix.tobytes("png")


def render_pages(doc: "fitz.Document", out_dir: str | Path, dpi: int = 150,
                 page_numbers: list[int] | None = None,
                 on_progress=None) -> dict[int, Path]:
    """逐页渲染 PNG 到 ``out_dir/page-XXXX.png``。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    nums = page_numbers or list(range(1, doc.page_count + 1))
    result: dict[int, Path] = {}
    for i, page_no in enumerate(nums, 1):
        if page_no < 1 or page_no > doc.page_count:
            continue
        target = out / f"page-{page_no:04d}.png"
        data = render_page_png(doc, page_no, dpi=dpi)
        write_bytes_atomic(target, data)
        result[page_no] = target
        if on_progress:
            on_progress(i, len(nums), page_no)
    return result


def save_crop(doc: "fitz.Document", page_no: int, rect: Rect, out_path: str | Path,
              dpi: int = 600) -> Path:
    data = render_crop_png(doc, page_no, rect, dpi=dpi)
    return write_bytes_atomic(out_path, data)


def pdf_page_sizes(doc: "fitz.Document") -> list[tuple[float, float, int]]:
    out = []
    for page in doc:
        r = page.rect
        out.append((round(r.width, 2), round(r.height, 2), int(page.rotation)))
    return out
