"""插图区域收紧逻辑的单元测试（纯函数，不依赖数据库）。

真实故障（TurboQuant p8）：解析阶段给出的 Figure 3 bbox 从 y=95.8 一直到 y=535.0，
把上面的 Figure 2 与中间的 ``2.2 NEEDLE-IN-A-HAYSTACK`` 标题一起框了进来；
前端因此出现"同一张图显示两次、图里夹着正文"。这里锁住修正后的边界规则。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.paper import figure_regions as fr
from backend.services.paper.figure_regions import (
    SEP_PAD, _number_of, _parts_of, _snap_edges, _tighten, inject_images,
)
from backend.parsing.extract.render import ink_bbox
from backend.parsing.models import Rect


PAGE = Rect(0, 0, 612, 792)


def test_tighten_figure_ignores_heading_inside_the_ink_band():
    """图注在下：解析层给的 raw bbox 已经是"墨迹带"，带内的标题/图注**不算分隔**。

    实测 9984-boomerang p23 图 9：墨迹带 78.9~231.3 里的 `19 (Every 2nd layer)`（y 172~180）
    其实是图内标注，旧口径拿它当上边界 → 图体被收到 185.9，裁出来只剩坐标轴标签 + 图例。
    """
    bbox = [112.84, 78.90, 499.15, 231.30]          # 解析层按图注分段给出的墨迹带
    caption = {"y0": 238.2, "y1": 292.1}            # Figure 9 的图注
    structs = [{"y0": 172.1, "y1": 179.9}]          # 图内那行标注（被判成 heading）
    _x0, y0, _x1, y1 = _tighten(bbox, caption, structs, 792.0, below=True)
    assert y0 == pytest.approx(78.90)               # 不再被图内标注顶下来
    assert y1 == pytest.approx(238.2)


def test_tighten_figure_never_raises_the_top_by_a_heading_above_the_band():
    """图的上边界**只认解析层给的墨迹带**：带外的标题不再参与（原来会把顶边往下压）。"""
    bbox = [112.84, 320.00, 499.15, 535.01]
    caption = {"y0": 541.6, "y1": 584.9}
    structs = [{"y0": 294.7, "y1": 304.6}]          # `2.2 NEEDLE-IN-A-HAYSTACK`（在带之上）
    _x0, y0, _x1, y1 = _tighten(bbox, caption, structs, 842.0, below=True)
    assert y0 == pytest.approx(320.00)              # 上边界保持图体自己的上沿
    assert y1 == pytest.approx(541.6)               # 下边界仍然收到图注顶边


def test_tighten_figure_without_separator_keeps_bbox_top():
    """图注上方没有标题/图注时（页首的图）：顶边保持 bbox，底边收到图注顶边。"""
    bbox = [117.57, 95.80, 494.42, 220.63]
    caption = {"y0": 231.0, "y1": 241.0}
    x0, y0, x1, y1 = _tighten(bbox, caption, [], 842.0, below=True)
    assert (x0, y0, x1) == (117.57, 95.80, 494.42)
    assert y1 == pytest.approx(231.0)


def test_tighten_table_uses_caption_below():
    """题注在表上方：上边界 = 题注底边，下边界 = 表块自身底边与下方标题顶边取小。"""
    bbox = [104.0, 228.71, 508.0, 700.0]
    caption = {"y0": 228.71, "y1": 246.67}
    structs = [{"y0": 720.0, "y1": 730.0}]
    _x0, y0, _x1, y1 = _tighten(bbox, caption, structs, 842.0, below=False)
    assert y0 == pytest.approx(246.67)
    assert y1 == pytest.approx(700.0)      # 表块底边比下一个标题更紧，取表块底边


def test_tighten_table_clipped_by_next_heading():
    """表块 bbox 过长、下方紧接着标题时，下边界收到标题顶边（让开 SEP_PAD）。"""
    bbox = [104.0, 228.71, 508.0, 800.0]
    caption = {"y0": 228.71, "y1": 246.67}
    structs = [{"y0": 600.0, "y1": 610.0}]
    _x0, _y0, _x1, y1 = _tighten(bbox, caption, structs, 842.0, below=False)
    assert y1 == pytest.approx(600.0 - SEP_PAD)


def test_tighten_bails_out_when_band_too_small():
    """收紧后高度过小（会裁空）时退回原始 bbox。"""
    bbox = [104.0, 300.0, 508.0, 320.0]
    caption = {"y0": 302.0, "y1": 310.0}
    structs = [{"y0": 298.0, "y1": 300.0}]
    assert _tighten(bbox, caption, structs, 842.0, below=True) == bbox


def test_number_of_reads_figure_and_table_numbers():
    assert _number_of("Figure 3: Evaluation of Llama") == "3"
    assert _number_of("**Table 12**: results") == "12"
    assert _number_of("图 5：方差") == "5"
    assert _number_of("no number here") == ""


# --------------------------------------------------------------------------
# 贴边：边界不能"离文字太近"（多截）也不能"离文字太远留白缝"（少截）
# 真实故障 1（attention p4）：图块 bbox 的 y0=79.7，图题文字块是 [71.2, 81.2]，
#   出图余量把图题切掉半行 —— 图顶上多一条"被切一半的字"。
# 真实故障 2（TurboQuant p8）：图 2 的 bbox 底边 229.8，图注顶边 231.0，
#   只差 1.2pt，裁出来图注顶部被切掉一条。
# --------------------------------------------------------------------------


def test_snap_edges_pushes_top_below_intruding_title():
    """边界切进**图注/题注块**里 → 推到它下方（题注是论文文字，不该被裁进图里）。

    ⚠️ 必须把该块放进 `avoid`：`_snap_edges` 对"图注块"与"图内标注块"是**两种处置**
    （2026-09-27 实测分出来的）：图注块推开，图内小标注（子图标题/刻度）反而要**收进来**，
    否则就会出现用户报的"少裁"（turboquant p8 图 2 的三个子图标题被切掉 7.8pt，
    该 x 切片 99.6% 的墨迹没进裁图）。见 `test_snap_edges_includes_in_figure_label_when_cut`。
    """
    title = Rect(147.78, 71.2, 450.2, 81.16)
    out = _snap_edges([171.96, 81.0, 470.01, 273.19], [title], PAGE, avoid=[title])
    assert out[1] == pytest.approx(81.16 + 1.2)
    assert (out[0], out[2], out[3]) == (171.96, 470.01, 273.19)


def test_snap_edges_includes_in_figure_label_when_cut():
    """**图内**的单行标注被边界切到时，边界要往外让开、把整行收进来（少裁修复）。"""
    label = Rect(147.78, 71.2, 450.2, 81.16)          # 图内子图标题行（不在 avoid 里）
    out = _snap_edges([171.96, 79.69, 470.01, 273.19], [label], PAGE)
    assert out[1] == pytest.approx(71.2 - 1.2)        # 上边界往上让开，整行进图
    assert out[0] == pytest.approx(147.78 - 1.2)      # 左边界也被这行字穿过 → 同样让开
    assert (out[2], out[3]) == (470.01, 273.19)


def test_snap_edges_keeps_top_when_gap_is_safe():
    """边界在**图注块**下方、且已经贴到安全距离之外时不动它 —— 免得把图体上沿裁掉。"""
    title = Rect(147.78, 71.2, 450.2, 81.16)          # 文字块底边 81.16
    rect = [171.96, 79.69, 470.01, 273.19]            # 79.69 已经比 81.16+TEXT_PAD 低
    assert _snap_edges(rect, [title], PAGE, avoid=[title]) == rect


def test_snap_edges_pushes_bottom_above_caption():
    """边界离下方图注不足 TEXT_PAD → 推到图注上方（图注顶部不再被切）。"""
    caption = Rect(108.0, 231.0, 504.0, 240.96)
    out = _snap_edges([117.57, 95.8, 494.42, 229.8], [caption], PAGE)
    assert out[3] == pytest.approx(231.0 - 1.2)


def test_snap_edges_ignores_distant_text():
    """远处正文不该动边界（否则图体 bbox 会被平白推掉一大截）。"""
    body_para = Rect(108.0, 465.09, 505.66, 540.74)
    rect = [193.56, 69.0, 418.44, 404.75]
    assert _snap_edges(rect, [body_para], PAGE) == rect


def test_snap_edges_bails_out_when_region_would_collapse():
    """贴到文字边上会把区域收到最小高度以下 → 保留原边界（宁可多裁一点也不要裁空）。"""
    caption = Rect(108.0, 60.0, 504.0, 100.5)         # 边界正好切进图注里
    rect = [104.0, 40.0, 508.0, 100.5]
    assert _snap_edges(rect, [caption], PAGE) == rect  # 收到 98.8 后只剩 58.8 高 → 放弃贴边


# --------------------------------------------------------------------------
# 一张图截成两份：区域内部夹着别的图注（TurboQuant p8 的 Figure 3 框住了 Figure 2 的图注）
# --------------------------------------------------------------------------


def test_parts_of_splits_at_inner_caption():
    inner = [{"id": "b02", "type": "caption", "y0": 231.0, "y1": 240.96}]
    pieces = _parts_of([112.84, 95.8, 499.15, 535.01], inner, PAGE)
    assert len(pieces) == 2
    assert pieces[0][0][3] == pytest.approx(231.0 - 1.2)      # 上段止于图注上方
    assert pieces[1][0][1] == pytest.approx(240.96 + 1.2)     # 下段起于图注下方
    assert pieces[0][1] is None and pieces[1][1] is None


def test_parts_of_keeps_single_piece_without_inner_caption():
    pieces = _parts_of([117.57, 95.8, 494.42, 229.8],
                       [{"id": "x", "type": "para", "y0": 231.0, "y1": 241.0}], PAGE)
    assert pieces == [([117.57, 95.8, 494.42, 229.8], None)]


def test_parts_of_marks_tail_touching_page_footer():
    """图体画到页脚 → 最后一段标记 continues（跨页续图由调用方补下一页的条目）。"""
    pieces = _parts_of([104.0, 100.0, 508.0, 790.0], [], PAGE)
    assert len(pieces) == 1 and pieces[0][1] == {"continues": True}


# --------------------------------------------------------------------------
# 墨迹裁白
# --------------------------------------------------------------------------


def test_ink_bbox_trims_white_margin():
    """按 bbox 裁出来四周的白边要被量掉（TurboQuant p8 右侧近 40% 是空白）。"""
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(50, 60, 150, 120))
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0))
    shape.commit()
    got = ink_bbox(doc, 1, Rect(0, 0, 200, 200), pad=0.0)
    assert got.x0 == pytest.approx(50, abs=1.5)
    assert got.y0 == pytest.approx(60, abs=1.5)
    assert got.x1 == pytest.approx(150, abs=1.5)
    assert got.y1 == pytest.approx(120, abs=1.5)
    doc.close()


def test_ink_bbox_returns_input_for_blank_region():
    import pymupdf as fitz

    doc = fitz.open()
    doc.new_page(width=200, height=200)
    rect = Rect(10, 10, 100, 100)
    assert ink_bbox(doc, 1, rect, pad=0.0) == rect
    doc.close()


# --------------------------------------------------------------------------
# Markdown 插图注入（页级产物的 Markdown 本体里原本没有任何图片语法）
# --------------------------------------------------------------------------


def test_inject_images_puts_ref_above_caption():
    md = "Body paragraph.\n\n**Figure 3: Evaluation**\n\nMore text."
    out = inject_images(md, {"3": "![Figure 3](/api/x.png)"})
    assert out.index("![Figure 3](/api/x.png)") < out.index("**Figure 3: Evaluation**")
    assert "Body paragraph." in out and "More text." in out


def test_inject_images_appends_unmatched_at_tail():
    md = "**Figure 1: cap**\n\ntext"
    out = inject_images(md, {"1": "![Figure 1](a.png)", "9": "![Figure 9](b.png)"})
    assert out.startswith("![Figure 1](a.png)")
    assert out.rstrip().endswith("![Figure 9](b.png)")


def test_inject_images_respects_existing_image_syntax():
    md = "![模型自己给的图](a.png)\n\n**Figure 2: cap**"
    assert inject_images(md, {"2": "![Figure 2](b.png)"}) == md


def test_inject_images_noop_without_refs():
    assert inject_images("text", {}) == "text"
    assert inject_images("", {"1": "![Figure 1](a.png)"}) == ""


# --------------------------------------------------------------------------
# 两个导入开关：图片是否包含图注 / 深度图片搜索（解析时固化，服务层读它出图）
# --------------------------------------------------------------------------


def _write_ir(path: Path, stats: dict | None) -> None:
    """造一个最小 IR：只需要 `paper` 表 + `stats_json`（`IRReader.paper()` 就查它）。"""
    import json
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        # 同一个测试里可能改写两次（开/关各一次），所以建表/写行都要幂等
        conn.execute("CREATE TABLE IF NOT EXISTS paper "
                     "(paper_id TEXT PRIMARY KEY, stats_json TEXT)")
        conn.execute("DELETE FROM paper")
        conn.execute("INSERT INTO paper VALUES ('p1', ?)",
                     (json.dumps(stats) if stats is not None else None,))
        conn.commit()
    finally:
        conn.close()


def _fake_paths(monkeypatch, tmp_path: Path, stats: dict | None) -> Path:
    from backend.services.paper import paper_service

    ir = tmp_path / "ir.sqlite"
    _write_ir(ir, stats)
    monkeypatch.setattr(paper_service, "paper_paths", lambda pid: {"ir": ir})
    fr.invalidate_cache()
    return ir


def test_parse_options_default_to_off(monkeypatch, tmp_path):
    """读不到（从未解析过 / 旧 IR 没这个字段）一律全关 —— 默认口径就是关。"""
    from backend.services.paper import paper_service

    monkeypatch.setattr(paper_service, "paper_paths",
                        lambda pid: {"ir": tmp_path / "missing.sqlite"})
    fr.invalidate_cache()
    assert fr.parse_options("p1") == {"figure_caption": False, "deep_figure_search": False}
    assert fr.caption_in_crop("p1") is False
    _fake_paths(monkeypatch, tmp_path, {"venue": "NeurIPS"})       # IR 在、但没有这个字段
    assert fr.caption_in_crop("p1") is False


def test_parse_options_read_the_frozen_values(monkeypatch, tmp_path):
    """导入时选的开关随解析产物固化（IR `paper.stats_json` → `parse_options`），服务层照它出图。"""
    _fake_paths(monkeypatch, tmp_path,
                {"parse_options": {"figure_caption": True, "deep_figure_search": True}})
    assert fr.parse_options("p1") == {"figure_caption": True, "deep_figure_search": True}
    assert fr.caption_in_crop("p1") is True
    # 非布尔值按布尔语义收（任务参数从 HTTP 表单来，可能是 "true"/1）
    _fake_paths(monkeypatch, tmp_path, {"parse_options": {"figure_caption": 1}})
    assert fr.caption_in_crop("p1") is True


def test_region_version_carries_the_caption_switch(monkeypatch, tmp_path):
    """URL 版本串必须带图注开关：开关不改 bbox，光靠区域哈希挡不住浏览器里 7 天的旧卡片。"""
    region = {"hash": "ab12cd34"}
    _fake_paths(monkeypatch, tmp_path, {"parse_options": {"figure_caption": False}})
    off = fr.region_version(region, "p1")
    _fake_paths(monkeypatch, tmp_path, {"parse_options": {"figure_caption": True}})
    on = fr.region_version(region, "p1")
    assert off == "ab12cd34c0" and on == "ab12cd34c1" and off != on


def test_card_crop_falls_back_to_the_plain_crop(monkeypatch, tmp_path):
    """「图片是否包含图注」关掉时 `card=1` 也走纯图 —— 正文与"点开看大图"必须是同一张。"""
    region = {"bbox": [10.0, 10.0, 100.0, 100.0], "hash": "ab12cd34",
              "kind": "figure", "number": "1", "part": 0}
    monkeypatch.setattr(fr, "_resolve_part", lambda *a, **k: (region, 0))
    asked: list[bool] = []

    def fake_path(pid, page, index, part=0, *, card=False):
        asked.append(card)
        p = tmp_path / f"region-card{int(card)}.png"
        p.write_bytes(b"png")                       # 已存在 → 提前返回，不需要真 PDF
        return p

    monkeypatch.setattr(fr, "region_crop_path", fake_path)
    monkeypatch.setattr(fr, "caption_in_crop", lambda pid: False)
    out = fr.ensure_region_crop("p1", 8, 0, card=True)
    assert asked == [False] and out.name == "region-card0.png"

    asked.clear()
    monkeypatch.setattr(fr, "caption_in_crop", lambda pid: True)
    out = fr.ensure_region_crop("p1", 8, 0, card=True)
    assert asked == [True] and out.name == "region-card1.png"


def test_snap_edges_does_not_walk_into_the_next_column():
    """贴边不能**逐块累积**地走进相邻那一栏（KAT p3 图 3：右边界 286.7 → 558.8）。

    实测事故（2026-09-28）：区域是左栏的句子树 [39.7, 325.5, 286.7, 406.5]，右栏正文被解析器
    并成了两块（x 306.6~452.9 与 452.9~557.6）。`_snap_x` 的"把被切的块整个收进来"先是避开
    了 `GROW_MAX_RATIO`（每步外扩 ≤ 区域宽的 40%），但**两块各自都在上限内** → 边界先被收到
    454、再被收到 558.8，横跨整页；裁图于是混进一栏正文（用户："图 3 裁错了"）。
    新增的 `SNAP_INSIDE_MAX`（露在外面的部分 ≤ 块自身的 25%）把这一支挡住。
    """
    tight = [39.68, 325.54, 286.68, 406.54]
    texts = [
        Rect(306.6, 328.0, 557.7, 336.1),     # 右栏正文（整栏宽）
        Rect(306.6, 348.9, 452.9, 357.0),     # 右栏正文，被并成两块的第一块
        Rect(452.9, 348.9, 557.6, 357.0),     # 第二块（紧接着第一块）
        Rect(239.8, 392.9, 341.0, 403.0),     # 跨栏合并的标题行（"…4. Model"）
    ]
    out = _snap_edges(tight, texts, PAGE)
    assert out[2] <= 300.0                    # 没有走进右栏
    assert out[0] == pytest.approx(39.68, abs=0.5)
    assert out[1] == pytest.approx(325.54, abs=0.5)
    assert out[3] == pytest.approx(406.54, abs=0.5)

    # 反向：**只露一点点**的真标签照旧被收进来（attention p4 的 `Scaled …` 那种情形）
    tile = [100.0, 300.0, 286.0, 400.0]
    label = Rect(200.0, 330.0, 292.0, 340.0)   # 92pt 宽，只有 6pt 露在区域外
    out2 = _snap_edges(tile, [label], PAGE)
    assert out2[2] >= 293.0                    # 被收进来了（292 + TEXT_PAD）
