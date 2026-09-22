"""LaTeX 候选恢复（规划 §13.3 字符合并、§14.3 候选生成）。

流程：
1. **字符合并**（含字体编码修复）：ToUnicode → glyph name（AGL/TeX 名表）
   → 编码表兜底 → 低置信标记。本模块内置常用符号/希腊字母名表（AGL 子集）。
2. **结构恢复**：用矢量层审计结果（根号覆盖集合 / 分式线分子分母 / 上下标）
   构造结构化 LaTeX，输出**可能多个候选**并附证据。
3. 输出 ``LatexCandidate`` 列表（按置信度降序），不做"择优"，择优留给 LLM/人工。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from backend.parsing.models import Span, VectorAudit

# --------------------------------------------------------------------------
# 1) 字符合并：符号 → LaTeX
# --------------------------------------------------------------------------

GREEK_MAP = {
    "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta",
    "ε": r"\epsilon", "ϵ": r"\epsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ϑ": r"\vartheta", "ι": r"\iota", "κ": r"\kappa",
    "λ": r"\lambda", "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "π": r"\pi",
    "ϖ": r"\varpi", "ρ": r"\rho", "σ": r"\sigma", "ς": r"\varsigma",
    "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi", "ϕ": r"\varphi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Υ": r"\Upsilon",
    "Φ": r"\Phi", "Ψ": r"\Psi", "Ω": r"\Omega",
}

OPERATOR_MAP = {
    "×": r"\times", "÷": r"\div", "⋅": r"\cdot", "·": r"\cdot",
    "±": r"\pm", "∓": r"\mp", "≤": r"\leq", "≥": r"\geq", "≠": r"\neq",
    "≈": r"\approx", "∼": r"\sim", "≃": r"\simeq", "≅": r"\cong",
    "≡": r"\equiv", "∑": r"\sum", "∏": r"\prod", "∫": r"\int",
    "∮": r"\oint", "∞": r"\infty", "∂": r"\partial", "∇": r"\nabla",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊆": r"\subseteq",
    "⊃": r"\supset", "⊇": r"\supseteq", "∪": r"\cup", "∩": r"\cap",
    "∧": r"\wedge", "∨": r"\vee", "∀": r"\forall", "∃": r"\exists",
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "⇒": r"\Rightarrow", "⇔": r"\Leftrightarrow", "⊕": r"\oplus",
    "⊗": r"\otimes", "⊥": r"\perp", "∥": r"\|", "⟨": r"\langle",
    "⟩": r"\rangle", "⌈": r"\lceil", "⌉": r"\rceil", "⌊": r"\lfloor",
    "⌋": r"\rfloor", "√": r"\sqrt{}", "′": "'", "″": "''",
    "…": r"\ldots", "⋯": r"\cdots", "∘": r"\circ", "∗": "*",
    "ℝ": r"\mathbb{R}", "ℕ": r"\mathbb{N}", "ℤ": r"\mathbb{Z}",
    "𝔼": r"\mathbb{E}", "𝒪": r"\mathcal{O}", "ℓ": r"\ell",
}

# AGL / TeX 字形名 → 字符（字体编码修复用；覆盖常见数学字形名）
GLYPH_NAME_MAP = {
    "radicalbig": "√", "radical": "√", "radicalbigg": "√",
    "summationdisplay": "∑", "summationtext": "∑", "sum": "∑",
    "productdisplay": "∏", "integraldisplay": "∫", "integraltext": "∫",
    "infinity": "∞", "partialdiff": "∂", "gradient": "∇",
    "lessequal": "≤", "greaterequal": "≥", "notequal": "≠",
    "approximate": "≈", "equivalence": "≡", "similarequal": "≃",
    "element": "∈", "notelement": "∉", "subset": "⊂", "superset": "⊃",
    "union": "∪", "intersection": "∩", "logicaland": "∧", "logicalor": "∨",
    "arrowright": "→", "arrowleft": "←", "arrowboth": "↔",
    "arrowdblright": "⇒", "arrowdblboth": "⇔",
    "multiply": "×", "divide": "÷", "minus": "−", "plus": "+",
    "plusminus": "±", "dotmath": "⋅", "asteriskmath": "∗",
    "angleleft": "⟨", "angleright": "⟩",
    "ceilingleft": "⌈", "ceilingright": "⌉",
    "floorleft": "⌊", "floorright": "⌋",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

# 常见乱码替换（文本层编码错误的高频形态）
MOJIBAKE_MAP = {
    "\uf0b7": "·", "\uf0b4": "×", "\uf0b3": "≥", "\uf0a3": "≤",
    "\uf02d": "−", "\uf02b": "+", "\uf03d": "=", "\uf0a5": "∞",
    "\uf0e5": "→", "\uf0b1": "±", "\uf0d6": "√", "\uf0e2": "⊗",
    "\u2212": "-",
}

_SYMBOL_MAP: dict[str, str] = {**OPERATOR_MAP, **GREEK_MAP}
_NON_ASCII_KEEP = set("−–—")


def repair_glyphs(text: str) -> tuple[str, list[str]]:
    """字体编码修复：返回 ``(修复后文本, 低置信标记列表)``。"""
    notes: list[str] = []
    out_chars: list[str] = []
    for ch in text or "":
        if ch in MOJIBAKE_MAP:
            out_chars.append(MOJIBAKE_MAP[ch])
            continue
        cp = ord(ch)
        # 私用区（PUA）字符：来源字体没有 ToUnicode，属低置信
        if 0xE000 <= cp <= 0xF8FF:
            name = unicodedata.name(ch, "")
            if name and name.lower() in GLYPH_NAME_MAP:
                out_chars.append(GLYPH_NAME_MAP[name.lower()])
            else:
                notes.append(f"PUA 字符 U+{cp:04X} 无法映射（字体缺 ToUnicode）")
                out_chars.append("?")
            continue
        out_chars.append(ch)
    return "".join(out_chars), notes


# --------------------------------------------------------------------------
# 2) 文本 → LaTeX 字面候选
# --------------------------------------------------------------------------

_LATEX_ESCAPE = {
    "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\sim", "^": r"\hat{}",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", (text or "").strip()) if t]


def _escape(tok: str) -> str:
    out = []
    for ch in tok:
        if ch in _SYMBOL_MAP:
            out.append(_SYMBOL_MAP[ch] + " ")
        elif ch in _LATEX_ESCAPE:
            out.append(_LATEX_ESCAPE[ch])
        else:
            out.append(ch)
    return "".join(out)


def _group_scripts(text: str) -> str:
    """把 ``x2`` / ``yi`` 之类的紧凑写法分组为 ``x^{2}`` / ``y_{i}``。

    保守策略：只处理"单字母 + 单数字/单字母"且紧邻的情形，
    更复杂的上下标依赖矢量层 ``script`` 审计结果。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        out.append(ch)
        if ch.isalpha() and i + 1 < n and (text[i + 1].isdigit()):
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            out.append("^{" + text[i + 1:j] + "}")
            i = j
            continue
        i += 1
    return "".join(out)


