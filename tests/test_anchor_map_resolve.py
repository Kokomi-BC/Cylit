"""锚点 id 的唯一定义：``normalize_block_id`` / ``resolve_anchor``（纯函数、不依赖数据库）。

背景（真 bug）：五套锚点前缀各写一套规则 —— ``row-<行 id>``（公式/表格 chunk）、
``blk-<块 id>``（解析器 markdown 锚点）、``fig-p010-01`` / ``fig:p10:n1``（图）、
``md:p3`` / ``pdf:p3``（视图页）。后端 ``_anchor_page`` 当时只认 md/pdf 两种，于是
``row-`` 那类锚点的 ``page`` 恒为 ``None``（批注/高亮"跳到那一页"永远不发生）。
这里钉住归一化后的**唯一**口径。
"""

from __future__ import annotations

from backend.services.paper.anchor_map import normalize_block_id, resolve_anchor

#: 正文行 id → 页（`reader_service.build_reader` 里的 row_pages 形状）
ROW_PAGES = {"p006-03": 6, "p001-00": 1}
#: 解析块 id → 页（block_pages 形状）
BLOCK_PAGES = {"turbo-p02-b07": 2, "blk-turbo-p02-b07": 2}


def test_normalize_block_id_strips_pseudo_prefixes() -> None:
    assert normalize_block_id("row-p006-03") == "p006-03"
    assert normalize_block_id("blk-turbo-p02-b07") == "turbo-p02-b07"
    assert normalize_block_id("p006-03") == "p006-03"
    assert normalize_block_id("  row-p006-03  ") == "p006-03"


def test_normalize_block_id_is_idempotent() -> None:
    for raw in ("row-p006-03", "blk-X", "p001-00", "row-blk-X", "", None):
        once = normalize_block_id(raw)
        assert normalize_block_id(once) == once


def test_normalize_block_id_none_is_empty() -> None:
    assert normalize_block_id(None) == ""
    assert normalize_block_id("") == ""


def test_resolve_md_and_pdf_page_anchors() -> None:
    md = resolve_anchor("md:p3")
    assert md["kind"] == "md-page" and md["page"] == 3 and md["row_id"] == ""
    pdf = resolve_anchor("pdf:p12", row_pages=ROW_PAGES, block_pages=BLOCK_PAGES)
    assert pdf["kind"] == "pdf-page" and pdf["page"] == 12


def test_resolve_row_anchor_takes_page_from_row_table() -> None:
    out = resolve_anchor("row-p006-03", row_pages=ROW_PAGES, block_pages=BLOCK_PAGES)
    assert out == {"kind": "row", "row_id": "p006-03", "page": 6}


def test_resolve_row_anchor_without_page_is_none_not_guessed() -> None:
    """行锚点查不到页时是 None（"跳到哪一页"由前端退化为按页浏览，不能猜成第 1 页）。"""
    out = resolve_anchor("row-p999-01", row_pages=ROW_PAGES)
    assert out == {"kind": "row", "row_id": "p999-01", "page": None}


def test_resolve_figure_anchor_reads_page_from_id() -> None:
    dashed = resolve_anchor("fig-p010-01")
    assert dashed["kind"] == "figure" and dashed["page"] == 10
    colon = resolve_anchor("fig:p10:n1")
    assert colon["kind"] == "figure" and colon["page"] == 10


def test_resolve_figure_prefers_figure_table_over_id_segment() -> None:
    out = resolve_anchor("fig-p010-01", fig_pages={"fig-p010-01": 11})
    assert out == {"kind": "figure", "row_id": "", "page": 11}


def test_resolve_blk_anchor_hits_block_table() -> None:
    out = resolve_anchor("blk-turbo-p02-b07", block_pages=BLOCK_PAGES)
    assert out == {"kind": "block", "row_id": "turbo-p02-b07", "page": 2}


def test_resolve_plain_ids_hit_row_then_block_tables() -> None:
    """归一化后先查行表、再查块表：命中谁就定谁的 kind（行优先，行才是定位单位）。"""
    assert resolve_anchor("p001-00", row_pages=ROW_PAGES)["kind"] == "row"
    assert resolve_anchor("turbo-p02-b07", block_pages=BLOCK_PAGES)["kind"] == "block"


def test_resolve_unknown_when_nothing_matches() -> None:
    out = resolve_anchor("nothing-like-this", row_pages=ROW_PAGES, block_pages=BLOCK_PAGES)
    assert out == {"kind": "unknown", "row_id": "nothing-like-this", "page": None}


def test_resolve_empty_and_none_are_unknown() -> None:
    blank = {"kind": "unknown", "row_id": "", "page": None}
    assert resolve_anchor(None) == blank
    assert resolve_anchor("") == blank
    assert resolve_anchor("   ") == blank


def test_resolve_never_raises_on_missing_tables() -> None:
    """不传任何查表：md/pdf/fig 仍能解析（页码在 id 里），其余退化为 unknown。"""
    assert resolve_anchor("md:p1")["page"] == 1
    assert resolve_anchor("row-p001-00")["page"] is None
    assert resolve_anchor("p001-00")["kind"] == "unknown"

