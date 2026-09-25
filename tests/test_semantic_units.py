"""语义单元层单测（Fine-grained Semantic Unit）：单元划分 / 父子关系 / ±1 邻居 / 校验不变量。

与 `test_kb_row_chunking.py` 的分工：那边测"行 → chunk"的装配与校验，
这边专测**新加的单元层**（`chunk_type="unit"` 那一层，检索单位）与它的两条不变量：
`row ⊆ unit ⊆ chunk`（单元是 chunk 的原子）、每个单元恰有一个父块。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge import chunking


def _row(i: int, page: int, text: str, *, type_: str = "para", zh: str = "",
         break_: bool = False, skip: str = "", section: str = "§1",
         section_key: str = "1") -> dict:
    return {"id": f"p{page:03d}-{i:02d}", "page": page, "index": i, "type": type_,
            "level": 0, "number": "", "original": text, "translation": zh,
            "semantic_break": break_, "skip_reason": skip,
            "section_path": section, "section_title": section,
            "section_key": section_key, "section_kind": "body",
            "structural": type_ in ("table", "figure", "caption")}


def _rows() -> list[dict]:
    """一棵两节点的树：§1（含 1.1）+ §2；行里埋了锚点、超长行、结构行与跳过行。"""
    return [
        _row(0, 1, "# 1 TITLE", type_="heading", zh="# 1 标题"),
        _row(1, 1, "A" * 300, zh="甲" * 100),
        _row(2, 1, "B" * 300, zh="乙" * 100, break_=True),
        _row(3, 1, "C" * 300, zh="丙" * 100),
        _row(4, 1, "D" * 300, zh="丁" * 100, break_=True),
        _row(5, 1, "## 1.1 Sub", type_="heading", zh="## 1.1 子", section="§1 / 1.1",
             section_key="1.1"),
        _row(6, 1, "E" * 300, zh="戊" * 100, section="§1 / 1.1", section_key="1.1"),
        _row(7, 1, "F" * 900, zh="己" * 300, section="§1 / 1.1", section_key="1.1"),
        _row(8, 2, "**Table 1: Demo.**", type_="caption", zh="**表 1：示例。**"),
        _row(9, 2, "| a | b |\n|---|---|\n| 1 | 2 |", type_="table", zh="| a | b |"),
        _row(10, 2, "ref entry", skip="ref_section"),
    ]


def _tree(rows: list[dict]) -> list[dict]:
    def node(key, title, row_id, children=None, path=None):
        item = {"key": key, "number": key, "title": title, "title_zh": title,
                "level": 1, "page": 1, "row_id": row_id, "children": children or [],
                "parent": None, "path": path or title}
        for child in item["children"]:
            child["parent"] = item
        return item

    sub = node("1.1", "## 1.1 Sub", "p001-05", path="# 1 TITLE / ## 1.1 Sub")
    root = node("1", "# 1 TITLE", "p001-00", [sub])
    index = {r["id"]: r["index"] for r in rows}
    for item in chunking._walk([root]):
        item["row_index"] = index.get(item["row_id"])
    return [root]


@pytest.fixture()
def paper(monkeypatch):
    rows = _rows()
    tree = _tree(rows)
    monkeypatch.setattr(chunking, "row_units", lambda pid, **kw: rows)
    monkeypatch.setattr(chunking, "section_tree", lambda pid, rws=None: tree)
    return rows, tree


def _build(rows, tree, **kw):
    params = {"target": 1200, "hard": 2000, "floor": 200, "use_breaks": True,
              "unit_max_chars": 700}
    params.update(kw)
    return chunking.build(rows, tree, **params)


# --------------------------------------------------------------------------
# 1. 单元划分
# --------------------------------------------------------------------------


def test_units_partition_rows_exactly_once(paper):
    rows, tree = paper
    built = _build(rows, tree)
    units = built["units"]
    covered = [rid for u in units for rid in u["row_ids"]]
    expected = [r["id"] for r in rows
                if not r.get("skip_reason") and not chunking._is_structural(r)]
    assert covered == expected, "单元必须按行序恰好覆盖一次（不漏、不重、不乱序）"


def test_units_split_at_anchor_and_cap(paper):
    rows, tree = paper
    built = _build(rows, tree)
    spans = [u["row_ids"] for u in built["units"]]
    # `semantic_break` 的语义是"**这一行**开始一个新单元"：
    #   p001-02 带锚点 → 新单元（并带上 p001-03 直到下一个锚点）
    #   p001-04 带锚点 → 又一个新单元
    #   p001-05 是小节标题 → TOC 边界，必然切
    assert spans == [["p001-00", "p001-01"], ["p001-02", "p001-03"],
                     ["p001-04"], ["p001-05", "p001-06"], ["p001-07"]]
    # 900 字单行超过 unit_max=700，但**单行不可拆** → 仍是一条单元
    assert ["p001-07"] in spans
    assert all(u["chars"] <= 700 for u in built["units"] if len(u["row_ids"]) > 1)


def test_cap_splits_long_run_without_anchors(paper):
    rows, tree = paper
    # 把锚点去掉，让 §1 的正文变成一整段（300×4 = 1200），cap=700 必须在行边界切
    for row in rows:
        row["semantic_break"] = False
    built = _build(rows, tree, unit_max_chars=700)
    spans = [tuple(u["row_ids"]) for u in built["units"]]
    assert ("p001-00", "p001-01", "p001-02") in spans      # 300*3 = 900 > 700？见下
    # 精确口径：标题 8 字 + 300 + 300 = 608 ≤ 700 再 +300 = 908 > 700 → 在第三行前切
    assert ("p001-03", "p001-04") in spans
    assert built["stats"]["unit_capped"] >= 1


def test_cap_never_splits_single_row(paper):
    rows, tree = paper
    for row in rows:
        row["semantic_break"] = False
    built = _build(rows, tree, unit_max_chars=100)          # 故意调得比任何一行都小
    singles = [u for u in built["units"] if u["chars"] > 100]
    assert singles and all(len(u["row_ids"]) == 1 for u in singles)


# --------------------------------------------------------------------------
# 2. 父子关系与邻居
# --------------------------------------------------------------------------


def test_every_unit_has_exactly_one_parent_chunk(paper):
    rows, tree = paper
    built = _build(rows, tree)
    chunk_keys = {c["key"] for c in built["chunks"]}
    for unit in built["units"]:
        assert unit["parent_chunk_id"] in chunk_keys
    # chunk 的 unit_ids 恰好覆盖它的行
    for chunk in built["chunks"]:
        row_ids = [r["id"] for r in chunk["rows"]]
        unit_ids = chunk["unit_ids"]
        assert unit_ids, "每条 chunk 至少要有一个单元"
        by_id = {u["unit_id"]: u for u in built["units"]}
        covered = [rid for uid in unit_ids for rid in by_id[uid]["row_ids"]]
        assert covered == row_ids, (chunk["key"], covered, row_ids)


def test_neighbours_are_within_the_same_parent(paper):
    rows, tree = paper
    built = _build(rows, tree)
    by_id = {u["unit_id"]: u for u in built["units"]}
    for unit in built["units"]:
        for field in ("prev", "next"):
            other = unit[field]
            if other is None:
                continue
            assert other in by_id
            assert by_id[other]["parent_chunk_id"] == unit["parent_chunk_id"], field


def test_first_and_last_unit_have_no_dangling_neighbour(paper):
    rows, tree = paper
    built = _build(rows, tree)
    groups: dict[str, list[dict]] = {}
    for unit in built["units"]:
        groups.setdefault(str(unit["parent_chunk_id"]), []).append(unit)
    for group in groups.values():
        assert group[0]["prev"] is None and group[-1]["next"] is None


# --------------------------------------------------------------------------
# 3. 校验层的单元不变量
# --------------------------------------------------------------------------


def _valid_units(built):
    return [dict(u) for u in built["units"]]


def test_validate_accepts_unit_layer(paper):
    rows, tree = paper
    built = _build(rows, tree)
    assert built["report"]["ok"], built["report"]["errors"]


def test_validate_flags_unit_without_parent(paper):
    rows, tree = paper
    built = _build(rows, tree)
    units = _valid_units(built)
    orphan = units[0]["unit_id"]
    # 没有任何 chunk 认领这条单元 → unit_without_parent
    chunks = [{**c, "unit_ids": [u for u in c["unit_ids"] if u != orphan]}
              for c in built["chunks"]]
    report = chunking.validate(chunks, rows, hard=2000, units=units)
    assert any(e["kind"] == "unit_without_parent" for e in report["errors"])


def test_validate_flags_unit_split_across_chunks(paper):
    rows, tree = paper
    built = _build(rows, tree)
    units = _valid_units(built)
    # 把第一条单元的行挂到一条不含它的 chunk 上 → 单元被拆到两个父里
    other = next(c for c in built["chunks"]
                 if units[0]["row_ids"][0] not in [r["id"] for r in c["rows"]])
    chunks = [dict(c) for c in built["chunks"]]
    for chunk in chunks:
        if chunk["key"] == other["key"]:
            chunk["unit_ids"] = list(chunk["unit_ids"]) + [units[0]["unit_id"]]
    report = chunking.validate(chunks, rows, hard=2000, units=units)
    assert any(e["kind"] == "unit_split_across_chunks" for e in report["errors"])


def test_validate_flags_over_max_multi_row_unit(paper):
    rows, tree = paper
    built = _build(rows, tree)
    units = _valid_units(built)
    for unit in units:
        if len(unit["row_ids"]) > 1:
            unit["chars"] = 5000
            break
    report = chunking.validate(built["chunks"], rows, hard=2000, units=units)
    assert any(e["kind"] == "unit_over_max" for e in report["errors"])


def test_validate_flags_out_of_order_units(paper):
    rows, tree = paper
    built = _build(rows, tree)
    units = list(reversed(_valid_units(built)))
    report = chunking.validate(built["chunks"], rows, hard=2000, units=units)
    assert any(e["kind"] == "unit_order" for e in report["errors"])


def test_unit_records_do_not_generate_text(paper):
    """单元正文必须**逐字来自行**（LLM 只分组、代码取原文）。"""
    rows, tree = paper
    built = _build(rows, tree)
    by_id = {r["id"]: r for r in rows}
    for unit in built["units"]:
        expected_en = "\n\n".join(by_id[rid]["original"] for rid in unit["row_ids"])
        assert unit["en"] == expected_en
        expected_zh = "\n\n".join(by_id[rid]["translation"].strip()
                                  for rid in unit["row_ids"]
                                  if (by_id[rid]["translation"] or "").strip())
        assert unit["zh"] == expected_zh
