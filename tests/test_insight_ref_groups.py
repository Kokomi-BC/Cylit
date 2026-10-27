"""解读标号的**兜底分组**：一处连续材料只写一个标号。

用户 2026-12 口径（与对话侧 `backend/agent/cite_groups.py` 同一套规则，契约见
`backend/services/understanding/group_refs.py` 顶部）：

- 正文里只有两种形态：`[A012]`（单块）与 `[A012,A013]`（一处由几块连续材料组成，
  一个方括号 + 半角逗号、无空格）；
- `[A012][A013]` 是**两个标号**（两枚按钮），后端把能证明是"同一处"的并排标号归一掉；
- 一个组 = 同一页行号相邻的几块，或 `pids` 有交集（同一块跨页两半）；
- **几何信息取不到就不合并**，认不出的短码让整组保持原样 —— 绝不猜、绝不吞号。
"""

from __future__ import annotations

import json

from backend.services.understanding import group_refs, understanding_service as us

BT = chr(96)          # 反引号（围栏 / 行内代码），写成常量免得本文件自己被当成代码块


def _locate(table: dict):
    """locate 注入：短码（大写）→ {page, row, pids}。"""
    return lambda code: table.get(str(code).upper())


def _at(page: int, row: int, *pids: str) -> dict:
    return {"page": page, "row": row, "pids": list(pids)}


#: 同页相邻（p5 的第 2 / 3 行）、同页但隔得很远（第 2 / 9 行）、跨页（p5 末 / p6 首）、
#: 同一块被切成跨页两半（pids 一样）—— 四种几何，四类判定的样本
GEO = {
    "A012": _at(5, 2, "r-aa"),
    "A013": _at(5, 3, "r-bb"),
    "A020": _at(5, 9, "r-cc"),
    "A021": _at(6, 1, "r-dd"),
    "A030": _at(7, 4, "r-ee"),
    "A031": _at(8, 1, "r-ee"),
}


# --------------------------------------------------------------------------
# 1. 扫描：两种形态、代码块跳过、认不出的原样留着
# --------------------------------------------------------------------------


def test_parse_reads_single_and_grouped_marks() -> None:
    marks = group_refs.parse_code_brackets("单块[A012]，一组[A012,A013]。")
    assert [m.codes for m in marks] == [("A012",), ("A012", "A013")]
    assert [m.raw for m in marks] == ["[A012]", "[A012,A013]"]


def test_parse_never_swallows_what_it_cannot_read() -> None:
    """论文自己的 `[12, Eq.3]` / 页码 / 年份不是标号，认不出就一个字符都不动。"""
    text = "见[12, Eq.3]、[见附录]、[2024]、[]，只有[7]与[A012]是"
    assert [m.raw for m in group_refs.parse_code_brackets(text)] == ["[A012]"]
    out, _ = group_refs.group_code_runs(text, _locate(GEO))
    for raw in ("[12, Eq.3]", "[见附录]", "[2024]", "[]", "[7]"):
        assert raw in out, f"{raw} 被吞了"


def test_code_blocks_are_skipped() -> None:
    """代码块里的标号是**示例**：归一化把示例改掉，读者就照着错形态抄了。"""
    inline = f"示例 {BT}[A012][A013]{BT} 与正文[A012][A013]。"
    fenced = f"前\n\n{BT * 3}\n[A012][A013]\n{BT * 3}\n\n后[A012][A013]"
    for text, want in ((inline, f"示例 {BT}[A012][A013]{BT} 与正文[A012,A013]。"),
                       (fenced, f"前\n\n{BT * 3}\n[A012][A013]\n{BT * 3}\n\n后[A012,A013]")):
        out, _ = group_refs.group_code_runs(text, _locate(GEO))
        assert out == want
    assert [m.raw for m in group_refs.parse_code_brackets(fenced)] == ["[A012]", "[A013]"]


def test_json_fence_tail_of_the_result_is_skipped() -> None:
    text = f"正文[A012][A013]\n\n{BT * 3}json\n{{\"refs\": [\"A012\"]}}\n{BT * 3}"
    out, _ = group_refs.group_code_runs(text, _locate(GEO))
    assert out.startswith("正文[A012,A013]") and "{\"refs\": [\"A012\"]}" in out


