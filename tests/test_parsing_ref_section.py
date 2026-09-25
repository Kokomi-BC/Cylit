"""参考文献区 / 附录 的类型标注（**全部离线**，不解析 PDF）。

用户报的真问题：`_annotate_sections` 里 `in_refs` 一旦置位**永不复位** ——
turboquant 的 `## REFERENCES` 在 p10，于是 p16–p22 的附录 A–E（Lemma、微分熵定义、QJL 证明、
实现细节，约 1.57 万字符）全被改写成 `ref`，被知识库整段跳过。

这里守住的口径（用户确认）：
- 只有**明确 Appendix / Supplementary**，或参考文献区之后的 `A` / `A.1` 单字母大节，才进入 appendix；
- `Lemma` / `Proof` / `Theorem` / `Implementation` 单独出现**不触发**附录；
- 参考文献区之后的常规编号大节、或不像文献条目的标题（`## Attention Visualizations`）→ 回到 body；
- 即使在参考文献区里，也只有"像文献条目"的段落才改写成 `ref`；
- `section_title`（以及由它派生的 `block.section_id`）语义**不变**：仍只写在 heading 块上。
"""

from __future__ import annotations

from backend.parsing.models import Block
from backend.parsing.pipeline import _annotate_sections
from backend.parsing.structure.paragraph import (is_appendix_heading,
                                                 is_number_only_heading,
                                                 is_prose_like,
                                                 looks_like_reference_entry)

TURBOQUANT_BIB = ("Josh Achiam, Steven Adler, Sandhini Agarwal, et al. GPT-4 technical report. "
                  "arXiv preprint arXiv:2303.08774, 2023.")
TURBOQUANT_PROSE = ("For a random variable x we denote its differential entropy as h(x). For "
                    "random variables x and y, the mutual information is symmetric.")


def _block(bid: str, page: int, order: int, type_: str, text: str,
           font_size: float = 10.0) -> Block:
    return Block(id=bid, paper_id="paper", page_no=page, order_in_page=order,
                 order_global=order, type=type_, text=text, text_clean=text,
                 font_size=font_size).finalize()


# --------------------------------------------------------------------------
# 谓词
# --------------------------------------------------------------------------


def test_appendix_heading_requires_explicit_or_letter_section() -> None:
    assert is_appendix_heading("Appendix A: Proofs", after_refs=False)
    assert is_appendix_heading("Supplementary Material", after_refs=False)
    # 参考文献区之后的单字母大节
    assert is_appendix_heading("AEXTENDED PRIOR WORK", after_refs=True)
    assert is_appendix_heading("C.1TURBOQUANT", after_refs=True)
    # 不在参考文献区之后：`ABSTRACT` 不能被当成 "A BSTRACT"
    assert not is_appendix_heading("ABSTRACT", after_refs=False)
    assert not is_appendix_heading("AEXTENDED PRIOR WORK", after_refs=False)


def test_lemma_proof_implementation_do_not_trigger_appendix() -> None:
    for text in ("Lemma 1 (coordinate distribution).", "Proof.", "Theorem 2.", "Implementation"):
        assert not is_appendix_heading(text, after_refs=True), text


def test_reference_entry_predicate() -> None:
    assert looks_like_reference_entry(TURBOQUANT_BIB)
    assert looks_like_reference_entry("[13] Vaswani et al. Attention is all you need. NIPS, 2017.")
    assert looks_like_reference_entry(
        "Elasticsearch. vector-search. Qdrant vector search. https://qdrant.tech/.")
    assert not looks_like_reference_entry(TURBOQUANT_PROSE)


def test_prose_like_predicate() -> None:
    assert is_prose_like(TURBOQUANT_PROSE)
    assert is_prose_like("Given that TURBOQUANT employs random rotation to mitigate worst-case "
                         "input scenarios, understanding the distribution matters here.")
    assert not is_prose_like(TURBOQUANT_BIB)
    assert not is_prose_like("short")


def test_number_only_heading() -> None:
    assert is_number_only_heading("1645.2945.16")
    assert is_number_only_heading("4128128 5.0025.5")
    assert not is_number_only_heading("## GNMT + RL [38] 结果")
    assert not is_number_only_heading("3.1 Encoder and Decoder Stacks")


# --------------------------------------------------------------------------
# 状态机：正文 → 参考文献 → 附录 → …
# --------------------------------------------------------------------------


