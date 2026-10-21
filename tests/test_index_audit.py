"""索引自检的判定纯函数（**构造数据**，不连库、不 mock 网络）。

覆盖五组检查的每个判定分支，重点是"该报 error 的报 error、该只报 warn 的不要升级"：

1. 行覆盖：未覆盖的正文行、空 ``row_ids``、孤儿 ``row_id``；被 skip / 结构行**不算**正文行；
2. 锚点：``row-x`` 这类恒等假 id 必须是 error，解析不到的普通 block_id 只准 warn（并带形态分布），
   四种可解析写法（行 id / ``anchor_map`` 命中 / ``row-<行 id>`` / ``blk-<块 id>``）都不许误报；
3. 解读引用：markdown 里的短码缺失 → error；inventory 为空而 markdown 非空 → error；
4. 索引卫生：id 重复 → error；``order_global`` 并列、指针悬空 → 只 warn；
   ``row_ids`` 跨页只进统计（不产生发现）；
5. 行身份（pid）覆盖率：缺 pid、pid 1:1 冲突（跨页 / 同页）、孤儿 pid、retired 复用
   → error；注册表为空（还没生成）、注册表里有身份却没活行 → 只 warn；
6. 全部健康时**零发现**（否则"有发现"这个信号本身就没有意义）。
"""

from __future__ import annotations

