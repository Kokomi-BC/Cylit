"""块 id → 正文行 id 的锚点映射（回归测试，纯函数、不依赖数据库）。

背景（真 bug）：知识库/相似检索结果里点「跳回原文」**什么都不发生**。原因是两套命名空间：

- chunk 的 ``block_id`` 是**解析块 id**（``attentionisa-p01-b06``）；
- 已翻译页的正文行 id 是**页级产物的位置编号**（``p001-06``），跟块毫无对应关系
  （页未翻译时才退回块 id）。

前端的深链定位只认行 id，于是 `?row=attentionisa-p01-b06` 永远查不到 → 静默返回、空跳。
这里把桥钉住：合成数据覆盖 ① 一行对应一块 / ② 多块并成一行 / ③ 块文本被页级改写（走窗口回退）
/ ④ 太短的块（公式符号）不给映射（前端退化为按页跳，不乱跳一行）/ ⑤ 保序（同一句话在前面出现过）。
"""

from __future__ import annotations

from backend.services.paper.anchor_map import (
    build_block_row_map, normalize, rows_by_page,
)


def _blocks(*items: tuple[str, int, str]) -> list[dict]:
    return [{"id": bid, "page": page, "text": text} for bid, page, text in items]


def _rows(page: int, *items: tuple[str, str]) -> dict:
    return {page: [{"id": rid, "original": text} for rid, text in items]}


#: 真实的第 1 页（Attention Is All You Need）：15 个解析块，页级产物只切出 10 行 ——
#: 作者块、单位块被并进同一行，公式块（`$$ * † $$`）在行里只是一个 `†`。
_ATTENTION_BLOCKS = _blocks(
    ("attentionisa-p01-b01", 1, "Attention Is All You Need"),
    ("attentionisa-p01-b02", 1, "Ashish Vaswani Noam Shazeer Niki ParmarJakob Uszkoreit"),
    ("attentionisa-p01-b03", 1, "Google BrainGoogle ResearchGoogle Research"),
    ("attentionisa-p01-b04", 1, "avaswani@google.comnoam@google.comnikip@google.comusz@google"),
    ("attentionisa-p01-b05", 1, "arXiv:1706.03762v5 [cs.CL] 6 Dec 2017"),
    ("attentionisa-p01-b06", 1, "$$ * † $$"),
    ("attentionisa-p01-b08", 1, "Google Research University of Toronto"),
    ("attentionisa-p01-b09", 1, "The dominant sequence transduction models are based on "
                                 "complex recurrent or convolutional neural networks"),
    ("attentionisa-p01-b10", 1, "1 Introduction"),
)
_ATTENTION_ROWS = _rows(
    1,
    ("p001-00", "# Attention Is All You Need"),
    ("p001-01", "Ashish Vaswani\\*  Noam Shazeer\\*  Niki Parmar\\*  Jakob Uszkoreit\\*"),
    ("p001-02", "Llion Jones\\*  Aidan N. Gomez\\* †  Łukasz Kaiser\\*\nGoogle Research"),
    ("p001-03", "Illia Polosukhin\\* ‡\nillia.polosukhin@gmail.com"),
    ("p001-04", "# arXiv:1706.03762v5 [cs.CL] 6 Dec 2017"),
    ("p001-05", "# Abstract"),
    ("p001-06", "The dominant sequence transduction models are based on complex recurrent "
                "or convolutional neural networks that include an encoder and a decoder."),
    ("p001-07", "# 1 Introduction"),
    ("p001-08", "Recurrent neural networks, long short-term memory [13] and gated recurrent"),
    ("p001-09", "\\*Equal contribution. Listing order is random."),
)


def test_normalize_strips_markdown_and_latex_noise() -> None:
    assert normalize("# 1 Introduction") == "1introduction"
    assert normalize("Llion Jones\\*  Aidan N. Gomez\\* †") == "llionjonesaidanngomez"
    assert normalize("Attention Is All You Need") == normalize("attention   is ALL you need")
    assert normalize(None) == ""


def test_heading_and_paragraph_map_to_their_row() -> None:
    mapping = build_block_row_map(_ATTENTION_BLOCKS, _ATTENTION_ROWS)
    assert mapping["attentionisa-p01-b01"] == "p001-00"       # 标题（原文带 `#`）
    assert mapping["attentionisa-p01-b09"] == "p001-06"       # 正文段落（行更长）
    assert mapping["attentionisa-p01-b10"] == "p001-07"       # `1 Introduction` → `# 1 Introduction`


def test_merged_blocks_all_point_at_the_same_row() -> None:
    """作者块 / 单位块在页级产物里被并进同一行 —— 它们都该跳到那一行。"""
    mapping = build_block_row_map(_ATTENTION_BLOCKS, _ATTENTION_ROWS)
    assert mapping["attentionisa-p01-b02"] == "p001-01"
    assert mapping["attentionisa-p01-b08"] == "p001-02"


