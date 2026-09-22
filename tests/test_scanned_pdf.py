"""扫描件（影印 PDF）兼容测试：没有文本层时正文由视觉模型读出。

覆盖三层：
1. 页 Markdown → 块的切分规则（纯函数，离线）；
2. 扫描页判定（真的用 pymupdf 造一个"只剩图像"的 PDF）；
3. 整条流水线：扫描 PDF + 注入的读页函数 → 块 / 页面信息 / 跳过公式插图检测。
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from backend.parsing.ocr.vision_reader import (
    blocks_from_page_markdown, looks_scanned, page_text_chars,
    split_page_markdown, title_from_markdown,
)
from backend.parsing.pipeline import PaperBuilder

FAKE_PAGE_MD = """# Scanned Paper on Quantization

We study the distortion of quantized vectors. Let $x \\in \\mathbb{R}^d$.

## 2 Method

The MSE distortion is defined as

$$ D_{mse} := \\mathbb{E}\\|x - Q(x)\\|_2^2. $$

**Figure 1**: MSE distortion versus bit-width.
"""


# --------------------------------------------------------------------------
# 1. 页 Markdown → 块
# --------------------------------------------------------------------------


def test_split_page_markdown_classifies_and_keeps_order():
    items = split_page_markdown(FAKE_PAGE_MD)
    kinds = [k for k, _ in items]
    assert kinds == ["heading", "para", "heading", "para", "formula",
                     "caption"]


def test_split_page_markdown_handles_multiline_and_unclosed_math():
    md = "# T\n\n$$\n a + b\n = c\n$$\n\n尾段。"
    items = split_page_markdown(md)
    assert [k for k, _ in items] == ["heading", "formula", "para"]
    assert "a + b" in items[1][1] and "= c" in items[1][1]


def test_split_page_markdown_detects_table_and_refs():
    md = ("| a | b |\n| - | - |\n| 1 | 2 |\n\n"
          "- [1] Vaswani et al. Attention is all you need.\n"
          "- [2] Dao et al. FlashAttention.")
    kinds = [k for k, _ in split_page_markdown(md)]
    assert kinds == ["table", "ref"]


def test_split_page_markdown_normalizes_double_heading_markers():
    """实测视觉模型会写 ``## ## 0.2 PRIOR WORK``，必须规整成一层。"""
    items = split_page_markdown("## ## 0.2 PRIOR WORK\n\n正文。")
    assert items[0] == ("heading", "## 0.2 PRIOR WORK")
    assert split_page_markdown("# # # Title")[0] == ("heading", "# Title")


def test_blocks_from_page_markdown_makes_distinct_reading_bands():
    blocks = blocks_from_page_markdown(FAKE_PAGE_MD, page_no=2, paper_id="scan-demo",
                                       page_width=600, page_height=800)
    assert [b.type for b in blocks] == ["heading", "para", "heading", "para",
                                        "formula", "caption"]
    assert all(b.page_no == 2 for b in blocks)
    # 每块一条递增的虚拟横带：排序（阅读顺序）才稳定
    ys = [b.rect.y0 for b in blocks]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)
    assert blocks[0].meta["heading_level"] == 1
    assert blocks[2].meta["heading_level"] == 2
    assert blocks[0].content_hash and blocks[0].md_anchor.startswith("blk-")


def test_block_text_is_bare_markdown_body():
    """Block 存**裸文本**，Markdown 标记由生成器统一加（否则事实源会叠两层）。"""
    from backend.parsing.md.generator import block_body_text

    blocks = blocks_from_page_markdown(FAKE_PAGE_MD, page_no=1, paper_id="p")
    heading = next(b for b in blocks if b.type == "heading")
    caption = next(b for b in blocks if b.type == "caption")
    assert not heading.text.startswith("#")
    assert "**" not in caption.text
    # 生成器加一次标记：既是可读 Markdown，又不重复
    md = block_body_text(heading, md=True)
    assert md.endswith(heading.text) and md.count("# ") == 1 and "## #" not in md
    assert block_body_text(caption, md=True).count("**") == 2


def test_double_heading_markers_in_source_page_stay_single():
    """视觉模型写出 ``## ## 0.2 PRIOR WORK`` 时，最终 Markdown 只能有一层标记。"""
    from backend.parsing.md.generator import block_body_text

    blocks = blocks_from_page_markdown("## ## 0.2 PRIOR WORK\n\n正文。",
                                       page_no=1, paper_id="p")
    md = block_body_text(blocks[0], md=True)
    assert md == "### 0.2 PRIOR WORK", md


