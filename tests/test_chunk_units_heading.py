"""单元层：只有标题的单元不写进库（命中一条 17 字符的标题等于交白卷）。

为什么落在「写库」这一步，而不是改分区：
- 「单元恰好覆盖每个正文行一次」「单元不超 unit_max_chars」都是校验层与测试钉着的不变量；
- 更上游的两种写法都不成立：把标题行从分区里剔掉会破覆盖不变量；把标题并进下一个单元
  又常被 unit_max_chars 挡回去（后面那条单元往往已经顶到上限），线上因此仍剩 37 条
  小于 60 字符的 unit —— 真正的毛病是那几条 17`47 字符的纯标题块；
- 只看 type == "heading" 不够：线上有一批「## 0.2 PRIOR WORK」在解析侧是 para，
  所以判据要同时看文字（chunking.is_heading_like）。
"""

from __future__ import annotations

from backend.services.knowledge import chunking


def _row(idx: int, kind: str, text: str, section: str = "S") -> dict:
    return {"id": f"r{idx}", "index": idx, "page": 1, "type": kind,
            "original": text, "translation": "", "section_key": section,
            "section_title": section, "section_path": [section]}


def _kept(rows: list[dict], candidates: list[dict]) -> list[dict]:
    # 与生产同一条判据：**整批行**算一次标题集合（标题被排版切成两块时要一起算）
    heading_ids = chunking.heading_like_ids(rows)
    return [u for u in candidates
            if not all(rid in heading_ids for rid in u["row_ids"])]


def test_heading_like_detection_covers_para_typed_markdown_headings() -> None:
    assert chunking.is_heading_like(_row(1, "para", "## 0.2 PRIOR WORK"))
    assert chunking.is_heading_like(_row(1, "heading", "Some Title"))
    assert not chunking.is_heading_like(
        _row(1, "para", "## 0.1 PROBLEM DEFINITION  Formally, our goal is to … ."))
    assert not chunking.is_heading_like(_row(1, "para", "普通正文，没有井号"))


def test_title_split_across_two_blocks_is_one_heading() -> None:
    """标题被排版切成两块时（第二块不带 #），整条单元仍然只算标题 —— 用户口径 2026-12-05。

    线上实测的两条：`## 2.2 NEEDLE-IN-A-HAYSTACK` 与后半截 `（Needle-in-a-Haystack）`；
    单看第二行它是正文（没有井号、也不长），于是这个单元照样写进了库，点开引用只有一行章节名。
    """
    rows = [_row(1, "heading", "## 2.2 NEEDLE-IN-A-HAYSTACK"),
            _row(2, "para", "（Needle-in-a-Haystack）")]
    assert {"r1", "r2"} <= chunking.heading_like_ids(rows)
    assert _kept(rows, [{"row_ids": ["r1", "r2"]}]) == []
    # 续行必须**紧挨**着标题：隔了正文就不再算（否则会把正文里的短句一起吞掉）
    rows2 = [_row(1, "heading", "## 2.2 NEEDLE-IN-A-HAYSTACK"),
             _row(2, "para", "We evaluated TURBOQUANT on Llama-3.1."),
             _row(3, "para", "（Needle-in-a-Haystack）")]
    assert chunking.heading_like_ids(rows2) == {"r1"}
    # 公式 / 表格行不许因为"短"被当成标题续行
    rows3 = [_row(1, "heading", "## 2.3 END-TO-END"),
             _row(2, "formula", "$$D_{\text{mse}}$$")]
    assert chunking.heading_like_ids(rows3) == {"r1"}


def test_only_heading_units_are_dropped_before_writing() -> None:
    rows = [_row(1, "para", "## 0.2 PRIOR WORK"),
            _row(2, "para", "Online quantization needs no calibration.")]
    candidates = [{"row_ids": ["r1"]}, {"row_ids": ["r2"]}]
    assert [u["row_ids"] for u in _kept(rows, candidates)] == [["r2"]]


def test_body_unit_is_kept_even_if_it_starts_with_a_hash() -> None:
    rows = [_row(1, "para",
                 "## 0.1 PROBLEM DEFINITION  Formally, our goal is to design a quantizer.")]
    assert len(_kept(rows, [{"row_ids": ["r1"]}])) == 1
