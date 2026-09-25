"""扫描件插图裁剪：视觉模型给粗框 → 像素墨迹定边界。

背景（实测）：影印页上**没有任何可用的版面坐标** —— 块的 rect 是等分整页的虚拟横带、
矢量层为空、页图是一整张位图，所以几何法（``figure_cut``）在扫描页必然 0 张图，
而且带 OCR 文本层的影印页还会把印章/切条裁成"插图"。

这里的口径：
- 模型只负责"图号 → 大致在哪"（``<!-- layout ... -->`` 版面块），**绝不直接出图**；
- 真实边界由页图墨迹决定（``ink_bbox`` + 覆盖率校验）；
- 三道校验任一不过 → **不出图**（宁可少一张，也不往 Markdown 里插裁错的图）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz
import pytest

from backend.parsing.ocr.vision_reader import PageRead, parse_layout_block
from backend.parsing.pipeline import (
    SCAN_MAX_AREA_RATIO, PaperBuilder, _box_iou, _point_box, _scan_crop_dpi,
)
from backend.parsing.models import Rect
from backend.parsing.structure.figure_cut import caption_number

PAGE_W, PAGE_H = 612.0, 792.0
# 页面上真正的"图"：一块深色矩形 + 两条内部线（模拟一张图）
FIG_RECT = fitz.Rect(120.0, 200.0, 500.0, 420.0)


# --------------------------------------------------------------------------
# 1. 版面块（纯函数）
# --------------------------------------------------------------------------


def test_parse_layout_block_extracts_and_strips():
    md = ("# T\n\n正文段落。\n\n**Figure 3**: 说明。\n\n"
          '<!-- layout {"figures":[{"number":"3","bbox":[0.18,0.35,0.95,0.68],'
          '"caption_bbox":[0.17,0.685,0.82,0.70],"caption":"**Figure 3**: a  b\\nc"}]} -->')
    clean, figs = parse_layout_block(md)
    assert "layout" not in clean and "<!--" not in clean
    assert clean.rstrip().endswith("**Figure 3**: 说明。")
    assert figs == [{"number": "3", "bbox": (0.18, 0.35, 0.95, 0.68),
                     "caption_bbox": (0.17, 0.685, 0.82, 0.70),
                     "caption": "Figure 3: a b c"}]      # 空白压平、去掉加粗标记


def test_parse_layout_block_is_lenient():
    # 百分比坐标、上下写反、缺 caption_bbox、坏 JSON、空列表：都不该抛异常
    _c, figs = parse_layout_block('x\n<!-- layout {"figures":[{"number":1,"bbox":[10,20,90,60]}]} -->')
    assert figs[0]["bbox"] == (0.1, 0.2, 0.9, 0.6) and figs[0]["caption_bbox"] is None
    _c, figs = parse_layout_block('x\n<!-- layout {"figures":[{"number":"2","bbox":[0.9,0.8,0.1,0.2]}]} -->')
    assert figs[0]["bbox"] == (0.1, 0.2, 0.9, 0.8)
    assert parse_layout_block("x\n<!-- layout {oops} -->")[1] == []
    assert parse_layout_block("x\n<!-- layout {\"figures\":[]} -->")[1] == []
    assert parse_layout_block("没有任何版面块") == ("没有任何版面块", [])


def test_page_read_records_layout_mark():
    assert PageRead.parse("正文").layout_mark is False
    assert PageRead.parse('正文\n<!-- layout {"figures":[]} -->').layout_mark is True


def test_caption_number_accepts_markdown_and_cjk():
    assert caption_number("Figure 3: Evaluation") == "3"
    assert caption_number("**Table 12**: results") == "12"
    assert caption_number("图 5：方差") == "5"
    assert caption_number("no number") == ""


def test_point_box_and_dpi_helpers():
    page = Rect(0, 0, PAGE_W, PAGE_H)
    box = _point_box([0.25, 0.5, 0.5, 0.75], page)
    assert (box.x0, box.y0, box.x1, box.y1) == (153.0, 396.0, 306.0, 594.0)
    assert _point_box(None, page) is None and _point_box([1, 2, 3], page) is None
    assert _box_iou(Rect(0, 0, 10, 10), Rect(0, 0, 10, 10)) == pytest.approx(1.0)
    assert _box_iou(Rect(0, 0, 10, 10), Rect(20, 20, 30, 30)) == 0.0
    # 扫描源 200dpi → 不按 600dpi 上采样；源未知 → 用调用方的值
    assert _scan_crop_dpi(600, 200) == 240
    assert _scan_crop_dpi(600, 0) == 600
    assert _scan_crop_dpi(600, 3038) == 600


# --------------------------------------------------------------------------
# 2. 整条流水线：扫描件 + 视觉模型（含版面块）
# --------------------------------------------------------------------------


def _make_scan_pdf(path: Path) -> Path:
    """造一页"扫描件"：页面上有一张深色图 + 上下各几行正文条，**没有文本层**。"""
    src = fitz.open()
    page = src.new_page(width=PAGE_W, height=PAGE_H)
    page.draw_rect(FIG_RECT, color=(0, 0, 0), fill=(0.2, 0.2, 0.2), width=1)
    page.draw_line(fitz.Point(FIG_RECT.x0, 300), fitz.Point(FIG_RECT.x1, 300),
                   color=(0, 0, 0), width=2)
    page.draw_line(fitz.Point(FIG_RECT.x0, 360), fitz.Point(FIG_RECT.x1, 360),
                   color=(0, 0, 0), width=2)
    for i in range(3):                                # 图**上方**的正文行（段落）
        y = 60 + i * 15
        page.draw_rect(fitz.Rect(90, y, 520, y + 7), color=(0, 0, 0), fill=(0, 0, 0))
    for i in range(6):                                # 图**下方**的正文行（段落）
        y = 560 + i * 15
        page.draw_rect(fitz.Rect(90, y, 520, y + 7), color=(0, 0, 0), fill=(0, 0, 0))

    pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), alpha=False,
                          colorspace=fitz.csGRAY)
    out = fitz.open()
    new = out.new_page(width=PAGE_W, height=PAGE_H)
    new.insert_image(new.rect, stream=pix.tobytes("png"))
    out.save(str(path), garbage=4, deflate=True)
    out.close()
    src.close()
    return path


def _layout_md(bbox, caption_bbox=(0.10, 0.72, 0.55, 0.745), number="1") -> str:
    payload = {"figures": [{"number": number, "bbox": list(bbox),
                            "caption_bbox": list(caption_bbox)}]}
    return ("# [MOCK] Scanned With Figure\n\n"
            "We study quantization error bounds for high-dimensional vectors.\n\n"
            "**Figure 1**: Synthetic figure for the scan pipeline.\n\n"
            f"<!-- layout {json.dumps(payload)} -->")


@pytest.fixture()
def scan_pdf(tmp_path) -> Path:
    return _make_scan_pdf(tmp_path / "scan.pdf")


def _build(scan_pdf: Path, tmp_path: Path, md: str, name: str = "scan-demo"):
    def fake_read(page_no: int, image: bytes) -> str:
        assert image[:4] == b"\x89PNG"          # 传进来的必须是 PNG 字节
        return md

    d = tmp_path / name
    return PaperBuilder(scan_pdf, name, d, max_pages=1,
                        vision_fn=fake_read).build(), d


def test_scan_figure_cropped_from_model_proposal(scan_pdf, tmp_path):
    """模型给一个**偏大偏歪**的框 → 边界被墨迹收紧到真实的图体上。"""
    # 提案比真实图体大一圈（左侧多 60pt、上边多 80pt）
    md = _layout_md((0.10, 0.15, 0.95, 0.62))
    result, d = _build(scan_pdf, tmp_path, md)

    assert len(result.figures) == 1, result.warnings
    fb = result.figures[0]
    assert fb.page_number == 1
    assert fb.audit.get("source") == "scan"
    assert fb.effective_dpi == pytest.approx(200, abs=1)
    # 收紧到真实图体（±4pt）
    for got, want in zip((fb.rect.x0, fb.rect.y0, fb.rect.x1, fb.rect.y1),
                         (FIG_RECT.x0, FIG_RECT.y0, FIG_RECT.x1, FIG_RECT.y1)):
        assert got == pytest.approx(want, abs=4.0), (fb.rect.to_list(), FIG_RECT)

    # 裁图落盘 + 图注绑定
    crop = Path(fb.image_path)
    assert crop.is_file() and crop.stat().st_size > 0
    assert crop.name == f"{fb.block_id}.png"
    assert fb.caption.startswith("Figure 1")
    assert fb.caption_block_id, "必须绑到图注块（阅读器的图+图注卡片要用它）"
    cap_block = next(b for b in result.blocks if b.id == fb.caption_block_id)
    assert cap_block.type == "caption"
    assert cap_block.meta.get("page_bbox"), "图注的真实位置要落进 meta（块的 rect 是虚拟横带）"

    # Markdown 里出现图片引用（这是用户要的最终效果）
    md_text = (d / "md" / "full.md").read_text(encoding="utf-8")
    assert "![" in md_text and crop.name in md_text
    # 图注只出现一次（插图块自身不带文字，避免同一句图注重复）
    assert md_text.count("Synthetic figure for the scan pipeline") == 1
    # 版面块（给程序看的）绝不能进正文事实源：否则会被当正文、送去翻译、显示给用户
    assert "layout" not in md_text
    assert not any("layout" in (b.text_clean or "") or "layout" in (b.text or "")
                   for b in result.blocks)
    assert "layout" not in (d / "md" / "blocks.json").read_text(encoding="utf-8")


def test_scan_figure_rejects_whole_page_box(scan_pdf, tmp_path):
    """模型把框画成整页（面积占比过高）→ 不出图。"""
    result, _d = _build(scan_pdf, tmp_path, _layout_md((0.0, 0.0, 1.0, 1.0)), "scan-full")
    assert result.figures == []
    assert any("面积占比" in w for w in result.warnings)
    assert SCAN_MAX_AREA_RATIO < 1.0


def test_scan_figure_rejects_blank_box(scan_pdf, tmp_path):
    """模型把框画在空白处 → 页图上找不到对应墨迹区域 → 不出图。"""
    result, _d = _build(scan_pdf, tmp_path, _layout_md((0.05, 0.66, 0.45, 0.70)), "scan-blank")
    assert result.figures == []
    assert any(("找不到对应的墨迹区域" in w) or ("几乎没有内容" in w)
               for w in result.warnings), result.warnings


def test_scanned_page_without_layout_block_keeps_zero_figures(scan_pdf, tmp_path):
    """模型没给版面块（旧行为）→ 与今天一致：0 张图，不报错。"""
    md = "# Scan\n\n**Figure 1**: caption only, no layout block.\n"
    result, _d = _build(scan_pdf, tmp_path, md, "scan-nolayout")
    assert result.figures == []
    assert result.blocks, "正文块照常产出"


def test_scan_figures_flow_into_reader_regions(scan_pdf, tmp_path, monkeypatch):
    """阅读器那一侧：图注锚定的收紧/贴边/切分必须**跳过**扫描件。

    真实故障（实测）：带 OCR 文本层的影印页上，图内轴标签会被聚成 heading 块，
    ``_tighten`` 拿它当分隔元素，把插图收成 25pt 细条、并把相邻图整张吃掉。
    """
    from backend.services.paper import figure_regions, paper_service

    md = _layout_md((0.10, 0.15, 0.95, 0.62))
    result, d = _build(scan_pdf, tmp_path, md, "scan-reader")
    assert len(result.figures) == 1
    fb = result.figures[0]

    def fake_paths(paper_id: str) -> dict:
        return {"ir": d / "ir.sqlite", "figures": d / "figures", "pdf": d / "source.pdf"}

    monkeypatch.setattr(paper_service, "paper_paths", fake_paths)
    figure_regions.invalidate_cache("scan-reader")
    regions = figure_regions.page_regions("scan-reader", 1)

    assert len(regions) == 1
    region = regions[0]
    assert region["kind"] == "figure" and region["number"] == "1"
    assert region["source"] == "scan"
    # 区域 = 像素定下的边界（没有被任何"收紧"改小）
    for got, want in zip(region["bbox"], fb.rect.to_list()):
        assert got == pytest.approx(want, abs=1.0)

    # 阅读器的插图条目：url 指向按需裁图接口
    media = figure_regions.media_of_page("scan-reader", 1)
    assert len(media) == 1 and media[0]["number"] == "1"
    assert "/figures/region/1/0" in media[0]["url"]
    # 扫描件的卡片不拼图注（见 test_scan_card_is_figure_only）
    assert figure_regions.caption_rects("scan-reader", 1, region) == []
    figure_regions.invalidate_cache("scan-reader")


def test_scan_figure_sits_right_above_its_caption(scan_pdf, tmp_path):
    """插图块的阅读顺序必须**紧跟在自己的图注之前**（图片行出现在图注上方）。

    真实故障（实测）：插图块用像素算出的真实 y 时，它会被排到正文中间，图片行离图注很远。
    扫描页的块 rect 是等分虚拟横带，阅读顺序全靠它排，所以插图块要共用图注的横带。
    """
    result, d = _build(scan_pdf, tmp_path, _layout_md((0.10, 0.15, 0.95, 0.62)),
                       "scan-order")
    ordered = sorted(result.blocks, key=lambda b: b.order_global)
    fig_i = next(i for i, b in enumerate(ordered) if b.type == "figure")
    cap_i = next(i for i, b in enumerate(ordered) if b.type == "caption")
    assert fig_i == cap_i - 1, [b.type for b in ordered]

    md_lines = [ln.strip() for ln in
                (d / "md" / "full.md").read_text(encoding="utf-8").split("\n")]
    img = next(i for i, ln in enumerate(md_lines) if ln.startswith("!["))
    cap = next(i for i, ln in enumerate(md_lines) if ln.startswith("**Figure 1"))
    assert img < cap, md_lines[max(0, img - 2):cap + 2]
    # 中间只允许有锚点/块注释这类标记行，不能夹着别的正文块
    assert all(not ln or ln.startswith("<") for ln in md_lines[img + 1:cap]), \
        md_lines[img:cap + 2]


def test_scan_figure_does_not_reorder_or_duplicate_caption(scan_pdf, tmp_path):
    """插图独立成块：图注块仍是 caption，插图块是 figure，两者不重复。"""
    result, _d = _build(scan_pdf, tmp_path, _layout_md((0.10, 0.15, 0.95, 0.62)),
                        "scan-blocks")
    fig_blocks = [b for b in result.blocks if b.type == "figure"]
    cap_blocks = [b for b in result.blocks if b.type == "caption"]
    assert len(fig_blocks) == 1 and len(cap_blocks) == 1
    assert fig_blocks[0].id == result.figures[0].block_id
    assert not (fig_blocks[0].text_clean or "").strip(), "插图块不带文字（否则图注会出现两次）"


def test_scan_figure_window_comes_from_pixels_not_model_box(scan_pdf, tmp_path):
    """模型框"上面多圈了正文、下面只盖住半张图"时，窗口必须由像素重算。

    真实故障（实测 p7）：模型给 ``y 228~402``，而图体实际是 ``y 200~420``、上面还有一段正文
    与一个 2.1 标题。直接拿模型框当窗口，裁出来是"正文 + 标题 + 上半张图"。
    """
    # 提案：上边探进正文段落、下边只到图体一半
    md = _layout_md((0.06, 0.09, 0.95, 0.38))
    result, _d = _build(scan_pdf, tmp_path, md, "scan-badseed")
    assert len(result.figures) == 1, result.warnings
    fb = result.figures[0]
    for got, want in zip((fb.rect.x0, fb.rect.y0, fb.rect.x1, fb.rect.y1),
                         (FIG_RECT.x0, FIG_RECT.y0, FIG_RECT.x1, FIG_RECT.y1)):
        assert got == pytest.approx(want, abs=5.0), (fb.rect.to_list(), FIG_RECT)
    assert fb.rect.y1 >= FIG_RECT.y1 - 5, "不能被模型框的下沿截断"


def test_scan_figure_window_rejects_text_paragraph(scan_pdf, tmp_path):
    """模型框画在正文段落上 → 页面上找不到"图"（文字组不算图）→ 不出图。"""
    # 提案正好落在图上方那段正文（y 60~105）
    md = _layout_md((0.14, 0.07, 0.86, 0.14))
    result, _d = _build(scan_pdf, tmp_path, md, "scan-textseed")
    assert result.figures == []
    assert any("找不到对应的墨迹区域" in w for w in result.warnings), result.warnings


# --------------------------------------------------------------------------
# 3. 完整图注：整段文本 + 整段框（切边界用、卡片用）
# --------------------------------------------------------------------------


def test_cut_figure_at_caption_both_directions():
    """图注切分只认**短行带**，且必须是"多行成段"或"尾巴很薄"。"""
    from backend.parsing.structure.ink_layout import InkBand, caption_cut_band

    fig_band = InkBand(300.0, 540.0, 110.0, 505.0)          # 图体：一条高带
    cap_bands = [InkBand(541.6, 551.9, 108.0, 504.0),       # 图注第一行
                 InkBand(556.0, 566.0, 108.0, 504.0),       # 图注第二行
                 InkBand(570.0, 580.0, 108.0, 504.0)]       # 图注第三行
    win = Rect(110, 300, 505, 580)
    bands = [fig_band, *cap_bands]
    # 模型框准确（落在第一行）→ 切在整段图注的上沿
    assert caption_cut_band(bands, Rect(108, 541.6, 504, 545), win) == 541.6
    # 模型框偏下（落在第二行）→ 仍切整段图注的上沿（带结构比模型坐标可信）
    assert caption_cut_band(bands, Rect(108, 556.0, 504, 566), win) == 541.6
    # 模型框整块压在一条高大图体带里（p7 实测：把子图标题 "(b) TURBOQUANT_mse" 当图注）
    # → 没有短行带与它相交 → 不切，否则会把下半张图裁掉
    tall = [InkBand(300.0, 530.0, 110.0, 505.0)]
    assert caption_cut_band(tall, Rect(300, 400, 420, 415), Rect(110, 300, 505, 530)) is None
    # 框一半在图体带上、一半压到图注首行（p8 Figure 2 的实测情形）→ 仍切在整段图注上沿
    assert caption_cut_band(bands, Rect(108, 530.0, 504, 545), win) == 541.6
    assert caption_cut_band(bands, None, win) is None
    # 孤零零一行、下面还压着大半个图体（p7 实测）→ 不切
    lone = [fig_band, InkBand(400.0, 412.0, 300.0, 420.0)]
    assert caption_cut_band(lone, Rect(300, 400, 420, 412), Rect(110, 300, 505, 540)) is None


def test_text_group_is_not_a_figure():
    """一个文字段落不能被当成图（模型把框画在正文上时的兜底）。"""
    from backend.parsing.structure.ink_layout import InkBand, is_text_group

    para = [InkBand(100.0 + i * 14, 110.0 + i * 14, 108.0, 504.0) for i in range(6)]
    assert is_text_group(para) is True
    figure = [InkBand(300.0, 480.0, 110.0, 505.0)]          # 一张大图：一条高带
    assert is_text_group(figure) is False
    mixed = [InkBand(300.0, 480.0, 110.0, 505.0),
             InkBand(490.0, 500.0, 200.0, 400.0)]           # 图 + 底下一行坐标轴标签
    assert is_text_group(mixed) is False
    assert is_text_group([InkBand(100.0, 110.0, 108.0, 504.0)]) is False   # 只有一行


def test_usable_caption_box_rejects_implausible():
    from backend.parsing.pipeline import _usable_caption_box

    page = Rect(0, 0, PAGE_W, PAGE_H)
    assert _usable_caption_box(Rect(120, 541, 505, 598), page) is True
    assert _usable_caption_box(Rect(100, 300, 500, 700), page) is False   # 太高：框进了正文
    assert _usable_caption_box(Rect(300, 541, 400, 560), page) is False   # 太窄：不像图注


def test_complete_caption_text_and_box(scan_pdf, tmp_path, monkeypatch):
    """长图注被切成两块 → 图注文本要**整条**接起来（且不能带换行，否则图片行断成两行）。"""
    from backend.services.paper import figure_regions, paper_service

    md = ("# [MOCK] Scanned\n\n正文。\n\n"
          "**Figure 1**: A complete caption that continues on the second line and\n"
          "then a third line.\n\n"
          "<!-- layout {\"figures\":[{\"number\":\"1\",\"bbox\":[0.10,0.15,0.95,0.62],"
          "\"caption_bbox\":[0.10,0.63,0.85,0.70],"
          "\"caption\":\"Figure 1: 模型给的整段图注\"}]} -->")
    result, d = _build(scan_pdf, tmp_path, md, "scan-cap")
    fb = result.figures[0]
    # 文本来自**转录的图注块**（事实源），不是模型在版面块里复述的那份
    assert fb.caption.startswith("Figure 1: A complete caption")
    assert "third line." in fb.caption
    assert "\n" not in fb.caption and "  " not in fb.caption

    md_text = (d / "md" / "full.md").read_text(encoding="utf-8")
    assert next(ln for ln in md_text.split("\n") if ln.startswith("![")) == \
        f"![figure](../figures/{Path(fb.image_path).name})"      # 没被换行截断

    # 图注框（整段）写进 meta，供"图+图注卡片"用
    cap_blk = next(b for b in result.blocks if b.id == fb.caption_block_id)
    box = cap_blk.meta["page_bbox"]
    assert box[3] - box[1] > 20, box       # 整段（多行）而不是一行
    assert box[1] >= fb.rect.y1 - 2, (box, fb.rect.to_list())

    # 阅读器那条路（markdown_refs）用完整图注当 alt 文本
    def fake_paths(paper_id: str) -> dict:
        return {"ir": d / "ir.sqlite", "figures": d / "figures", "pdf": scan_pdf}

    monkeypatch.setattr(paper_service, "paper_paths", fake_paths)
    figure_regions.invalidate_cache("scan-cap")
    refs = figure_regions.markdown_refs("scan-cap", 1, prefix="/api/v1")
    # alt 文本取整条图注（截到 80 字，但必须已经越过"第二行"的接缝）
    assert "then a third" in refs["1"] and "\n" not in refs["1"], refs
    figure_regions.invalidate_cache("scan-cap")


def test_scan_card_is_figure_only(scan_pdf, tmp_path, monkeypatch):
    """扫描件的卡片 = 纯图，不拼图注。

    实测两个毛病都出自"拼图注"：图注粗框偏窄 → 两侧字母被切掉；图体窗口可能已经含了
    图注第一行 → 同一句话出现两次（"像拼接的一样"）。图注在阅读器里本来就有自己的一行、
    导出 Markdown 里也紧跟图片下方，所以扫描件的卡片不拼不会丢信息。
    """
    from backend.services.paper import figure_regions, paper_service

    def fake_read(page_no: int, image: bytes) -> str:
        return _layout_md((0.10, 0.15, 0.95, 0.62))     # 有 caption_bbox

    result, d = _build(scan_pdf, tmp_path, fake_read(1, b""), "scan-card")

    def fake_paths(paper_id: str) -> dict:
        return {"ir": d / "ir.sqlite", "figures": d / "figures", "pdf": scan_pdf}

    monkeypatch.setattr(paper_service, "paper_paths", fake_paths)
    figure_regions.invalidate_cache("scan-card")
    region = figure_regions.page_regions("scan-card", 1)[0]
    assert region["source"] == "scan"
    assert figure_regions.caption_rects("scan-card", 1, region) == []
    figure_regions.invalidate_cache("scan-card")
