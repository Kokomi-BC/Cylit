"""P4b 共享坐标契约：解析器块号做两侧共同的行格，按块号配对。

为什么需要契约（对应 P4 测出来的两类修不掉的毛病）：

- **整页错位**：实测 turbo p1，A 把摘要相邻两句并成一块、B 拆成两块；两侧块数不等时
  `pair_rows` 只能靠结构键差分，"多出来的那段该插在哪"**不可判定**，匹配器把插入放到页尾，
  于后面每行都错位一行。
- **页首分歧**：两个同级标题（层级编号都为空）在键上完全一样，分不清谁是谁 ——
  实测把英文标题配到了中文摘要上，`_row_key` 加上全大写缩略语也修不掉。

契约 = 两侧都声明"我的每个输出块覆盖哪些解析器块"，合并时按块号取交。
`1:N` 合并因此能表示成**一行**（左 = A 那一块，右 = B 那两块拼起来），既不错位也不留空栏。
"""

from __future__ import annotations

from backend.services.translation import coords


def _blocks(*texts: str) -> list[dict]:
    return [{"text": t, "type": "para", "order": i + 1} for i, t in enumerate(texts)]


# --------------------------------------------------------------------------
# 1. 坐标清单与契约文本
# --------------------------------------------------------------------------


def test_codes_and_lattice_text() -> None:
    blocks = _blocks("第一段", "第二段", "第三段")
    assert coords.codes_of(blocks) == ["b01", "b02", "b03"]
    text = coords.lattice_text(blocks)
    assert "[b01]" in text and "(para)" in text
    # 空文本块不参与坐标（编号也不占位）
    assert coords.codes_of([{"text": "x"}, {"text": ""}, {"text": "y"}]) == ["b01", "b03"]


def test_src_tail_says_it_is_required_and_how_to_merge() -> None:
    tail = coords.SRC_TAIL
    assert "src" in tail
    # 三种情形都要讲清楚（1:1 / 1:N 合并 / 1 拆 2）
    assert '"b03"' in tail and '"b03|b04"' in tail
    assert "项数" in tail and "必须相等" in tail
    # 写不出来可以给空数组 → 系统退回结构配对（不留死路）
    assert "空数组" in tail


# --------------------------------------------------------------------------
# 2. 解析与校验
# --------------------------------------------------------------------------


def test_parse_src_from_fenced_json() -> None:
    raw = '原文\n\n```json\n{"original_md": "x", "src": ["b01", "b02|b03"]}\n```'
    assert coords.parse_src(raw) == ["b01", "b02|b03"]


def test_parse_src_absent_returns_empty() -> None:
    assert coords.parse_src('{"original_md": "x"}') == []
    assert coords.parse_src("没有 JSON") == []


def test_split_codes_normalizes() -> None:
    assert coords.split_codes("B02|b03") == ["b02", "b03"]
    assert coords.split_codes("") == []


def test_validate_src_accepts_a_full_partition() -> None:
    codes = ["b01", "b02", "b03"]
    covered, why = coords.validate_src(["b01", "b02|b03"], codes, block_count=2)
    assert why == "" and covered == [["b01"], ["b02", "b03"]]


def test_validate_src_accepts_one_block_split_into_two_blocks() -> None:
    """一个块被拆成两个输出块：同一个块号可以出现在**相邻**两项里。"""
    covered, why = coords.validate_src(["b01", "b02", "b02"], ["b01", "b02"], block_count=3)
    assert why == "" and covered == [["b01"], ["b02"], ["b02"]]


def test_validate_src_rejects_count_mismatch() -> None:
    _, why = coords.validate_src(["b01"], ["b01", "b02"], block_count=3)
    assert "项数" in why


def test_validate_src_rejects_unknown_code() -> None:
    _, why = coords.validate_src(["b01", "b09"], ["b01", "b02"], block_count=2)
    assert "不在本页" in why


def test_validate_src_rejects_missing_coverage() -> None:
    """漏掉一个块 → 坐标不可信，必须让调用方退回结构配对（半个坐标比没有更坏）。"""
    _, why = coords.validate_src(["b01"], ["b01", "b02"], block_count=1)
    assert "没覆盖" in why


def test_validate_src_rejects_out_of_order() -> None:
    _, why = coords.validate_src(["b02", "b01"], ["b01", "b02"], block_count=2)
    assert "顺序" in why


