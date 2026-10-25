"""「只有标题」的正文块要并进相邻正文（而不是靠模型提示词）。

线上实测（chunk 表元数据）：这 8 块的 `chunk_type` 都是 `text`、`anchor_used` 是空的 ——
说明它们来自**打包层**（节点自己的行只剩标题、下一块的首行又正好是断点），
跟模型的 `breaks` 无关。提示词里本来就写着「不要标标题行的下一行」，再加一句也不会改变打包层的纪律。
"""

from __future__ import annotations

from backend.services.knowledge.knowledge_service import _merge_heading_only


def _row(idx: int, kind: str, text: str) -> dict:
    return {"id": f"r{idx}", "index": idx, "page": 1, "type": kind,
            "original": text, "translation": "", "section_title": ""}


def _chunk(rows: list[dict], key: str) -> dict:
    return {"rows": rows, "node": None, "key": key}


def test_heading_chunk_is_merged_into_the_next_one() -> None:
    chunks = [
        _chunk([_row(1, "heading", "## 2.1 EMPIRICAL VALIDATION")], "a"),
        _chunk([_row(2, "para", "We compare two quantization methods.")], "b"),
    ]
    assert _merge_heading_only(chunks, "a", limit=4000) == 1
    assert len(chunks) == 1
    assert [r["index"] for r in chunks[0]["rows"]] == [1, 2]   # 标题在正文前面，行序不变


def test_trailing_heading_is_left_alone() -> None:
    """最后一小块只有标题时没有"下一块"可并 —— 保持原样，不往回并（会打乱行序）。"""
    chunks = [_chunk([_row(1, "heading", "# REFERENCES")], "a")]
    assert _merge_heading_only(chunks, "a", limit=4000) == 0
    assert len(chunks) == 1


def test_merge_respects_the_hard_limit() -> None:
    chunks = [
        _chunk([_row(1, "heading", "## 2.1 EMPIRICAL VALIDATION")], "a"),
        _chunk([_row(2, "para", "x" * 500)], "b"),
    ]
    assert _merge_heading_only(chunks, "a", limit=200) == 0
    assert len(chunks) == 2


def test_normal_chunk_is_untouched() -> None:
    chunks = [
        _chunk([_row(1, "para", "body")], "a"),
        _chunk([_row(2, "para", "more body")], "b"),
    ]
    assert _merge_heading_only(chunks, "a", limit=4000) == 0
    assert len(chunks) == 2
