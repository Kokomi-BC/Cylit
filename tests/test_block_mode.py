"""**块模式**（A 定块、B 照译）：B 的 prompt、产物、读时配对。

为什么要单开一份契约：两路各自分块时，"两侧块结构对得上"只能**事后**靠 `pair_rows`
猜（实测 46 页里 43% 走到按块号配对、30% 掉到 difflib 兜底）。块模式把这件事前移：
B 收到的就是 A 的块清单，逐块译、逐块回 —— 于是"对得上"变成**生成时的约定**，
读时只需按序号配 `pair_rows_by_blocks`。这份测试钉住这条链路的四段：

1. prompt：分段规则 / `breaks` / `src` / carry **都不在** B 的契约里（都归 A 了）；
2. 输出契约：定长字符串数组 `blocks`（不是 `translation_md`）；
3. 解析：宽容取 `blocks`，取不到返回空数组（调用方判这一页失败，不静默降级）；
4. 配对：块数相等 → 序号 1:1 且质量标记 `blocks`；块数不等 → 返回空让调用方退回旧兜底。
"""
from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.services.translation.page_translate import (
    BLOCK_HEAD_NOTE, BLOCK_TRANSLATE_SYSTEM, PAGE_SCHEMA_B_BLOCKS,
    PAGE_USER_TEMPLATE_B_BLOCKS, PageCallResult, TRANSLATE_SYSTEM,
    _SHARED_SEGMENTATION_RULES, a_page_lookahead, build_path_payload,
    format_block_list, parse_blocks, parse_break_flags, pair_rows_by_blocks,
    parse_carry, render_block_prompt, split_blocks,
)

A_MD = """# 3 METHOD

TurboQuant quantizes the residual.

| Method | Score |
|---|---|
| A | 0.85 |
"""


# --------------------------------------------------------------------------
# 1. prompt：职责划界（分块 / 边界 / 坐标都不再属于 B）
# --------------------------------------------------------------------------


def test_block_prompt_hands_segmentation_to_path_a() -> None:
    assert _SHARED_SEGMENTATION_RULES not in BLOCK_TRANSLATE_SYSTEM, "分段规则归 A 路了"
    assert "分块不是你的职责" in BLOCK_TRANSLATE_SYSTEM
    for gone in ("【breaks：", "src"):
        assert gone not in BLOCK_TRANSLATE_SYSTEM, f"{gone} 不该再出现在 B 的契约里"
    # 与普通 B 路 prompt 的差别是**结构性的**，不是措辞
    assert _SHARED_SEGMENTATION_RULES in TRANSLATE_SYSTEM


def test_block_prompt_keeps_what_b_still_owes() -> None:
    """块模式减的是"分块"，不是"翻译质量"：公式 / 表格 / 参考文献双语规则都留着。"""
    for marker in ("【块清单：输出必须与它一一对应】", "长度正好 N 的字符串数组",
                   "不许合并、不许拆分", "只把论文标题译成中文、用《》括起来",
                   "所有数学都必须写进公式定界符"):
        assert marker in BLOCK_TRANSLATE_SYSTEM, marker


def test_block_prompt_collects_the_page_tail_from_path_a() -> None:
    """页尾收句：A 路**提前转录**，把下一页开头给 B，让 B 把半句收成完整句（`carry_md`）。"""
    for marker in ("【页尾：这次能拿到 A 路**下一页的开头**】",
                   "把本页末尾被版面切断的那句收成完整句",
                   "把**你实际用到的那段原文**逐字符照抄进 `carry_md`",
                   "抄多会造成下一页重复、抄少会漏译",
                   # 收句**不能**改变块数：它只是最后一块内部的长短变化
                   "数组的长度**仍然必须正好 N**",
                   # 页首去重与"一句话只出现一次"
                   "【页首：上一页可能已经译掉了本页开头】", "空字符串"):
        assert marker in BLOCK_TRANSLATE_SYSTEM, marker


def test_block_schema_is_a_string_array() -> None:
    assert PAGE_SCHEMA_B_BLOCKS["required"] == ["blocks"]
    assert PAGE_SCHEMA_B_BLOCKS["properties"]["blocks"]["items"]["type"] == "string"
    assert "carry_md" in PAGE_SCHEMA_B_BLOCKS["properties"]
    assert "translation_md" not in PAGE_SCHEMA_B_BLOCKS["properties"]