def test_short_block_is_not_mapped() -> None:
    """`$$ * † $$` 归一化后只剩一个 `†`：短于阈值 → 不给映射（宁可退化为按页跳）。"""
    mapping = build_block_row_map(_ATTENTION_BLOCKS, _ATTENTION_ROWS)
    assert "attentionisa-p01-b06" not in mapping


def test_window_fallback_when_row_rewrites_the_head() -> None:
    """块文本的**头部**在页级产物里被改写（模型加的引导语）→ 用中段窗口仍能配上。"""
    blocks = _blocks(("p1-b01", 1, "!!! Rewritten head. "
                                    "quantization error is bounded by the distortion rate"))
    rows = _rows(1, ("p001-00", "quantization error is bounded by the distortion rate"))
    assert build_block_row_map(blocks, rows) == {"p1-b01": "p001-00"}


def test_falls_back_to_earlier_rows_when_cursor_finds_nothing() -> None:
    """游标只前进，但走投无路时**回退到全页再找一次**。

    页眉/脚注这类内容在读序上可能排在正文之后（`b08` 在 `b07` 之前），块顺序与行顺序
    并不严格一致；宁可配到靠前那一行，也不要因为"游标已经过去了"而不给映射
    （不给映射 = 前端退化为按页跳，用户要多找一眼）。
    """
    blocks = _blocks(
        ("p1-b01", 1, "deep dive into the distortion rate"),
        ("p1-b02", 1, "the proposed codebook rotation is applied"),
    )
    rows = _rows(
        1,
        ("p001-00", "the proposed codebook rotation is applied here"),
        ("p001-01", "filler filler filler"),
        ("p001-02", "deep dive into the distortion rate"),
    )
    mapping = build_block_row_map(blocks, rows)
    assert mapping["p1-b01"] == "p001-02"
    assert mapping["p1-b02"] == "p001-00"


def test_page_without_rows_is_skipped() -> None:
    """未翻译的页没有行 → 不进映射（那些页的正文行 id 就是块 id，前端直命即可）。"""
    blocks = _blocks(("p1-b01", 1, "Some paragraph text here"), ("p2-b01", 2, "Another paragraph"))
    assert build_block_row_map(blocks, _rows(1, ("p001-00", "Some paragraph text here"))) \
        == {"p1-b01": "p001-00"}


def test_rows_by_page_shape() -> None:
    payloads = [
        {"page": 1, "rows": [{"id": "p001-00", "original": "a"}]},
        {"page": 2, "rows": []},
        {"page": 0, "rows": [{"id": "x", "original": "y"}]},
    ]
    assert rows_by_page(payloads) == {1: [{"id": "p001-00", "original": "a"}], 2: []}


def test_real_page_mapping_is_total_for_prose() -> None:
    """真实第 1 页：正文/标题块都必须配得上行。

    允许缺席的只有两种：公式符号块（`$$ * † $$` → 只剩 `†`）与邮箱块
    （页级产物把邮箱并进了作者行的另一种切法，归一化后没有公共长串）——
    它们都会退化为"跳到第 1 页"，而不是乱跳一行。
    """
    mapping = build_block_row_map(_ATTENTION_BLOCKS, _ATTENTION_ROWS)
    missing = {b["id"] for b in _ATTENTION_BLOCKS if b["id"] not in mapping}
    assert missing == {"attentionisa-p01-b04", "attentionisa-p01-b06"}

def test_bad_row_id_only_skips_that_row() -> None:
    """一页里有一行缺 id（坏产物）→ **只跳过那一行**，其余块照常映射。

    以前是 ``if any(not rid for rid in row_ids): continue``：那一页的**所有**块都失去锚点
    （一个坏行毒死整页）。题头/脚注这类行的 id 缺失本来就不该影响正文块。
    """
    blocks = _blocks(("p1-b01", 1, "the distortion rate of the quantizer is bounded"),
                     ("p1-b02", 1, "codebook rotation is applied on the unit sphere"))
    rows = {1: [
        {"id": "", "original": "filler filler filler filler"},
        {"id": "p001-01", "original": "the distortion rate of the quantizer is bounded"},
        {"id": "p001-02", "original": "codebook rotation is applied on the unit sphere"},
    ]}
    mapping = build_block_row_map(blocks, rows)
    assert mapping == {"p1-b01": "p001-01", "p1-b02": "p001-02"}


def test_block_without_page_is_not_mapped() -> None:
    """没有页码的块**不映射**（以前缺省成第 1 页，拿去和第 1 页的行硬配 → 跳错页）。"""
    blocks = [{"id": "p1-b01", "page": None, "text": "the distortion rate is bounded"},
              {"id": "p1-b02", "page": "", "text": "the distortion rate is bounded"},
              {"id": "p1-b03", "page": 1, "text": "the distortion rate is bounded"}]
    rows = _rows(1, ("p001-00", "the distortion rate is bounded"))
    assert build_block_row_map(blocks, rows) == {"p1-b03": "p001-00"}
