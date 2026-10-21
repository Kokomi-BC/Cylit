"""索引自检的判定纯函数（**构造数据**，不连库、不 mock 网络）。

覆盖四组检查的每个判定分支，重点是"该报 error 的报 error、该只报 warn 的不要升级"：

1. 行覆盖：未覆盖的正文行、空 ``row_ids``、孤儿 ``row_id``；被 skip / 结构行**不算**正文行；
2. 锚点：``row-x`` 这类恒等假 id 必须是 error，解析不到的普通 block_id 只准 warn（并带形态分布），
   四种可解析写法（行 id / ``anchor_map`` 命中 / ``row-<行 id>`` / ``blk-<块 id>``）都不许误报；
3. 解读引用：markdown 里的短码缺失 → error；inventory 为空而 markdown 非空 → error；
4. 索引卫生：id 重复 → error；``order_global`` 并列、指针悬空 → 只 warn；
   ``row_ids`` 跨页只进统计（不产生发现）；
5. 全部健康时**零发现**（否则"有发现"这个信号本身就没有意义）。
"""

from __future__ import annotations

from scripts.index_audit import (
    _run,
    base_id,
    check_anchors,
    check_index_hygiene,
    check_row_coverage,
    check_understanding,
    check_unit_links,
    id_form_stats,
    id_prefix,
    row_span_stats,
)


# --------------------------------------------------------------------------
# 构造数据的小工厂
# --------------------------------------------------------------------------


def _row(rid: str, page: int = 1, *, type_: str = "para", skip: str = "",
         structural: bool = False) -> dict:
    """页级行（``chunking.row_units`` 的形状，只留判定用得到的字段）。"""
    return {"id": rid, "page": page, "type": type_,
            "skip_reason": skip, "structural": structural}


def _chunk(cid: str, *, chunk_type: str = "unit", block_id: str | None = None,
           order: int = 1, row_ids=(), **meta) -> dict:
    """chunk 记录（``_load_chunks`` 归一化后的形状）。"""
    metadata = {"row_ids": list(row_ids)}
    metadata.update(meta)
    return {"id": cid, "chunk_type": chunk_type,
            "block_id": cid if block_id is None else block_id,
            "order_global": order, "page_no": 1, "metadata": metadata}


def _anchors(row_ids=(), block_ids=(), block_row: dict | None = None) -> dict:
    """``_load_anchors`` 的返回形状（有 id 就算"在第 1 页上有这一行 / 这一块"）。"""
    return {"row_pages": {str(rid): 1 for rid in row_ids},
            "block_pages": {str(bid): 1 for bid in block_ids},
            "block_row": dict(block_row or {})}


def _levels(findings: list[dict]) -> list[str]:
    return [f["level"] for f in findings]


# --------------------------------------------------------------------------
# 1. 行覆盖
# --------------------------------------------------------------------------


def test_uncovered_body_row_is_error():
    rows = [_row("p001-00"), _row("p001-01")]
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00"])]
    findings = check_row_coverage(rows, chunks)
    assert _levels(findings) == ["error"]
    assert "p001-01" in findings[0]["detail"]


def test_skipped_and_structural_rows_are_not_body():
    """被 skip 的行、结构行（图/表/认证图注）不由 unit 承载 → 不许报"未覆盖"。"""
    rows = [_row("p001-00"),
            _row("p001-01", skip="ref"),
            _row("p001-02", type_="table", structural=True)]
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00"])]
    assert check_row_coverage(rows, chunks) == []


def test_orphan_row_id_is_error():
    rows = [_row("p001-00")]
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00", "p009-99"])]
    findings = check_row_coverage(rows, chunks)
    assert _levels(findings) == ["error"]
    assert "p009-99" in findings[0]["detail"]


def test_empty_unit_row_ids_is_error():
    rows = [_row("p001-00")]
    chunks = [_chunk("p:unit:u1", row_ids=[]),
              _chunk("p:unit:u2", row_ids=["p001-00"])]
    findings = check_row_coverage(rows, chunks)
    assert _levels(findings) == ["error"]
    assert "p:unit:u1" in findings[0]["detail"]


def test_no_unit_chunks_is_error():
    """没有 unit chunk 时行覆盖根本无从校验 —— 必须说出来，不能静默通过。"""
    findings = check_row_coverage([_row("p001-00")], [_chunk("p:text:1", chunk_type="text")])
    assert _levels(findings) == ["error"]
    assert "unit chunk" in findings[0]["detail"]


def test_unit_ids_pointing_to_missing_chunk_is_error():
    chunks = [
        _chunk("p:unit:u1", row_ids=["p001-00"]),
        _chunk("p:A:text:p001-00", chunk_type="text", order=0,
               row_ids=["p001-00"], unit_ids=["p:unit:u9"]),
    ]
    findings = check_unit_links(chunks)
    assert _levels(findings) == ["error"]
    assert "u9" in findings[0]["detail"]


