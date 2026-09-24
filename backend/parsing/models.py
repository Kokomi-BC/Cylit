"""PDF 解析的数据结构（extract / structure / formula / qa / markdown 之间的契约）。

坐标系统一使用 PyMuPDF 的页面坐标（pt，原点左上，y 向下）。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from backend.core.utils import json_dumps, json_loads, normalize_text, sha256_text

# --------------------------------------------------------------------------
# 几何
# --------------------------------------------------------------------------

# 数学字体关键字（公式块识别证据之一，规划 §13.2）
MATH_FONT_HINTS = (
    "cmmi", "cmsy", "cmex", "cmsy", "msam", "msbm", "math", "stix",
    "symbol", "euclid", "mtmi", "mtsy", "mtextra", "cambria math",
)
# 常见等宽/正文字体（用于排除）
SERIF_HINTS = ("times", "nimbus", "liberation", "dejavu", "arial", "helvetica",
               "georgia", "garamond", "palatino", "book", "roman", "cmr", "nimbusrom")

GREEK_RANGES = ((0x370, 0x3FF), (0x1D6A8, 0x1D7CB))
MATH_OPERATOR_CHARS = set("=+-×÷⋅·±∓≤≥≠≈∼≃≅≡∑∏∫∮√∞∂∇∈∉⊂⊆⊃⊇∪∩∧∨∀∃→←↔⇒⇔⊕⊗⊥∥|⟨⟩⌈⌉⌊⌋′″†‡*")


def is_greek(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in GREEK_RANGES)


def is_math_operator(ch: str) -> bool:
    return ch in MATH_OPERATOR_CHARS


@dataclass
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    def union(self, other: "Rect") -> "Rect":
        return Rect(min(self.x0, other.x0), min(self.y0, other.y0),
                    max(self.x1, other.x1), max(self.y1, other.y1))

    def expand(self, dx: float, dy: float | None = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def intersects(self, other: "Rect") -> bool:
        return not (self.x1 <= other.x0 or other.x1 <= self.x0
                    or self.y1 <= other.y0 or other.y1 <= self.y0)

    def overlap_ratio(self, other: "Rect") -> float:
        """自身与 other 的交集 / 自身面积。"""
        if self.area <= 0:
            return 0.0
        ix0, iy0 = max(self.x0, other.x0), max(self.y0, other.y0)
        ix1, iy1 = min(self.x1, other.x1), min(self.y1, other.y1)
        if ix1 <= ix0 or iy1 <= iy0:
            return 0.0
        return (ix1 - ix0) * (iy1 - iy0) / self.area

    def to_list(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]

    @staticmethod
    def from_list(v: Iterable[float] | None) -> "Rect | None":
        if not v:
            return None
        vals = list(v)
        if len(vals) != 4:
            return None
        return Rect(*(float(x) for x in vals))

    @staticmethod
    def from_any(v: Any) -> "Rect | None":
        if v is None:
            return None
        if isinstance(v, Rect):
            return v
        if isinstance(v, dict):
            return Rect(float(v.get("x0", 0)), float(v.get("y0", 0)),
                        float(v.get("x1", 0)), float(v.get("y1", 0)))
        return Rect.from_list(v)


def rects_span(rects: list[Rect]) -> Rect:
    r = Rect(math.inf, math.inf, -math.inf, -math.inf)
    for x in rects:
        r = Rect(min(r.x0, x.x0), min(r.y0, x.y0), max(r.x1, x.x1), max(r.y1, x.y1))
    if r.x0 is math.inf:
        return Rect(0, 0, 0, 0)
    return r


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------


@dataclass
class Span:
    """span 级最小单位（含 bbox 与字体，规划 §13.1）。"""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font: str = ""
    size: float = 0.0
    flags: int = 0
    color: int = 0
    origin_y: float = 0.0

    @property
    def rect(self) -> Rect:
        return Rect(self.x0, self.y0, self.x1, self.y1)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, self.size)

    @property
    def is_math_font(self) -> bool:
        f = (self.font or "").lower()
        return any(h in f for h in MATH_FONT_HINTS)

    @property
    def is_bold(self) -> bool:
        return bool(self.flags & 2 ** 4) or "bold" in (self.font or "").lower()

    @property
    def is_italic(self) -> bool:
        return bool(self.flags & 2 ** 1) or "italic" in (self.font or "").lower() \
            or "oblique" in (self.font or "").lower()

    @property
    def math_ratio(self) -> float:
        """该 span 中希腊字母 / 数学算符占比（公式块识别证据之一）。"""
        chars = [c for c in self.text if not c.isspace()]
        if not chars:
            return 0.0
        return sum(1 for c in chars if is_greek(c) or is_math_operator(c)) / len(chars)

    @property
    def style_key(self) -> tuple[str, int]:
        return (self.font or "", round(self.size, 1))

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Span":
        return Span(**{k: d.get(k, v) for k, v in Span(text="").to_dict().items()})


@dataclass
class TextLine:
    """一行文本（由 span 按基线聚合）。"""

    spans: list[Span] = field(default_factory=list)
    page_no: int = 0
    block_no: int = 0
    line_no: int = 0

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def rect(self) -> Rect:
        return rects_span([s.rect for s in self.spans])

    @property
    def size(self) -> float:
        if not self.spans:
            return 0.0
        return max(s.size for s in self.spans)

    @property
    def font(self) -> str:
        return self.spans[0].font if self.spans else ""

    @property
    def math_font_chars(self) -> int:
        return sum(len(s.text) for s in self.spans if s.is_math_font)

    @property
    def math_symbol_count(self) -> int:
        return sum(1 for s in self.spans for c in s.text
                   if is_greek(c) or is_math_operator(c))

    @property
    def visible_chars(self) -> int:
        return sum(1 for s in self.spans for c in s.text if not c.isspace())

    def to_dict(self) -> dict:
        return {"page_no": self.page_no, "block_no": self.block_no,
                "line_no": self.line_no, "spans": [s.to_dict() for s in self.spans]}


@dataclass
class RawBlock:
    """PyMuPDF 原始文本块（结构层输入）。"""

    page_no: int
    block_no: int
    lines: list[TextLine] = field(default_factory=list)
    kind: str = "text"   # text | image
    split_index: int = 0  # 跨栏切开后的子块序号（0 表示未切开）

    @property
    def rect(self) -> Rect:
        rs = [ln.rect for ln in self.lines if ln.spans]
        return rects_span(rs) if rs else Rect(0, 0, 0, 0)

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    @property
    def sizes(self) -> list[float]:
        return [ln.size for ln in self.lines if ln.spans]

    @property
    def dominant_size(self) -> float:
        vals = [round(s, 1) for s in self.sizes]
        if not vals:
            return 0.0
        return max(set(vals), key=vals.count)

    @property
    def dominant_font(self) -> str:
        fonts = [s.font for ln in self.lines for s in ln.spans]
        return max(set(fonts), key=fonts.count) if fonts else ""


@dataclass
class PageInfo:
    page_no: int
    width: float
    height: float
    rotation: int = 0
    columns: int = 1
    gutter_x: float | None = None
    image_path: str | None = None
    text_chars: int = 0
    block_count: int = 0
    has_text_layer: bool = True
    header_band: float = 0.0     # 页眉带底边 y
    footer_band: float = 0.0     # 页脚带顶边 y

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# 结构层输出
# --------------------------------------------------------------------------


@dataclass
class Block:
    """结构化 block（★ 全系统锚点，id 形如 ``p07-b12``）。"""

    id: str
    paper_id: str
    page_no: int
    order_in_page: int
    order_global: int
    type: str = "para"           # para|heading|caption|formula|figure|table|ref
    col: int = 0
    rect: Rect = field(default_factory=lambda: Rect(0, 0, 0, 0))
    text: str = ""               # 文本层原样
    text_clean: str = ""         # 规范化后
    source_text: str = ""        # 未处理源证据（公式字符合并用）
    source_kind: str = "text"
    font_names: list[str] = field(default_factory=list)
    font_size: float = 0.0
    line_count: int = 0
    char_count: int = 0
    content_hash: str = ""
    md_anchor: str = ""
    section_id: str | None = None
    chunk_id: str | None = None
    is_inline: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def finalize(self) -> "Block":
        self.char_count = len(self.text_clean or self.text or "")
        self.content_hash = sha256_text(self.text_clean or self.text or "")
        self.md_anchor = self.md_anchor or f"blk-{self.id}"
        return self

    def to_ir_dict(self) -> dict:
        return {
            "id": self.id, "paper_id": self.paper_id, "page_no": self.page_no,
            "order_in_page": self.order_in_page, "order_global": self.order_global,
            "type": self.type, "col": self.col,
            "x0": self.rect.x0, "y0": self.rect.y0, "x1": self.rect.x1, "y1": self.rect.y1,
            "text": self.text, "text_clean": self.text_clean,
            "source_text": self.source_text or self.text, "source_kind": self.source_kind,
            "font_names": json_dumps(self.font_names), "font_size": self.font_size,
            "line_count": self.line_count, "char_count": self.char_count,
            "content_hash": self.content_hash, "md_anchor": self.md_anchor,
            "section_id": self.section_id, "chunk_id": self.chunk_id,
            "is_inline": 1 if self.is_inline else 0,
            "meta_json": json_dumps(self.meta) if self.meta else None,
        }

    @staticmethod
    def from_ir_dict(d: dict) -> "Block":
        return Block(
            id=d["id"], paper_id=d.get("paper_id", ""), page_no=d.get("page_no", 0),
            order_in_page=d.get("order_in_page", 0), order_global=d.get("order_global", 0),
            type=d.get("type", "para"), col=d.get("col", 0),
            rect=Rect(d.get("x0", 0), d.get("y0", 0), d.get("x1", 0), d.get("y1", 0)),
            text=d.get("text") or "", text_clean=d.get("text_clean") or "",
            source_text=d.get("source_text") or "", source_kind=d.get("source_kind") or "text",
            font_names=json_loads(d.get("font_names"), []) or [],
            font_size=d.get("font_size") or 0.0,
            line_count=d.get("line_count") or 0, char_count=d.get("char_count") or 0,
            content_hash=d.get("content_hash") or "", md_anchor=d.get("md_anchor") or "",
            section_id=d.get("section_id"), chunk_id=d.get("chunk_id"),
            is_inline=bool(d.get("is_inline")), meta=json_loads(d.get("meta_json"), {}) or {},
        )


# --------------------------------------------------------------------------
# 公式（规划 §14.2 / §13.3 C1）
# --------------------------------------------------------------------------


@dataclass
class RadicalBar:
    """根号横杠审计结果。"""

    bar_bbox: list[float]
    covered_glyphs: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    length: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FractionLine:
    """分式线审计结果。"""

    y: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    numerator_glyphs: list[str] = field(default_factory=list)
    denominator_glyphs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScriptGroup:
    """上下标分组。"""

    base: str
    sup: str = ""
    sub: str = ""
    bbox: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VectorAudit:
    """矢量层审计输出（规划 §13.3 C1 的 audit 结构）。"""

    kind: str = "plain"          # plain|radical|fraction|radical_fraction
    radical_bars: list[RadicalBar] = field(default_factory=list)
    fraction_lines: list[FractionLine] = field(default_factory=list)
    scripts: list[ScriptGroup] = field(default_factory=list)
    brackets: list[list[float]] = field(default_factory=list)
    grade_hint: str = "B"
    notes: list[str] = field(default_factory=list)

    @property
    def has_structure(self) -> bool:
        return bool(self.radical_bars or self.fraction_lines)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "radical_bars": [r.to_dict() for r in self.radical_bars],
            "fraction_lines": [f.to_dict() for f in self.fraction_lines],
            "scripts": [s.to_dict() for s in self.scripts],
            "brackets": self.brackets,
            "grade_hint": self.grade_hint,
            "notes": self.notes,
        }

    @staticmethod
    def from_dict(d: dict | None) -> "VectorAudit":
        d = d or {}
        return VectorAudit(
            kind=d.get("kind", "plain"),
            radical_bars=[RadicalBar(**r) for r in d.get("radical_bars", []) or []],
            fraction_lines=[FractionLine(**f) for f in d.get("fraction_lines", []) or []],
            scripts=[ScriptGroup(**s) for s in d.get("scripts", []) or []],
            brackets=d.get("brackets", []) or [],
            grade_hint=d.get("grade_hint", "B"),
            notes=d.get("notes", []) or [],
        )


@dataclass
class FormulaBlock:
    """公式块完整生命周期（规划 §14.2）。"""

    block_id: str
    paper_id: str
    page_number: int
    rect: Rect
    raw_text: str = ""
    image_path: str | None = None
    vector_data: VectorAudit = field(default_factory=VectorAudit)
    latex_candidates: list[str] = field(default_factory=list)
    latex_repaired: str | None = None
    latex_final: str | None = None
    confidence: float = 0.0
    grade: str = "C"
    source: str = "text"
    status: str = "candidate"
    validation: dict[str, Any] = field(default_factory=dict)
    review_note: str | None = None
    is_inline: bool = False

    @property
    def latex_candidate(self) -> str:
        return self.latex_candidates[0] if self.latex_candidates else ""

    def to_ir_dict(self) -> dict:
        return {
            "block_id": self.block_id, "paper_id": self.paper_id,
            "page_number": self.page_number,
            "x0": self.rect.x0, "y0": self.rect.y0, "x1": self.rect.x1, "y1": self.rect.y1,
            "raw_text": self.raw_text, "image_path": self.image_path,
            "vector_data_json": json_dumps(self.vector_data.to_dict()),
            "latex_candidate": json_dumps(self.latex_candidates),
            "latex_repaired": self.latex_repaired, "latex_final": self.latex_final,
            "confidence": self.confidence, "grade": self.grade, "source": self.source,
            "status": self.status,
            "validation_json": json_dumps(self.validation) if self.validation else None,
            "review_note": self.review_note, "is_inline": 1 if self.is_inline else 0,
        }


# --------------------------------------------------------------------------
# 插图 / 表格
# --------------------------------------------------------------------------


@dataclass
class FigureBlock:
    block_id: str
    paper_id: str
    page_number: int
    rect: Rect
    image_path: str | None = None
    caption_block_id: str | None = None
    caption: str = ""
    width_px: int = 0
    height_px: int = 0
    effective_dpi: float = 0.0
    edge_ink: float = 0.0
    subfigure_count: int = 0
    aspect_ratio: float = 1.0
    audit: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    def to_ir_dict(self) -> dict:
        return {
            "block_id": self.block_id, "paper_id": self.paper_id,
            "page_number": self.page_number,
            "x0": self.rect.x0, "y0": self.rect.y0, "x1": self.rect.x1, "y1": self.rect.y1,
            "image_path": self.image_path, "caption_block_id": self.caption_block_id,
            "caption": self.caption, "width_px": self.width_px, "height_px": self.height_px,
            "effective_dpi": self.effective_dpi, "edge_ink": self.edge_ink,
            "subfigure_count": self.subfigure_count, "aspect_ratio": self.aspect_ratio,
            "audit_json": json_dumps(self.audit) if self.audit else None,
            "status": self.status,
        }


@dataclass
class TableBlock:
    block_id: str
    paper_id: str
    page_number: int
    rect: Rect
    caption_block_id: str | None = None
    caption: str = ""
    image_path: str | None = None
    row_count: int = 0
    col_count: int = 0
    has_borders: bool = False
    grid: list[float] = field(default_factory=list)
    status: str = "image_only"

    def to_ir_dict(self) -> dict:
        return {
            "block_id": self.block_id, "paper_id": self.paper_id,
            "page_number": self.page_number,
            "x0": self.rect.x0, "y0": self.rect.y0, "x1": self.rect.x1, "y1": self.rect.y1,
            "caption_block_id": self.caption_block_id, "caption": self.caption,
            "image_path": self.image_path, "row_count": self.row_count,
            "col_count": self.col_count, "has_borders": 1 if self.has_borders else 0,
            "grid_json": json_dumps(self.grid) if self.grid else None,
            "status": self.status,
        }


# --------------------------------------------------------------------------
# 解析总结果
# --------------------------------------------------------------------------


@dataclass
class ParseResult:
    paper_id: str
    title: str = ""
    authors: str = ""
    # 首页抽出来的摘要原文（元数据用；正文照旧走 block，别拿它当内容源）
    abstract: str = ""
    page_count: int = 0
    pages: list[PageInfo] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    formulas: list[FormulaBlock] = field(default_factory=list)
    figures: list[FigureBlock] = field(default_factory=list)
    tables: list[TableBlock] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        by_type: dict[str, int] = {}
        for b in self.blocks:
            by_type[b.type] = by_type.get(b.type, 0) + 1
        return {
            "pages": len(self.pages), "blocks": len(self.blocks),
            "formulas": len(self.formulas), "figures": len(self.figures),
            "tables": len(self.tables),
            **{f"blocks_{k}": v for k, v in by_type.items()},
        }

    def grade_distribution(self) -> dict[str, int]:
        dist = {"A": 0, "B": 0, "C": 0}
        for f in self.formulas:
            dist[f.grade] = dist.get(f.grade, 0) + 1
        return dist

    def block_by_id(self, block_id: str) -> Block | None:
        for b in self.blocks:
            if b.id == block_id:
                return b
        return None


def clean_block_text(text: str) -> str:
    return normalize_text(text)
