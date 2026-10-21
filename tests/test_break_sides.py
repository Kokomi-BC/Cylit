"""断点的**按路归属**：一条行可能同时被两路标为边界，单值字段记得住一个。

这不是洁癖：`semantic_break_side` 是单值，而 A 路的英文锚点与 B 路的中文块边界经常落在
**同一段**。块模式的中文边界在合并时无条件写 "translation"，实测把 turboquant 的 A 路边界
从 63 个压到 38 个 —— 英文侧的分块于是少掉一半语义边界。判据改成以集合
`semantic_break_sides` 为准（没有这个字段的旧产物退回单值口径）。
"""

from __future__ import annotations

from backend.services.knowledge import chunking
from backend.services.translation.page_translate import apply_semantic_breaks

from tests.test_semantic_breaks import _rows


def test_is_break_reads_the_sides_set():
    row = {"semantic_break": True, "semantic_break_side": "translation",
           "semantic_break_sides": ["original", "translation"]}
    assert chunking.is_break(row) is True          # 并集：不看路
    assert chunking.is_break(row, "a") is True     # 两路都标了 → 两路都算
    assert chunking.is_break(row, "b") is True


def test_is_break_falls_back_to_the_single_side_for_old_products():
    """存量产物没有 `semantic_break_sides`：口径必须与改造前逐字一致。"""
    row = {"semantic_break": True, "semantic_break_side": "original"}
    assert chunking.is_break(row, "a") is True
    assert chunking.is_break(row, "b") is False
    assert chunking.is_break({"semantic_break": False}, "a") is False


def test_apply_semantic_breaks_records_the_side_set():
    rows = _rows(("para", "Opening paragraph of this page.", "本页开头段。"),
                 ("para", "Recall is measured on the hidden sentence.", "召回率是在隐藏句子上测的。"))
    stats = apply_semantic_breaks(rows, ["Recall is measured on the hidden sentence."])
    assert stats["accepted"] == 1
    assert rows[1]["semantic_break_sides"] == ["original"]
    # 每次重算都清掉旧集合：否则上一轮的归属会粘在新划分上
    apply_semantic_breaks(rows, [])
    assert "semantic_break_sides" not in rows[1]


def test_both_sides_marked_means_both_roads_get_a_wall():
    """两路都标同一行时，两路的语义单元都必须在这里断开（walls 的输入就是它）。"""
    rows = [{"id": "p001-00", "index": 0, "page": 1, "type": "para", "level": 0,
             "number": "", "original": "Alpha paragraph.", "translation": "甲段。",
             "section_key": "1", "section_path": "1", "semantic_break": False},
            {"id": "p001-01", "index": 1, "page": 1, "type": "para", "level": 0,
             "number": "", "original": "Beta paragraph.", "translation": "乙段。",
             "section_key": "1", "section_path": "1", "semantic_break": True,
             "semantic_break_side": "original",
             "semantic_break_sides": ["original", "translation"]}]
    for road in ("a", "b"):
        units = chunking.semantic_units(rows, use_breaks=True, road=road)
        assert [u["rows"][0]["id"] for u in units] == ["p001-00", "p001-01"]
