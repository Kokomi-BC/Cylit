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

from backend.services.translation.page_translate import (
    BLOCK_TRANSLATE_SYSTEM, PAGE_SCHEMA_B_BLOCKS, PAGE_USER_TEMPLATE_B_BLOCKS,
    PageCallResult, TRANSLATE_SYSTEM, _SHARED_SEGMENTATION_RULES,
    build_path_payload, format_block_list, parse_blocks, pair_rows_by_blocks,
    render_block_prompt, split_blocks,
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
    for gone in ("【breaks：", "src", "carry_md"):
        assert gone not in BLOCK_TRANSLATE_SYSTEM, f"{gone} 不该再出现在 B 的契约里"
    # 与普通 B 路 prompt 的差别是**结构性的**，不是措辞
    assert _SHARED_SEGMENTATION_RULES in TRANSLATE_SYSTEM


def test_block_prompt_keeps_what_b_still_owes() -> None:
    """块模式减的是"分块"，不是"翻译质量"：公式 / 表格 / 参考文献双语规则都留着。"""
    for marker in ("【块清单：输出必须与它一一对应】", "长度正好 N 的字符串数组",
                   "不许合并、不许拆分", "只把论文标题译成中文、用《》括起来",
                   "所有数学都必须写进公式定界符"):
        assert marker in BLOCK_TRANSLATE_SYSTEM, marker
    # 页边界补句**明确交出去**（不许自己猜下一页）
    assert "不要**去猜下一页" in BLOCK_TRANSLATE_SYSTEM


def test_block_schema_is_a_string_array() -> None:
    assert PAGE_SCHEMA_B_BLOCKS["required"] == ["blocks"]
    assert PAGE_SCHEMA_B_BLOCKS["properties"]["blocks"]["items"]["type"] == "string"
    assert "translation_md" not in PAGE_SCHEMA_B_BLOCKS["properties"]


def test_render_block_prompt_carries_the_contract() -> None:
    text = render_block_prompt(page=3, page_total=22, blocks="【块 1/2】（type=para）\nHi.",
                               block_count=2, prefix="PREFIX\n", section="3 METHOD",
                               previous="前文结尾")
    assert "PREFIX" in text and "前文结尾" in text
    assert "第 3 页 / 共 22 页；章节：3 METHOD" in text
    assert "共 2 块" in text and "长度正好 2" in text
    assert "【块 1/2】（type=para）" in text
    # 补句与坐标的输入**不再出现**（这是块模式的"减"）
    for gone in ("【下一页开头】", "【页边界参考", "{src_tail}"):
        assert gone not in text, gone


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
