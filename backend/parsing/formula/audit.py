"""矢量层公式审计（规划 §13.3 C1）—— 判根号范围与分式线。

判据强度：**矢量几何 > 一致性约束 > 文本层字面**；三者冲突 → grade C。

算法严格按规划实现：

```
1) 过滤水平短线：|y1-y0| < 2pt 且长度 ∈ [0.5×字高, 3×字高]
2) 每条线 L：取 x 重叠且位于 L 下方 0~1.2×字高的 span 集合 S
3) 判定：
     横杠 + 下方 span 数 == 1 → \\sqrt{X}
     横杠 + 下方 span 数 > 1  → \\sqrt{X_1 X_2 …}（记录被覆盖集合）
     线上下均有 span 且左右留白 → 分式线（上=分子，下=分母）
4) 输出 audit：{kind, bar_bbox, covered[], excluded[], grade}
```
"""

from __future__ import annotations

from backend.parsing.extract.vector_layer import Drawing
from backend.parsing.models import (
    FractionLine, RadicalBar, Rect, ScriptGroup, Span, VectorAudit,
)

# 规划给定常数
BAR_MAX_THICKNESS = 2.0          # pt
BAR_LEN_MIN_FACTOR = 0.5         # × 字高
BAR_LEN_MAX_FACTOR = 3.0         # × 字高
BELOW_MIN_FACTOR = 0.0           # 下方起始（相对字高）
BELOW_MAX_FACTOR = 1.2           # 下方结束


def find_horizontal_bars(drawings: list[Drawing], font_height: float) -> list[Drawing]:
    """步骤 1：过滤水平短线。"""
    h = max(4.0, font_height)
    lo, hi = BAR_LEN_MIN_FACTOR * h, BAR_LEN_MAX_FACTOR * h
    out: list[Drawing] = []
    for d in drawings:
        if not d.stroke:
            continue
        dy = abs(d.p1[1] - d.p0[1])
        if dy >= BAR_MAX_THICKNESS:
            continue
        if not (lo <= d.length <= hi):
            continue
        if d.rect.width < d.rect.height * 2:
            continue
        out.append(d)
    return out


def _spans_below(bar: Drawing, spans: list[Span], font_height: float) -> list[Span]:
    """步骤 2：x 重叠、位于 L 下方 0~1.2×字高的 span。"""
    h = max(4.0, font_height)
    y_lo = bar.y + BELOW_MIN_FACTOR * h
    y_hi = bar.y + BELOW_MAX_FACTOR * h
    out = []
    for s in spans:
        if s.y0 > y_hi or s.y1 < y_lo:
            continue
        # x 重叠
        if min(s.x1, bar.x1) - max(s.x0, bar.x0) <= 0.5:
            continue
        out.append(s)
    return out


def _spans_above(bar: Drawing, spans: list[Span], font_height: float) -> list[Span]:
    h = max(4.0, font_height)
    y_lo = bar.y - BELOW_MAX_FACTOR * h
    y_hi = bar.y - 0.05 * h
    out = []
    for s in spans:
        if s.y0 > y_hi or s.y1 < y_lo:
            continue
        if min(s.x1, bar.x1) - max(s.x0, bar.x0) <= 0.5:
            continue
        out.append(s)
    return out


def _side_whitespace(bar: Drawing, spans: list[Span], font_height: float) -> tuple[bool, bool]:
    """分式线判据：左右留白（两侧在同高度带上没有紧邻字符）。"""
    h = max(4.0, font_height)
    left_free = True
    right_free = True
    for s in spans:
        if abs(((s.y0 + s.y1) / 2) - bar.y) > 0.7 * h:
            continue
        if 0 < bar.x0 - s.x1 <= 0.6 * h:
            left_free = False
        if 0 < s.x0 - bar.x1 <= 0.6 * h:
            right_free = False
    return left_free, right_free


def _spans_to_text(spans: list[Span]) -> str:
    return "".join(s.text for s in sorted(spans, key=lambda s: s.x0)).strip()