# --------------------------------------------------------------------------
# 2. 一个标号 = 一处：该合的合、不该合的不合
# --------------------------------------------------------------------------


def test_single_code_is_untouched() -> None:
    text = "只依据一段材料[A012]。"
    out, stats = group_refs.group_code_runs(text, _locate(GEO))
    assert out == text and stats["groups"] == 0 and stats["marks_before"] == 1


def test_already_grouped_mark_is_untouched() -> None:
    """模型自己写对了的 `[A012,A013]` 不许被拆开（哪怕几何上并不相邻）—— 只合不拆。"""
    text = "一组材料[A012,A020]。"
    out, stats = group_refs.group_code_runs(text, _locate(GEO))
    assert out == text and stats["groups"] == 0


def test_same_page_adjacent_rows_merge_into_one_mark() -> None:
    out, stats = group_refs.group_code_runs("量化误差在坐标间变得均匀[A012][A013]。", _locate(GEO))
    assert out == "量化误差在坐标间变得均匀[A012,A013]。"
    assert stats["groups"] == 1 and stats["absorbed"] == 1
    assert stats["marks_before"] == 2 and stats["marks_after"] == 1


def test_same_page_but_far_apart_does_not_merge() -> None:
    text = "两处不同的材料[A012][A020]。"
    out, _ = group_refs.group_code_runs(text, _locate(GEO))
    assert out == text, "同页但行号差 7 —— 不是一处，不许并进同一个方括号"


def test_cross_page_does_not_merge() -> None:
    text = "跨页的两块[A020][A021]。"
    out, _ = group_refs.group_code_runs(text, _locate(GEO))
    assert out == text


def test_pids_intersection_merges_the_two_halves_of_one_block() -> None:
    """同一块被切成跨页两半：几何上够不着，但行身份一样 → 一处。"""
    out, _ = group_refs.group_code_runs("这一段被切成两半[A030][A031]。", _locate(GEO))
    assert out == "这一段被切成两半[A030,A031]。"


def test_long_chain_splits_at_the_first_break() -> None:
    out, stats = group_refs.group_code_runs("[A012][A013][A020]。", _locate(GEO))
    assert out == "[A012,A013][A020]。"
    assert stats["groups"] == 1 and stats["absorbed"] == 1


def test_only_whitespace_separates_one_run_punctuation_splits() -> None:
    out, _ = group_refs.group_code_runs("[A012] [A013]。", _locate(GEO))
    assert out == "[A012,A013]。", "只隔空白仍是同一处标号（多打了一个空格而已）"
    text = "[A012]，[A013]。"
    out, _ = group_refs.group_code_runs(text, _locate(GEO))
    assert out == text, "中间隔着标点就是两处引用"


# --------------------------------------------------------------------------
# 3. 认不出：整组不合并、绝不吞号
# --------------------------------------------------------------------------


def test_unknown_code_keeps_the_whole_run_as_is() -> None:
    """组里只要有一个短码认不出（模型编的 / 索引重建后没了），整组保持原样。"""
    text = "一句话[A012][A999]。"
    out, stats = group_refs.group_code_runs(text, _locate(GEO))
    assert out == text
    assert stats["groups"] == 0 and stats["skipped_runs"] == 1


def test_missing_geometry_never_merges() -> None:
    """locate 给不出 page/row：这对不合并，正文一个字不改（绝不猜）。"""
    text = "一句话[A012][A013]。"
    out, stats = group_refs.group_code_runs(text, lambda code: None)
    assert out == text and stats["skipped_runs"] == 1
    out, stats = group_refs.group_code_runs(text, None)
    assert out == text and stats["skipped_runs"] == 1


def test_broken_locator_degrades_to_no_merge() -> None:
    def boom(code: str):
        raise RuntimeError("索引没了")

    text = "一句话[A012][A013]。"
    out, stats = group_refs.group_code_runs(text, boom)
    assert out == text and stats["groups"] == 0


# --------------------------------------------------------------------------
# 4. 组内顺序与幂等
# --------------------------------------------------------------------------


def test_group_keeps_the_document_order() -> None:
    """组内短码按**文档顺序**并列，不排序 —— 排序会打乱读者顺着原文看下去的上下文。"""
    table = {"A031": _at(3, 7), "A012": _at(3, 8)}
    out, _ = group_refs.group_code_runs("[A031][A012]。", _locate(table))
    assert out == "[A031,A012]。"


