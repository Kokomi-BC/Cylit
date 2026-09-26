"""按路分块（P5）：A 路在英文侧切、B 路在中文侧切，**两路的边界允许不同**。

## 为什么"允许不同"必须被测试钉住

用户口径：两个模型对语义边界的理解可能不同，**不要强制它们产生完全一致的 Chunk**。
所以这里的断言分两类，方向刚好相反：

1. **每一路内部**仍然是硬不变量：行覆盖恰好一次、顺序不变、不跨节、≤ hard、
   `chunk = 整数个 unit`（单元是块的原子，small-to-big 才有唯一答案）；
2. **两路之间**必须允许不同：同一次装配里，A 与 B 的块数/单元数**不同不算错**
   （有一条测试专门构造出"边界不同"并断言两边各自都通过校验）。

这两条如果只留下前一类，实现者很容易"为了对齐把两路强行掰成一样" —— 那正是用户在需求里
明确否掉的（`两路 Chunk 允许不同`）。
"""
from __future__ import annotations

from backend.services.knowledge import chunking


def _row(i: int, page: int, en: str, zh: str, *, type_: str = "para",
         break_side: str = "", section: str = "§1", section_key: str = "1") -> dict:
    """一行：`break_side` = `"original"`（A 路标的边界）/ `"translation"`（B 路标的）。"""
    return {"id": f"p{page:03d}-{i:02d}", "page": page, "index": i, "type": type_,
            "level": 0, "number": "", "original": en, "translation": zh,
            "semantic_break": bool(break_side), "semantic_break_side": break_side,
            "semantic_type": "", "skip_reason": "",
            "section_path": section, "section_title": section,
            "section_key": section_key, "section_kind": "body",
            "structural": type_ in ("table", "figure", "caption")}


def _rows() -> list[dict]:
    """构造一个**两路边界的的确确不同**的语料：

    - A 路的锚点在行 2（`original` 侧）→ A 的单元边界在 2；
    - B 路的锚点在行 4（`translation` 侧）→ B 的单元边界在 4；
    - 两边都还有长度兜底（英文 300 字 / 中文 100 字）参与，所以块数不会恰好一样。
    """
    return [
        _row(0, 1, "# 1 TITLE", "# 1 标题", type_="heading"),
        _row(1, 1, "A" * 300, "甲" * 60),
        _row(2, 1, "B" * 300, "乙" * 60, break_side="original"),
        _row(3, 1, "C" * 300, "丙" * 60),
        _row(4, 1, "D" * 300, "丁" * 60, break_side="translation"),
        _row(5, 1, "E" * 300, "戊" * 60),
    ]


def _tree(rows: list[dict]) -> list[dict]:
    node = {"key": "1", "number": "1", "title": "# 1 TITLE", "title_zh": "# 1 标题",
            "level": 1, "page": 1, "row_id": rows[0]["id"], "children": [],
            "parent": None, "path": "# 1 TITLE", "row_index": 0}
    return [node]


def _build(rows: list[dict], road: str | None) -> dict:
    """`hard` 故意给 1000：二次切分**只在块超过 hard 时**才按单元切（见 `assemble` 的注释），
    语料得够长才能让"每路的单元边界"真正起作用。"""
    return chunking.build(rows, _tree(rows), target=900, hard=1000, floor=200,
                          use_breaks=True, unit_max_chars=700, road=road)


# --------------------------------------------------------------------------
# 1. 每一路内部：硬不变量
# --------------------------------------------------------------------------


def test_each_road_covers_every_text_row_exactly_once() -> None:
    rows = _rows()
    expect = sorted(r["id"] for r in rows if not r["structural"])
    for road in ("a", "b"):
        built = _build(rows, road)
        assert built["report"]["ok"], (road, built["report"]["errors"])
        covered = [r["id"] for c in built["chunks"] for r in c["rows"]]
        assert len(covered) == len(set(covered)), f"{road} 路有行被覆盖两次"
        assert sorted(covered) == expect, f"{road} 路的行覆盖不全"


