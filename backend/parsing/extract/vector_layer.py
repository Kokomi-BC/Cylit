"""矢量层提取：线段 / 矩形路径（规划 §13.1）。

这是**判根号与分式线的关键输入**（规划 §13.3 C1）。PyMuPDF 的
``page.get_drawings()`` 返回路径集合，我们把每条路径规整为
``Drawing``：类型、bbox、端点、是否水平/垂直、线宽、填充情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import pymupdf as fitz

from backend.parsing.models import Rect


@dataclass
class Drawing:
    """一条矢量路径的规整表示。"""

    kind: str                 # line | rect | quad | curve | other
    rect: Rect
    p0: tuple[float, float] = (0.0, 0.0)
    p1: tuple[float, float] = (0.0, 0.0)
    width: float = 1.0
    stroke: bool = False
    fill: bool = False
    fill_opacity: float = 1.0
    stroke_opacity: float = 1.0
    items_count: int = 0

    @property
    def is_horizontal(self) -> bool:
        return abs(self.p1[1] - self.p0[1]) <= 1.5 and abs(self.p1[0] - self.p0[0]) > 1.0

    @property
    def is_vertical(self) -> bool:
        return abs(self.p1[0] - self.p0[0]) <= 1.5 and abs(self.p1[1] - self.p0[1]) > 1.0

    @property
    def length(self) -> float:
        dx = self.p1[0] - self.p0[0]
        dy = self.p1[1] - self.p0[1]
        return (dx * dx + dy * dy) ** 0.5

    @property
    def y(self) -> float:
        return (self.p0[1] + self.p1[1]) / 2

    @property
    def x0(self) -> float:
        return min(self.p0[0], self.p1[0])

    @property
    def x1(self) -> float:
        return max(self.p0[0], self.p1[0])

    def is_thin_bar(self, max_thickness: float = 2.0) -> bool:
        """是否是"细横杠"（根号横杠 / 分式线候选）。"""
        return (
            self.stroke and self.is_horizontal
            and abs(self.p1[1] - self.p0[1]) <= max_thickness
            and self.rect.height <= max_thickness * 2
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "bbox": self.rect.to_list(),
            "p0": [round(self.p0[0], 2), round(self.p0[1], 2)],
            "p1": [round(self.p1[0], 2), round(self.p1[1], 2)],
            "width": round(self.width, 3), "stroke": self.stroke, "fill": self.fill,
        }


def _iter_items(path: dict) -> Iterator[tuple[str, tuple]]:
    for item in path.get("items", []) or []:
        if not item:
            continue
        op = item[0]
        if op == "l" and len(item) >= 3:
            yield "line", (item[1], item[2])
        elif op == "re" and len(item) >= 2:
            yield "rect", (item[1],)
        elif op == "qu" and len(item) >= 2:
            yield "quad", (item[1],)
        elif op == "c" and len(item) >= 5:
            yield "curve", (item[1], item[4])
        else:
            yield "other", ()


def _drawing_from_path(path: dict) -> Drawing | None:
    r = path.get("rect")
    if r is None:
        return None
    rect = Rect(float(r.x0), float(r.y0), float(r.x1), float(r.y1))
    width = float(path.get("width", 0.0) or (path.get("w", 0.0) or 0.0))
    stroke = bool(path.get("color") is not None or path.get("stroke_opacity", 1) not in (None, 0))
    fill = bool(path.get("fill") is not None)

    items = list(_iter_items(path))
    kind = "other"
    p0 = (rect.x0, rect.y0)
    p1 = (rect.x1, rect.y1)
    if len(items) == 1:
        k, payload = items[0]
        kind = k
        if k == "line":
            a, b = payload
            p0 = (float(a.x), float(a.y))
            p1 = (float(b.x), float(b.y))
        elif k == "curve":
            a, b = payload
            p0 = (float(a.x), float(a.y))
            p1 = (float(b.x), float(b.y))
    elif len(items) == 0:
        kind = "rect"
    else:
        kinds = {k for k, _ in items}
        kind = kinds.pop() if len(kinds) == 1 else "other"
        if kind == "line":
            pts = [payload for k, payload in items if k == "line"]
            p0 = (float(pts[0][0].x), float(pts[0][0].y))
            p1 = (float(pts[-1][1].x), float(pts[-1][1].y))

    if width <= 0 and stroke:
        width = 0.5

    return Drawing(
        kind=kind, rect=rect, p0=p0, p1=p1, width=width,
        stroke=stroke, fill=fill,
        fill_opacity=float(path.get("fill_opacity", 1.0) or 1.0),
        stroke_opacity=float(path.get("stroke_opacity", 1.0) or 1.0),
        items_count=len(items),
    )


def extract_drawings(page: "fitz.Page") -> list[Drawing]:
    out: list[Drawing] = []
    try:
        raw = page.get_drawings()
    except Exception as exc:  # noqa: BLE001
        return out
    for path in raw:
        try:
            d = _drawing_from_path(path)
        except Exception:  # noqa: BLE001
            continue
        if d is not None:
            out.append(d)
    return out


def horizontal_bars(drawings: list[Drawing], max_thickness: float = 2.0) -> list[Drawing]:
    """筛出细横杠候选（规划 §13.3 C1 步骤 1）。"""
    return [d for d in drawings if d.is_thin_bar(max_thickness)]
