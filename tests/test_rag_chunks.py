"""双语 chunk 语料测试（RAG 检索前的一步：同一 chunk 出 zh/en 两行）。

**全部离线**：只测纯函数（签名 / 页内对齐 / 变体展开），不连 MySQL、不调模型。
真实论文上的覆盖率实测见 docs/dev-log.md §17.34。
"""

from __future__ import annotations

from backend.services.knowledge.knowledge_service import (
    MIN_ZH_CHARS, _block_sig, _chunk_variants, _lookup_zh, _make_chunk, _slice_zh,
    _snap_sentence, _zh_content, _zh_index,
)

PAPER = "demo-paper"


def _page(page: int, rows: list[tuple[str, str]]) -> dict:
    return {"page": page,
            "rows": [{"id": f"p{page:03d}-{i:02d}", "original": a, "translation": b}
                     for i, (a, b) in enumerate(rows)]}


def _block(bid: str, page: int, order: int, text: str) -> dict:
    return {"id": bid, "page": page, "order": order, "text": text, "type": "para"}


# --------------------------------------------------------------------------
# 签名与页内对齐
# --------------------------------------------------------------------------


def test_block_sig_drops_layout_noise_only() -> None:
    # 解析产物与页级产物是同一页的两次转写：标题层级符、空格、标点都会飘
    assert _block_sig("## TURBOQUANT: ONLINE VECTOR QUANTIZATION") == \
        _block_sig("# TurboQuant：Online Vector  quantization")
    assert _block_sig("$$ D_{\\text{mse}} $$") == _block_sig("$$D_{\\text{mse}}$$")
    # 但不同的内容必须仍然区分得开
    assert _block_sig("inner product error") != _block_sig("mean squared error")


def test_zh_index_keeps_page_order_and_skips_half_translated_rows() -> None:
    idx = _zh_index([_page(1, [("english one", "中文一"), ("english two", ""),
                               ("", "只有译文")])])
    assert idx == {1: [(_block_sig("english one"), "中文一")]}


def test_lookup_zh_exact_then_page_local() -> None:
    idx = _zh_index([
        _page(1, [("mean squared error of the quantizer", "量化器的均方误差")]),
        _page(2, [("mean squared error of the quantizer", "另一页的同名句子")]),
    ])
    assert _lookup_zh(idx, 1, "mean squared error of the quantizer")[0] == "量化器的均方误差"
    assert _lookup_zh(idx, 2, "mean squared error of the quantizer")[0] == "另一页的同名句子"
    assert _lookup_zh(idx, 3, "mean squared error of the quantizer") is None


def test_lookup_zh_matches_across_different_segmentation() -> None:
    """解析块是行的一截（两侧切分不同）—— 必须仍能配到**那一行**。"""
    row = ("quantizers output thus we aim to design quantizers that for any desired "
           "bit-width b minimize the following expected distortion measure")
    idx = _zh_index([_page(3, [("something else entirely about gpu hardware", "无关的一段"),
                               (row, "因此，我们的目标是设计这样的量化器。")])])

    part = "quantizer’s output. Thus, we aim to design quantizers that for any desired bit-width b"
    hit = _lookup_zh(idx, 3, part)[0]
    assert hit and "因此" in hit                      # 配到的是这一行，不是"无关的一段"
    assert "无关" not in hit


def test_lookup_zh_refuses_unrelated_text() -> None:
    idx = _zh_index([_page(4, [("a completely unrelated sentence about gpu hardware",
                                "完全无关的一句")])])
    assert _lookup_zh(idx, 4, "vector quantization reduces distortion") is None
    assert _lookup_zh(idx, 4, "abc") is None          # 太短，不给兜底机会


def test_lookup_zh_slices_when_row_is_much_coarser() -> None:
    """行比块大得多（页级产物把好几块并成一行）→ 按比例切片，而不是整行端走。"""
    row = "第一句讲背景信息。" * 8 + "第二句才是这个块的内容。"
    idx = _zh_index([_page(5, [(row.strip(), "甲" * 60 + "。乙" * 60 + "。丙" * 60 + "。")])])

    row_dst, idx_no, lo, hi = _lookup_zh(idx, 5, row.strip()[:40])   # 块只覆盖行的开头
    assert idx_no == 0 and hi - lo < 180              # 区间明显短于整行译文（180+ 字）
    assert len(row_dst) > 180                          # 返回的是整行全文 + 区间（供区间消费）


