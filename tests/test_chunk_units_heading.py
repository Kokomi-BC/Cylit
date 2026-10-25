"""单元层：标题不能单独成一个单元（命中一条 17 字符的标题等于交白卷）。

实测（线上两篇重建后）：text 层已经合过，但 unit 层仍会出「## 0.2 PRIOR WORK」（17 字符）、
「# A 扩展的先前工作」（11 字符）—— 因为单元是按「节点边界 + 断点 + 长度上限」切的，
断点落在标题的下一行时，标题就被切成一个单元。

约束放在 _merge_heading_units（单元记录生成**之前**）：
semantic_units 的切点契约不动（test_semantic_units / test_kb_row_chunking 钉着它）。
"""

from __future__ import annotations

from backend.services.knowledge import chunking


def _row(idx: int, kind: str, text: str, section: str = "S") -> dict:
    return {"id": f"r{idx}", "index": idx, "page": 1, "type": kind,
            "original": text, "translation": "", "section_key": section,
            "section_title": section, "section_path": [section]}


def _units(*groups: list[dict]) -> list[dict]:
    return [{"rows": list(g)} for g in groups]


def test_heading_unit_is_merged_into_the_next_one() -> None:
    parts = _units([_row(1, "heading", "## 0.2 PRIOR WORK")],
                   [_row(2, "para", "Online quantization needs no calibration.")])
    out = chunking._merge_heading_units(parts, road=None, limit=4000)
    assert len(out) == 1
    assert [r["index"] for r in out[0]["rows"]] == [1, 2]


def test_trailing_heading_unit_merges_backwards() -> None:
    parts = _units([_row(1, "para", "body")],
                   [_row(2, "heading", "# Attention Visualizations")])
    out = chunking._merge_heading_units(parts, road=None, limit=4000)
    assert len(out) == 1
    assert [r["index"] for r in out[0]["rows"]] == [1, 2]


def test_merge_respects_unit_max_chars() -> None:
    parts = _units([_row(1, "heading", "## 0.2 PRIOR WORK")],
                   [_row(2, "para", "x" * 500)])
    out = chunking._merge_heading_units(parts, road=None, limit=100)
    assert len(out) == 2


def test_normal_units_stay_untouched() -> None:
    parts = _units([_row(1, "para", "a")], [_row(2, "para", "b")])
    out = chunking._merge_heading_units(parts, road=None, limit=4000)
    assert [len(u["rows"]) for u in out] == [1, 1]
