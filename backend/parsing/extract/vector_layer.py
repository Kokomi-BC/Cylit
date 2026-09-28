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
    fill_rgb: tuple[float, float, float] | None = None
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

    def is_invisible_fill(self, min_channel: float = 0.97) -> bool:
        """是不是"**纯白填充、没有描边**"的背景板（不是墨迹）。

        实测（2026-09-28，sniffer p4/p8）：排版工具给每张插图垫了一块**白色矩形**
        （``type=f``、``fill=(1,1,1)``、无 ``color`` —— 白底白纸，肉眼完全看不见）：

            p4  [28.2, 62.8, 563.3, 317.5]（图 2 那整幅） / [17.9, 354.9, 428.3, 550.4]（图 3）
            p8  [1.9, 37.4, 483.8, 266.8]（盖住图 7 + 图 9 + 图 8 的上半）

        它比图大得多、又横跨整页，被算进"图体墨迹并集"后：图 3 的 bbox 从本栏
        （x ≤ 286）涨到 x 431、图 7/8/9 的 bbox 全都变成通栏（x −1.1 ~ 547.6）
        —— 用户截图里"一张卡片里左右两栏两张图拼在一起、图里夹着正文"。
        判据只认"填充色接近纯白 + 无描边 + 不透明"：彩色背景块、带边框的白块（描边单独一条路径）
        都不受影响，照样当墨迹。
        """
        if not self.fill or self.stroke or self.fill_rgb is None:
            return False
        if self.fill_opacity < 0.9:
            return False
        return all(c >= min_channel for c in self.fill_rgb)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "bbox": self.rect.to_list(),
            "p0": [round(self.p0[0], 2), round(self.p0[1], 2)],
            "p1": [round(self.p1[0], 2), round(self.p1[1], 2)],
            "width": round(self.width, 3), "stroke": self.stroke, "fill": self.fill,
            "fill_rgb": list(self.fill_rgb) if self.fill_rgb else None,
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
    raw_fill = path.get("fill")
    # 填充色要留着：纯白填充 + 无描边 = 背景板，不是墨迹（见 `Drawing.is_invisible_fill`）
    fill_rgb = (tuple(float(c) for c in raw_fill[:3]) if isinstance(raw_fill, (tuple, list))
                and len(raw_fill) >= 3 else None)

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
        stroke=stroke, fill=fill, fill_rgb=fill_rgb,
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