def test_render_block_prompt_carries_the_contract() -> None:
    text = render_block_prompt(page=3, page_total=22, blocks="【块 1/2】（type=para）\nHi.",
                               block_count=2, prefix="PREFIX\n", section="3 METHOD",
                               previous="前文结尾",
                               lookahead="【A 路下一页开头（**仅上下文**）】\n<下一页转录原文>\nTail.",
                               head_note=BLOCK_HEAD_NOTE)
    assert "PREFIX" in text and "前文结尾" in text
    assert "第 3 页 / 共 22 页；章节：3 METHOD" in text
    assert "共 2 块" in text and "长度正好 2" in text
    assert "【块 1/2】（type=para）" in text
    # 页尾上下文与页首去重都在（块模式靠 A 路的前后两页，不靠解析器派的【页边界参考】）
    assert "A 路下一页开头" in text and "Tail." in text
    assert "【本页开头去重】" in text
    assert "carry_md" in text
    for gone in ("【页边界参考", "{src_tail}"):
        assert gone not in text, gone


def test_format_block_list_keeps_numbering_with_an_empty_block() -> None:
    """块号就是位置：空块（整块已被上一页译完）也必须占号，否则后面全部错位。"""
    blocks = [{"type": "para", "text": ""}, {"type": "para", "text": "第二块"}]
    text = format_block_list(blocks)
    assert "【块 1/2】" in text and "【块 2/2】" in text
    assert "第 1 项输出空字符串" in text
    assert "第二块" in text


def test_a_page_lookahead_reads_path_a_markdown() -> None:
    md = "communication bot-\n\ntlenecks between HBM and SRAM.\n\n![x](y.png)\n\n( a ) noise line"
    text = a_page_lookahead(md, lines=2)
    assert "communication bot-" in text and "tlenecks between HBM and SRAM." in text
    assert "<下一页转录原文>" in text
    assert a_page_lookahead("   \n\n") == ""


# --------------------------------------------------------------------------
# 2. 块清单渲染 / blocks 解析
# --------------------------------------------------------------------------


def test_format_block_list_numbers_every_block() -> None:
    blocks = split_blocks(A_MD)
    assert len(blocks) == 3, [b["type"] for b in blocks]
    text = format_block_list(blocks)
    for i, block in enumerate(blocks, 1):
        assert f"【块 {i}/3】（type={block['type']}）" in text
    assert "# 3 METHOD" in text          # 给的是**全文**（B 就是照着它译的）
    assert "| A | 0.85 |" in text


def test_parse_blocks_accepts_the_usual_shapes() -> None:
    assert parse_blocks('{"blocks": ["一", "二"]}') == ["一", "二"]
    assert parse_blocks('说明\n```json\n{"blocks": ["一"]}\n```\n完') == ["一"]
    assert parse_blocks('好的：{"blocks": ["一"]} 以上') == ["一"]
    assert parse_blocks('{"translation_md": "旧契约"}') == []
    assert parse_blocks("") == []


def test_parse_carry_works_on_a_block_mode_reply() -> None:
    """块模式的回复里**没有** `translation_md`：`carry_md` 必须照样取得到（否则页首会重复）。"""
    assert parse_carry('{"blocks": ["一"], "carry_md": "tlenecks …"}') == "tlenecks …"
    assert parse_carry('```json\n{"blocks": ["一"], "carry_md": "tail."}\n```') == "tail."
    assert parse_carry('{"blocks": ["一"]}') == ""
    # 普通契约仍然照旧
    assert parse_carry('{"translation_md": "一", "carry_md": "x"}') == "x"


# --------------------------------------------------------------------------
# 3. 读时配对：块模式走**序号 1:1**，质量标记 `blocks`
# --------------------------------------------------------------------------


def test_pair_rows_by_blocks_pairs_by_index() -> None:
    rows, quality = pair_rows_by_blocks(A_MD, ["中文标题", "中文段落", "中文表格"], 4)
    assert quality == "blocks"
    assert [r["original"] for r in rows] == [b["text"] for b in split_blocks(A_MD)]
    assert [r["translation"] for r in rows] == ["中文标题", "中文段落", "中文表格"]
    assert [r["id"] for r in rows] == ["p004-00", "p004-01", "p004-02"]
    # 标题的 level / number 必须跟着搬（目录靠它们，见 coords 里同一条教训）
    assert rows[0]["level"] == 1 and rows[0]["type"] == "heading"
    assert rows[1]["level"] == 0 and rows[2]["type"] == "table"


