"""位图层提取：内嵌图清单（页码 / bbox / 像素 / 哈希，规划 §13.1）。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pymupdf as fitz

from backend.parsing.models import Rect


@dataclass
class ImageInfo:
    page_no: int
    xref: int
    rect: Rect
    width_px: int = 0
    height_px: int = 0
    colorspace: str = ""
    bpc: int = 0
    ext: str = ""
    digest: str = ""
    effective_dpi: float = 0.0
    is_small: bool = False

    def to_dict(self) -> dict:
        return {
            "page_no": self.page_no, "xref": self.xref, "bbox": self.rect.to_list(),
            "width_px": self.width_px, "height_px": self.height_px,
            "colorspace": self.colorspace, "bpc": self.bpc, "ext": self.ext,
            "digest": self.digest, "effective_dpi": self.effective_dpi,
        }


def _effective_dpi(width_px: int, rect: Rect) -> float:
    """内嵌位图的有效 dpi（风险 R5：内嵌分辨率不足）。"""
    if rect.width <= 0 or rect.height <= 0:
        return 0.0
    dpi_x = width_px / (rect.width / 72.0)
    return round(dpi_x, 1)


def extract_images(doc: "fitz.Document", page_numbers: list[int] | None = None
                   ) -> dict[int, list[ImageInfo]]:
    out: dict[int, list[ImageInfo]] = {}
    nums = page_numbers or list(range(1, doc.page_count + 1))
    for page_no in nums:
        if page_no < 1 or page_no > doc.page_count:
            continue
        page = doc[page_no - 1]
        infos: list[ImageInfo] = []
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox", (0, 0, 0, 0))
            rect = Rect(*[float(v) for v in bbox])
            if rect.area <= 0:
                continue
            xref = int(info.get("xref", 0) or 0)
            w = int(info.get("width", 0) or 0)
            h = int(info.get("height", 0) or 0)
            cs = info.get("colorspace")
            infos.append(ImageInfo(
                page_no=page_no, xref=xref, rect=rect, width_px=w, height_px=h,
                colorspace=str(cs) if cs is not None else "",
                bpc=int(info.get("bpc", 0) or 0),
                ext=str(info.get("ext", "") or ""),
                digest=str(info.get("digest", "") or ""),
                effective_dpi=_effective_dpi(w, rect),
                is_small=(w < 40 or h < 40),
            ))
        out[page_no] = infos
    return out


def extract_image_bytes(doc: "fitz.Document", xref: int) -> bytes | None:
    try:
        img = doc.extract_image(xref)
    except Exception:  # noqa: BLE001
        return None
    return img.get("image") if img else None


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()
