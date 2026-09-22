"""结构层（规划 §13.2）：阅读顺序 / 段落 / 公式块 / 插图 / 表格。"""

from backend.parsing.structure.figure_cut import (
    FigureCandidate, TableCandidate, detect_figures, detect_tables, find_caption_blocks,
)
from backend.parsing.structure.formula_block import (
    FormulaCandidate, detect_formula_candidates,
)
from backend.parsing.structure.paragraph import (
    HeaderFooter, build_blocks_for_page, classify_text, detect_boilerplate,
    estimate_body_style, segment_paragraphs,
)
from backend.parsing.structure.reading_order import (
    ColumnLayout, detect_layout, order_page_blocks, self_check_order,
    split_blocks_by_column,
)

__all__ = [
    "ColumnLayout", "detect_layout", "order_page_blocks", "self_check_order",
    "split_blocks_by_column",
    "segment_paragraphs", "build_blocks_for_page", "estimate_body_style",
    "detect_boilerplate", "HeaderFooter", "classify_text",
    "FormulaCandidate", "detect_formula_candidates",
    "FigureCandidate", "TableCandidate", "detect_figures", "detect_tables",
    "find_caption_blocks",
]
