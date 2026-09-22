"""C4 质量指标（规划 §13.3 C4、§14.6 目标指标）。

质量指标直接对应第 5 章实验表格：
A/B/C 占比、LLM 结构改动率、回渲染漏检率、一致性真实命中数。

> 复核清单（``build_review_checklist`` / ``ReviewItem``）已随"复核模式"一起移除：
> 解析阶段的这些问题不再进人力复核队列，只保留质量指标本身。
"""

from __future__ import annotations

from typing import Any

from backend.core.utils import now_iso
from backend.parsing.models import FormulaBlock, ParseResult

# §14.6 目标指标
TARGET_GRADE_A = 0.45
TARGET_GRADE_B = 0.40
TARGET_GRADE_C_MAX = 0.15
TARGET_GRADE_C_WARN = 0.20
TARGET_LLM_STRUCTURE_CHANGE = 0.10


def build_quality_report(
    result: ParseResult,
    *,
    consistency_findings: list[dict] | None = None,
    llm_repaired_count: int = 0,
    llm_structure_changed: int = 0,
    rerender_checked: int = 0,
    rerender_failed: int = 0,
) -> dict[str, Any]:
    """产出质量指标表（规划 §14.6 + 阶段验收表数据）。"""
    dist = result.grade_distribution()
    total_f = max(1, sum(dist.values()))
    ratio = {k: round(v / total_f, 4) for k, v in dist.items()}

    checks: list[dict] = []
    checks.append(_check("A 级占比", ratio.get("A", 0.0), TARGET_GRADE_A, ">=",
                         f"{ratio.get('A', 0.0) * 100:.1f}%"))
    checks.append(_check("B 级占比", ratio.get("B", 0.0), TARGET_GRADE_B, ">=",
                         f"{ratio.get('B', 0.0) * 100:.1f}%"))
    checks.append(_check("C 级占比", ratio.get("C", 0.0), TARGET_GRADE_C_MAX, "<=",
                         f"{ratio.get('C', 0.0) * 100:.1f}%"))
    structure_change = (llm_structure_changed / llm_repaired_count) \
        if llm_repaired_count else 0.0
    checks.append(_check("LLM 结构改动率", structure_change, TARGET_LLM_STRUCTURE_CHANGE,
                         "<=", f"{structure_change * 100:.1f}%"))
    miss_rate = (rerender_failed / rerender_checked) if rerender_checked else 0.0
    checks.append(_check("回渲染校验漏检率", miss_rate, 0.02, "<=",
                         f"{miss_rate * 100:.2f}%" if rerender_checked else "未接入回渲染"))
    hits = len([c for c in (consistency_findings or []) if c.get("severity") == "high"])
    checks.append({
        "name": "一致性检查真实命中数", "value": hits, "target": ">= 1",
        "passed": hits >= 1, "display": str(hits),
    })

    counts = result.counts()
    return {
        "paper_id": result.paper_id,
        "generated_at": now_iso(),
        "counts": counts,
        "grade_distribution": dist,
        "grade_ratio": ratio,
        "c_grade_warning": ratio.get("C", 0.0) > TARGET_GRADE_C_WARN,
        "checks": checks,
        "duration_ms": result.duration_ms,
        "warnings": result.warnings,
    }


def _check(name: str, value: float, target: float, op: str, display: str) -> dict:
    passed = value >= target if op == ">=" else value <= target
    return {"name": name, "value": round(value, 4), "target": f"{op} {target}",
            "passed": bool(passed), "display": display}