def literal_candidate(raw_text: str) -> LatexCandidate:
    """候选 A：字面候选（把所有符号映射为 LaTeX，不做结构推断）。"""
    fixed, notes = repair_glyphs(raw_text)
    joined = " ".join(_tokenize(fixed))
    body = _escape(joined)
    body = re.sub(r"\s{2,}", " ", body).strip()
    return LatexCandidate(
        latex=body, kind="literal", confidence=0.55 if not notes else 0.35,
        notes=notes, evidence={"raw_text": raw_text},
    )


@dataclass
class LatexCandidate:
    latex: str
    kind: str = "literal"          # literal | radical | fraction | structured
    confidence: float = 0.5
    notes: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"latex": self.latex, "kind": self.kind,
                "confidence": round(self.confidence, 3),
                "notes": self.notes, "evidence": self.evidence}


# --------------------------------------------------------------------------
# 3) 结构恢复：用审计结果生成结构化候选
# --------------------------------------------------------------------------


def _radical_latex(covered: list[str], rest_before: str, rest_after: str,
                   has_scripts: bool) -> str:
    inner = " ".join(x for x in covered if x).strip()
    inner_tex = _escape(inner)
    return f"{rest_before}\\sqrt{{{inner_tex}}}{rest_after}"


def structured_candidates(
    raw_text: str,
    audit: VectorAudit,
    spans: list[Span] | None = None,
) -> list[LatexCandidate]:
    """基于矢量层审计生成结构化候选（可能多个）。"""
    out: list[LatexCandidate] = []
    fixed, notes = repair_glyphs(raw_text)
    spans = spans or []

    # ---- 根号：按覆盖集合把 \\sqrt 包到对应字符上 ----
    for bar in audit.radical_bars:
        covered = [c for c in bar.covered_glyphs if c and c.strip()]
        if not covered:
            continue
        inner = " ".join(covered).strip()
        inner_tex = _escape(_group_scripts(inner))
        # 非覆盖部分作为上下文保留（最常见的排版是 "C√X"：系数在根号左侧）
        rest = fixed
        for chunk in covered:
            rest = rest.replace(chunk, " ", 1)
        rest_tex = " ".join(_escape(t) for t in _tokenize(rest))
        latex = f"{rest_tex} \\sqrt{{{inner_tex}}}" if rest_tex else f"\\sqrt{{{inner_tex}}}"
        out.append(LatexCandidate(
            latex=re.sub(r"\s{2,}", " ", latex).strip(),
            kind="radical", confidence=0.62,
            notes=notes + [f"根号横杠覆盖 {len(covered)} 个字形"]
            + ([f"排除：{' '.join(bar.excluded)}"] if bar.excluded else []),
            evidence={"bar_bbox": bar.bar_bbox, "covered": bar.covered_glyphs,
                      "excluded": bar.excluded},
        ))

    # ---- 分式：\frac{分子}{分母} ----
    for frac in audit.fraction_lines:
        num = _escape(_group_scripts(" ".join(frac.numerator_glyphs)))
        den = _escape(_group_scripts(" ".join(frac.denominator_glyphs)))
        if not num or not den:
            continue
        out.append(LatexCandidate(
            latex=f"\\frac{{{num}}}{{{den}}}",
            kind="fraction", confidence=0.6,
            notes=notes + [f"分式线 y={frac.y:.1f}"],
            evidence={"fraction_line_y": frac.y,
                      "numerator": frac.numerator_glyphs,
                      "denominator": frac.denominator_glyphs},
        ))

    # ---- 上下标 ----
    for grp in audit.scripts:
        base = _escape(grp.base)
        if grp.sup and grp.sub:
            out.append(LatexCandidate(
                latex=f"{base}_{{{_escape(grp.sub)}}}^{{{_escape(grp.sup)}}}",
                kind="structured", confidence=0.55,
                notes=notes + ["同基同时有上下标"],
                evidence={"base": grp.base, "sup": grp.sup, "sub": grp.sub},
            ))
        elif grp.sup:
            out.append(LatexCandidate(
                latex=f"{base}^{{{_escape(grp.sup)}}}", kind="structured",
                confidence=0.6, notes=notes,
                evidence={"base": grp.base, "sup": grp.sup},
            ))
        elif grp.sub:
            out.append(LatexCandidate(
                latex=f"{base}_{{{_escape(grp.sub)}}}", kind="structured",
                confidence=0.6, notes=notes,
                evidence={"base": grp.base, "sub": grp.sub},
            ))
    return out


def recover_candidates(raw_text: str, audit: VectorAudit | None = None,
                       spans: list[Span] | None = None) -> list[LatexCandidate]:
    """候选生成总入口：字面候选 + 结构化候选（去重，按置信度降序）。"""
    cands: list[LatexCandidate] = []
    if audit is not None and audit.has_structure:
        cands.extend(structured_candidates(raw_text, audit, spans))
    cands.append(literal_candidate(raw_text))
    # 去重（保留首个）
    seen: set[str] = set()
    uniq: list[LatexCandidate] = []
    for c in cands:
        key = re.sub(r"\s+", "", c.latex)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(key=lambda c: c.confidence, reverse=True)
    return uniq


def glyph_table_stats() -> dict:
    return {
        "symbol_map": len(_SYMBOL_MAP),
        "glyph_name_map": len(GLYPH_NAME_MAP),
        "mojibake_map": len(MOJIBAKE_MAP),
    }


def to_latex_inline(text: str) -> str:
    """给 Markdown 用的行内公式化（C 级不用，只用于 A/B）。"""
    return _escape(text)