def test_unit_ids_present_chunk_is_clean():
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00"], unit_ids=["p:unit:u1"])]
    assert check_unit_links(chunks) == []


# --------------------------------------------------------------------------
# 2. 锚点可解析
# --------------------------------------------------------------------------


def test_fake_row_id_is_error():
    """``row-x``：带 row- 前缀却指不到任何真实行 = 恒等假 id（公式条目缺 row_id 时写出来的）。"""
    chunks = [_chunk("p:formula:f1", chunk_type="formula", block_id="row-x",
                     row_ids=["p001-00"])]
    findings = check_anchors(chunks, _anchors(row_ids=["p001-00"]))
    assert _levels(findings) == ["error"]
    assert "row-x" in findings[0]["detail"]


def test_unresolvable_block_id_is_warn_with_form_stats():
    """解析不到的普通 block_id（版面模型给的伪块 id）只准 warn，且要给出形态分布。"""
    chunks = [_chunk("p:formula:f1", chunk_type="formula",
                     block_id="turbo-p03-x01", row_ids=[]),
              _chunk("p:formula:f2", chunk_type="formula",
                     block_id="turbo-p04-x02", row_ids=[])]
    findings = check_anchors(chunks, _anchors(row_ids=["p001-00"]))
    assert _levels(findings) == ["warn"]
    assert "形态分布" in findings[0]["detail"]
    assert "turbo-p" in findings[0]["detail"]


def test_anchor_accepts_all_resolvable_forms():
    chunks = [_chunk("a", block_id="p001-00"),          # 页级行 id
              _chunk("b", block_id="row-p001-00"),      # row-<行 id>
              _chunk("c", block_id="blk-p-b01"),        # blk-<块 id>
              _chunk("d", block_id="att-p01-b06")]      # anchor_map 命中
    anchors = _anchors(row_ids=["p001-00"], block_ids=["p-b01"],
                       block_row={"att-p01-b06": "p001-00"})
    assert check_anchors(chunks, anchors) == []


def test_anchor_warn_counts_row_id_fallback():
    """解析不到行的公式块若带 ``metadata.row_id``，要在同一条 warn 里报出来（是否是问题看它）。"""
    chunks = [_chunk("p:formula:f1", chunk_type="formula", block_id="turbo-p03-b09",
                     row_ids=[], row_id="p003-04"),
              _chunk("p:formula:f2", chunk_type="formula", block_id="turbo-p05-x01",
                     row_ids=[], row_id=None)]
    findings = check_anchors(chunks, _anchors(row_ids=["p003-04"]))
    assert _levels(findings) == ["warn"]
    assert "其中 1 条有 metadata.row_id 兜底" in findings[0]["detail"]


def test_blk_prefix_to_unknown_block_is_warn_not_error():
    chunks = [_chunk("a", block_id="blk-p-b99")]
    findings = check_anchors(chunks, _anchors(row_ids=["p001-00"], block_ids=["p-b01"]))
    assert _levels(findings) == ["warn"]


def test_missing_block_id_is_warn():
    chunks = [{"id": "a", "chunk_type": "unit", "block_id": "",
               "order_global": 1, "metadata": {"row_ids": ["p001-00"]}}]
    findings = check_anchors(chunks, _anchors(row_ids=["p001-00"]))
    assert _levels(findings) == ["warn"]


def test_id_prefix_forms():
    assert id_prefix("p001-06") == "p"
    assert id_prefix("attentionisa-p01-b06") == "attentionisa-p"
    assert id_prefix("row-x") == "row-x"
    assert id_prefix("") == "∅"


def test_id_form_stats_counts_forms():
    chunks = [_chunk("a", block_id="p001-00"), _chunk("b", block_id="p001-01"),
              _chunk("c", chunk_type="formula", block_id="turbo-p03-x01")]
    stats = id_form_stats(chunks, _anchors(row_ids=["p001-00", "p001-01"]))
    assert stats["total"] == 3 and stats["resolved"] == 2
    assert stats["forms"]["p"] == 2


# --------------------------------------------------------------------------
# 3. 解读引用的完整性
# --------------------------------------------------------------------------


def test_missing_short_code_is_error():
    markdown = "方法链条：先 [A003]，再 [A007]。"
    refs = {"resolved": {"A003": "p:unit:u1"}}
    inventory = [{"code": "A003", "chunk_id": "p:unit:u1"}]
    findings = check_understanding(markdown, refs, inventory)
    assert _levels(findings) == ["error"]
    assert "A007" in findings[0]["detail"]


def test_inventory_empty_but_markdown_not_is_error():
    findings = check_understanding("正文 [A001]", {"resolved": {}}, [])
    assert set(_levels(findings)) == {"error"}
    assert any("inventory 为空" in f["detail"] for f in findings)


