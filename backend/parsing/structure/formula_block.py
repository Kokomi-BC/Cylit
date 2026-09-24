"""公式块识别（规划 §13.2）。

三证据融合：
1. **数学字体 span**：字体名命中 CMMI/CMSY/STIX 等数学字体族；
2. **矢量笔画**：块内存在细横杠 / 根号笔画 / 密集短路径（矩阵括号等）；
3. **行内孤立符号密度**：希腊字母 + 数学算符占比高，且行较短、独立成行。

判定门槛（保守，宁可漏判也不误判正文）：
- 必须满足「数学字体 或 矢量笔画」二者之一；
- 且符号密度 ≥ 阈值；
- 且不与大量正文行混排（长行数少）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.parsing.extract.vector_layer import Drawing
from backend.parsing.formula.noise import is_noise_formula
from backend.parsing.models import (
    RawBlock, Rect, TextLine, is_greek, is_math_operator,
)

# 数学字体字符占比阈值
MATH_FONT_RATIO = 0.28
# 符号密度阈值
SYMBOL_DENSITY = 0.12
# 公式行最长字符数（超过则更像正文）
MAX_FORMULA_LINE_CHARS = 140
# 块内正文行占比上限
MAX_PROSE_LINE_RATIO = 0.45

# 常见英文功能词：出现 2 个以上基本可以确定是正文而不是公式
_PROSE_WORDS = {
    "the", "of", "and", "is", "are", "we", "in", "for", "to", "with", "that",
    "which", "this", "as", "by", "on", "be", "can", "from", "or", "an", "it",
    "our", "have", "has", "not", "but", "at", "all", "any", "if", "then",
    "than", "such", "also", "using", "used", "where", "when", "each", "more",
}


def _is_prose_line(text: str) -> bool:
    """粗判是否英文正文行（防止把含公式的正文行误判为公式块）。"""
    low = text.lower()
    hits = 0
    for w in low.replace(",", " ").replace(".", " ").replace("(", " ").split():
        if w in _PROSE_WORDS:
            hits += 1
            if hits >= 2:
                return True
    return False


def _word_ratio(text: str) -> float:
    """≥3 个字母的英文单词字符数占比（正文特征）。"""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    long_words = sum(len(w) for w in re.findall(r"[A-Za-z]{3,}", text))
    return long_words / len(chars)


@dataclass
class FormulaCandidate:
    """公式区域候选（**非最终结果**，规划 §14.3 候选生成）。"""

    page_no: int
    block_no: int
    rect: Rect
    lines: list[TextLine]
    raw_text: str
    math_font_chars: int = 0
    total_chars: int = 0
    symbol_count: int = 0
    vector_hits: int = 0
    score: float = 0.0
    reason: str = ""
    is_inline: bool = False

    @property
    def symbol_density(self) -> float:
        return self.symbol_count / max(1, self.total_chars)

    @property
    def math_font_ratio(self) -> float:
        return self.math_font_chars / max(1, self.total_chars)


def _line_symbol_stats(line: TextLine) -> tuple[int, int, int]:
    """返回 ``(符号数, 总字符数, 数学字体字符数)``。"""
    total = 0
    sym = 0
    math_font = 0
    for span in line.spans:
        for ch in span.text:
            if ch.isspace():
                continue
            total += 1
            if span.is_math_font:
                math_font += 1
            if is_greek(ch) or is_math_operator(ch):
                sym += 1
        if span.is_math_font:
            math_font += sum(1 for c in span.text if c.isspace())
    return sym, total, math_font


def _line_is_formula_like(line: TextLine) -> tuple[bool, float, str]:
    sym, total, math_font = _line_symbol_stats(line)
    if total < 2:
        return False, 0.0, ""
    density = sym / total
    font_ratio = math_font / total
    text = line.text.strip()
    # 纯脚注符号（`∗†`）符号密度很高，但没有任何数学内容 —— 不是公式（用户报过）
    if is_noise_formula(text):
        return False, density, ""
    reasons = []
    if font_ratio >= MATH_FONT_RATIO:
        reasons.append("math_font")
    if density >= SYMBOL_DENSITY:
        reasons.append("symbol_density")
    strong_operator = any(c in text for c in "=≤≥≈∑∏∫√±→∈")
    if strong_operator and density >= 0.06:
        reasons.append("operator")
    if not reasons:
        return False, density, ""
    if len(text) > MAX_FORMULA_LINE_CHARS and density < 0.18:
        return False, density, ""
    # 正文行排除：含 2 个以上英文功能词的长行不是公式（"the MSE of ... is bounded by"）
    if len(text) >= 30 and _is_prose_line(text):
        return False, density, ""
    # 以句号结尾且长度不小 → 更像正文句子
    if text.endswith(".") and len(text) >= 45 and density < 0.20:
        return False, density, ""
    # 打分
    score = min(1.0, 0.45 * font_ratio + 0.55 * min(1.0, density / 0.25))
    if strong_operator:
        score = min(1.0, score + 0.2)
    return True, score, "+".join(reasons)


def detect_formula_candidates(
    blocks: list[RawBlock],
    drawings_by_page: dict[int, list[Drawing]] | None = None,
    body_size: float = 10.0,
) -> list[FormulaCandidate]:
    """从原始文本块中筛出公式区域候选。"""
    drawings_by_page = drawings_by_page or {}
    out: list[FormulaCandidate] = []

    for ab in blocks:
        page_drawings = drawings_by_page.get(ab.page_no, [])
        block_rect = ab.rect
        # 块附近的矢量笔画（细横杠 / 小路径）：必须与块矩形有明显重叠，
        # 否则会把整页的图注线框当成公式证据（误判正文）
        near = [
            d for d in page_drawings
            if block_rect.overlap_ratio(d.rect) > 0.5
            and (d.is_thin_bar(2.5) or (d.rect.width < body_size * 8
                                        and d.rect.height < body_size * 4))
        ]
        vector_hits = len(near)

        formula_lines: list[TextLine] = []
        prose_lines = 0
        total_chars = 0
        sym_total = 0
        math_font_total = 0
        reasons: set[str] = set()
        scores: list[float] = []

        for ln in ab.lines:
            text = ln.text.strip()
            if not text:
                continue
            ok, score, reason = _line_is_formula_like(ln)
            _, total, math_font = _line_symbol_stats(ln)
            total_chars += total
            sym, _, _ = _line_symbol_stats(ln)
            sym_total += sym
            math_font_total += math_font
            if ok:
                formula_lines.append(ln)
                scores.append(score)
                if reason:
                    reasons.update(reason.split("+"))
            elif total >= 30:
                prose_lines += 1

        if not formula_lines:
            continue

        prose_ratio = prose_lines / max(1, prose_lines + len(formula_lines))
        has_math_font = (math_font_total / max(1, total_chars)) >= MATH_FONT_RATIO
        has_vector = vector_hits >= 1
        if prose_ratio > MAX_PROSE_LINE_RATIO:
            continue
        if not (has_math_font or has_vector):
            continue
        if (sym_total / max(1, total_chars)) < SYMBOL_DENSITY * 0.7:
            continue
        # 独立成式的公式通常满足：多行，或短行 + 强证据（数学字体/矢量笔画）
        avg_score = sum(scores) / max(1, len(scores))
        single_short = (len(formula_lines) == 1
                        and len(formula_lines[0].text.strip()) <= 60
                        and (has_math_font or has_vector) and avg_score >= 0.32)
        if len(formula_lines) == 1 and not single_short:
            continue
        # 正文特征过滤：以长英文单词为主的"行"是正文，不是公式
        whole_text = " ".join(ln.text.strip() for ln in formula_lines)
        if _word_ratio(whole_text) > 0.42 and not has_vector:
            continue

        rect = _merge_rects([ln.rect for ln in formula_lines])
        # 文本层原样保留换行（证据）；raw_text 用空格连接便于后续候选生成
        raw_text = " ".join(ln.text.strip() for ln in formula_lines if ln.text.strip())
        cand = FormulaCandidate(
            page_no=ab.page_no,
            block_no=ab.block_no,
            rect=rect,
            lines=formula_lines,
            raw_text=raw_text,
            math_font_chars=math_font_total,
            total_chars=total_chars,
            symbol_count=sym_total,
            vector_hits=vector_hits,
            score=round(min(1.0, sum(scores) / max(1, len(scores))
                            + (0.1 if has_math_font else 0.0)
                            + (0.1 if has_vector else 0.0)), 3),
            reason=",".join(sorted(reasons)) or "geometry",
            is_inline=len(formula_lines) == 1 and rect.height <= body_size * 1.6
            and prose_lines > 0,
        )
        out.append(cand)

    return out


def _merge_rects(rects: list[Rect]) -> Rect:
    r = Rect(1e9, 1e9, -1e9, -1e9)
    for x in rects:
        r = Rect(min(r.x0, x.x0), min(r.y0, x.y0), max(r.x1, x.x1), max(r.y1, x.y1))
    return Rect(0, 0, 0, 0) if r.x0 > 1e8 else r


def has_sqrt_glyph(text: str) -> bool:
    return any(c in text for c in "√∛∜")