def test_running_twice_changes_nothing() -> None:
    text = "一处[A012][A013]，另一处[A030][A031]，两处[A020][A021]，坏的[A012][A999]。"
    once, _ = group_refs.group_code_runs(text, _locate(GEO))
    twice, stats = group_refs.group_code_runs(once, _locate(GEO))
    assert twice == once
    assert stats["groups"] == 0 and stats["absorbed"] == 0


def test_duplicate_code_is_written_once() -> None:
    out, _ = group_refs.group_code_runs("[A012][A012]。", _locate(GEO))
    assert out == "[A012]。"


# --------------------------------------------------------------------------
# 5. 落库链路：inventory 自带几何，codes 照旧列全
# --------------------------------------------------------------------------


def _inventory() -> list[dict]:
    """材料清单条目（与 `_inventory_items` 同构：行号在 row_ids、身份在 pids）。"""
    return [
        {"code": "A012", "chunk_id": "c12", "page": 5, "row_ids": ["p005-02", "p005-03"],
         "pids": ["r-aa", "r-ab"]},
        {"code": "A013", "chunk_id": "c13", "page": 5, "row_ids": ["p005-04"], "pids": ["r-ac"]},
        {"code": "A020", "chunk_id": "c20", "page": 5, "row_ids": ["p005-20"], "pids": ["r-ba"]},
    ]


def test_apply_ref_groups_uses_the_inventory_geometry() -> None:
    out, stats = group_refs.apply_ref_groups("一处[A012][A013]，两处[A013][A020]。", _inventory())
    assert out == "一处[A012,A013]，两处[A013][A020]。", "前一块的末行(03)接后一块的首行(04)"
    assert stats["groups"] == 1


def test_apply_ref_groups_without_row_ids_does_not_merge() -> None:
    """老数据的存量清单只有页码、没有行号与 pids：判不出"是不是相邻的两块" → 原样留着。"""
    inventory = [{"code": "A012", "chunk_id": "c12", "page": 5},
                 {"code": "A013", "chunk_id": "c13", "page": 5}]
    text = "一处[A012][A013]。"
    out, stats = group_refs.apply_ref_groups(text, inventory)
    assert out == text and stats["groups"] == 0 and stats["marks_after"] == 2


def test_apply_ref_groups_without_any_page_does_not_merge() -> None:
    """清单条目连页码都没有 → 这一对完全认不出 → 不合并（也不会把 run 记成已归一）。"""
    inventory = [{"code": "A012", "chunk_id": "c12"}, {"code": "A013", "chunk_id": "c13"}]
    text = "一处[A012][A013]。"
    out, stats = group_refs.apply_ref_groups(text, inventory)
    assert out == text and stats["skipped_runs"] == 1


def test_grouped_mark_still_lists_every_code() -> None:
    """契约 3：组不是新 code —— `codes` 里照样列出组内每个短码（前端据此取并集闪）。"""
    out, _ = group_refs.apply_ref_groups("一处[A012][A013]。", _inventory())
    assert us.merge_ref_codes([], out) == ["A012", "A013"]


def test_inventory_items_carry_row_ids() -> None:
    """分组要用行号：清单条目必须带上 metadata 的 row_ids（与 pids 同序等长）。"""
    class _Row:
        id = "c12"
        chunk_type = "unit"
        page_no = 5
        section = "1"
        content_preview = "预览"
        metadata_json = json.dumps({"road": "a", "lang": "en", "row_ids": ["p005-02"],
                                    "pids": ["r-aa"]})

    items = us._inventory_items([_Row()], "a", ("en", "any"))
    assert items[0]["row_ids"] == ["p005-02"]
    assert items[0]["pids"] == ["r-aa"]


# --------------------------------------------------------------------------
# 6. 提示词：一处一个标号（这条与对话侧同一口径）
# --------------------------------------------------------------------------


def test_prompt_asks_for_one_mark_per_place() -> None:
    from backend.services.understanding import prompts

    text = prompts._SHARED
    assert "一处连续材料只写一个标号" in text
    assert "[A012,A013]" in text, "示例必须就是最终形态：一个方括号 + 半角逗号"
    assert "[A023][A031]" not in text.split("3. **一处连续材料")[1].split("4. ")[0]