def test_all_codes_known_is_clean():
    markdown = "看 [A001] 与 [A002]。"
    refs = {"resolved": {"A002": "p:unit:u2"}}
    inventory = [{"code": "A001", "chunk_id": "p:unit:u1"},
                 {"code": "A002", "chunk_id": "p:unit:u2"}]
    assert check_understanding(markdown, refs, inventory) == []


# --------------------------------------------------------------------------
# 4. 索引卫生 / 跨页统计
# --------------------------------------------------------------------------


def test_order_global_tie_is_only_warn():
    chunks = [_chunk("a", row_ids=["p001-00"], order=7),
              _chunk("b", chunk_type="text", row_ids=["p001-00"], order=7)]
    findings = check_index_hygiene(chunks)
    assert _levels(findings) == ["warn"]
    assert "order_global" in findings[0]["detail"]


def test_duplicate_chunk_id_is_error():
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00"], order=1),
              _chunk("p:unit:u1", row_ids=["p001-01"], order=2)]
    findings = check_index_hygiene(chunks)
    assert _levels(findings) == ["error"]
    assert "p:unit:u1" in findings[0]["detail"]


def test_dangling_unit_pointer_is_warn():
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00"],
                     prev_unit_id="p:unit:zz", next_unit_id=None,
                     parent_chunk_id="p:A:text:p001-00")]
    findings = check_index_hygiene(chunks)
    assert _levels(findings) == ["warn", "warn"]   # 每个字段一条，便于分开定位
    details = " ".join(f["detail"] for f in findings)
    assert "prev_unit_id" in details and "parent_chunk_id" in details
    assert "next_unit_id" not in details


def test_lang_suffix_is_stripped_before_comparing_ids():
    """库里的 chunk id 带 ``:en``/``:zh``/``:any``，元数据指针**不带** ——
    两边先换算再比；否则每条指针都被报成"指向不存在的 chunk"（全是假警）。"""
    chunks = [
        _chunk("p:text:p001-00:en", chunk_type="text", block_id="p001-00", order=0,
               row_ids=["p001-00"], unit_ids=["p:unit:u1"]),
        _chunk("p:unit:u1:en", block_id="p001-00", order=1, row_ids=["p001-00"],
               unit_ids=["p:unit:u1"], prev_unit_id=None, next_unit_id=None,
               parent_chunk_id="p:text:p001-00"),
        _chunk("p:unit:u1:zh", block_id="p001-00", order=2, row_ids=["p001-00"],
               unit_ids=["p:unit:u1"], prev_unit_id=None, next_unit_id=None,
               parent_chunk_id="p:text:p001-00"),
    ]
    assert check_unit_links(chunks) == []
    assert check_index_hygiene(chunks) == []
    assert base_id("p:B:unit:p003-04:zh") == "p:B:unit:p003-04"


def test_row_span_cross_page_is_stats_only():
    rows = [_row("p001-00", 1), _row("p002-00", 2)]
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00", "p002-00"])]
    assert check_row_coverage(rows, chunks) == []
    stats = row_span_stats(chunks, rows)
    assert stats["cross_page"] == 1
    assert "p1→p2" in stats["examples"][0]


def test_check_crash_is_recorded_without_raising():
    """一条检查炸了只记 ``check crashed``，后续检查照常跑（整篇审计不中断）。"""
    findings: list[dict] = []
    _run(findings, "row_coverage", check_row_coverage, "不是列表", [])
    assert _levels(findings) == ["error"]
    assert "check crashed" in findings[0]["detail"]
    _run(findings, "index_hygiene", check_index_hygiene, [])
    assert len(findings) == 1


# --------------------------------------------------------------------------
# 全部健康 → 零发现
# --------------------------------------------------------------------------


def test_healthy_index_has_no_findings():
    rows = [_row("p001-00"), _row("p001-01"),
            _row("p001-02", type_="table", structural=True)]
    chunks = [
        _chunk("p:A:text:p001-00", chunk_type="text", block_id="p001-00", order=0,
               row_ids=["p001-00", "p001-01"], unit_ids=["p:unit:u1"]),
        _chunk("p:unit:u1", block_id="p001-00", order=1,
               row_ids=["p001-00", "p001-01"], unit_ids=["p:unit:u1"],
               prev_unit_id=None, next_unit_id=None,
               parent_chunk_id="p:A:text:p001-00"),
    ]
    anchors = _anchors(row_ids=["p001-00", "p001-01", "p001-02"],
                       block_ids=["att-p01-b01"])
    findings = (check_row_coverage(rows, chunks)
                + check_unit_links(chunks)
                + check_anchors(chunks, anchors)
                + check_index_hygiene(chunks)
                + check_understanding("健康 [A001]",
                                      {"resolved": {"A001": "p:unit:u1"}},
                                      [{"code": "A001", "chunk_id": "p:unit:u1"}]))
    assert findings == []
    assert row_span_stats(chunks, rows)["cross_page"] == 0