from scripts.index_audit import (
    _row_page,
    _run,
    base_id,
    check_anchors,
    check_chunk_pids,
    check_index_hygiene,
    check_row_coverage,
    check_row_pids,
    check_understanding,
    check_unit_links,
    flatten_page_rows,
    id_form_stats,
    pid_stats,
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
# 5. 行身份（pid）覆盖率
# --------------------------------------------------------------------------


def _pid_row(pid: str | None, page: int = 1, rid: str | None = None) -> dict:
    """阅读器读模型里的行（``reader_service.page_products`` 的 ``rows[]`` 形状 + 所在页）。"""
    row = {"id": rid or f"p{page:03d}-00", "page": page,
           "type": "para", "original": "x", "translation": "x"}
    if pid is not None:
        row["pid"] = pid
    return row


def _reg(rows=(), retired=()) -> dict:
    """``row_identity.load`` 的返回形状（``rows`` / ``retired``）。"""
    return {"version": 1, "rows": [dict(e) for e in rows],
            "retired": [dict(e) for e in retired]}


def _entry(pid: str, page: int = 1, order: int = 0) -> dict:
    """注册表里一条活身份。"""
    return {"pid": pid, "page": page, "order": order, "fp": "fp", "created_at": "t"}


def _errors(findings: list[dict]) -> str:
    return " ".join(f["detail"] for f in findings if f["level"] == "error")


def test_body_row_without_pid_is_error():
    """正文行没有 pid → error，并附**前 5 个**行 id（多了只给数量，不刷屏）。"""
    rows = [_pid_row(None, 1, f"p001-0{i}") for i in range(7)]
    findings = check_row_pids(rows, _reg())
    assert _levels(findings)[0] == "error"
    assert "7/7 个正文行没有 pid" in findings[0]["detail"]
    assert "p001-04" in findings[0]["detail"]
    assert "p001-05" not in findings[0]["detail"]


def test_same_pid_on_two_pages_is_error():
    """同一个 pid 落在两页上 = 身份被重绑（跨页 1:1 是硬要求）。"""
    rows = [_pid_row("r-aaaa", 1), _pid_row("r-bbbb", 2),
            _pid_row("r-aaaa", 2, "p002-01")]
    reg = _reg([_entry("r-aaaa", 1, 0), _entry("r-bbbb", 2, 0)])
    findings = check_row_pids(rows, reg)
    assert "error" in _levels(findings)
    assert "不是 1:1" in _errors(findings) and "跨页 1 个" in _errors(findings)


def test_same_pid_twice_in_one_page_is_error():
    """同一页里两行共用一个 pid 同样不是 1:1（只是不跨页）。"""
    rows = [_pid_row("r-aaaa", 1), _pid_row("r-aaaa", 1, "p001-01")]
    findings = check_row_pids(rows, _reg([_entry("r-aaaa", 1, 0)]))
    assert "error" in _levels(findings)
    assert "不是 1:1" in _errors(findings) and "跨页 0 个" in _errors(findings)


def test_orphan_pid_is_error():
    """活行用了注册表里根本没有的身份 → 孤儿（引用它的人必然跳错）。"""
    rows = [_pid_row("r-zzzz", 1)]
    findings = check_row_pids(rows, _reg([_entry("r-aaaa", 1, 0)]))
    assert any(f["level"] == "error" and "孤儿" in f["detail"] for f in findings)
    assert "r-zzzz" in _errors(findings)


def test_retired_pid_reused_as_live_row_is_error():
    """retired 里的 pid 又出现在活行上 = 最严重的错，且**不许**再按孤儿重复计一遍。"""
    rows = [_pid_row("r-old", 1)]
    reg = _reg([_entry("r-new", 1, 0)],
               [{"pid": "r-old", "page": 1, "fp": "fp", "retired_at": "t",
                 "reason": "text-changed"}])
    findings = check_row_pids(rows, reg)
    assert "已退休" in _errors(findings) and "重绑" in _errors(findings)
    assert "孤儿" not in _errors(findings)


def test_registry_duplicate_pid_and_order_collision_are_error():
    """注册表自身也要自洽：同一 pid 重复登记、同一页同一个 order 被两条身份抢 → error。"""
    rows = [_pid_row("r-a", 1, "p001-00"), _pid_row("r-b", 1, "p001-01")]
    reg = _reg([_entry("r-a", 1, 0), _entry("r-a", 1, 0), _entry("r-b", 1, 0)])
    findings = check_row_pids(rows, reg)
    assert "重复登记" in _errors(findings)
    assert "槽位被重复登记" in _errors(findings)


def test_missing_registry_is_warn_not_error():
    """注册表为空（还没生成，第一次读 page_products 会建）→ 只 warn，绝不把每个 pid 报成孤儿。"""
    findings = check_row_pids([_pid_row("r-aaaa", 1)], _reg())
    assert _levels(findings) == ["warn"]
    assert "注册表为空" in findings[0]["detail"]


def test_registry_pid_without_live_row_is_warn():
    """注册表里有身份、读模型里却没有对应的活行 → 只 warn（读模型可能还没算全）。"""
    rows = [_pid_row("r-a", 1, "p001-00")]
    findings = check_row_pids(rows, _reg([_entry("r-a", 1, 0), _entry("r-gone", 1, 1)]))
    assert _levels(findings) == ["warn"]
    assert "r-gone" in findings[0]["detail"]


def test_pid_stats_counts_without_findings():
    """统计不判级：零发现时也给出覆盖率（人读汇总 / --json 靠它显示"这组跑了"）。"""
    rows = [_pid_row("r-a", 1, "p001-00"), _pid_row("r-b", 1, "p001-01"),
            _pid_row("r-a", 2, "p002-00")]
    stats = pid_stats(rows, _reg([_entry("r-a", 1, 0)], [{"pid": "r-old"}]))
    assert stats == {"rows": 3, "with_pid": 3, "unique_pids": 2,
                     "registry": 1, "retired": 1}


def test_row_pids_error_makes_exit_code_1(monkeypatch):
    """退出码语义不变：这组出现 error → 1（与其它组同一条计数路径）。"""
    from scripts import index_audit

    monkeypatch.setattr(index_audit, "_load_paper_ids", lambda: ["p"])
    monkeypatch.setattr(index_audit, "audit_paper", lambda pid: {
        "paper_id": pid, "chunks": 0, "row_pids": {},
        "findings": [index_audit.finding("error", "row_pids", "缺 pid")],
        "row_span": {}, "id_forms": {}})
    assert index_audit.main([]) == 1
    monkeypatch.setattr(index_audit, "audit_paper", lambda pid: {
        "paper_id": pid, "chunks": 0, "row_pids": {},
        "findings": [index_audit.finding("warn", "row_pids", "注册表为空")],
        "row_span": {}, "id_forms": {}})
    assert index_audit.main([]) == 0


def test_healthy_row_pids_have_no_findings():
    """pid 1:1、都在注册表里、注册表自洽 → 零发现。"""
    rows = [_pid_row("r-a", 1, "p001-00"), _pid_row("r-b", 1, "p001-01"),
            _pid_row("r-c", 2, "p002-00")]
    reg = _reg([_entry("r-a", 1, 0), _entry("r-b", 1, 1), _entry("r-c", 2, 0)])
    assert check_row_pids(rows, reg) == []


def test_flatten_page_rows_keeps_pid_and_page():
    """页列表 → 行列表：``pid`` 原样带出来，``page`` 由**页**补上（行本身不带页号）。"""
    pages = [{"page": 3, "rows": [{"id": "p003-00", "pid": "r-a"}]},
             {"page": 4, "rows": [{"id": "p004-00", "pid": "r-b"}]}]
    rows = flatten_page_rows(pages)
    assert [r["pid"] for r in rows] == ["r-a", "r-b"]
    assert [r["page"] for r in rows] == [3, 4]


def test_row_page_falls_back_to_id_prefix():
    """行没有 ``page`` 字段时退回行 id 的页码前缀；都认不出给 0（宁可认不出，也不猜）。"""
    assert _row_page({"id": "p015-06"}) == 15
    assert _row_page({"id": "p015-06", "page": 2}) == 2
    assert _row_page({"id": "p015-06", "page": 0}) == 15
    assert _row_page({}) == 0

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


# --------------------------------------------------------------------------
# 5b. chunk 的 pids（阶段 2a）
# --------------------------------------------------------------------------


def _registry(*pids: str) -> dict:
    """注册表的最小形状：每个 pid 一个活条目（页/序号够用即可）。"""
    return {"version": 1, "retired": [],
            "rows": [{"pid": pid, "page": 1, "order": i, "fp": "x"}
                     for i, pid in enumerate(pids)]}


def test_chunk_pids_missing_field_is_only_warn():
    """老索引（建块时还没有行身份）整片缺字段 —— 只能 warn，否则线上立刻一片红。"""
    chunks = [_chunk("p:unit:u1", row_ids=["p001-00"]),
              _chunk("p:unit:u2", row_ids=["p001-01"])]
    findings = check_chunk_pids(chunks, _registry("r-1", "r-2"))
    assert _levels(findings) == ["warn"]
    assert "pids" in findings[0]["detail"]
    assert "2/2" in findings[0]["detail"]


def test_chunk_pids_length_mismatch_is_error():
    chunks = [_chunk("p:a:text:p001-00", chunk_type="text",
                     row_ids=["p001-00", "p001-01"], pids=["r-1"])]
    findings = check_chunk_pids(chunks, _registry("r-1", "r-2"))
    assert _levels(findings) == ["error"]
    assert "p:a:text:p001-00" in findings[0]["detail"]


def test_chunk_pids_unknown_pid_is_error():
    chunks = [_chunk("p:a:text:p001-00", chunk_type="text",
                     row_ids=["p001-00", "p001-01"], pids=["r-1", "r-ghost"])]
    findings = check_chunk_pids(chunks, _registry("r-1", "r-2"))
    assert _levels(findings) == ["error"]
    assert "r-ghost" in findings[0]["detail"]


def test_chunk_pids_partial_pids_only_warns():
    chunks = [_chunk("p:a:text:p001-00", chunk_type="text",
                     row_ids=["p001-00", "p001-01"], pids=["r-1"], pids_missing=1)]
    findings = check_chunk_pids(chunks, _registry("r-1", "r-2"))
    assert _levels(findings) == ["warn"]
    assert "pids_missing" in findings[0]["detail"]


def test_chunk_pids_healthy_has_no_findings():
    chunks = [_chunk("p:a:text:p001-00", chunk_type="text",
                     row_ids=["p001-00", "p001-01"], pids=["r-1", "r-2"]),
              _chunk("p:formula:f001", chunk_type="formula",
                     row_ids=["p001-00"], pids=["r-1"])]
    assert check_chunk_pids(chunks, _registry("r-1", "r-2")) == []


def test_chunk_pids_skip_unknown_check_when_registry_is_empty():
    """注册表还没生成 → 不判“找不到”（否则每篇论文第一次建索引都假红）。"""
    chunks = [_chunk("p:a:text:p001-00", chunk_type="text",
                     row_ids=["p001-00"], pids=["r-1"])]
    assert check_chunk_pids(chunks, {}) == []

