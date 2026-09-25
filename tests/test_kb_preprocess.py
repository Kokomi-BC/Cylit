"""知识库索引层预处理（**全部离线**，纯函数）。

规则只服务知识库：阅读器视图不受影响。这里守住的四条边界：
1. 公式（`$…$` / `$$…$$`）一字不改；
2. 引用编号保留 —— `[13](url)` 只去掉 URL；
3. 摘要/正文段落有保护条款（没有 `## Abstract` 标题的论文也不能被误杀）；
4. 表格数字行不再被当成 heading。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge.preprocess import (attached_caption_ids,
                                                   first_body_index,
                                                   heading_level, is_author_block,
                                                   is_front_matter,
                                                   is_table_number_heading,
                                                   split_long_text,
                                                   strip_presentation)
from backend.services.knowledge import preprocess

AUTHORS = "Amir Zandieh, Majid Daliri, Majid Hadian, Vahab Mirrokni"


# --------------------------------------------------------------------------
# 1. Markdown 表现格式
# --------------------------------------------------------------------------


def test_strip_bold_link_image_and_heading_marker() -> None:
    src = ("## 3.2 Attention\n"
           "**bold** and ![fig](../figures/a.png) and [the paper](https://x.y/z) "
           "plus `__under__`.")
    out = strip_presentation(src)
    assert "##" not in out and "**" not in out and "__" not in out
    assert "![" not in out and "](http" not in out
    assert "3.2 Attention" in out
    assert "bold" in out and "the paper" in out


def test_citation_link_keeps_number_only() -> None:
    """用户口径：`[13](…)` 这种 citation link 保留 `[13]`，只去掉 URL。"""
    out = strip_presentation("Recurrent networks [13](https://arxiv.org/abs/1) and [7, 8](http://x) work.")
    assert "[13]" in out and "[7, 8]" in out
    assert "http" not in out and "arxiv" not in out


def test_plain_citation_number_untouched() -> None:
    assert "[13]" in strip_presentation("as shown in [13] and [14, 15].")


# --------------------------------------------------------------------------
# 2. 公式禁区
# --------------------------------------------------------------------------


@pytest.mark.parametrize("math", [
    "$Q : \\mathbb{R}^d \\to \\{0, 1\\}^B$",
    "$$\\begin{aligned} D(x,\\hat{x}) &= a \\\\ &= b \\end{aligned}$$",
    "$$* †$$",
    "\\langle y, x\\rangle",
])
def test_math_never_touched(math: str) -> None:
    out = strip_presentation(f"定义如下 **重点** {math} 其中 x 是向量。")
    assert math in out
    assert "**" not in out


def test_inline_math_with_underscore_survives() -> None:
    out = strip_presentation("矩阵 $W_1$ 与 $\\frac{a}{b}$ 都在。")
    assert "$W_1$" in out and "\\frac{a}{b}" in out


# --------------------------------------------------------------------------
# 3. 前沿元数据
# --------------------------------------------------------------------------


def test_author_and_affiliation_blocks_are_front_matter() -> None:
    assert is_front_matter("Amir Zandieh Majid Daliri Majid Hadian",
                           page_no=1, before_body=True, authors=AUTHORS)
    assert is_front_matter("Google Research New York University Google DeepMind",
                           page_no=1, before_body=True, authors=AUTHORS)
    assert is_front_matter("Vahab Mirrokni Google Research",
                           page_no=1, before_body=True, authors=AUTHORS)


@pytest.mark.parametrize("text", [
    "zandieh@google.com daliri.majid@nyu.edu",
    "## arXiv:1706.03762v5 [cs.CL] 6 Dec 2017",
    "https://www.elastic.co/enterprise-search/",
    "Equal contribution. Listing order is random. Jakob proposed replacing RNNs.",
    "Work performed while at Google Brain.",
    "31st Conference on Neural Information Processing Systems (NIPS 2017), Long Beach, CA, USA.",
])
def test_strong_front_matter_patterns(text: str) -> None:
    assert is_front_matter(text, page_no=1, before_body=False, authors=AUTHORS)


def test_abstract_paragraph_is_protected() -> None:
    """attention 的摘要段落**没有** `## Abstract` 标题，就落在正文起点之前 —— 不能丢。"""
    abstract = ("The dominant sequence transduction models are based on complex recurrent or "
                "convolutional neural networks that include an encoder and a decoder. The best "
                "performing models also connect the encoder and decoder through an attention "
                "mechanism. We propose a new simple network architecture, the Transformer.")
    assert not is_front_matter(abstract, page_no=1, before_body=True, authors=AUTHORS)


def test_body_paragraph_on_page_one_is_kept() -> None:
    assert not is_front_matter("We propose TURBOQUANT, a two-stage approach.",
                               page_no=1, before_body=False, authors=AUTHORS)


def test_front_matter_only_applies_to_first_two_pages() -> None:
    assert not is_front_matter("zandieh@google.com", page_no=5, before_body=False,
                               authors=AUTHORS)


def test_author_block_needs_two_names() -> None:
    assert not is_author_block("Vaswani et al. (2017) proposed the Transformer.",
                               "Ashish Vaswani, Noam Shazeer")


def test_first_body_index_prefers_numbered_section_or_abstract() -> None:
    rows = [{"id": "b1", "type": "heading", "text": "## Attention Is All You Need"},
            {"id": "b2", "type": "para", "text": "Ashish Vaswani Noam Shazeer"},
            {"id": "b3", "type": "para", "text": "The dominant sequence…"},
            {"id": "b4", "type": "heading", "text": "## 1Introduction"},
            {"id": "b5", "type": "para", "text": "Recurrent networks…"}]
    assert first_body_index(rows) == 3
    assert first_body_index([{"id": "b1", "type": "heading", "text": "## ABSTRACT"}]) == 0


# --------------------------------------------------------------------------
# 4. 表格数字行 / 图注
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "1645.2945.16", "68.3859.5446.2850.06", "4128128 5.0025.5", "24.639.922.3 · 101.4 · 10",
])
def test_table_number_rows_are_not_headings(text: str) -> None:
    assert is_table_number_heading(text)


@pytest.mark.parametrize("text", [
    "## 3.2 Attention", "### B.2 QJL：1 比特内积量化", "## GNMT + RL [38] 结果",
])
def test_real_headings_are_not_table_rows(text: str) -> None:
    assert not is_table_number_heading(text)


def test_attached_caption_requires_caption_type() -> None:
    """attention 的 Table 2 把 caption_block_id 指到一段**正文**上 —— 不能因此丢掉正文。"""
    rows = [{"id": "c1", "type": "caption", "text": "**Figure 1: overview**"},
            {"id": "p1", "type": "para", "text": "Table 2 summarizes our results…"}]
    assert attached_caption_ids(rows, {"c1", "p1"}) == {"c1"}


def test_heading_level() -> None:
    assert heading_level("## ABSTRACT") == 2
    assert heading_level("### C.1TURBOQUANT") == 3
    assert heading_level("ABSTRACT") == 0


# --------------------------------------------------------------------------
# 5. 超长块的句子边界切分
# --------------------------------------------------------------------------


def test_split_long_text_respects_hard_cap_and_keeps_text() -> None:
    text = "。".join(f"这是第{i}个句子用来把段落撑长" for i in range(200))
    pieces = split_long_text(text, 200)
    assert len(pieces) > 1
    assert all(len(p) <= 200 for p in pieces), "正文片段不得超过硬上限"
    assert "".join(pieces) == text, "切分不得丢字（切点都在句末）"


def test_split_long_text_keeps_math_whole() -> None:
    formula = "$$" + "x" * 300 + "$$"
    text = "说明" * 40 + formula + "后续" * 40
    pieces = split_long_text(text, 200)
    assert any(formula in p for p in pieces), "公式不能被切开"
    assert all(p.count("$$") % 2 == 0 for p in pieces), "$$ 不能单侧出现"


def test_split_short_text_is_noop() -> None:
    assert split_long_text("短短一句话。", 200) == ["短短一句话。"]
    assert split_long_text("", 200) == []


def test_module_exposes_only_pure_helpers() -> None:
    """这一层是纯函数层：不该出现任何 DB / 文件访问的导入。"""
    assert not hasattr(preprocess, "session_scope")