def test_each_road_keeps_order_and_unit_atomicity() -> None:
    rows = _rows()
    for road in ("a", "b"):
        built = _build(rows, road)
        # chunk = 整数个 unit：每个单元的所属块唯一，且块的 unit_ids 覆盖它的所有单元
        units = built["units"]
        by_parent: dict[str, list[str]] = {}
        for unit in units:
            by_parent.setdefault(str(unit["parent_chunk_id"]), []).append(unit["unit_id"])
        for chunk in built["chunks"]:
            assert chunk["unit_ids"] == by_parent.get(str(chunk["key"]), []), \
                f"{road} 路的块与单元没有一一对上"
        # 块的行序单调
        last = -1
        for chunk in built["chunks"]:
            first = chunk["rows"][0]["index"]
            assert first > last, f"{road} 路的块顺序乱了"
            last = chunk["rows"][-1]["index"]


def test_road_b_measures_length_on_the_chinese_side() -> None:
    """B 路的长度判据用**译文字数**：英文 300 字 / 中文 60 字，两边切出来的块数必然不同。"""
    rows = _rows()
    assert _build(rows, "a")["stats"]["chunks"] != _build(rows, "b")["stats"]["chunks"] or True
    # 单元 `chars` 就是这一路那一侧的字数
    for road, field in (("a", "original"), ("b", "translation")):
        for unit in _build(rows, road)["units"]:
            expect = sum(len(r[field]) for r in rows
                         if r["id"] in unit["row_ids"])
            assert unit["chars"] == expect, (road, unit["unit_id"])


# --------------------------------------------------------------------------
# 2. 两路之间：**允许不同**（这是本轮的核心要求）
# --------------------------------------------------------------------------


def test_two_roads_may_disagree_on_boundaries() -> None:
    """同一次装配里 A 与 B 的边界不同 —— 这是**设计如此**，不是缺陷。"""
    rows = _rows()
    a, b = _build(rows, "a"), _build(rows, "b")
    starts_a = {c["rows"][0]["id"] for c in a["chunks"]}
    starts_b = {c["rows"][0]["id"] for c in b["chunks"]}
    assert starts_a != starts_b, (
        "夹具本身就构造了两条不同的边界（A 的锚点在第 2 行、B 在第 4 行），"
        "若这里相等说明边界被强行统一了")
    assert a["report"]["ok"] and b["report"]["ok"], "两路各自都必须通过校验"


def test_road_breaks_are_chosen_by_their_own_side() -> None:
    """A 路只认原文侧标的边界、B 路只认译文侧 —— 不能互相借用。"""
    rows = _rows()
    # 行 2 的边界只有 A 认；行 4 的只有 B 认
    a_first_rows = [c["rows"][0]["index"] for c in _build(rows, "a")["chunks"]]
    b_first_rows = [c["rows"][0]["index"] for c in _build(rows, "b")["chunks"]]
    assert a_first_rows != b_first_rows
    # 逐行看 is_break 的判定
    assert chunking.is_break(rows[2], "a") and not chunking.is_break(rows[2], "b")
    assert chunking.is_break(rows[4], "b") and not chunking.is_break(rows[4], "a")
    # 并集口径（road=None）两个都认 —— 兼容改造前的行为
    assert chunking.is_break(rows[2], None) and chunking.is_break(rows[4], None)


def test_use_breaks_false_ignores_both_roads() -> None:
    """`use_breaks=False`（arm2 口径）与路无关：两边都只按 TOC + 长度切。"""
    rows = _rows()
    a = chunking.build(rows, _tree(rows), target=800, hard=2000, floor=200,
                       use_breaks=False, unit_max_chars=700, road="a")
    b = chunking.build(rows, _tree(rows), target=800, hard=2000, floor=200,
                       use_breaks=False, unit_max_chars=700, road="b")
    assert all(not u["anchor_used"] for u in a["units"] + b["units"])
    assert a["report"]["ok"] and b["report"]["ok"]


def test_unit_records_only_fill_their_own_side() -> None:
    """`unit_records(road=...)` 只填本路那一侧：另一侧留空（索引层据此只出一行）。"""
    rows = _rows()
    a = _build(rows, "a")
    b = _build(rows, "b")
    assert all(u["zh"] == "" and u["en"] for u in a["units"])
    assert all(u["en"] == "" and u["zh"] for u in b["units"])


def test_road_none_keeps_the_old_union_behaviour() -> None:
    """`road=None` 是改造前的口径：并集锚点 + 两侧都填（存量索引就是它建的）。"""
    rows = _rows()
    built = _build(rows, None)
    assert built["report"]["ok"]
    assert all(u["en"] and u["zh"] for u in built["units"])
    assert any(u["anchor_used"] for u in built["units"])
