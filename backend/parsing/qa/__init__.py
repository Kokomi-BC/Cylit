"""质保层（规划 §13.3）：一致性约束表 / 插图体检 / 公式分级统计 / 质量指标。

- ``consistency``：C2 一致性约束（常数 / 幂次量纲 / 定理↔附录）
- ``figure_audit``：C3 插图体检（edge_ink、子图数、effective_dpi、长宽比）
- ``report``：C4 质量指标（A/B/C 占比等，供第 5 章实验）
"""

from backend.parsing.qa.consistency import (
    ConsistencyFinding, extract_constants_from_text, parse_rule_table,
    run_consistency_checks,
)
from backend.parsing.qa.figure_audit import (
    FigureAudit, audit_figure_crop, audit_figure_pixels,
)
from backend.parsing.qa.report import build_quality_report

__all__ = [
    "run_consistency_checks", "ConsistencyFinding", "extract_constants_from_text",
    "parse_rule_table",
    "audit_figure_crop", "audit_figure_pixels", "FigureAudit",
    "build_quality_report",
]
