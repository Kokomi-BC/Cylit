"""MySQL 业务模型层（规划 §10，12 张表）。

铁律（规划 §8 三条禁止）：
1. ❌ MySQL 不存 block 原文 / chunk 全文（只允许 ≤200 字预览）
2. ❌ 不把 LanceDB 当查询主库
3. ❌ 前端 / LLM 不直连存储

因此这里每张表都只存「定位 + 状态 + 元数据」。
"""

from backend.repositories.mysql.models import (
    Annotation,
    Base,
    Block,
    Chunk,
    Formula,
    Highlight,
    ModelConfig,
    Note,
    Paper,
    ReadingProgress,
    Setting,
    Task,
    Translation,
)

__all__ = [
    "Base", "Paper", "Block", "Translation", "Formula", "Highlight", "Note",
    "Annotation", "ReadingProgress", "Task", "ModelConfig", "Chunk",
    "Setting",
]
