"""两路提示词（A = 英文转录 / B = 中文翻译）：契约分开，但**分段规则必须一致**。

## 为什么要专门测"两份 prompt 的一致性"

融合模式里"两侧块结构对得上"是同一次推理的副产物；拆成两路之后这个保证没有了。
替代方案是把**同一套分段规则**写进两份 prompt —— 于是"两份 prompt 里的分段规则还一样吗"
就从"代码风格问题"变成了**正确性问题**：它一漂，`pair_rows` 的 `structure` 比例就会升，
阅读器开始出现单侧行。

同理，A 路不许产出译文、B 路不许产出原文：任何一边"顺手也做另一半"，都会让两路各自
产出一份内容不同的原文，索引层就会出现两组互相冲突的 `original`。

另外两条硬约束也在这里钉住：
- A 路**不给【下一页开头】**（转录只写本页看到的内容，拿到下一页反而容易把半句补完）；
- B 路必须保留 carry_md / 跨页衔接的三条规则（那是修过"同一句译两遍""半句占位"的地方）。
"""
from __future__ import annotations

import pytest

from backend.services.translation.page_translate import (
    BREAK_TYPES, PAGE_SCHEMA_A, PAGE_SCHEMA_B, PAGE_USER_TEMPLATE,
    PAGE_USER_TEMPLATE_A, PAGE_USER_TEMPLATE_B, SYSTEM_PROMPT, TRANSLATE_SYSTEM,
    TRANSCRIBE_SYSTEM, path_system_prompt, render_page_prompt,
)

BOTH = [TRANSCRIBE_SYSTEM, TRANSLATE_SYSTEM]


# --------------------------------------------------------------------------
# 1. 每路只对自己那一侧负责
# --------------------------------------------------------------------------


def test_transcribe_prompt_is_english_only() -> None:
    assert "【original_md：原文转录】" in TRANSCRIBE_SYSTEM
    assert "translation_md" not in TRANSCRIBE_SYSTEM, "A 路不该提到译文产物"
    assert "carry_md" not in TRANSCRIBE_SYSTEM
    assert "中文译文由另一条独立流水线负责" in TRANSCRIBE_SYSTEM


def test_translate_prompt_is_chinese_only() -> None:
    assert "【translation_md：中文译文】" in TRANSLATE_SYSTEM
    assert "【original_md：原文转录】" not in TRANSLATE_SYSTEM, "B 路不该要求转录原文"
    assert "英文原文由另一条独立流水线负责" in TRANSLATE_SYSTEM
    # carry_md 仍在契约里，但**已停用**（2026-12-27：改成与英文逐块对齐，不再跨页收句）
    assert "【carry_md：" in TRANSLATE_SYSTEM
    assert "已停用" in TRANSLATE_SYSTEM or "停用" in TRANSLATE_SYSTEM


def test_translate_prompt_keeps_cross_page_rules() -> None:
    """跨页口径（2026-12-27 改版）：**页首照译、页尾停在断处**，两侧逐块对齐。"""
    for marker in ("照本页原文译出来", "停在断处", "排版单位", "逐块对齐"):
        assert marker in TRANSLATE_SYSTEM, marker
    assert "跳过这半句" not in TRANSLATE_SYSTEM, "carry 停用后不该再让模型跳过页首半句"


def test_transcribe_prompt_stops_at_page_break() -> None:
    """A 路**不许**用下一页把半句补完 —— 补句是译文那一侧的事。"""
    assert "照原样停在断处" in TRANSCRIBE_SYSTEM
    assert "不要用下一页的内容补完" in TRANSCRIBE_SYSTEM


# --------------------------------------------------------------------------
# 2. 两份 prompt 的公共部分必须真的相同（结构性一致靠它）
# --------------------------------------------------------------------------


def test_both_paths_share_the_same_segmentation_rules() -> None:
    """分段规则是"两侧块结构一致"的唯一保证 —— 两路必须逐字一致。"""
    from backend.services.translation.page_translate import (
        _SHARED_SEGMENTATION_RULES,
    )

    assert len(_SHARED_SEGMENTATION_RULES) > 200, "分段规则不该是空壳"
    for prompt in BOTH:
        assert _SHARED_SEGMENTATION_RULES in prompt
    # 关键字必须都在（防止有人把整段换成一句"请合理分段"）
    for marker in ("一个自然段 = 一块", "标题**单独成块", "不要按字数机械切分"):
        assert marker in _SHARED_SEGMENTATION_RULES