def test_pair_rows_by_blocks_refuses_mismatched_counts() -> None:
    """块数不等 = 模型自己并/拆了块 → 返回空，让调用方退回 `pair_rows`（不假装对齐）。"""
    assert pair_rows_by_blocks(A_MD, ["只有一块"], 4) == ([], "")
    assert pair_rows_by_blocks("", ["一"], 4) == ([], "")


def test_build_path_payload_keeps_blocks_and_provenance() -> None:
    blocks = ["中文标题", "中文段落", "中文表格"]
    result = PageCallResult(original_md="", translation_md="\n\n".join(blocks),
                            blocks=list(blocks), model="m")
    payload = build_path_payload(path="b", page=4, page_total=9, result=result,
                                blocks=result.blocks, a_blocks=3)
    assert payload["blocks"] == blocks
    assert payload["blocks_from_a"] is True and payload["a_blocks"] == 3
    # 旧的 B 路字段仍在（搜索 / 导出只认整页 Markdown）
    assert payload["translation_md"] == "\n\n".join(blocks)
    assert payload["carry_md"] == ""
    # 非块模式：一个字段都不多
    plain = build_path_payload(path="b", page=4, page_total=9, result=result)
    assert "blocks" not in plain and "blocks_from_a" not in plain


# --------------------------------------------------------------------------
# 4. 合并层：块模式优先于坐标 / 结构配对
# --------------------------------------------------------------------------


def test_merge_page_prefers_block_pairing() -> None:
    from backend.services.paper import reader_service

    a = {"original_md": A_MD, "page_total": 1}
    b = {"translation_md": "中文标题\n\n中文段落\n\n中文表格",
         "blocks": ["中文标题", "中文段落", "中文表格"], "blocks_from_a": True}
    merged = reader_service._merge_page("selfcheck-block-mode", 1, a, b, {}, "paths")
    assert merged is not None
    assert merged["align"] == "blocks"
    assert [r["translation"] for r in merged["rows"]] == ["中文标题", "中文段落", "中文表格"]
    assert merged["one_sided"] == 0


def test_render_block_prompt_has_both_context_slots() -> None:
    assert "{lookahead}" in PAGE_USER_TEMPLATE_B_BLOCKS
    assert "{head_note}" in PAGE_USER_TEMPLATE_B_BLOCKS


# --------------------------------------------------------------------------
# 4b. 中文侧的语义边界：**位置即锚点**（与块等长的布尔数组）
# --------------------------------------------------------------------------


def test_block_prompt_asks_for_chinese_semantic_breaks() -> None:
    """块模式下中文分块不能没有边界：B 回一个与块等长的布尔数组，位置就是锚点。"""
    for marker in ("【break_blocks：给检索分块的语义边界（与块等长）】",
                   "**与块数等长的布尔数组**", "**第 1 块**", "宁少不多"):
        assert marker in BLOCK_TRANSLATE_SYSTEM, marker
    assert PAGE_SCHEMA_B_BLOCKS["properties"]["break_blocks"]["items"]["type"] == "boolean"
    assert "break_blocks" in PAGE_USER_TEMPLATE_B_BLOCKS


def test_parse_break_flags_is_lenient() -> None:
    assert parse_break_flags('{"blocks": ["a", "b"], "break_blocks": [false, true]}') == [False, True]
    assert parse_break_flags('{"break_blocks": ["true", 1, "否"]}') == [True, True, False]
    assert parse_break_flags('```json\n{"break_blocks": [true]}\n```') == [True]
    assert parse_break_flags('{"blocks": ["a"]}') == []
    assert parse_break_flags("") == []


def test_pair_rows_by_blocks_marks_chinese_breaks_by_position() -> None:
    rows, quality = pair_rows_by_blocks(A_MD, ["一", "二", "三"], 7,
                                       break_flags=[True, True, False])
    assert quality == "blocks"
    assert "block_break" not in rows[0], "第 1 块永远不标（页首没有上一个语义单元）"
    assert rows[1].get("block_break") is True
    assert "block_break" not in rows[2]


