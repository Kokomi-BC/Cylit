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


def render_crop_png(doc: "fitz.Document", page_no: int, rect: Rect, dpi: int = 600,
                    padding: float = 2.0, max_pixels: int = 40_000_000) -> bytes:
    """局部高倍裁图（公式卡 / 复核三联屏用）。"""
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