def test_segmentation_says_the_page_image_is_the_only_evidence() -> None:
    """两路拆开后最容易踩的坑：解析器文本里的空行被当成分段依据 → 两侧各拆各的。

    实测 turbo p1：A 把摘要里相邻两句并成一块、B 按解析器空行拆成两块，于是从第 4 行起
    英中整体错位一行。规则里必须明确"依据只有页图 + 段首缩进"，并写明代价。
    """
    for prompt in BOTH:
        assert "解析器文本里的空行**不算分段证据**" in prompt
        assert "段首缩进" in prompt
        assert "整页错位一行" in prompt


def test_both_paths_share_page_image_and_heading_rules() -> None:
    for prompt in BOTH:
        assert "页图是唯一事实来源" in prompt
        assert "禁止猜测、禁止用常识补全" in prompt
        assert "【全文标题骨架】" in prompt
        assert "同一层级在全文必须一致" in prompt


def test_both_paths_carry_the_banned_anchor_list() -> None:
    """校验层会拒掉这些位置，所以两路都要把"不要标在哪里"写清楚。"""
    for prompt in BOTH:
        for banned in ("一句话中间", "公式与紧随其后的解释之间", "表格标题与表体之间",
                       "标题行的**下一行**", "本页**第一块**"):
            assert banned in prompt, banned


def test_all_break_types_are_offered_to_the_model() -> None:
    for prompt in BOTH:
        for btype in BREAK_TYPES:
            assert f"`{btype}`" in prompt, f"{btype} 没写进 prompt"
    # 两路都强调"类型只是标签、不参与切分"
    for prompt in BOTH:
        assert "不参与切分判定" in prompt


def test_breaks_contract_has_start_but_no_end_field() -> None:
    """只有 `start`：单元结束位置由**代码**算（下一个边界的前一行），比模型自报稳。"""
    for prompt in BOTH:
        assert '"start"' in prompt
    for schema in (PAGE_SCHEMA_A, PAGE_SCHEMA_B):
        assert set(schema["properties"]["breaks"]["items"]["required"]) == {"start"}

    # 融合模式的 prompt 也升到 v2（同一个解析器）
    assert '"start"' in SYSTEM_PROMPT


# --------------------------------------------------------------------------
# 3. 按路取 prompt / 模板
# --------------------------------------------------------------------------


def test_path_system_prompt_mapping() -> None:
    assert path_system_prompt("a") is TRANSCRIBE_SYSTEM
    assert path_system_prompt("b") is TRANSLATE_SYSTEM
    with pytest.raises(ValueError):
        path_system_prompt("c")


def test_schemas_require_only_their_own_side() -> None:
    assert PAGE_SCHEMA_A["required"] == ["original_md"]
    assert PAGE_SCHEMA_B["required"] == ["translation_md"]
    assert "translation_md" not in PAGE_SCHEMA_A["properties"]
    assert "original_md" not in PAGE_SCHEMA_B["properties"]
    assert "carry_md" in PAGE_SCHEMA_B["properties"]
    assert "breaks" not in PAGE_SCHEMA_A["required"]      # 锚点缺失只退化成机械切分
    assert "breaks" not in PAGE_SCHEMA_B["required"]


def test_templates_are_path_specific() -> None:
    assert "{lookahead}" not in PAGE_USER_TEMPLATE_A, "A 路模板不该有下一页槽位"
    assert "{lookahead}" in PAGE_USER_TEMPLATE_B
    assert "{lookahead}" in PAGE_USER_TEMPLATE          # 融合模式保持原样


def test_render_page_prompt_drops_lookahead_for_path_a() -> None:
    common = dict(page=3, page_total=22, body="Body.", lookahead="【下一页开头】的内容")
    a = render_page_prompt(**common, path="a")
    b = render_page_prompt(**common, path="b")
    fused = render_page_prompt(**common)
    # 2026-12-27 起 carry 停用：A、B 两路都不再喂【下一页开头】（融合模式是存量臂，保留原样）
    assert "【下一页开头】的内容" not in a
    assert "【下一页开头】的内容" not in b
    assert "【下一页开头】的内容" in fused
    for text in (a, b, fused):
        assert "第 3 页 / 共 22 页" in text


def test_render_page_prompt_rejects_unknown_path() -> None:
    with pytest.raises(ValueError):
        render_page_prompt(page=1, page_total=1, body="x", path="c")


def test_path_templates_ask_for_the_right_output_fields() -> None:
    assert "original_md" in PAGE_USER_TEMPLATE_A
    assert "translation_md" not in PAGE_USER_TEMPLATE_A
    assert "translation_md" in PAGE_USER_TEMPLATE_B
    assert "carry_md" in PAGE_USER_TEMPLATE_B
