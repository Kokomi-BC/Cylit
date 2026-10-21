"""自检的**别名表完整性**那一条（阶段 4 的不变量：正文短码 → 行身份 pid）。

只测判定与统计（构造数据，不连库）：

1. 正文短码**既没有 refs_json.pids、其 chunk 也没有 metadata.pids** → 只 warn（附 code +
   建议跑 backfill_ref_pids.py），不是 error —— 重建索引之前引用照样能按旧 chunk id 跳；
2. 任一侧有 pids → 零发现（refs_json 那一份跨重建最硬，chunk 那一份是建索引时写的）；
3. 完全不认得的短码**不算缺口**（那是 check_understanding 的 error，backfill 也补不出来）；
4. 覆盖率 100% → 只进 ``[stat]``（"这组跑了、全绿"要看得见）。
"""

from __future__ import annotations

from scripts import index_audit as audit


def _chunk(cid: str, pids: list | None = None) -> dict:
    meta: dict = {"row_ids": [f"{cid}-row"]}
    if pids is not None:
        meta["pids"] = list(pids)
    return {"id": cid, "chunk_type": "unit", "block_id": cid, "page_no": 1,
            "order_global": 1, "metadata": meta}


def test_neither_source_is_warn_with_backfill_hint():
    """两边都没有行身份 → warn（带 code 与建议），不升级成 error。"""
    findings = audit.check_ref_pid_alias("方法见 [A001] 与 [A002]。",
                                        {"resolved": {"A001": "c1", "A002": "c2"}},
                                        [{"code": "A002", "chunk_id": "c2"}],
                                        [_chunk("c1"), _chunk("c2")])

    assert len(findings) == 1
    item = findings[0]
    assert item["level"] == "warn" and item["check"] == "understanding_ref_alias"
    assert "2/2" in item["detail"]
    assert "A001" in item["detail"] and "A002" in item["detail"]
    assert "backfill_ref_pids.py" in item["detail"]


def test_refs_json_pids_satisfy_the_invariant():
    """refs_json.pids 里有这条（阶段 3 落库的那一份）→ 零发现，统计 100%。"""
    refs = {"resolved": {"A001": "c1"}, "pids": {"A001": ["r-1"]}}

    assert audit.check_ref_pid_alias("见 [A001]。", refs, [], [_chunk("c1")]) == []
    assert audit.ref_pid_coverage("见 [A001]。", refs, [], [_chunk("c1")]) == {
        "codes": 1, "with_pids": 1, "gaps": []}


def test_chunk_pids_also_satisfy_the_invariant():
    """chunk 的 metadata.pids 非空也算有身份（读路径的兜底就是它）。"""
    refs = {"resolved": {"A001": "c1"}}

    assert audit.check_ref_pid_alias("见 [A001]。", refs, [], [_chunk("c1", ["r-1"])]) == []


def test_inventory_chunk_is_consulted_when_resolved_is_stale():
    """resolved 的 chunk 已经不在 → 按 inventory 的 chunk_id 判（那条 chunk 有 pids 就算有）。"""
    refs = {"resolved": {"A001": "c-old"}}
    inventory = [{"code": "A001", "chunk_id": "c-new"}]

    assert audit.check_ref_pid_alias("见 [A001]。", refs, inventory,
                                     [_chunk("c-new", ["r-1"])]) == []


def test_unknown_codes_are_not_counted_as_gaps():
    """完全不认得的短码不算缺口（那是 check_understanding 的 error，补不出来）。"""
    got = audit.ref_pid_coverage("见 [A001] 与 [A777]。",
                                 {"resolved": {"A001": "c1"}}, [], [_chunk("c1", ["r-1"])])

    assert got == {"codes": 1, "with_pids": 1, "gaps": []}


def test_stats_aggregate_all_paths():
    """整篇统计：两路各自的缺口加起来（report 里一行看得完）。"""
    stats = audit.ref_pid_alias_stats(
        [("见 [A001]。", {"resolved": {"A001": "c1"}}, []),
         ("见 [A002] 与 [A003]。",
          {"resolved": {"A002": "c2", "A003": "c-gone"}}, [])],
        [_chunk("c1", ["r-1"]), _chunk("c2")])

    assert stats == {"codes": 3, "with_pids": 1, "gaps": 2}


def test_full_coverage_prints_stat_line(capsys):
    """覆盖 100% → 零发现 + 一行 ``[stat]``（否则"这组跑了"看不出来）。"""
    audit.print_human([{"paper_id": "p", "chunks": 1, "findings": [],
                        "understanding_ref_alias": {"codes": 3, "with_pids": 3,
                                                    "gaps": 0}}])

    line = capsys.readouterr().out
    assert "[stat] understanding_ref_alias: 3/3 个正文短码有行身份" in line
