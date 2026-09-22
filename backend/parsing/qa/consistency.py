"""C2 一致性约束表（规划 §13.3 C2，收窄为三类以降低误报）。

| 类型 | 检查方式 | 真实错例 |
|---|---|---|
| 常数 | 文中常数必须在 ``rule`` 表内唯一；同符号两个值即报警 | 正文"≈2.7" 与 `√(3π/2)≈2.17` 并存 |
| 幂次/量纲 | 含 `d`、`b` 的表达式做符号幂次核对 | — |
| 定理↔附录 | 附录推导作为独立路径与正文常数比对 | `(π/2)·4·(√3π/2)=√3π²` 反证 C.1 的 `(√3π)/2` |

本模块**只报告不自动改**：命中项落进 ``result.stats["consistency_findings"]`` 与质量报告
（早期版本会写 ``review`` 表，复核模式移除后不再写）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from backend.parsing.models import Block

# 常量模式：符号 = 数值，或 "≈ 2.7207" 形式
_SYMBOL_CONST = re.compile(
    r"(?P<symbol>[A-Za-z\\{}^_]{1,24}|[πΠ])"
    r"\s*(?:=|≈|\\approx|\\simeq)\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)"
)
# 裸数值
_BARE_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
# √ 表达式（形如 \sqrt{3}\pi/2 或 (√3π)/2）
_SQRT_EXPR = re.compile(r"(?:\\sqrt\{[^}]{1,12}\}|√\s*\{?[^}\s)]{1,12}\}?)\s*"
                        r"(?:\\pi|π)?(?:\s*/\s*\d+)?")
# 幂次模式：d^{...}, b^{...}, x^2
_POWER = re.compile(r"(?P<base>[a-zA-Z])\s*\^\s*\{?(?P<exp>[^}\s,)]{1,8})")


@dataclass
class ConsistencyFinding:
    kind: str                 # constant|power|theorem_appendix
    symbol: str = ""
    severity: str = "medium"
    message: str = ""
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "symbol": self.symbol, "severity": self.severity,
                "message": self.message, "evidence": self.evidence}


# --------------------------------------------------------------------------
# 常量抽取
# --------------------------------------------------------------------------

# 常见数学常量（用于把表达式折算成数值）
_CONST_VALUES = {
    "pi": math.pi, "π": math.pi, "e": math.e,
}


def _safe_eval_expr(expr: str) -> float | None:
    """把简单数值表达式折算为浮点（只做白名单运算，不用 eval）。"""
    s = (expr or "").strip()
    s = s.replace("\\cdot", "*").replace("·", "*").replace("×", "*")
    s = s.replace("\\times", "*").replace("−", "-")
    s = s.replace("π", "pi").replace("\\pi", "pi")
    s = s.replace("\\sqrt", "sqrt").replace("√", "sqrt")
    s = re.sub(r"sqrt\{([^}]*)\}", r"sqrt(\1)", s)
    s = re.sub(r"sqrt(\d+(?:\.\d+)?)", r"sqrt(\1)", s)
    if not re.fullmatch(r"[\d\s\.\+\-\*/\(\)pie sqrt]*", s):
        return None
    try:
        return float(eval(s, {"__builtins__": {}}, {"sqrt": math.sqrt, "pi": math.pi,
                                                     "e": math.e}))  # noqa: S307
    except Exception:  # noqa: BLE001
        return None


def extract_constants_from_text(text: str) -> list[dict]:
    """从文本中抽取「符号 = 数值」与「√ 表达式 ≈ 数值」两类常量声明。"""
    out: list[dict] = []
    t = text or ""
    for m in _SYMBOL_CONST.finditer(t):
        sym = m.group("symbol").strip()
        try:
            val = float(m.group("value"))
        except ValueError:
            continue
        if len(sym) == 1 and sym.isdigit():
            continue
        out.append({"symbol": sym, "value": val, "raw": m.group(0),
                    "origin": "symbol_eq_value"})
    # √ 表达式 + 近似值
    for sm in _SQRT_EXPR.finditer(t):
        expr = sm.group(0)
        tail = t[sm.end():sm.end() + 24]
        nm = re.search(r"(?:≈|\\approx|~)\s*(-?\d+(?:\.\d+)?)", tail)
        if not nm:
            continue
        try:
            stated = float(nm.group(1))
        except ValueError:
            continue
        computed = _safe_eval_expr(expr)
        out.append({"symbol": expr.strip(), "value": stated, "computed": computed,
                    "raw": f"{expr}{nm.group(0)}", "origin": "sqrt_expr_approx"})
    return out


# --------------------------------------------------------------------------
# 一致性检查
# --------------------------------------------------------------------------


def run_consistency_checks(
    blocks: list[Block],
    rules: list[dict] | None = None,
    formula_latex: dict[str, str] | None = None,
    tolerance_ratio: float = 0.02,
) -> list[ConsistencyFinding]:
    """对全篇文本做三类一致性检查。"""
    rules = rules or []
    formula_latex = formula_latex or {}
    findings: list[ConsistencyFinding] = []

    # ---------- ① 常数：同符号多值 / 与声明值冲突 ----------
    symbol_values: dict[str, list[tuple[float, str]]] = {}
    for b in blocks:
        if b.type in ("ref",):
            continue
        for c in extract_constants_from_text(b.text_clean or b.text):
            symbol_values.setdefault(c["symbol"], []).append((c["value"], b.id))
            # √ 表达式的自洽性：声明值 vs 折算式
            if c.get("computed") is not None:
                tol = max(abs(c["computed"]) * tolerance_ratio, 1e-3)
                if abs(c["computed"] - c["value"]) > tol:
                    findings.append(ConsistencyFinding(
                        kind="constant", symbol=c["symbol"], severity="high",
                        message=(f"{c['symbol']} 的排版值与按表达式折算值不一致："
                                 f"文中 ≈{c['value']}，按式折算 ≈{c['computed']:.4f}"),
                        evidence={"block_id": b.id, "raw": c["raw"],
                                  "stated": c["value"], "computed": c["computed"]},
                    ))

    for sym, vals in symbol_values.items():
        uniq = _dedupe_values([v for v, _ in vals], tolerance_ratio)
        if len(uniq) > 1:
            findings.append(ConsistencyFinding(
                kind="constant", symbol=sym, severity="high",
                message=(f"符号 {sym} 在文中出现 {len(uniq)} 个不同数值："
                         f"{', '.join(f'{v:g}' for v in uniq)}"),
                evidence={"values": uniq,
                          "blocks": sorted({bid for _, bid in vals})},
            ))

    for rule in rules:
        sym = rules_symbol(rule)
        declared = rule.get("value_float")
        if not sym or declared is None:
            continue
        seen = symbol_values.get(sym, [])
        for v, bid in seen:
            tol = max(abs(declared) * 0.5, 1e-6)
            if abs(v - declared) > tol and _looks_like_same_quantity(sym, v, declared):
                findings.append(ConsistencyFinding(
                    kind="constant", symbol=sym, severity="high",
                    message=(f"符号 {sym} 的文中取值 {v:g} 与声明的 {declared:g} 冲突"),
                    evidence={"block_id": bid, "declared": declared, "found": v,
                              "rule_note": rule.get("note")},
                ))

    # ---------- ② 幂次 / 量纲 ----------
    power_by_base: dict[str, set[str]] = {}
    for b in blocks:
        for m in _POWER.finditer(b.text_clean or b.text):
            base, exp = m.group("base"), m.group("exp").strip()
            if base in ("d", "b", "n", "k", "m", "D", "B"):
                power_by_base.setdefault(base, set()).add(exp)
    for base, exps in power_by_base.items():
        # 同一基出现多个互不兼容的幂次，可能排版丢失了括号
        if len(exps) >= 2 and "2" in exps and "1" in exps:
            findings.append(ConsistencyFinding(
                kind="power", symbol=base, severity="medium",
                message=f"符号 {base} 同时以幂次 {sorted(exps)} 出现，请核对量纲",
                evidence={"exponents": sorted(exps)},
            ))

    # ---------- ③ 定理 ↔ 附录 ----------
    findings.extend(_check_theorem_appendix(blocks, formula_latex, rules))
    return findings


def rules_symbol(rule: dict) -> str:
    return (rule.get("symbol") or "").strip()


def _dedupe_values(values: list[float], ratio: float = 0.02) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out:
            out.append(v)
            continue
        if abs(v - out[-1]) > max(abs(out[-1]) * ratio, 1e-6):
            out.append(v)
    return out


def _looks_like_same_quantity(sym: str, found: float, declared: float) -> bool:
    """避免把 "n = 10"（实验设置）误判为常量冲突：只比量级相近的值。"""
    if declared == 0:
        return False
    return 0.1 * abs(declared) <= abs(found) <= 10 * abs(declared)


_APPENDIX_BLOCK = re.compile(
    r"^(?P<sec>[A-C](?:\.\d+)*)\s+.*?(?:proof|derivation|appendix)",
    re.IGNORECASE | re.DOTALL,
)


def _check_theorem_appendix(
    blocks: list[Block],
    formula_latex: dict[str, str],
    rules: list[dict],
) -> list[ConsistencyFinding]:
    """附录推导作为独立路径与正文常量比对（规划 §13.3 C2 第三类）。

    做法：找出形如 ``\\frac{\\sqrt{3}\\pi}{2}`` 的常量表达式，
    在正文与附录中分别折算成数值；若正文与附录给出的同一符号值不同 → 报警。

    附录判定：块文本以 ``A/B/C`` 编号 + 含 proof/derivation/appendix 关键字开头，
    或本体位于被判定为附录的页面（由 pipeline 通过 ``meta['in_appendix']`` 标注）。
    """
    findings: list[ConsistencyFinding] = []
    appendix_block_ids = {b.id for b in blocks
                          if b.meta.get("in_appendix")
                          or _APPENDIX_BLOCK.match((b.text_clean or b.text or "").strip())}
    if not appendix_block_ids:
        return findings

    body_vals: dict[str, float] = {}
    app_vals: dict[str, float] = {}
    for bid, latex in formula_latex.items():
        expr = _extract_constant_expr(latex)
        if not expr:
            continue
        val = _safe_eval_expr(expr)
        if val is None:
            continue
        if bid in appendix_block_ids:
            app_vals.setdefault(expr, val)
        else:
            body_vals.setdefault(expr, val)

    # 同名符号（去掉系数提取形状）在两个路径下不一致
    for expr_body, v_body in body_vals.items():
        for expr_app, v_app in app_vals.items():
            if _shape_key(expr_body) != _shape_key(expr_app):
                continue
            if abs(v_body - v_app) > max(abs(v_body) * 0.02, 1e-6):
                findings.append(ConsistencyFinding(
                    kind="theorem_appendix", symbol=expr_body, severity="high",
                    message=(f"正文表达式 {expr_body} ≈ {v_body:.4f} 与附录 "
                             f"{expr_app} ≈ {v_app:.4f} 不一致"),
                    evidence={"body": expr_body, "appendix": expr_app,
                              "body_value": v_body, "appendix_value": v_app},
                ))
    for rule in rules:
        note = (rule.get("note") or "")
        if "appendix" in note.lower() or "附录" in note:
            declared = rule.get("value_float")
            if declared is None:
                continue
            for expr, val in body_vals.items():
                if abs(val - declared) > max(abs(declared) * 0.02, 1e-6) \
                        and not any(True for a in app_vals.values()
                                    if abs(a - declared) <= max(abs(declared) * 0.02, 1e-6)):
                    findings.append(ConsistencyFinding(
                        kind="theorem_appendix", symbol=expr, severity="medium",
                        message=(f"正文常量 {expr} ≈ {val:.4f} 与附录声明值 "
                                 f"{declared:g} 不符，且附录中未找到相符派生"),
                        evidence={"expr": expr, "value": val, "declared": declared},
                    ))
    return findings


_SHAPE = re.compile(r"[^a-zπ]+")


def _shape_key(expr: str) -> str:
    """把表达式归一为"形状"：只保留字母与 π，便于跨路径比对。"""
    s = expr.replace("\\", "").replace("{", "").replace("}", "")
    s = s.replace("sqrt", "r").replace("pi", "π")
    return _SHAPE.sub("", s.lower())


def _extract_constant_expr(latex: str) -> str | None:
    """从一条公式里抽出"常量表达式"（不含变量）。"""
    if not latex:
        return None
    # 去掉变量与括号结构，只保留由数字 / π / \sqrt 组成的片段
    for m in re.finditer(r"(?:\\frac\{[^{}]{1,30}\}\{[^{}]{1,20}\}"
                         r"|\d+\s*\*\s*\\sqrt\{[^}]{1,12}\}"
                         r"|\\sqrt\{[^}]{1,12}\}\s*\*?\s*\d*"
                         r"|\(?\s*\\sqrt\{[^}]{1,12}\}\s*\\pi\s*\)?\s*/\s*\d+)", latex):
        cand = m.group(0)
        if _safe_eval_expr(cand) is not None:
            return cand
    return None


def parse_rule_table(rows: list[dict]) -> list[dict]:
    """把外部（IR ``rule`` 表 / 手工）行规整为内部规则结构。"""
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        v = r.get("value_float")
        if v is None and r.get("value") is not None:
            try:
                v = float(str(r["value"]).strip())
            except (TypeError, ValueError):
                v = None
        out.append({
            "symbol": (r.get("symbol") or "").strip(),
            "kind": r.get("kind") or "constant",
            "value": r.get("value"),
            "value_float": v,
            "tolerance": r.get("tolerance"),
            "note": r.get("note"),
            "source_block": r.get("source_block"),
        })
    return out