def test_formula_blocks_are_wrapped_as_display_math():
    """公式块必须写成 ``$$ … $$``：页级翻译的原文转录与译文靠块序号对齐。"""
    blocks = blocks_from_page_markdown(FAKE_PAGE_MD, page_no=1, paper_id="p")
    formula = next(b for b in blocks if b.type == "formula")
    assert formula.text.startswith("$$") and formula.text.rstrip().endswith("$$")


def test_title_from_markdown_uses_first_heading():
    assert title_from_markdown(FAKE_PAGE_MD) == "Scanned Paper on Quantization"
    # 优先最浅层级：论文标题（#）优先于章节标题（##）
    assert title_from_markdown("## 2 Method\n\n# Real Title\n\nBody.") == "Real Title"
    # 从论文中间某页开始扫描时，首页第一段往往是上一页的续句：
    # **不能**拿它当标题（会把一整段正文塞进 paper.title）
    mid_page = ("quantizer's output. Thus, we aim to design quantizers that for "
                "any desired bit-width $b$ minimize the following measures:\n\n"
                "## 2 Method\n\nBody.")
    assert title_from_markdown(mid_page) == "2 Method"
    assert title_from_markdown("只有一段正文，没有任何标题。") == ""


# --------------------------------------------------------------------------
# 2. 扫描页判定（真造一个无文本层的 PDF）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scanned_pdf(tmp_path_factory, sample_pdf) -> Path | None:
    """把示例 PDF 的前 2 页渲染成位图、再拼成"纯图像 PDF"。"""
    if sample_pdf is None:
        return None
    out = tmp_path_factory.mktemp("scanned") / "scanned.pdf"
    src = fitz.open(str(sample_pdf))
    dst = fitz.open()
    try:
        for pno in (3, 4):
            page = src[pno - 1]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False,
                                  colorspace=fitz.csGRAY)
            new_page = dst.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=pix.tobytes("png"))
        dst.save(str(out), garbage=4, deflate=True)
    finally:
        src.close()
        dst.close()
    return out


def test_scanned_pdf_has_no_text_layer(scanned_pdf):
    if scanned_pdf is None:
        pytest.skip("未找到示例 PDF")
    doc = fitz.open(str(scanned_pdf))
    try:
        assert doc.page_count == 2
        assert all(page_text_chars(doc[i]) == 0 for i in range(2))
        assert all(looks_scanned(doc[i]) for i in range(2))
    finally:
        doc.close()


def test_plain_pdf_is_not_flagged_as_scanned(sample_pdf):
    if sample_pdf is None:
        pytest.skip("未找到示例 PDF")
    doc = fitz.open(str(sample_pdf))
    try:
        assert not looks_scanned(doc[2])          # 第 3 页是正常文本页
        assert page_text_chars(doc[2]) > 200
    finally:
        doc.close()


# --------------------------------------------------------------------------
# 3. 整条流水线
# --------------------------------------------------------------------------


def test_scanned_pdf_builds_blocks_from_vision(tmp_path, scanned_pdf):
    if scanned_pdf is None:
        pytest.skip("未找到示例 PDF")
    calls: list[int] = []

    def fake_read(page_no: int, image: bytes) -> str:
        assert image[:4] == b"\x89PNG"           # 传进来的必须是 PNG 字节
        calls.append(page_no)
        return FAKE_PAGE_MD

    out = tmp_path / "scan-demo"
    builder = PaperBuilder(scanned_pdf, "scan-demo", out, max_pages=2,
                           vision_fn=fake_read)
    result = builder.build(store_ir=False, write_md=False)

    assert calls == [1, 2], "每一页扫描页都要读一次，且只读一次"
    assert result.stats["scanned_pages"] == [1, 2]
    assert all(p.has_text_layer is False for p in result.pages)
    assert result.blocks, "扫描页必须产出块"
    kinds = {b.type for b in result.blocks}
    assert {"heading", "para", "formula", "caption"} <= kinds
    # 扫描页没有矢量层：公式/插图检测必须跳过，否则整页图会被当成一张插图
    assert result.formulas == []
    assert result.figures == []
    # 标题也来自模型（文本层没有标题可取）
    assert result.title == "Scanned Paper on Quantization"
    # 块 id 仍按统一规则分配，翻译链路无需特殊处理
    assert all(b.id.startswith("scandemo-p") for b in result.blocks)


def test_scanned_pdf_without_vision_fn_warns(tmp_path, scanned_pdf):
    """没有视觉模型时：不报错、给出告警、其余流程照常。"""
    if scanned_pdf is None:
        pytest.skip("未找到示例 PDF")
    builder = PaperBuilder(scanned_pdf, "scan-noop", tmp_path / "scan-noop",
                           max_pages=2)
    result = builder.build(store_ir=False, write_md=False)
    assert any("未配置视觉模型" in w for w in result.warnings)
    assert result.blocks == []