def audit_formula_vectors(
    drawings: list[Drawing],
    spans: list[Span],
    rect: Rect,
    font_height: float | None = None,
) -> VectorAudit:
    """主入口：对公式区域做矢量层审计。"""
    fh = font_height or (max((s.height for s in spans), default=10.0))
    audit = VectorAudit()

    local_spans = [s for s in spans if rect.intersects(s.rect) or rect.overlap_ratio(s.rect) > 0.4]
    if not local_spans:
        local_spans = spans
    bars = find_horizontal_bars(drawings, fh)

    if not bars:
        audit.kind = "plain"
        audit.grade_hint = "A"
        audit.notes.append("无矢量横杠，无结构性歧义")
        return audit

    for bar in bars:
        below = _spans_below(bar, local_spans, fh)
        above = _spans_above(bar, local_spans, fh)
        left_free, right_free = _side_whitespace(bar, local_spans, fh)

        # 分式线：线上线下均有 span
        if above and below:
            audit.fraction_lines.append(FractionLine(
                y=round(bar.y, 2), x0=round(bar.x0, 2), x1=round(bar.x1, 2),
                numerator_glyphs=list(_spans_to_text(above))[:24],
                denominator_glyphs=list(_spans_to_text(below))[:24],
            ))
            continue

        # 根号横杠：线下方有 span，线上方无
        if below and not above:
            covered = [s.text for s in sorted(below, key=lambda s: s.x0)]
            # 被排除者：同高度带内但 x 不重叠的 span
            covered_ids = {id(s) for s in below}
            excluded = [s.text for s in local_spans
                        if id(s) not in covered_ids
                        and abs(((s.y0 + s.y1) / 2) - bar.y) <= BELOW_MAX_FACTOR * fh
                        and (s.x1 <= bar.x0 + 0.5 or s.x0 >= bar.x1 - 0.5)]
            audit.radical_bars.append(RadicalBar(
                bar_bbox=bar.rect.to_list(),
                covered_glyphs=covered,
                excluded=excluded,
                length=round(bar.length, 2),
            ))
            continue

        # 没有下方字符：可能是装饰线或误检
        if not left_free or not right_free:
            audit.notes.append(f"横杠 y={bar.y:.1f} 未匹配到覆盖字符，忽略")

    if audit.radical_bars and audit.fraction_lines:
        audit.kind = "radical_fraction"
        audit.grade_hint = "C"
    elif audit.radical_bars:
        audit.kind = "radical"
        audit.grade_hint = "B"
    elif audit.fraction_lines:
        audit.kind = "fraction"
        audit.grade_hint = "B"
    else:
        audit.kind = "plain"
        audit.grade_hint = "A"

    audit.scripts = detect_scripts(local_spans, fh)
    if len(audit.radical_bars) >= 2 and audit.fraction_lines:
        audit.grade_hint = "C"
        audit.notes.append("根号与分式线混合，结构歧义风险高")
    return audit


def detect_scripts(spans: list[Span], font_height: float, size_ratio: float = 0.82
                   ) -> list[ScriptGroup]:
    """上下标分组：字号明显更小且纵向偏移的 span 归到最近的基础字符。"""
    if not spans:
        return []
    base_sizes = [s.size for s in spans if s.size > 0]
    if not base_sizes:
        return []
    base_size = max(set([round(x, 1) for x in base_sizes]), key=base_sizes.count)
    ordered = sorted(spans, key=lambda s: s.x0)
    groups: list[ScriptGroup] = []
    for i, s in enumerate(ordered):
        if s.size <= 0 or s.size > base_size * size_ratio:
            continue
        # 找左侧最近的基础 span
        base = None
        for j in range(i - 1, -1, -1):
            if ordered[j].size > base_size * size_ratio:
                base = ordered[j]
                break
        if base is None:
            continue
        is_sup = s.origin_y < base.origin_y - 0.1 * font_height
        is_sub = s.origin_y > base.origin_y + 0.1 * font_height
        if not (is_sup or is_sub):
            continue
        text = s.text.strip()
        if not text:
            continue
        existing = next((g for g in groups if g.base == base.text), None)
        if existing is None:
            existing = ScriptGroup(base=base.text, bbox=base.rect.to_list())
            groups.append(existing)
        if is_sup and not existing.sup:
            existing.sup = text
        elif is_sub and not existing.sub:
            existing.sub = text
    return groups