def test_merge_page_turns_block_flags_into_translation_side_breaks() -> None:
    from backend.services.paper import reader_service

    a = {"original_md": A_MD, "page_total": 1}
    b = {"translation_md": "一\n\n二\n\n三", "blocks": ["一", "二", "三"],
         "blocks_from_a": True, "break_blocks": [True, True, False]}
    merged = reader_service._merge_page("selfcheck-block-breaks", 1, a, b, {}, "paths")
    rows = merged["rows"]
    assert rows[1].get("semantic_break") is True
    assert rows[1]["semantic_break_side"] == "translation"
    assert "block_break" not in rows[1], "内部键不许漏到接口上"
    assert merged["break_stats"]["by_side"]["translation"] >= 1


def test_read_model_rejoins_the_sentence_split_by_the_page_break() -> None:
    """页尾那句被上一页收完之后：本页第 1 块整块为空 → 把它的**原文**并回上一页末行、去掉这半行。"""
    from backend.services.paper import reader_service

    p9 = {"page": 9, "rows": [{"id": "p009-00", "index": 0, "original": "applied to the",
                               "translation": "……施加于"}], "one_sided": 0}
    p10 = {"page": 10, "rows": [
        {"id": "p010-00", "index": 0, "original": "input embedding vectors during training.",
         "translation": ""},
        {"id": "p010-01", "index": 1, "original": "Next.", "translation": "下一句"}],
        "one_sided": 1}
    reader_service._rejoin_carried_rows(
        [p9, p10], {9: {"carry_md": "input embedding vectors during training."}})
    assert p9["rows"][-1]["original"] == "applied to the input embedding vectors during training."
    assert [r["id"] for r in p10["rows"]] == ["p010-00"]
    assert p10["rows"][0]["original"] == "Next." and p10["one_sided"] == 0


def test_read_model_keeps_rows_when_there_is_no_carry() -> None:
    """没有 carry 申报（上一页页尾本来就收句了）→ 一行都不许动。"""
    from backend.services.paper import reader_service

    p1 = {"page": 1, "rows": [{"id": "p001-00", "index": 0, "original": "A", "translation": "甲"}],
          "one_sided": 0}
    p2 = {"page": 2, "rows": [{"id": "p002-00", "index": 0, "original": "B", "translation": ""}],
          "one_sided": 1}
    reader_service._rejoin_carried_rows([p1, p2], {1: {}})
    assert [r["id"] for r in p2["rows"]] == ["p002-00"] and p2["one_sided"] == 1


# --------------------------------------------------------------------------
# 5. 等 A 路（B 的输入来自 A：**A 必须领先**）
# --------------------------------------------------------------------------


class _FakeStore:
    """只实现 `reload()` / `page()` 的最小替身：模拟"对路刚写到盘上"。"""

    def __init__(self, pages: list[dict | None]) -> None:
        self._pages = pages
        self._i = 0
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1

    def page(self, page: int) -> dict | None:
        item = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return item


def test_await_a_page_returns_when_the_page_is_there() -> None:
    from backend.services.translation import translation_service as ts

    store = _FakeStore([{"original_md": "# 3 METHOD"}])
    got = ts.await_a_page(store, "selfcheck", 3, timeout=0.1, required=True)
    assert got == {"original_md": "# 3 METHOD"}


def test_await_a_page_picks_up_a_page_written_after_the_first_look(monkeypatch) -> None:
    """第一眼不在、reload 之后出现 → 必须能拿到（这就是"A 路刚落盘"的那一瞬）。"""
    from backend.services.translation import translation_service as ts

    monkeypatch.setattr(ts, "_active_path_task", lambda paper_id, path: {"id": "fake-a"})
    store = _FakeStore([None, {"original_md": "body"}])
    got = ts.await_a_page(store, "selfcheck", 3, timeout=5, required=True)
    assert got == {"original_md": "body"} and store.reloads >= 2


def test_await_a_page_gives_up_when_path_a_is_not_running(monkeypatch) -> None:
    """A 路没在跑（没排上 / 已结束）→ 立刻放弃：`required` 抛，`optional` 返回 None。"""
    from backend.services.translation import translation_service as ts

    monkeypatch.setattr(ts, "_active_path_task", lambda paper_id, path: None)
    store = _FakeStore([None])
    assert ts.await_a_page(store, "selfcheck", 4, timeout=30, required=False) is None
    with pytest.raises(ValidationError):
        ts.await_a_page(store, "selfcheck", 4, timeout=30, required=True)
