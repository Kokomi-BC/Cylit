"""派生 ID 的列宽守卫（回归：长论文 id 撑爆 chunk.id 的 1406 错误）。

背景：``chunk.id = "{paper_id}:{chunk_type}:{首块 id}[-{末块 id}]"``，
早期列宽是 ``varchar(64)``。论文 id 变长后（如
``turboquant-online-vector-iclr-26``）插入直接报
``1406 Data too long for column 'id'``，整个索引构建失败。
这里用最坏情况反推长度，保证模型声明的列宽够用。
"""

from __future__ import annotations

from backend.core.utils import slugify
from backend.repositories.mysql.models import Block, Chunk
from backend.services.knowledge.knowledge_service import _make_chunk, new_chunk_id

# 最坏情况：slugify 上限 48 字符（见 core.utils.slugify max_len），块 id 前缀 12 字符
LONG_PAPER_ID = "a" * 48
WORST_BLOCK_ID = "turboquanton-p99-b99"  # 12 + "-p99-b99"


def _block(block_id: str, order: int) -> dict:
    return {"id": block_id, "page": 99, "type": "para", "order": order,
            "text": "x", "hash": "h", "section": ""}


def test_slugify_caps_paper_id_at_48() -> None:
    """前提校验：论文 id 来自 slugify，其 max_len 就是长度上界。"""
    title = "TurboQuant Online Vector Quantization With Near Optimal Distortion Rate"
    assert len(slugify(title, fallback="paper")) <= 48


def test_chunk_id_fits_declared_column() -> None:
    chunk = _make_chunk(
        LONG_PAPER_ID, "text", "内容",
        _block(WORST_BLOCK_ID, 1), _block("turboquanton-p98-b01", 2),
    )
    width = Chunk.__table__.c.id.type.length
    assert width is not None and width >= len(chunk["id"]), (
        f"chunk.id 需要 {len(chunk['id'])} 字符，但列宽只有 {width}：{chunk['id']}"
    )


def test_new_chunk_id_fits_declared_column() -> None:
    cid = new_chunk_id(LONG_PAPER_ID, "formula")
    width = Chunk.__table__.c.id.type.length
    assert width is not None and width >= len(cid), cid


def test_block_chunk_id_not_narrower_than_chunk_id() -> None:
    """``block.chunk_id`` 存的就是 Chunk.id，宽度不能比它小。"""
    chunk_width = Chunk.__table__.c.id.type.length
    block_width = Block.__table__.c.chunk_id.type.length
    assert block_width is not None and chunk_width is not None
    assert block_width >= chunk_width
