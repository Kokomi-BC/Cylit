"""IR 读写（规划 §9）。

IR = 每篇论文一个 SQLite，**构建期产物 / 调试与审计留痕 / 增量重跑与可复现**，
不是运行时依赖（运行时真相源 = Markdown 正文 + MySQL 定位）。
"""

from backend.parsing.ir.rebind import (
    BlockFingerprint, rebind_block_ids, write_rebind_report,
)
from backend.parsing.ir.ir_writer import IRWriter
from backend.parsing.ir.ir_reader import IRReader

__all__ = [
    "IRWriter", "IRReader",
    "BlockFingerprint", "rebind_block_ids", "write_rebind_report",
]