def test_slice_zh_snaps_to_sentence_boundary() -> None:
    text = "第一句话。第二句话在这里。第三句话。"
    piece = _slice_zh(text, 0.2, 0.6)
    assert piece in text and piece.endswith("。") and 0 < len(piece) < len(text)


def test_slice_zh_keeps_edges_whole() -> None:
    text = "向量量化这一植根于信源编码的问题。"
    assert _slice_zh(text, 0.0, 1.0) == text
    assert _slice_zh(text, 0.01, 0.99) == text        # 贴边不切，避免从词中间切进去


def test_snap_sentence_reaches_sentence_end_at_text_end() -> None:
    """句末标点**正好落在整行末尾**时必须能吸附过去。

    旧实现的循环上界是 `i < n`，这种情形会一路扫到结尾后**退回原始比例位置** ——
    切点就停在词中间（真实事故：`…对其随机化。我` ⏎⏎ `们可以通过…`）。
    """
    text = "前一句。我们可以生成矩阵。"
    assert _snap_sentence(text, 5, forward=True) == len(text)
    assert _snap_sentence(text, 3, forward=True) == 4          # 正常情形不受影响


def test_slice_zh_snaps_to_row_end_when_only_sentence_end_is_last_char() -> None:
    """比例切点落在词中间、而唯一的句末标点在整行末尾 → 应该**吸附到行尾**。

    真实事故：hi 落在「我们|可以通过」之间，`_snap_sentence` 因为上界写成 `i < n`
    扫不到行尾那个 `。`，于是切出 `…对其随机化。我`，下一块再切出 `们可以通过…`。
    """
    text = "我们首先将该向量与随机矩阵相乘来对其随机化。我们可以通过对正态矩阵施加分解生成矩阵。"
    piece = _slice_zh(text, 0.0, 0.57)
    assert piece == text.strip()
    assert piece.endswith("。")


def test_zh_content_keeps_formula_that_row_already_has_once() -> None:
    """译文行里已经有这条公式 → 解析侧那条公式块不再插一遍（同一条公式连着出现两次）。"""
    en = "for random variables with the distribution f(x) ="
    latex = r"f_X(x) = \frac{\Gamma(d/2)}{\sqrt{\pi}\,\Gamma((d-1)/2)} \left(1 - x^2\right)^{(d-3)/2}"
    zh_row = f"为分布为 ${latex}$ 的随机变量设计一个标量量化器。"
    zh = _zh_index([_page(5, [(en, zh_row)])])

    with_formula = [(5, en, "text"), (5, f"${latex}$", "formula")]
    assert _zh_content(with_formula, zh).count(latex) == 1

    # 反例：译文里没有这条公式（还没翻译进来）→ 公式块照样要插进去，不能被误挡
    plain = [(5, en, "text"), (5, f"${latex}$", "formula")]
    zh_plain = _zh_index([_page(5, [(en, "只译了半句：")])])
    assert latex in _zh_content(plain, zh_plain)


def test_zh_content_joins_consecutive_pieces_of_same_row() -> None:
    """同一页级行的连续片段合成一段，且**行内原有的换行照原样保留**（算法框那种一行多步）。"""
    row_en = "Step 1: do a thing  Step 2: do another thing"
    row_zh = "1: 第一件事  \n2: 第二件事"
    zh = _zh_index([_page(5, [(row_en, row_zh)])])

    content = _zh_content([(5, "Step 1: do a thing", "text"),
                           (5, "Step 2: do another thing", "text")], zh)
    assert content == row_zh
    assert "\n\n" not in content                      # 没有被拆成两段


# --------------------------------------------------------------------------
# 变体展开
# --------------------------------------------------------------------------


def _text_chunk(blocks: list[dict]) -> dict:
    text = "\n\n".join(b["text"] for b in blocks)
    return _make_chunk(PAPER, "text", text, blocks[0], blocks[-1],
                       section="1 INTRO",
                       parts=[(b["page"], b["text"]) for b in blocks],
                       metadata={"block_ids": [b["id"] for b in blocks]})