def _turboquant_like() -> list[Block]:
    return [
        _block("b01", 1, 1, "heading", "## TURBOQUANT"),
        _block("b02", 1, 2, "para", "Vector quantization is a classic problem."),
        _block("b03", 1, 3, "heading", "## 1Introduction"),
        _block("b04", 1, 4, "para", "Recurrent models factor computation along positions."),
        _block("b05", 10, 5, "heading", "## REFERENCES"),
        _block("b06", 10, 6, "ref", TURBOQUANT_BIB),
        _block("b07", 10, 7, "para", "Thomas D Ahle, Michael Kapralov, Jakob BT Knudsen. "
                                     "Sampling and estimation. In Proceedings of the 50th "
                                     "Annual ACM Symposium, 2018."),
        _block("b08", 15, 8, "heading", "## AEXTENDED PRIOR WORK"),
        _block("b09", 16, 9, "para", TURBOQUANT_PROSE),
        _block("b10", 16, 10, "heading", "### B.1SHANNON LOWER BOUND ON DISTORTION"),
        _block("b11", 16, 11, "para", "Lemma 1 (coordinate distribution of random point on "
                                      "hypersphere). For any positive integer d, if x is uniform "
                                      "on the sphere then its coordinates are independent."),
        _block("b12", 17, 12, "heading", "### C.1TURBOQUANT"),
        _block("b13", 17, 13, "para", "Proof. The third line follows by Lemma 4 and the "
                                      "definition of the residual vector."),
    ]


def test_appendix_prose_is_not_marked_ref() -> None:
    blocks = _turboquant_like()
    _annotate_sections(blocks)
    kinds = {b.id: (b.type, b.meta["section_kind"]) for b in blocks}
    assert kinds["b06"][0] == "ref" and kinds["b06"][1] == "refs"
    assert kinds["b07"][0] == "ref", "author-year 无编号的文献条目也要认出来"
    assert kinds["b09"] == ("para", "appendix"), "附录正文不能被吞掉"
    assert kinds["b11"] == ("para", "appendix"), "Lemma 段落不能被吞掉"
    assert kinds["b13"] == ("para", "appendix"), "Proof 段落不能被吞掉"


def test_in_ref_section_can_be_turned_off() -> None:
    blocks = _turboquant_like()
    _annotate_sections(blocks)
    after = [b for b in blocks if b.meta["section_kind"] != "refs"]
    assert any(b.meta["in_ref_section"] is False for b in after), "退出参考文献区后标志要回落"
    assert all(b.meta["in_ref_section"] for b in blocks if b.id in ("b06", "b07"))


def test_numbered_section_after_refs_returns_to_body() -> None:
    blocks = [_block("b01", 10, 1, "heading", "## REFERENCES"),
              _block("b02", 10, 2, "ref", TURBOQUANT_BIB),
              _block("b03", 11, 3, "heading", "## 3 Model Architecture"),
              _block("b04", 11, 4, "para", "The encoder is composed of a stack of layers.")]
    _annotate_sections(blocks)
    assert blocks[-1].meta["section_kind"] == "body"
    assert blocks[-1].type == "para"


def test_non_reference_heading_after_refs_returns_to_body() -> None:
    """attention 的 `## Attention Visualizations` 不是文献条目 → 参考文献区结束。"""
    blocks = [_block("b01", 12, 1, "heading", "## References"),
              _block("b02", 12, 2, "ref", "[38] Vaswani et al. Attention is all you need."),
              _block("b03", 13, 3, "heading", "## Attention Visualizations"),
              _block("b04", 13, 4, "para", "The Law will never be perfect but its application "
                                           "should be just.")]
    _annotate_sections(blocks)
    assert blocks[-1].meta["section_kind"] == "body"


def test_section_title_semantics_unchanged() -> None:
    """`section_title` 仍只写在 heading 块上（`block.section_id` 由它派生，阅读器依赖）。"""
    blocks = _turboquant_like()
    _annotate_sections(blocks)
    for b in blocks:
        if b.type == "heading":
            assert b.meta.get("section_title"), b.id
        else:
            assert "section_title" not in b.meta, b.id
    assert blocks[4].meta["section_title"] == "## REFERENCES"


def test_attention_visualizations_prose_kept_even_inside_refs() -> None:
    """万一附录标题没认出来：refs 里的正文段落也不会被改写（兜底判据）。"""
    blocks = [_block("b01", 10, 1, "heading", "## REFERENCES"),
              _block("b02", 10, 2, "ref", TURBOQUANT_BIB),
              _block("b03", 11, 3, "para", "Given that the model employs random rotation to "
                                           "mitigate worst-case inputs, we now analyse the "
                                           "distribution of the coordinates.")]
    _annotate_sections(blocks)
    assert blocks[-1].type == "para", "以 Given 开头的正文段落必须留下"
