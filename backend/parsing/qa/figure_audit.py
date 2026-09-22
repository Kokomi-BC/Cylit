"""C3 插图体检（规划 §13.3）。

判据：
- ``edge_ink > 0.01`` → 裁到正文（裁剪边界压到了文字像素）
- 子图数不符（估计值与图注中 (a)(b)(c) 数量不一致）
- ``effective_dpi < 300`` → 内嵌位图分辨率不足（风险 R5）
- 长宽比偏差 > 2% → 裁剪区域形状与原图不符
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.parsing.models import Rect

EDGE_INK_THRESHOLD = 0.01
MIN_EFFECTIVE_DPI = 300.0
ASPECT_TOLERANCE = 0.02

_SUBFIG = re.compile(r"\(\s*[a-hA-H]\s*\)")


@dataclass
class FigureAudit:
    edge_ink: float = 0.0
    effective_dpi: float = 0.0
    aspect_ratio: float = 1.0
    aspect_reference: float = 1.0
    aspect_deviation: float = 0.0
    subfigure_count: int = 0
    caption_subfigure_count: int = 0
    status: str = "ok"
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "edge_ink": round(self.edge_ink, 5),
            "effective_dpi": round(self.effective_dpi, 1),
            "aspect_ratio": round(self.aspect_ratio, 4),
            "aspect_reference": round(self.aspect_reference, 4),
            "aspect_deviation": round(self.aspect_deviation, 4),
            "subfigure_count": self.subfigure_count,
            "caption_subfigure_count": self.caption_subfigure_count,
            "status": self.status, "issues": self.issues,
        }


def audit_figure_crop(
    image_path: str | Path,
    *,
    rect: Rect | None = None,
    effective_dpi: float = 0.0,
    subfigure_count: int = 0,
    caption: str = "",
    reference_aspect: float | None = None,
) -> FigureAudit:
    """对裁好的插图 PNG 做体检。"""
    audit = FigureAudit(effective_dpi=effective_dpi, subfigure_count=subfigure_count)
    path = Path(image_path)

    width = height = 0
    try:
        from PIL import Image

        with Image.open(path) as im:
            width, height = im.size
            audit.edge_ink = edge_ink_ratio(im)
    except Exception as exc:  # noqa: BLE001
        audit.issues.append(f"无法读取裁图：{exc}")
        audit.status = "error"
        return audit

    if width and height:
        audit.aspect_ratio = width / height
        if rect and rect.height > 0:
            audit.aspect_reference = rect.width / rect.height
        elif reference_aspect:
            audit.aspect_reference = reference_aspect
        if audit.aspect_reference:
            audit.aspect_deviation = abs(audit.aspect_ratio - audit.aspect_reference) \
                / audit.aspect_reference

    audit.caption_subfigure_count = len(_SUBFIG.findall(caption or ""))

    if audit.edge_ink > EDGE_INK_THRESHOLD:
        audit.issues.append(
            f"裁图边缘墨迹比例 {audit.edge_ink:.3f} > {EDGE_INK_THRESHOLD}，疑似裁到正文"
        )
    if audit.effective_dpi and audit.effective_dpi < MIN_EFFECTIVE_DPI:
        audit.issues.append(
            f"内嵌位图有效 dpi {audit.effective_dpi:.0f} < {MIN_EFFECTIVE_DPI:.0f}"
        )
    if audit.caption_subfigure_count and subfigure_count \
            and audit.caption_subfigure_count != subfigure_count:
        audit.issues.append(
            f"子图数不符：图注含 {audit.caption_subfigure_count} 个，估得 {subfigure_count} 个"
        )
    if audit.aspect_reference and audit.aspect_deviation > ASPECT_TOLERANCE:
        audit.issues.append(
            f"长宽比偏差 {audit.aspect_deviation * 100:.1f}% > {ASPECT_TOLERANCE * 100:.0f}%"
        )

    audit.status = "warn" if audit.issues else "ok"
    return audit


def edge_ink_ratio(im, band_ratio: float = 0.02, max_samples: int = 400) -> float:
    """边缘带内的"墨迹"像素比例（深色像素占比）。

    为控制耗时，边缘带按行列等步长抽样，抽样数上限 ``max_samples``。
    """
    try:
        gray = im.convert("L")
    except Exception:  # noqa: BLE001
        return 0.0
    w, h = gray.size
    if w < 8 or h < 8:
        return 0.0
    band = max(1, min(int(min(w, h) * band_ratio), min(w, h) // 4))
    pixels = gray.load()
    step = max(1, (2 * band * w + 2 * band * h) // max(1, max_samples * 2))
    dark = 0
    total = 0

    def probe(x: int, y: int) -> None:
        nonlocal dark, total
        if pixels[x, y] < 160:
            dark += 1
        total += 1

    for x in range(0, w, step):
        for y in range(0, band, max(1, step)):
            probe(x, y)
            probe(x, h - 1 - y)
    for y in range(0, h, step):
        for x in range(0, band, max(1, step)):
            probe(x, y)
            probe(w - 1 - x, y)
    return dark / max(1, total)


def audit_figure_pixels(image_path: str | Path, rect: Rect, dpi: int = 600) -> FigureAudit:
    """仅用像素做体检（不需要额外上下文）。"""
    return audit_figure_crop(image_path, rect=rect, effective_dpi=dpi)