def test_variants_produce_zh_and_en_rows_with_suffix_ids() -> None:
    a = _block("p01-b01", 1, 1, "Vector quantization compresses vectors.")
    b = _block("p01-b02", 1, 2, "Our method needs no calibration data at all.")
    zh = _zh_index([_page(1, [(a["text"], "向量量化压缩高维向量，代价是量化误差。"),
                              (b["text"], "我们的方法完全不需要校准数据。")])])

    rows = _chunk_variants([_text_chunk([a, b])], zh, ("zh", "en"))

    assert [r["lang"] for r in rows] == ["zh", "en"]          # zh 在前
    assert rows[0]["id"].endswith(":zh") and rows[1]["id"].endswith(":en")
    assert rows[0]["id"].rsplit(":", 1)[0] == rows[1]["id"].rsplit(":", 1)[0]
    assert rows[0]["content"] == "向量量化压缩高维向量，代价是量化误差。\n\n我们的方法完全不需要校准数据。"
    assert "Vector quantization" in rows[1]["content"]
    assert rows[0]["metadata"]["lang"] == "zh"
    assert rows[0]["metadata"]["pair_id"] == rows[1]["metadata"]["pair_id"]
    assert rows[0]["content_hash"] != rows[1]["content_hash"]
    assert "_parts" not in rows[0]                            # 内部字段不外泄


def test_variants_dedupe_adjacent_pieces_from_same_row() -> None:
    """相邻两块共用一个译文行（切分不同）→ 同一段中文只留一次。"""
    shared = "Two blocks of this paragraph came from one translated row in the page product."
    a, b = _block("p01-b01", 1, 1, shared[:40]), _block("p01-b02", 1, 2, shared[40:])
    zh = _zh_index([_page(1, [(shared, "这一段在页级产物里只有一个译文行，不该出现两次。")])])

    rows = _chunk_variants([_text_chunk([a, b])], zh, ("zh",))
    assert rows and rows[0]["content"].count("不该出现两次") == 1


def test_variants_skip_zh_when_untranslated() -> None:
    a = _block("p01-b01", 1, 1, "Only the English side exists in this paper corpus.")
    rows = _chunk_variants([_text_chunk([a])], {}, ("zh", "en"))
    assert [r["lang"] for r in rows] == ["en"]                # 不拿原文冒充译文


def test_variants_emit_single_any_row_for_formula() -> None:
    """公式原子行**只出 1 行**（`lang=any`）：LaTeX 无语言之分，zh/en 各存一份是纯冗余。

    语言过滤那边（`retrieval._filter_rows` / LanceDB prefilter）把 `any` 当作
    "两种语言都通过"，所以中文、英文检索都召得回 —— 见 `test_lang_filter_keeps_any`。
    """
    a = _block("p02-b07", 2, 7, "$$ D_{mse} = E[||x - Q^{-1}(Q(x))||^2] $$")
    chunk = _make_chunk(PAPER, "formula", "公式 p02-b07：D_{mse}", a, a,
                        metadata={"latex": "D_{mse}"})
    rows = _chunk_variants([chunk], {}, ("zh", "en"))
    assert [r["lang"] for r in rows] == ["any"]
    assert "pair_id" not in rows[0]["metadata"]        # 没有"对侧语种"可配
    assert rows[0]["id"].endswith(":any")


def test_variants_use_translated_caption_for_figure() -> None:
    cap = "Figure 2: Quantization error versus bit-width."
    b = _block("p07-b11", 7, 40, "![fig](x.png)")
    chunk = _make_chunk(PAPER, "figure", f"插图 p07-b11：{cap}", b, b,
                        parts=[(7, cap)], metadata={"caption": cap})
    zh = _zh_index([_page(7, [(cap, "图 2：量化误差与位宽的关系。")])])

    rows = _chunk_variants([chunk], zh, ("zh", "en"))
    assert len(rows) == 2
    assert "图 2：量化误差与位宽的关系。" in rows[0]["content"]
    assert "Figure 2" in rows[1]["content"]


def test_variants_keep_short_zh_heading() -> None:
    """中文门槛按**字**算：小节标题（"## B.2 QJL：1 比特内积量化"）不能被丢掉。"""
    a = _block("p17-b01", 17, 1, "### B.2QJL: 1-BIT INNER PRODUCT QUANTIZATION")
    zh = _zh_index([_page(17, [("## B.2 QJL: 1-BIT INNER PRODUCT QUANTIZATION",
                                "## B.2 QJL：1 比特内积量化")])])
    rows = _chunk_variants([_text_chunk([a])], zh, ("zh", "en"))
    assert [r["lang"] for r in rows] == ["zh", "en"]
    assert len(rows[0]["content"]) >= MIN_ZH_CHARS


