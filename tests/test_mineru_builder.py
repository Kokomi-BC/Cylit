"""在线解析构建器：``content_list.json`` → 与本地解析**同构**的 ParseResult。

默认不跑（``@pytest.mark.online``，见 `tests/conftest.py`）：``pytest --run-online``。

这里钉住的是"换成 MinerU 之后，下游还能不能照旧工作"这件事，所以每条断言都对着一个
**具体的下游契约**，而不是"我们的代码输出了什么"：

1. 坐标：MinerU 的 bbox 是 0~1000 归一化（官方 SDK 的 ``bbox_attributes.max=1000``），
   必须换算成 pt 才能进 IR；换算错了阅读器的高亮、区域裁图全都会偏。
2. 顺序：**不做按 y 重排** —— 双栏论文按 y 排会把左右栏交错穿插。
3. 公式：MinerU 直接给 LaTeX，按 A 级采用（本地那条路才需要候选 + 三重校验）。
4. 插图：区域层把 ``source="online"`` 当"精确来源"，直接采用 bbox（不再收紧/贴边）。
5. 图注：MinerU 把图注并进 item 的 caption 字段，我们给它补一个块，才可翻译/可检索。
6. 产物：IR + full.md + blocks.json 齐全，`figure_regions` 能从中出图。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz
import pytest

from backend.parsing.online.mineru_builder import (
    MineruBuilder, build_from_bundle, html_table_to_text, strip_math_wrappers,
)

pytestmark = pytest.mark.online

PAGE_W, PAGE_H = 612.0, 792.0


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


def make_pdf(path: Path, pages: int = 2) -> Path:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((72, 120), f"Demo Paper page {i + 1}", fontsize=12)
        page.insert_text((72, 160), "Abstract: a tiny synthetic document.", fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def make_bundle(tmp_path: Path, items: list[dict], *,
                images: dict[str, bytes] | None = None,
                prefix: str = "task-uuid-1") -> dict:
    """造一个贴近真实的结果包目录（文件名带任务 uuid 前缀 + ``images/``）。"""
    from backend.parsing.online.mineru_client import locate_bundle

    raw = tmp_path / "mineru_raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{prefix}_content_list.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8")
    (raw / "full.md").write_text("# MinerU markdown\n", encoding="utf-8")
    for name, data in (images or {}).items():
        target = raw / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    bundle = locate_bundle(raw)
    bundle["batch_id"] = "B-test"
    bundle["raw_dir"] = str(raw)
    return bundle


def text_item(page: int, text: str, box: list[float], **extra) -> dict:
    return {"type": "text", "text": text, "page_idx": page - 1, "bbox": box, **extra}


def build(tmp_path: Path, items: list[dict], **kw):
    pages = int(kw.pop("pages", 2))
    pdf = make_pdf(tmp_path / "source.pdf", pages=pages)
    bundle = make_bundle(tmp_path, items, images=kw.pop("images", None))
    paper_dir = tmp_path / "paper"
    result = build_from_bundle(pdf, "demo-paper", paper_dir, bundle, **kw)
    return result, paper_dir, bundle


# --------------------------------------------------------------------------
# 1. 坐标换算
# --------------------------------------------------------------------------


def test_bbox_is_scaled_from_thousandths_to_points(tmp_path):
    result, _dir, _b = build(tmp_path, [
        text_item(1, "hello world", [100, 100, 500, 200]),
    ])
    b = result.blocks[0]
    assert (round(b.rect.x0, 1), round(b.rect.y0, 1),
            round(b.rect.x1, 1), round(b.rect.y1, 1)) == (61.2, 79.2, 306.0, 158.4)
    assert result.stats["mineru"]["bbox_space"].startswith("归一化")


def test_pt_coordinates_are_detected_by_overflow(tmp_path):
    """坐标超过 1000 ⟹ 只能是 pt（0~1000 归一化的上限就是 1000）。"""
    result, _dir, _b = build(tmp_path, [
        text_item(1, "wide", [50, 60, 1200, 300]),
    ])
    assert result.stats["mineru"]["bbox_space"].startswith("pt")
    assert result.blocks[0].rect.x1 == 612.0          # 越界被裁到页宽


def test_ambiguous_coordinates_follow_the_official_convention(tmp_path):
    """都落在页面尺寸之内（有歧义）时按官方契约取归一化，并把这个判断记进统计。"""
    result, _dir, _b = build(tmp_path, [
        text_item(1, "small", [10, 10, 200, 200]),
    ])
    assert result.stats["mineru"]["bbox_space"].startswith("归一化")
    assert round(result.blocks[0].rect.x0, 1) == 6.1


# --------------------------------------------------------------------------
# 2. 顺序 = MinerU 给的顺序
# --------------------------------------------------------------------------


def test_reading_order_follows_content_list_not_y_coordinate(tmp_path):
    """双栏：右栏在**上面**，但 content_list 先给左栏 —— 必须保持 MinerU 的顺序。

    本地解析要按 (页, y, x) 重排（它自己插入孤儿块）；在线这条路重排会把双栏
    "按 y 交错"排成一条，阅读器和翻译的对齐都会跟着错。
    """
    result, _dir, _b = build(tmp_path, [
        text_item(1, "LEFT-COLUMN-FIRST", [40, 600, 300, 640]),
        text_item(1, "RIGHT-COLUMN-SECOND", [320, 80, 580, 120]),
    ])
    texts = [b.text for b in result.blocks]
    assert texts == ["LEFT-COLUMN-FIRST", "RIGHT-COLUMN-SECOND"]
    assert [b.order_global for b in result.blocks] == [1, 2]


def test_blocks_get_ids_and_the_front_prefix(tmp_path):
    result, _dir, _b = build(tmp_path, [
        text_item(1, "one", [40, 100, 500, 140]),
        text_item(2, "two", [40, 100, 500, 140]),
    ])
    ids = [b.id for b in result.blocks]
    assert ids[0].endswith("-p01-b01") and ids[1].endswith("-p02-b01")
    assert ids[0].startswith("demopaper")


# --------------------------------------------------------------------------
# 3. 标题 / 公式 / 列表 / 代码
# --------------------------------------------------------------------------


def test_headings_come_from_text_level_and_reach_markdown(tmp_path):
    result, paper_dir, _b = build(tmp_path, [
        {"type": "title", "text": "1 Introduction", "page_idx": 0,
         "bbox": [80, 60, 520, 90], "text_level": 1},
        text_item(1, "Body paragraph.", [80, 100, 520, 160]),
        {"type": "text", "text": "1.1 Sub Section", "page_idx": 0,
         "bbox": [80, 170, 520, 195], "text_level": 2},
    ])
    headings = [b for b in result.blocks if b.type == "heading"]
    assert [h.text for h in headings] == ["1 Introduction", "1.1 Sub Section"]
    assert headings[0].meta["mineru_text_level"] == 1
    md = (paper_dir / "md" / "full.md").read_text(encoding="utf-8")
    assert "## 1 Introduction" in md            # 无编号层级按既有口径（2 级）
    assert "### 1.1 Sub Section" in md          # 带编号 → 2 + 点号数
    index = json.loads((paper_dir / "md" / "blocks.json").read_text(encoding="utf-8"))
    levels = {b["text"]: b["heading_level"] for b in index["blocks"]
              if b["type"] == "heading"}
    assert levels == {"1 Introduction": 2, "1.1 Sub Section": 3}


def test_equation_is_adopted_as_grade_a_latex(tmp_path):
    result, paper_dir, _b = build(tmp_path, [
        {"type": "equation", "text": "$$\\frac{a}{b} = c$$", "page_idx": 0,
         "bbox": [200, 300, 400, 340], "text_format": "block"},
    ])
    assert len(result.formulas) == 1
    f = result.formulas[0]
    assert f.latex_final == "\\frac{a}{b} = c" and f.grade == "A"
    assert f.source == "mineru" and f.status == "validated"
    host = result.block_by_id(f.block_id)
    assert host is not None and host.type == "formula"
    md = (paper_dir / "md" / "full.md").read_text(encoding="utf-8")
    assert "$$ \\frac{a}{b} = c $$" in md


def test_inline_equation_keeps_the_inline_flag(tmp_path):
    result, _dir, _b = build(tmp_path, [
        {"type": "equation", "text": "$x_i$", "page_idx": 0,
         "bbox": [200, 300, 240, 320], "text_format": "inline"},
    ])
    assert result.formulas[0].is_inline is True
    assert result.formulas[0].latex_final == "x_i"


def test_list_and_code_items_become_searchable_text(tmp_path):
    result, _dir, _b = build(tmp_path, [
        {"type": "list", "list_items": ["first", "second"], "page_idx": 0,
         "bbox": [80, 100, 500, 160]},
        {"type": "code", "code_body": "for i in x:\n    pass", "sub_type": "python",
         "page_idx": 0, "bbox": [80, 180, 500, 240]},
    ])
    assert result.blocks[0].text == "first\nsecond"
    assert result.blocks[1].text == "```python\nfor i in x:\n    pass\n```"


def test_layout_noise_types_are_dropped(tmp_path):
    """页码/页眉/页脚进正文只会污染翻译与检索 —— 本地解析也是剔除的。"""
    result, _dir, _b = build(tmp_path, [
        text_item(1, "real content", [80, 300, 500, 340]),
        {"type": "page_number", "text": "3", "page_idx": 0, "bbox": [290, 740, 320, 760]},
        {"type": "header", "text": "Journal of Demos", "page_idx": 0,
         "bbox": [80, 30, 500, 45]},
        {"type": "footer", "text": "2026", "page_idx": 0, "bbox": [80, 760, 200, 775]},
    ])
    assert [b.text for b in result.blocks] == ["real content"]
    assert result.stats["mineru"]["items"]["page_number"] == 1
    assert result.stats["mineru"]["items"]["header"] == 1


# --------------------------------------------------------------------------
# 4. 插图 + 图注
# --------------------------------------------------------------------------


def test_figure_keeps_mineru_bbox_and_gets_a_caption_block(tmp_path):
    result, paper_dir, _b = build(
        tmp_path,
        [{"type": "image", "img_path": "images/f1.jpg", "page_idx": 0,
          "bbox": [100, 200, 500, 500], "image_caption": ["Figure 1. Demo chart."]}],
        images={"images/f1.jpg": b"\xff\xd8\xff\xe0fake-jpeg"},
    )
    assert len(result.figures) == 1
    fig = result.figures[0]
    assert fig.caption == "Figure 1. Demo chart."
    # ⚠️ bbox 必须是 MinerU 给的那个（61.2/158.4/306/396），不能被区域层或解析层挪动
    assert (round(fig.rect.x0, 1), round(fig.rect.y0, 1),
            round(fig.rect.x1, 1), round(fig.rect.y1, 1)) == (61.2, 158.4, 306.0, 396.0)
    assert fig.audit["source"] == "online"
    # 图片文件复制进 figures/，Markdown 里能引用
    assert fig.image_path and Path(fig.image_path).is_file()
    host = result.block_by_id(fig.block_id)
    assert host.meta["image_rel"] == f"../figures/{Path(fig.image_path).name}"
    # 图注是一个**独立块**（可翻译、可检索），挂在图体之后
    cap = result.block_by_id(fig.caption_block_id)
    assert cap is not None and cap.type == "caption"
    assert cap.text == "Figure 1. Demo chart."
    assert cap.order_global > host.order_global
    assert cap.rect.y0 >= fig.rect.y1 - 0.01
    md = (paper_dir / "md" / "full.md").read_text(encoding="utf-8")
    assert "![Figure 1. Demo chart.]" in md and "**Figure 1. Demo chart.**" in md


def test_chart_items_are_treated_as_figures(tmp_path):
    result, _dir, _b = build(tmp_path, [
        {"type": "chart", "img_path": "images/c1.png", "page_idx": 0,
         "bbox": [100, 200, 500, 400], "chart_caption": ["Chart 2. Trends."]},
    ])
    assert len(result.figures) == 1
    assert result.figures[0].caption == "Chart 2. Trends."


def test_figure_without_image_file_still_produces_a_region(tmp_path):
    """MinerU 没给图片文件（或结果包里找不到）时不能整张图丢掉：区域层照样能从 PDF 裁出来。"""
    result, _dir, _b = build(tmp_path, [
        {"type": "image", "img_path": "images/missing.jpg", "page_idx": 0,
         "bbox": [100, 200, 500, 400], "image_caption": ["Figure 3. X."]},
    ])
    assert len(result.figures) == 1
    assert result.figures[0].image_path is None
    assert any("读不到 MinerU 的插图文件" in w for w in result.warnings)
    # 连 img_path 都没有的情况：同样只告警、不出文件，但不丢图
    second = tmp_path / "second"
    second.mkdir()
    result2, _d2, _b2 = build(second, [
        {"type": "image", "page_idx": 0, "bbox": [100, 200, 500, 400],
         "image_caption": ["Figure 4. Y."]},
    ])
    assert len(result2.figures) == 1
    assert any("没给插图文件" in w for w in result2.warnings)


# --------------------------------------------------------------------------
# 5. 表格
# --------------------------------------------------------------------------


def test_table_body_html_becomes_text_plus_structured_table(tmp_path):
    html = ("<table><tr><td>Model</td><td>Score</td></tr>"
            "<tr><td>Ours</td><td>99.1</td></tr></table>")
    result, paper_dir, _b = build(tmp_path, [
        {"type": "table", "table_body": html, "page_idx": 0,
         "bbox": [80, 400, 520, 520], "table_caption": ["Table 1. Results."],
         "num_rows": 2, "num_cols": 2},
    ])
    assert len(result.tables) == 1
    tb = result.tables[0]
    assert (tb.row_count, tb.col_count) == (2, 2)
    assert tb.status == "grid" and tb.has_borders is True
    host = result.block_by_id(tb.block_id)
    assert host.type == "table"
    assert "Model | Score" in host.text and "Ours | 99.1" in host.text
    assert "<td>" in host.meta["table_html"]
    cap = result.block_by_id(tb.caption_block_id)
    assert cap is not None and cap.type == "caption"
    md = (paper_dir / "md" / "full.md").read_text(encoding="utf-8")
    assert "Model | Score" in md


def test_empty_table_is_skipped_with_a_warning(tmp_path):
    result, _dir, _b = build(tmp_path, [
        {"type": "table", "table_body": "", "page_idx": 0, "bbox": [80, 400, 520, 520]},
        text_item(1, "kept", [80, 600, 500, 640]),
    ])
    assert result.tables == []
    assert [b.text for b in result.blocks] == ["kept"]
    assert any("空表格" in w for w in result.warnings)


# --------------------------------------------------------------------------
# 6. 产物：IR / Markdown / 报告 / 页图
# --------------------------------------------------------------------------


def test_products_are_written_like_the_local_pipeline(tmp_path):
    result, paper_dir, _b = build(tmp_path, [
        text_item(1, "page one text", [80, 100, 500, 140]),
        text_item(2, "page two text", [80, 100, 500, 140]),
    ])
    assert (paper_dir / "ir.sqlite").is_file()
    assert (paper_dir / "md" / "full.md").is_file()
    assert (paper_dir / "md" / "blocks.json").is_file()
    assert (paper_dir / "md" / "page-001.md").is_file()
    assert (paper_dir / "reports" / "parse_report.json").is_file()
    assert (paper_dir / "pages" / "page-0001.png").is_file()
    report = json.loads((paper_dir / "reports" / "parse_report.json")
                        .read_text(encoding="utf-8"))
    assert report["engine"] == "mineru"
    assert report["counts"]["blocks"] == 2
    assert result.stats["parse_options"]["engine"] == "mineru"
    assert result.counts()["pages"] == 2


def test_page_info_carries_the_rendered_page_images(tmp_path):
    result, _dir, _b = build(tmp_path, [text_item(1, "x", [80, 100, 500, 140])])
    pages = {p.page_no: p for p in result.pages}
    assert pages[1].image_path and Path(pages[1].image_path).is_file()
    assert pages[1].block_count == 1 and pages[1].text_chars > 0


def test_parse_stats_are_persisted_into_the_ir(tmp_path):
    """解析口径必须落在 IR 里（`IRWriter.write` 写的是**写库那一刻**的 `result.stats`）。

    为什么单独立一条：真机跑完第一次在线解析后，IR 的 `stats_json` 里 `mineru` 全是
    null —— 统计是在 `writer.write()` **之后**才填的，于是"这篇是怎么解析的、坐标按哪个
    口径换算"在 IR 里查不到（只剩 `parse_report.json` 有）。真机验证恰恰要靠 IR 核对，
    所以这条把它钉住。
    """
    result, paper_dir, _b = build(tmp_path, [text_item(1, "x", [80, 100, 500, 140])])
    from backend.parsing.ir.ir_reader import IRReader

    stats = json.loads(IRReader(paper_dir / "ir.sqlite").paper()["stats_json"])
    assert stats["engine"] == "mineru"
    assert stats["parse_options"]["engine"] == "mineru"
    assert stats["mineru"]["bbox_space"] and stats["mineru"]["figures_written"] == 0
    assert stats["mineru"]["items"]["text"] == 1
    # IR 里是"写库那一刻"的分段耗时（`persist` 段要写完才知道，只能在内存/报告里）
    assert {"render", "structure", "rebind"} <= set(stats["stage_ms"])
    assert "persist" in result.stats["stage_ms"]
    assert stats["quality"], "质保报告也要在 IR 里（与本地解析同一套口径）"


def test_zero_page_parse_report_and_reprint_keeps_translation(tmp_path):
    """重解析必须沿用旧块 ID —— 否则译文/高亮会全部对不上（两条引擎共用这条契约）。"""
    items = [text_item(1, "stable paragraph", [80, 100, 500, 140])]
    result1, paper_dir, bundle = build(tmp_path, items)
    first_id = result1.blocks[0].id
    # 在 md 里写一条译文（模拟翻译产物），再用同一个 bundle 重跑
    md_path = paper_dir / "md" / "full.md"
    md = md_path.read_text(encoding="utf-8")
    md_path.write_text(md + f"\n<!-- tr:{first_id} rev:1 sha:deadbeef -->\n稳定段落\n<!-- /tr:{first_id} -->\n",
                       encoding="utf-8")
    pdf = tmp_path / "source.pdf"
    result2 = build_from_bundle(pdf, "demo-paper", paper_dir, bundle)
    assert result2.blocks[0].id == first_id
    assert result2.stats["rebind"]["matched"] == 1
    assert "稳定段落" in md_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 7. 与区域层对接：在线插图走"精确来源"分支
# --------------------------------------------------------------------------


def test_region_layer_uses_the_online_bbox_verbatim(tmp_path, monkeypatch):
    """在线插图的区域 == MinerU 给的 bbox（不做收紧/贴边）。

    为什么这条重要：区域层那三道闸门（收紧/贴边/切分）全是为了修**本地解析**拿到的
    粗糙 bbox。MinerU 的 bbox 是版面模型给的准框，再贴一次边只会把它挪歪 ——
    这里在图体正下方/正上方各放一个文本块（本地那条路会命中贴边逻辑），
    断言区域**一个像素都没动**。
    """
    from backend.services.paper import figure_regions as fr
    from backend.services.paper import paper_service

    result, paper_dir, _b = build(tmp_path, [
        text_item(1, "text right above the figure", [61, 120, 306, 157]),
        {"type": "image", "img_path": "images/f1.jpg", "page_idx": 0,
         "bbox": [100, 200, 500, 500], "image_caption": ["Figure 1. Demo."]},
        text_item(1, "text right below the caption", [61, 400, 306, 430]),
    ], images={"images/f1.jpg": b"\xff\xd8\xff\xe0x"})

    monkeypatch.setattr(paper_service, "paper_paths",
                        lambda pid: {"ir": paper_dir / "ir.sqlite"})
    fr.invalidate_cache()
    regions = fr.all_regions("demo-paper", force=True)[1]
    assert len(regions) == 1
    reg = regions[0]
    assert reg["kind"] == "figure" and reg["source"] == "online"
    assert reg["number"] == "1"
    fig = result.figures[0]
    assert reg["bbox"] == [round(v, 2) for v in fig.rect.to_list()]


def test_local_figures_are_not_affected_by_the_online_branch(tmp_path, monkeypatch):
    """反向断言：同一份 bbox，只把 ``source`` 从 ``online`` 改回"本地"，区域就被贴边了。

    为什么必须有这条：跳过收紧/贴边的条件是 ``audit.source == "online"``（精确来源），
    而**不能**变成"所有插图都不贴边" —— 存量论文的出图口径一个字都不能动。
    构造上让上方文本块的底边与图体顶边**正好贴住**（本地那条路会把它推开 TEXT_PAD=1.2pt）。
    """
    from backend.services.paper import figure_regions as fr
    from backend.services.paper import paper_service

    result, paper_dir, _b = build(tmp_path, [
        text_item(1, "text touching the figure top", [61, 100, 306, 200]),
        {"type": "image", "img_path": "images/f1.jpg", "page_idx": 0,
         "bbox": [100, 200, 500, 500], "image_caption": ["Figure 1. Demo."]},
    ], images={"images/f1.jpg": b"\xff\xd8\xff\xe0x"})
    raw_top = round(result.figures[0].rect.y0, 2)

    # 把 IR 里的 figure 来源改回"本地"，其余一模一样
    import sqlite3

    conn = sqlite3.connect(str(paper_dir / "ir.sqlite"))
    conn.execute("UPDATE figure SET audit_json = ? WHERE paper_id = ?",
                 (json.dumps({"source": "text"}), "demo-paper"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(paper_service, "paper_paths",
                        lambda pid: {"ir": paper_dir / "ir.sqlite"})
    fr.invalidate_cache()
    reg = fr.all_regions("demo-paper", force=True)[1][0]
    assert reg["source"] != "online"
    # 顶边被挪过（实测 157.2 = 上沿 − TEXT_PAD）；在线那条路保持 158.4 一动不动
    assert reg["bbox"][1] != raw_top


# --------------------------------------------------------------------------
# 8. 纯函数
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("$$x$$", "x"),
    ("$x$", "x"),
    ("\\[x\\]", "x"),
    ("x", "x"),
    ("$$", "$$"),          # 只剩一对定界符（空公式）不动它，交给调用方判空
])
def test_strip_math_wrappers(raw, want):
    assert strip_math_wrappers(raw) == want


def test_html_table_to_text_keeps_rows_and_cells():
    html = ("<table><thead><tr><th>A</th><th>B</th></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>")
    assert html_table_to_text(html) == "A | B\n1 | 2"
    assert html_table_to_text("") == ""
    assert html_table_to_text("<p>plain &amp; simple</p>") == "plain & simple"


def test_missing_content_list_raises_a_clear_error(tmp_path):
    from backend.core.errors import ParseError

    pdf = make_pdf(tmp_path / "source.pdf", pages=1)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "full.md").write_text("# no content list", encoding="utf-8")
    with pytest.raises(ParseError) as err:
        MineruBuilder(pdf, "p", tmp_path / "paper",
                      bundle={"raw_dir": str(raw), "content_list": None}).build()
    assert "content_list" in str(err.value)


# --------------------------------------------------------------------------
# 9. 页码范围：切片内序号 → 原文档页号
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,total,want", [
    ("", 6, [1, 2, 3, 4, 5, 6]),
    ("3-4", 6, [3, 4]),
    ("2,4-6", 6, [2, 4, 5, 6]),
    ("2--2", 6, [2, 3, 4, 5]),
    ("7", 6, []),                   # 超出页数 → 空（映射表空会退回"原始页号 + 告警"）
    ("1-3,2-4", 6, [1, 2, 3, 4]),    # 重叠去重，保持顺序
])
def test_parse_page_ranges(raw, total, want):
    from backend.parsing.online.page_ranges import parse_page_ranges

    assert parse_page_ranges(raw, total) == want


@pytest.mark.parametrize("bad", ["abc", "1-", "1..3", "1,a", ","])
def test_parse_page_ranges_rejects_bad_syntax(bad):
    from backend.parsing.online.page_ranges import parse_page_ranges

    with pytest.raises(ValueError):
        parse_page_ranges(bad, 6)


def test_slice_page_index_is_mapped_to_real_pages(tmp_path):
    """**真机实测过的坑**：限定页码后 `content_list` 的 `page_idx` 从切片起点重新计数。

    实测（6 页 PDF）：请求 `page_ranges="3-4"` → 返回 `page_idx ∈ {0,1}`，内容是第 3、4 页。
    不映射的话整篇会挂到第 1、2 页 —— 阅读器里每页都有内容，看不出任何异常。
    """
    items = [
        {"type": "text", "text": "from page three", "page_idx": 0,
         "bbox": [80, 100, 500, 140]},
        {"type": "text", "text": "from page four", "page_idx": 1,
         "bbox": [80, 100, 500, 140]},
        {"type": "image", "img_path": "images/abc.jpg", "page_idx": 1,
         "bbox": [100, 200, 500, 400], "image_caption": ["Figure 9. Ranged."]},
    ]
    result, paper_dir, _b = build(
        tmp_path, items,
        pages=6,
        mineru_params={"page_ranges": "3-4", "model_version": "pipeline"},
        images={"images/abc.jpg": b"\xff\xd8\xff\xe0x"})

    paras = [(b.page_no, b.text) for b in result.blocks if b.type == "para"]
    assert paras == [(3, "from page three"), (4, "from page four")]
    assert result.figures[0].page_number == 4
    assert result.block_by_id(result.figures[0].caption_block_id).page_no == 4
    # 映射必须留痕（映射错了不会报错，事后只能靠它核对）
    stats = json.loads((paper_dir / "reports" / "parse_report.json")
                       .read_text(encoding="utf-8"))["stats"]
    assert stats["mineru"]["params"]["page_ranges"] == "3-4"
    assert stats["mineru_page_map"] == {"page_ranges": "3-4", "selected_pages": [3, 4]}


def test_multi_range_slice_indices_follow_the_selected_order(tmp_path):
    """`2,4-6`（4 页）→ 返回 0,1,2,3，顺序对应选中页 [2,4,5,6]（真机实测）。"""
    items = [
        {"type": "text", "text": f"slice {i}", "page_idx": i,
         "bbox": [80, 100, 500, 140]}
        for i in range(4)
    ]
    result, _dir, _b = build(tmp_path, items, pages=6,
                             mineru_params={"page_ranges": "2,4-6"})
    assert [(b.page_no, b.text) for b in result.blocks] == [
        (2, "slice 0"), (4, "slice 1"), (5, "slice 2"), (6, "slice 3")]


def test_without_page_ranges_page_index_is_page_number(tmp_path):
    """没填页码范围时行为不变：`page_idx + 1`（上面那条的反向断言）。"""
    result, _dir, _b = build(tmp_path, [text_item(2, "second", [80, 100, 500, 140])])
    assert result.blocks[0].page_no == 2
    assert "mineru_page_map" not in result.stats


def test_unparsable_page_ranges_falls_back_with_a_warning(tmp_path):
    """手改/旧数据里的脏页码范围：**不能**让整篇挂错页，退回原始页号并告警。"""
    result, _dir, _b = build(tmp_path, [text_item(2, "second", [80, 100, 500, 140])],
                             mineru_params={"page_ranges": "第八页"})
    assert result.blocks[0].page_no == 2
    assert any("无法解析" in w for w in result.warnings)


def test_slice_index_beyond_the_selected_pages_is_reported(tmp_path):
    result, _dir, _b = build(tmp_path, [
        {"type": "text", "text": "in range", "page_idx": 0, "bbox": [80, 100, 500, 140]},
        {"type": "text", "text": "beyond", "page_idx": 5, "bbox": [80, 100, 500, 140]},
    ], pages=6, mineru_params={"page_ranges": "3-4"})
    assert any("只选中了 2 页" in w for w in result.warnings)


# --------------------------------------------------------------------------
# 10. 多面板图：MinerU 把一张图的 (a)(b) 面板拆成多个 item
# --------------------------------------------------------------------------

#: 真机抓下来的 DeepSeek-V3.2 p5（归一化 bbox 原样）：论文里是 Figure 2 与 Figure 3 各两个面板，
#: MinerU 输出 4 个 `chart` item，并且把 "Figure 2 | …" 这条**图级图注挂在了下一行那张图上**。
DEEPSEEK_P5_PANELS: list[dict] = [
    {"type": "chart", "img_path": "images/a.jpg", "page_idx": 0,
     "bbox": [129, 109, 477, 293], "chart_caption": ["(a) BrowseComp Training Curve"]},
    {"type": "chart", "img_path": "images/b.jpg", "page_idx": 0,
     "bbox": [515, 108, 862, 293], "chart_caption": ["(b) SWE Training Curve"]},
    {"type": "chart", "img_path": "images/c.jpg", "page_idx": 0,
     "bbox": [127, 406, 480, 592],
     "chart_caption": ["Figure 2 | RL training curve of DeepSeek-V3.1-Terminus and "
                       "DeepSeek-V3.2-Exp on BrowseComp and SWE Verified.",
                       "(a) Prefilling"]},
    {"type": "chart", "img_path": "images/d.jpg", "page_idx": 0,
     "bbox": [512, 406, 867, 590],
     "chart_caption": ["(b) Decoding",
                       "Figure 3 | Inference costs of DeepSeek-V3.1-Terminus and "
                       "DeepSeek-V3.2-Exp on H800 clusters."]},
]


def test_side_by_side_panels_become_one_figure(tmp_path):
    """用户口径："标注 6 张图，实际 4 张" —— 4 个面板 item 必须合成 2 张图。

    判据是**版面**（顶底对齐 + 间隙小），不是"信 MinerU 的图注"：实测它把 Figure 2 的图注
    挂在了 Figure 3 那一行上，所以图注另外按阅读顺序重配（见下一条用例）。
    """
    result, _dir, _b = build(tmp_path, DEEPSEEK_P5_PANELS, pages=1)
    assert len(result.figures) == 2
    assert [f.subfigure_count for f in result.figures] == [2, 2]
    top, bottom = result.figures[0], result.figures[1]
    assert all(f.audit.get("merged") is True for f in (top, bottom))
    assert len(top.audit["img_paths"]) == 2
    # bbox = 两块的并集（归一化坐标换算成 pt 后比较）
    assert round(top.rect.x0, 1) == round(129 / 1000 * PAGE_W, 1)
    assert round(top.rect.x1, 1) == round(862 / 1000 * PAGE_W, 1)
    assert round(bottom.rect.x0, 1) == round(127 / 1000 * PAGE_W, 1)
    assert round(bottom.rect.x1, 1) == round(867 / 1000 * PAGE_W, 1)
    # 合并后的图从 PDF 整块裁（不是拿其中一个面板的图）
    for f in (top, bottom):
        assert f.image_path and Path(f.image_path).is_file()
        assert Path(f.image_path).stat().st_size > 0


def test_panel_captions_are_reassigned_in_reading_order(tmp_path):
    """图注重配：实测 MinerU 把 Figure 2 的图注挂到了下一行那块图上，按**阅读顺序**配才对。"""
    result, _dir, _b = build(tmp_path, DEEPSEEK_P5_PANELS, pages=1)
    top, bottom = result.figures
    assert top.caption.startswith("Figure 2 | RL training curve")
    assert "(a) BrowseComp Training Curve" in top.caption
    assert "(b) SWE Training Curve" in top.caption
    assert bottom.caption.startswith("Figure 3 | Inference costs")
    assert "(a) Prefilling" in bottom.caption
    assert "(b) Decoding" in bottom.caption
    # 图号取自重配后的图注（阅读器卡片上的"图 N"徽章用它）
    from backend.parsing.structure.figure_cut import caption_number

    assert caption_number(top.caption) == "2"
    assert caption_number(bottom.caption) == "3"
    # 每组一个图注块（可翻译、可检索）
    for f in (top, bottom):
        cap = result.block_by_id(f.caption_block_id)
        assert cap is not None and cap.type == "caption"


def test_stacked_images_are_not_merged(tmp_path):
    """反向断言：**竖排**相邻的两块不合并（那更可能是上下两张不同的图）。

    这是刻意留的保守边界：两行面板的布局少见，而把上下两张图并成一张的代价更大。
    """
    items = [
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0,
         "bbox": [100, 100, 500, 300], "image_caption": ["Figure 1 | Top."]},
        {"type": "image", "img_path": "images/b.jpg", "page_idx": 0,
         "bbox": [100, 320, 500, 520], "image_caption": ["Figure 2 | Bottom."]},
    ]
    result, _dir, _b = build(tmp_path, items, pages=1)
    assert len(result.figures) == 2
    assert not any(f.audit.get("merged") for f in result.figures)


def test_side_by_side_but_misaligned_images_are_not_merged(tmp_path):
    """反向断言：左右并排但**顶底没对齐**的两块不合并（左右栏各一张不同图的情形）。"""
    items = [
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0,
         "bbox": [80, 100, 450, 400], "image_caption": ["Figure 1 | Left."]},
        {"type": "image", "img_path": "images/b.jpg", "page_idx": 0,
         "bbox": [520, 200, 900, 500], "image_caption": ["Figure 2 | Right."]},
    ]
    result, _dir, _b = build(tmp_path, items, pages=1)
    assert len(result.figures) == 2


def test_gap_too_large_is_not_merged(tmp_path):
    """间隙超过 `PANEL_GAP_PT` 就不并（跨栏的"并排"往往是两张图）。"""
    items = [
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0,
         "bbox": [60, 100, 420, 300], "image_caption": ["(a) Left panel"]},
        {"type": "image", "img_path": "images/b.jpg", "page_idx": 0,
         "bbox": [620, 100, 950, 300], "image_caption": ["(b) Right panel"]},
    ]
    result, _dir, _b = build(tmp_path, items, pages=1)
    assert len(result.figures) == 2


def test_caption_count_mismatch_keeps_mineru_captions(tmp_path):
    """图级图注条数与组数对不上时**不做重配**（宁可保留 MinerU 原样，也不瞎配）。"""
    items = [
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0,
         "bbox": [100, 100, 450, 300], "image_caption": ["(a) One"]},
        {"type": "image", "img_path": "images/b.jpg", "page_idx": 0,
         "bbox": [460, 100, 800, 300], "image_caption": ["(b) Two"]},
        {"type": "image", "img_path": "images/c.jpg", "page_idx": 0,
         "bbox": [100, 400, 450, 600], "image_caption": ["(a) Three"]},
        {"type": "image", "img_path": "images/d.jpg", "page_idx": 0,
         "bbox": [460, 400, 800, 600], "image_caption": ["(b) Four"]},
    ]
    result, _dir, _b = build(tmp_path, items, pages=1)
    assert len(result.figures) == 2                 # 版面照样合并
    assert result.figures[0].caption == "(a) One\n(b) Two"
    assert result.figures[1].caption == "(a) Three\n(b) Four"


def test_single_panel_figure_keeps_mineru_own_image(tmp_path):
    """反向断言：**单面板**的图照旧用 MinerU 自己裁好的图（不去 PDF 重裁）。"""
    result, _dir, _b = build(tmp_path, [
        {"type": "image", "img_path": "images/a.jpg", "page_idx": 0,
         "bbox": [100, 100, 500, 400], "image_caption": ["Figure 1 | Solo."]},
    ], pages=1, images={"images/a.jpg": b"\xff\xd8\xff\xe0x"})
    assert len(result.figures) == 1
    fig = result.figures[0]
    assert not fig.audit.get("merged")
    assert fig.image_path and Path(fig.image_path).stem == fig.block_id
    assert Path(fig.image_path).suffix == ".jpg"


def test_region_layer_still_sees_one_region_per_merged_figure(tmp_path):
    """侧栏/正文的一致性：合并后的"图数"就是 `figure_regions` 出的区域数（同一个口径）。"""
    from backend.services.paper import figure_regions, paper_service

    result, paper_dir, _b = build(tmp_path, DEEPSEEK_P5_PANELS, pages=1)
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(paper_service, "paper_paths",
                            lambda pid: {"ir": paper_dir / "ir.sqlite"})
        figure_regions.invalidate_cache()
        regions = figure_regions.all_regions("demo-paper", force=True)[1]
    finally:
        monkeypatch.undo()
    assert len(regions) == len(result.figures) == 2
    assert [r["source"] for r in regions] == ["online", "online"]

