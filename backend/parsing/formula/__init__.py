"""公式处理（规划 §13.3 C1 矢量层审计、§14 全链路）。

分工边界（规划 §14.3）：
- **算法**：区域定位、结构判定（根号范围/分子分母/上下标）、字符合并、候选生成、三重校验、分级
- **LLM/VLM**：仅在候选修复/择优环节参与，且必须基于证据包（规划 §14.4）
"""

from backend.parsing.formula.audit import audit_formula_vectors, find_horizontal_bars
from backend.parsing.formula.latex_recover import (
    LatexCandidate, glyph_table_stats, recover_candidates,
)
from backend.parsing.formula.validate import ValidationResult, validate_formula

__all__ = [
    "audit_formula_vectors", "find_horizontal_bars",
    "recover_candidates", "LatexCandidate", "glyph_table_stats",
    "validate_formula", "ValidationResult",
]
