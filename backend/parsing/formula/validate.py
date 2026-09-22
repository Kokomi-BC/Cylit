"""三重校验（规划 §14.3 / §14.1 步骤 ⑥）。

1. **语法闭合校验**：花括号 / 括号配对，``\\frac`` 与 ``\\sqrt`` 参数个数，禁止裸下划线与裸尖号
2. **符号一致性校验**：候选与文本层证据的符号集合差集（防止 LLM 编造符号）
3. **常量/幂次一致性**：候选中的数值常量必须落在 ``rule`` 常量表容差内（规划 §13.3 C2）

回渲染比对（LaTeX 渲染图 vs 原裁图）需要渲染后端（KaTeX/MathJax），
本实现把「回渲染」抽象为可选钩子：未接入时记为 ``skipped``，
并在分级规则中按"未验证"处理（不会因此判 A）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.parsing.models import VectorAudit

# 常见 LaTeX 命令的参数个数（用于语法校验）
_COMMAND_ARITY = {
    r"\frac": 2, r"\dfrac": 2, r"\tfrac": 2, r"\sqrt": 1,
    r"\overline": 1, r"\underline": 1, r"\hat": 1, r"\bar": 1,
    r"\vec": 1, r"\dot": 1, r"\text": 1, r"\mathrm": 1,
    r"\left": 1, r"\right": 1, r"\sum": 0, r"\prod": 0, r"\int": 0,
}


@dataclass
class ValidationResult:
    syntax_ok: bool = False
    symbols_ok: bool = False
    constants_ok: bool = False
    rerender: str = "skipped"        # passed | failed | skipped
    rerender_score: float | None = None
    issues: list[str] = field(default_factory=list)
    symbol_diff: dict[str, list[str]] = field(default_factory=dict)
    constants_checked: list[dict] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.syntax_ok and self.symbols_ok and self.constants_ok

    def to_dict(self) -> dict:
        return {
            "syntax_ok": self.syntax_ok, "symbols_ok": self.symbols_ok,
            "constants_ok": self.constants_ok, "rerender": self.rerender,
            "rerender_score": self.rerender_score, "issues": self.issues,
            "symbol_diff": self.symbol_diff,
            "constants_checked": self.constants_checked, "all_passed": self.all_passed,
        }


# --------------------------------------------------------------------------
# 1) 语法闭合
# --------------------------------------------------------------------------

_SYMBOL_CHARS = set("∑∏∫√∞∂∇±×÷⋅·≤≥≠≈≡∈∉⊂⊆⊃⊇∪∩∧∨∀∃→←↔⇒⇔⊕⊗⊥∥⟨⟩"
                    "αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΥΦΨΩ")


def validate_syntax(latex: str) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not latex or not latex.strip():
        return False, ["LaTeX 为空"]

    # 花括号配对
    depth = 0
    for i, ch in enumerate(latex):
        if ch == "\\":
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                issues.append(f"位置 {i} 出现多余的 '}}'")
                break
    if depth != 0:
        issues.append(f"花括号不配对（残余深度 {depth}）")

    # \left / \right 配对
    n_left = len(re.findall(r"\\left\b", latex))
    n_right = len(re.findall(r"\\right\b", latex))
    if n_left != n_right:
        issues.append(f"\\left({n_left}) 与 \\right({n_right}) 数量不一致")

    # \begin / \end 配对
    begins = re.findall(r"\\begin\{(\w+\*?)\}", latex)
    ends = re.findall(r"\\end\{(\w+\*?)\}", latex)
    if sorted(begins) != sorted(ends):
        issues.append(f"环境不配对：begin={begins} end={ends}")

    # 裸下划线 / 裸尖号（LaTeX 中必须加花括号或转义）
    if re.search(r"(?<!\\)_", latex) and not re.search(r"(?<!\\)_\{", latex):
        issues.append("存在裸 '_'（未加花括号或转义）")
    if re.search(r"(?<!\\)\^(?!\{)", latex):
        issues.append("存在裸 '^'（未加花括号）")

    # 命令参数个数（只查未加花括号的明显错误：\frac{a}b）
    for cmd, arity in _COMMAND_ARITY.items():
        if arity == 0:
            continue
        idx = 0
        while True:
            pos = latex.find(cmd, idx)
            if pos < 0:
                break
            # 简单的花括号参数计数
            rest = latex[pos + len(cmd):]
            groups = 0
            i = 0
            while i < len(rest) and rest[i] in " \t":
                i += 1
            while i < len(rest) and rest[i] == "{":
                depth2 = 0
                while i < len(rest):
                    if rest[i] == "{":
                        depth2 += 1
                    elif rest[i] == "}":
                        depth2 -= 1
                        if depth2 == 0:
                            i += 1
                            break
                    i += 1
                groups += 1
                while i < len(rest) and rest[i] in " \t":
                    i += 1
            if groups < arity:
                issues.append(f"{cmd} 需要 {arity} 个花括号参数，实际 {groups} 个")
            idx = pos + len(cmd)

    return (not issues), issues


# --------------------------------------------------------------------------
# 2) 符号一致性
# --------------------------------------------------------------------------


def _symbol_set(latex: str) -> set[str]:
    out: set[str] = set()
    for ch in latex:
        if ch in _SYMBOL_CHARS:
            out.add(ch)
    # 命令形式的符号也计入
    for cmd in re.findall(r"\\[a-zA-Z]+", latex):
        out.add(cmd)
    return out


def _latex_cmd_to_char(latex: str) -> set[str]:
    """把 \\alpha \\leq 之类的命令还原成对应字符，便于与文本层比对。"""
    from backend.parsing.formula.latex_recover import GREEK_MAP, OPERATOR_MAP

    reverse = {v.replace("\\", ""): k for k, v in {**GREEK_MAP, **OPERATOR_MAP}.items()}
    out: set[str] = set()
    for cmd in re.findall(r"\\([a-zA-Z]+)", latex):
        if cmd in reverse:
            out.add(reverse[cmd])
    return out


def validate_symbols(latex: str, raw_text: str, audit: VectorAudit | None = None
                     ) -> tuple[bool, list[str], dict[str, list[str]]]:
    """候选符号必须能在证据中找到；出现新符号即报警（防 LLM 编造）。"""
    evidence_chars = set(raw_text or "")
    if audit:
        for bar in audit.radical_bars:
            evidence_chars.update("".join(bar.covered_glyphs))
            evidence_chars.update("".join(bar.excluded))
        for frac in audit.fraction_lines:
            evidence_chars.update("".join(frac.numerator_glyphs))
            evidence_chars.update("".join(frac.denominator_glyphs))
        for s in audit.scripts:
            evidence_chars.update(s.base + s.sup + s.sub)

    cand_syms = _symbol_set(latex)
    converted = _latex_cmd_to_char(latex)
    # 命名字符（\alpha 等）已映射为字符，参与"是否出现在证据中"的判断
    unknown = sorted(
        c for c in (cand_syms | converted)
        if not c.startswith("\\") and c not in evidence_chars and c not in " \t"
    )
    cmds = {c for c in cand_syms if c.startswith("\\")}
    allowed_roots = {c.rstrip("{}") for c in _ALLOWED_COMMANDS}
    unknown_cmds = sorted(c for c in cmds if c not in allowed_roots)
    issues: list[str] = []
    if unknown:
        issues.append(f"候选出现证据中不存在的符号：{''.join(unknown)}")
    if unknown_cmds:
        issues.append(f"候选使用白名单外的命令：{unknown_cmds}")
    diff = {"unknown_symbols": unknown, "unknown_commands": unknown_cmds}
    return (not issues), issues, diff


_ALLOWED_COMMANDS = {
    r"\frac", r"\dfrac", r"\tfrac", r"\sqrt", r"\sum", r"\prod", r"\int",
    r"\infty", r"\partial", r"\nabla", r"\alpha", r"\beta", r"\gamma",
    r"\delta", r"\epsilon", r"\zeta", r"\eta", r"\theta", r"\iota",
    r"\kappa", r"\lambda", r"\mu", r"\nu", r"\xi", r"\pi", r"\rho",
    r"\sigma", r"\tau", r"\upsilon", r"\phi", r"\varphi", r"\chi",
    r"\psi", r"\omega", r"\Gamma", r"\Delta", r"\Theta", r"\Lambda",
    r"\Xi", r"\Pi", r"\Sigma", r"\Upsilon", r"\Phi", r"\Psi", r"\Omega",
    r"\leq", r"\geq", r"\neq", r"\approx", r"\sim", r"\simeq", r"\cong",
    r"\equiv", r"\times", r"\div", r"\cdot", r"\pm", r"\mp", r"\in",
    r"\notin", r"\subset", r"\subseteq", r"\supset", r"\supseteq",
    r"\cup", r"\cap", r"\wedge", r"\vee", r"\forall", r"\exists",
    r"\to", r"\leftarrow", r"\leftrightarrow", r"\Rightarrow",
    r"\Leftrightarrow", r"\oplus", r"\otimes", r"\perp", r"\langle",
    r"\rangle", r"\lceil", r"\rceil", r"\lfloor", r"\rfloor",
    r"\left", r"\right", r"\big", r"\Big", r"\bigg", r"\Bigg",
    r"\hat", r"\bar", r"\vec", r"\dot", r"\ddot", r"\overline",
    r"\underline", r"\tilde", r"\mathbb", r"\mathcal", r"\mathrm",
    r"\text", r"\operatorname", r"\limits", r"\nolimits", r"\ldots",
    r"\cdots", r"\circ", r"\ell", r"\log", r"\ln", r"\exp", r"\max",
    r"\min", r"\sup", r"\inf", r"\arg", r"\dim", r"\det", r"\tr",
    r"\top", r"\mathbb{R}", r"\mathbb{E}", r"\%", r"\#", r"\&",
    r"\_", r"\$", r"\{", r"\}", r"\quad", r"\qquad", r"\ ", r"\,",
}


# --------------------------------------------------------------------------
# 3) 常量表校验
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")


def extract_constants(latex: str) -> list[float]:
    """抽出候选中的数值常量（排除作为下标/上标的裸整数）。"""
    out: list[float] = []
    for m in _NUMBER.finditer(latex or ""):
        try:
            out.append(float(m.group(1)))
        except ValueError:
            continue
    return out


def validate_constants(latex: str, rules: list[dict] | None = None,
                       tolerance: float = 1e-6) -> tuple[bool, list[str], list[dict]]:
    """检查候选中的常量是否与论文自身声明的常量冲突（规划 §13.3 C2）。

    ``rules`` 每项：``{symbol, value, value_float, tolerance, note}``。
    校验逻辑：若候选同时出现符号关联的数值且与声明值不符 → 报警。
    """
    issues: list[str] = []
    checked: list[dict] = []
    if not rules:
        return True, issues, checked
    nums = extract_constants(latex)
    for rule in rules:
        sym = rule.get("symbol") or ""
        val = rule.get("value_float")
        if val is None or not sym:
            continue
        tol = float(rule.get("tolerance") or tolerance)
        tol = max(tol, abs(val) * 1e-3)
        # 只有当候选同时包含该符号时，数值才有约束意义
        if sym and sym not in latex:
            continue
        conflict = None
        for n in nums:
            if abs(n - val) <= tol:
                conflict = None
                break
            if n > 0 and abs(n - val) / max(abs(val), 1e-9) < 0.25:
                conflict = n
        entry = {"symbol": sym, "declared": val, "numbers": nums,
                 "conflict": conflict, "note": rule.get("note")}
        checked.append(entry)
        if conflict is not None:
            issues.append(
                f"常量冲突：候选中的 {conflict} 与论文声明 {sym}={val} 不一致（容差 {tol:g}）"
            )
    return (not issues), issues, checked


# --------------------------------------------------------------------------
# 综合
# --------------------------------------------------------------------------


def validate_formula(latex: str, raw_text: str, audit: VectorAudit | None = None,
                     rules: list[dict] | None = None,
                     rerender_score: float | None = None,
                     rerender_threshold: float = 0.72) -> ValidationResult:
    """三重校验总入口。"""
    res = ValidationResult()
    res.syntax_ok, syn_issues = validate_syntax(latex)
    res.issues.extend(syn_issues)

    sym_ok, sym_issues, diff = validate_symbols(latex, raw_text, audit)
    res.symbols_ok = sym_ok
    res.issues.extend(sym_issues)
    res.symbol_diff = diff

    const_ok, const_issues, checked = validate_constants(latex, rules)
    res.constants_ok = const_ok
    res.issues.extend(const_issues)
    res.constants_checked = checked

    if rerender_score is None:
        res.rerender = "skipped"
    else:
        res.rerender = "passed" if rerender_score >= rerender_threshold else "failed"
        res.rerender_score = round(rerender_score, 4)
        if res.rerender == "failed":
            res.issues.append(
                f"回渲染比对未通过（相似度 {rerender_score:.3f} < {rerender_threshold}）"
            )
    return res


# --------------------------------------------------------------------------
# 分级（规划 §14.5）
# --------------------------------------------------------------------------


def grade_formula(
    *,
    audit: VectorAudit,
    validation: ValidationResult,
    candidate_kind: str,
    llm_changed_structure: bool = False,
    llm_reported_uncertainty: bool = False,
    is_bitmap: bool = False,
    has_ambiguous_encoding: bool = False,
) -> tuple[str, float, list[str]]:
    """返回 ``(grade, confidence, reasons)``。

    - **A**：文本层无歧义（无根号/分式/矩阵结构），无需修复即通过
    - **B**：几何候选唯一 + LLM 未改结构 + 三重校验全通过
    - **C**：三源冲突 / 回渲染失败 / 字体编码低置信 / 本来就是位图 / LLM 报不确定
    """
    reasons: list[str] = []
    if is_bitmap:
        return "C", 0.2, ["公式本身是位图，无文本层证据"]
    if audit.kind == "plain" and candidate_kind == "literal" and validation.syntax_ok \
            and validation.symbols_ok and validation.constants_ok and not has_ambiguous_encoding:
        return "A", 0.92, ["文本层无歧义，无结构性符号"]
    if has_ambiguous_encoding:
        reasons.append("字体编码低置信（PUA / 缺 ToUnicode）")
    if llm_reported_uncertainty:
        reasons.append("LLM 报告不确定性")
    if not validation.all_passed:
        reasons.append("三重校验未全部通过")
    if validation.rerender == "failed":
        reasons.append("回渲染比对差异超阈值")
    if llm_changed_structure:
        reasons.append("LLM 修复改变了结构")
    if audit.grade_hint == "C":
        reasons.append("矢量层显示根号与分式混合，结构歧义")

    if reasons:
        hard = (llm_reported_uncertainty or validation.rerender == "failed"
                or "结构歧义" in "".join(reasons) or has_ambiguous_encoding)
        return ("C" if hard else "B"), (0.35 if hard else 0.68), reasons

    # 三源一致：矢量层结构 + 校验通过 + 未改结构
    if audit.has_structure:
        conf = 0.78 if validation.rerender == "passed" else 0.7
        return "B", conf, ["几何候选与三重校验一致"]
    return "A", 0.85, ["无结构性符号且校验通过"]