def test_validate_src_rejects_empty_item() -> None:
    _, why = coords.validate_src(["b01", ""], ["b01"], block_count=2)
    assert "空项" in why


# --------------------------------------------------------------------------
# 3. 合并（这一层才是契约的价值所在）
# --------------------------------------------------------------------------


def test_same_partition_pairs_one_to_one() -> None:
    md_a = "Alpha paragraph.\n\nBeta paragraph."
    md_b = "甲段落。\n\n乙段落。"
    rows, quality = coords.pair_rows_by_src(md_a, ["b01", "b02"], md_b, ["b01", "b02"], 1)
    assert quality == "coords"
    assert [r["original"] for r in rows] == ["Alpha paragraph.", "Beta paragraph."]
    assert [r["translation"] for r in rows] == ["甲段落。", "乙段落。"]
    assert [r["id"] for r in rows] == ["p001-00", "p001-01"]


def test_merge_becomes_one_row_not_a_shift() -> None:
    """**P4b 的核心用例**：A 把两段并成一块、B 拆成两块 → 合成一行，而不是错位一行。

    没有契约时 `pair_rows` 会把这页后面所有行都错开（实测 turbo p1 的失败形态）。
    """
    md_a = "First part and second part merged.\n\nThird part."
    md_b = "第一部分。\n\n第二部分。\n\n第三部分。"
    rows, quality = coords.pair_rows_by_src(
        md_a, ["b01|b02", "b03"], md_b, ["b01", "b02", "b03"], 6)
    assert quality == "coords"
    assert len(rows) == 2, "合并必须变成一行，不是两行"
    assert rows[0]["original"] == "First part and second part merged."
    assert rows[0]["translation"] == "第一部分。\n\n第二部分。", "译文两段拼在同一行，且不重复原文"
    assert rows[1]["original"] == "Third part." and rows[1]["translation"] == "第三部分。"


def test_side_missing_a_block_never_starts_a_new_row() -> None:
    """一侧漏了一段（B 只覆盖了 b01、没覆盖 b02）→ 并进当前行，绝不新开一行去错位。"""
    md_a = "Only part."
    md_b = "第一段（对面把两段并成一块了）。"
    rows, quality = coords.pair_rows_by_src(md_a, ["b01|b02"], md_b, ["b01"], 3)
    assert quality == "coords"
    assert len(rows) == 1, "块号 b02 没有对面主人 → 并入当前行，不新开行"
    assert rows[0]["original"] == "Only part."
    assert "第一段" in rows[0]["translation"]


def test_head_divergence_is_resolved_by_coordinates() -> None:
    """页首分歧：同层级标题 + 作者行，靠块号配对而不是靠猜测。"""
    md_a = "# TITLE\n\nAuthors\n\n# ABSTRACT\n\nBody."
    md_b = "# 标题\n\n作者\n\n# 摘要\n\n正文。"
    rows, _ = coords.pair_rows_by_src(
        md_a, ["b01", "b02", "b03", "b04"], md_b, ["b01", "b02", "b03", "b04"], 1)
    assert rows[0]["original"].startswith("# TITLE") and rows[0]["translation"].startswith("# 标题")
    assert rows[2]["original"].startswith("# ABSTRACT") and rows[2]["translation"].startswith("# 摘要")


def test_block_split_into_two_rows_keeps_text_once_each() -> None:
    """一个块被拆成两个输出块：两侧都拆在同一位置 → 两行，文本不重复。"""
    md_a = "Part one. Part two."
    md_b = "第一部分。第二部分。"
    rows, _ = coords.pair_rows_by_src(md_a, ["b01"], md_b, ["b01"], 1)
    assert len(rows) == 1


def test_pair_rows_by_src_refuses_when_block_count_disagrees() -> None:
    """输出块数与 src 项数对不上 → 返回空，让调用方退回 `pair_rows`。"""
    rows, quality = coords.pair_rows_by_src("A\n\nB", ["b01"], "甲", ["b01"], 1)
    assert rows == [] and quality == ""


def test_pair_rows_by_src_handles_empty_markdown() -> None:
    rows, quality = coords.pair_rows_by_src("", [], "", [], 1)
    assert rows == [] and quality == ""
