"""匹配不到正文行的块 → 显式下发"到页/到公式"的落点（纯函数，不依赖数据库）。

背景（真 bug）：阅读器 payload 的 block_rows 只包含**能匹配到行**的块，于是
"查不到"同时意味着"这页没有它的行"和"这张表没算它"，前端只能猜 —— 线上那篇 509 个
块里 119 个匹配不到行（66 个是归一化后 <8 字符的公式块，53 个内容对不上），拿到这些
块 id 时无处可跳，只能整页闪一次。这里把 row_less_blocks 的规则钉住。
"""

from __future__ import annotations

from backend.services.paper.anchor_map import (
    MAX_ROW_LESS_BLOCKS, build_block_row_map, row_less_blocks,
)


def _blk(bid, page, btype="text", text="", formula=None) -> dict:
    block = {"id": bid, "page": page, "type": btype, "text": text}
    if formula is not None:
        block["formula"] = formula
    return block


def test_matched_blocks_are_not_listed() -> None:
    """能匹配到行的块不进列表（它走 block_rows 那条路）。"""
    blocks = [_blk("b01", 1, text="Attention Is All You Need"),
              _blk("b02", 1, text="Ashish Vaswani Noam Shazeer")]
    rows = {1: [{"id": "p001-01", "original": "Attention Is All You Need"},
                {"id": "p001-02", "original": "Ashish Vaswani Noam Shazeer"}]}
    block_rows = build_block_row_map(blocks, rows)
    assert set(block_rows) == {"b01", "b02"}
    assert row_less_blocks(blocks, block_rows) == []


def test_missing_or_zero_page_is_dropped() -> None:
    """没有合法页号的块不能进：没有页可跳，给出去只会让前端跳错一页。"""
    blocks = [_blk("no-page", None), _blk("zero", 0), _blk("blank", ""),
              _blk("junk", "p3"), _blk("ok", 3)]
    assert row_less_blocks(blocks, {}) == [
        {"id": "ok", "page": 3, "type": "text"}]


def test_formula_block_carries_latex() -> None:
    """公式块带 latex：前端靠它定位到页内那个 KaTeX 节点，而不是整页闪。

    键名取自 reader_service._formula_card：那张卡片里**没有** latex 这个键，最终值在
    latex_final（退化 latex_repaired → latex_candidate[0]），与解析流水线口径一致。
    """
    blocks = [
        _blk("f-final", 4, "formula", "$$ * † $$",
             {"latex_final": "\\alpha + \\beta", "latex_repaired": "old",
              "latex_candidate": ["oldest"]}),
        _blk("f-repaired", 4, "formula", "$$ * † $$",
             {"latex_final": None, "latex_repaired": "\\gamma",
              "latex_candidate": ["older"]}),
        _blk("f-candidate", 4, "formula", "$$ * † $$",
             {"latex_final": "", "latex_repaired": None,
              "latex_candidate": ["", "\\delta"]}),
    ]
    assert row_less_blocks(blocks, {}) == [
        {"id": "f-final", "page": 4, "type": "formula",
         "latex": "\\alpha + \\beta"},
        {"id": "f-repaired", "page": 4, "type": "formula", "latex": "\\gamma"},
        {"id": "f-candidate", "page": 4, "type": "formula", "latex": "\\delta"},
    ]


def test_block_without_latex_has_no_latex_key() -> None:
    """取不到 LaTeX 时**不写**那个键（空串会被前端当成"有公式可定位"）。"""
    blocks = [_blk("img", 2, "figure"),
              _blk("blank-formula", 2, "formula", "$$ $$",
                   {"latex_final": "  ", "latex_repaired": None,
                    "latex_candidate": []}),
              _blk("no-formula-key", 2, "formula"),
              _blk("no-formula-at-all", 2, "text")]
    items = row_less_blocks(blocks, {})
    assert [it["id"] for it in items] == ["img", "blank-formula",
                                          "no-formula-key", "no-formula-at-all"]
    for it in items:
        assert "latex" not in it


def test_duplicate_id_keeps_first() -> None:
    """重复 id 只留第一条（order_global 撞车时会出现，前端按 id 建索引）。"""
    blocks = [_blk("b1", 5), _blk("b1", 9), _blk("b2", 6), _blk("b2", 7)]
    assert row_less_blocks(blocks, {}) == [
        {"id": "b1", "page": 5, "type": "text"},
        {"id": "b2", "page": 6, "type": "text"}]


def test_empty_input() -> None:
    assert row_less_blocks([], {}) == []
    assert row_less_blocks([], {"x": "p001-01"}) == []
    assert row_less_blocks(None, None) == []


def test_order_follows_input() -> None:
    """顺序与 blocks 一致（前端按这个顺序补占位，顺序变了定位会跳）。"""
    blocks = [_blk("a", 1), _blk("b", 2), _blk("c", 1), _blk("d", 3)]
    assert [it["id"] for it in row_less_blocks(blocks, {})] == ["a", "b", "c", "d"]


def test_truncated_at_max_blocks() -> None:
    """超上限截断并告警（正常论文远到不了；只防坏数据把 payload 撑爆）。"""
    blocks = [_blk("b%d" % i, 1) for i in range(MAX_ROW_LESS_BLOCKS + 3)]
    items = row_less_blocks(blocks, {})
    assert len(items) == MAX_ROW_LESS_BLOCKS
    assert items[0]["id"] == "b0"
    assert items[-1]["id"] == "b%d" % (MAX_ROW_LESS_BLOCKS - 1)
