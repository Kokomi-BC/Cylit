"""阶段 3：读路径**按 pid 解析优先**，旧 chunk id 降级为兜底。

为什么要有这一层：重建索引会删掉全部 chunk 并按新切点重建，chunk id 随之全变，
而 rerow_identity 的 pid 是生成一次、重建时按指纹继承的 —— 于是"解读当时引用的那一段"
在重建之后只有 pid 还认得出。读路径三级回落：

① pid 解析（refs_json.pids 优先，老数据用现存 chunk 的 metadata.pids 惰性补齐）
   → 全部 ok 就用**解析出来的** page / row_id（自愈），并标 healed；
② 现存 chunk 锚点（今天的行为，split_live_refs）；
③ 过期兜底（stale_refs，带 page）。

判据：chunk 还在、pid 也对时，①②给出的字段必须**逐一致**（只多 pid_state / healed）。
性能：page_products 整篇只取一次，而且只有至少一条 code 带 pids 时才取。

全部离线：DB / 读模型 / 注册表都用构造数据替换（不跑解读、不连库、不写真实 data/）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from backend.services.paper import row_identity
from backend.services.understanding import understanding_service as us


# --------------------------------------------------------------------------
# 构造数据
# --------------------------------------------------------------------------


def _row(rid: str, page: int, index: int, text: str) -> dict:
    """阅读器读模型的一行（与 page_products 出来的形状同构：id / page / index / original）。"""
    return {"id": rid, "page": page, "index": index, "original": text}


def _reg(rows: dict[str, dict]) -> dict:
    """{pid: 行} → 注册表（指纹取的是**登记那一刻**这一行的文本）。"""
    return {"version": 1, "retired": [],
            "rows": [{"pid": pid, "page": int(r["page"]), "order": int(r["index"]),
                      "fp": row_identity.fingerprint(r)} for pid, r in rows.items()]}


def _inventory(**codes) -> list[dict]:
    """{code: (chunk_id, page)} → 落库的 inventory（材料清单）。"""
    return [{"code": code, "chunk_id": cid, "chunk_type": "unit", "page": page}
            for code, (cid, page) in codes.items()]


def _understanding(resolved: dict, inventory: list, *, pids: dict | None = None,
                   markdown: str = "见 [A001]。") -> "us.Understanding":
    refs = {"codes": list(resolved), "resolved": dict(resolved),
            "invalid": [], "cross_road": [], "ok": True}
    if pids is not None:                      # 老数据没有这个键（阶段 3 之前落的库）
        refs["pids"] = dict(pids)
    return us.Understanding(
        id="p:a", paper_id="p", path="a", markdown=markdown,
        refs_json=us.json_dumps(refs), inventory=us.json_dumps(inventory),
        refs_ok=True, model="m", prompt_tokens=0, completion_tokens=0, updated_at=None,
    )


class _Ctx:
    """build_understanding 一开始那次 session_scope：取 understanding 行与论文标题。"""

    class _Sess:
        def get(self, model, key):
            return None

        def add(self, row) -> None:
            pass

    def __enter__(self):
        return _Ctx._Sess()

    def __exit__(self, *args) -> bool:
        return False


# --------------------------------------------------------------------------
# ① pid 解析：chunk 还在 / 已不在
# --------------------------------------------------------------------------


def test_pid_first_matches_old_fields_when_chunk_is_alive(monkeypatch) -> None:
    """chunk 还在、pid 也对：①②逐字段相同（只多 healed）—— 本次"行为零变化"的判据。"""
    row = _row("p003-04", 3, 4, "alpha beta gamma")
    anchors = {"cid1": {"block_id": "b1", "row_id": "p003-04", "latex": None,
                        "pids": ["r-1"]}}
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: dict(anchors))
    monkeypatch.setattr(us, "_registry", lambda pid: _reg({"r-1": row}))
    monkeypatch.setattr(us, "_read_model_rows", lambda pid: [row])

    inventory = _inventory(A001=("cid1", 3))
    got = us.to_dict(_understanding({"A001": "cid1"}, inventory,
                                    pids={"A001": ["r-1"]}))

    item = got["refs_resolved"][0]
    old, stale = us.split_live_refs({"A001": "cid1"}, {"A001": inventory[0]},
                                    exists=lambda ids: set(anchors) & set(ids),
                                    anchors=anchors)
    assert {k: v for k, v in item.items() if k != "healed"} == old[0]
    assert item["row_id"] == "p003-04", "落点用 pid 解析出来的行"
    assert item["healed"] is True
    assert got["stale_refs"] == [] and not stale
    assert "pid_state" not in item, "解析全 ok 时不写 pid_state"


def test_chunk_gone_but_pids_resolve_heals_with_row_and_page(monkeypatch) -> None:
    """重建索引后 chunk id 全变：旧 chunk 查不到了，但 pids 能解析 → 仍然给出 row_id + page。"""
    row = _row("p003-04", 3, 4, "alpha beta gamma")
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: {})     # 旧 chunk 一条都不在了
    monkeypatch.setattr(us, "_registry", lambda pid: _reg({"r-1": row}))
    monkeypatch.setattr(us, "_read_model_rows", lambda pid: [row])

    got = us.to_dict(_understanding({"A001": "旧:cid"}, _inventory(A001=("旧:cid", 3)),
                                    pids={"A001": ["r-1"]}))

    item = got["refs_resolved"][0]
    assert got["stale_refs"] == [], "pid 认回来了就不算过期"
    assert (item["chunk_id"], item["row_id"], item["page"]) == ("", "p003-04", 3)
    assert item["healed"] is True and item["pids"] == ["r-1"]


def test_alive_chunk_is_preferred_by_pid_intersection(monkeypatch) -> None:
    """能配上现存 chunk 就补它的 chunk_id / block_id / latex（公式块与所在行共享 pids 时
    优先原本那一条，不把"精确到公式"换成"精确到行"）。"""
    row = _row("p003-04", 3, 4, "alpha beta gamma")
    anchors = {"p:a:formula:f1": {"block_id": "row-p003-04", "latex": "x^2",
                                 "pids": ["r-1"]},
               "p:a:unit:u1": {"block_id": "b1", "pids": ["r-1"]}}
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: dict(anchors))
    monkeypatch.setattr(us, "_registry", lambda pid: _reg({"r-1": row}))
    monkeypatch.setattr(us, "_read_model_rows", lambda pid: [row])

    got = us.to_dict(_understanding(
        {"A001": "p:a:formula:f1"},
        [{"code": "A001", "chunk_id": "p:a:formula:f1", "chunk_type": "formula",
          "page": 3}], pids={"A001": ["r-1"]}))

    item = got["refs_resolved"][0]
    assert item["chunk_id"] == "p:a:formula:f1", "交集打平时优先原本那一条"
    assert item["block_id"] == "row-p003-04" and item["latex"] == "x^2"
    assert item["row_id"] == "p003-04" and item["chunk_type"] == "formula"


# --------------------------------------------------------------------------
# ②③ 解析不成立时退回今天的行为（并带上 pid_state）
# --------------------------------------------------------------------------


def test_text_changed_pid_falls_back_to_stale_and_is_marked(monkeypatch) -> None:
    old = _row("p003-04", 3, 4, "alpha beta gamma delta")
    new = {**old, "original": "完全换了一段话，长度也不一样了"}
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: {})
    monkeypatch.setattr(us, "_registry", lambda pid: _reg({"r-1": old}))
    monkeypatch.setattr(us, "_read_model_rows", lambda pid: [new])

    got = us.to_dict(_understanding({"A001": "旧:cid"}, _inventory(A001=("旧:cid", 3)),
                                    pids={"A001": ["r-1"]}))

    assert got["refs_resolved"] == []
    assert got["stale_refs"][0]["pid_state"] == "text-changed"
    assert got["stale_refs"][0]["page"] == 3, "仍保留页码（前端照样能跳那一页）"


def test_gone_pid_keeps_the_chunk_anchor_and_is_marked(monkeypatch) -> None:
    """pid 在注册表里没有（行没了）：退回 ② —— chunk 还在就照样能跳，只是带上 gone。"""
    anchors = {"cid1": {"block_id": "b1", "row_id": "p003-04", "pids": ["r-gone"]}}
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: dict(anchors))
    monkeypatch.setattr(us, "_registry", lambda pid: {"version": 1, "rows": [], "retired": []})
    monkeypatch.setattr(us, "_read_model_rows", lambda pid: [])

    got = us.to_dict(_understanding({"A001": "cid1"}, _inventory(A001=("cid1", 3)),
                                    pids={"A001": ["r-gone"]}))

    item = got["refs_resolved"][0]
    assert got["stale_refs"] == []
    assert item["chunk_id"] == "cid1" and item["row_id"] == "p003-04"
    assert item["pid_state"] == "gone" and "healed" not in item


def test_old_data_without_saved_pids_uses_chunk_pids_in_memory(monkeypatch) -> None:
    """老数据 refs_json 没有 pids：用现存 chunk 的 metadata.pids 惰性补上（不写库）。"""
    row = _row("p003-04", 3, 4, "alpha beta gamma")
    anchors = {"cid1": {"block_id": "b1", "row_id": "p003-04", "pids": ["r-1"]}}
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: dict(anchors))
    monkeypatch.setattr(us, "_registry", lambda pid: _reg({"r-1": row}))
    monkeypatch.setattr(us, "_read_model_rows", lambda pid: [row])

    understanding = _understanding({"A001": "cid1"}, _inventory(A001=("cid1", 3)))
    got = us.to_dict(understanding)

    assert got["refs_resolved"][0]["healed"] is True
    assert "pids" not in json.loads(understanding.refs_json), "惰性补齐只在内存里，不写库"


def test_without_any_pids_behaviour_is_unchanged(monkeypatch) -> None:
    """既没有 refs.pids、chunk 也没有 pids（存量数据）→ 与改造前**逐字段相同**。"""
    anchors = {"cid1": {"block_id": "b1", "row_id": "p003-04", "latex": "x^2",
                       "pids": None}}

    def boom(paper_id):
        raise AssertionError("没有 pids 就不许取页级读模型 / 注册表")

    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: dict(anchors))
    monkeypatch.setattr(us, "_read_model_rows", boom)
    monkeypatch.setattr(us, "_registry", boom)

    inventory = _inventory(A001=("cid1", 3), A002=("cid2", 7))
    got = us.to_dict(_understanding({"A001": "cid1", "A002": "cid2"}, inventory))

    old, stale = us.split_live_refs({"A001": "cid1", "A002": "cid2"},
                                    {it["code"]: it for it in inventory},
                                    exists=lambda ids: set(anchors) & set(ids),
                                    anchors=anchors)
    assert got["refs_resolved"] == old and got["stale_refs"] == stale


# --------------------------------------------------------------------------
# 性能：page_products 整篇只取一次（且必要时才取）
# --------------------------------------------------------------------------


def test_page_products_is_read_once_for_the_whole_paper(monkeypatch) -> None:
    from backend.services.paper import reader_service

    first = _row("p003-04", 3, 4, "alpha beta gamma")
    second = _row("p007-01", 7, 1, "delta epsilon")
    calls: list[str] = []

    def fake_products(paper_id):
        calls.append(paper_id)
        return ([{"page": 3, "rows": [dict(first)]},
                 {"page": 7, "rows": [dict(second)]}], "paths")

    monkeypatch.setattr(reader_service, "page_products", fake_products)
    monkeypatch.setattr(us, "_chunk_anchors", lambda ids: {})      # 旧 chunk 都不在了
    monkeypatch.setattr(us, "_registry", lambda pid: _reg({"r-1": first, "r-2": second}))

    got = us.to_dict(_understanding(
        {"A001": "cid1", "A002": "cid2"},
        _inventory(A001=("cid1", 3), A002=("cid2", 7)),
        pids={"A001": ["r-1"], "A002": ["r-2"]}))

    assert calls == ["p"], "整篇只取一次（两条 code 都要解析也一样）"
    assert [i["row_id"] for i in got["refs_resolved"]] == ["p003-04", "p007-01"]
    assert [i["page"] for i in got["refs_resolved"]] == [3, 7]


# --------------------------------------------------------------------------
# 落库：generate 写 refs_json.pids（与 chunk metadata.pids 同源）
# --------------------------------------------------------------------------


def test_generate_persists_pids_from_chunk_metadata(monkeypatch) -> None:
    saved: dict = {}
    inventory = [
        {"code": "A001", "chunk_id": "cid1", "chunk_type": "unit", "page": 3,
         "section": "", "preview": "x", "pids": ["r-1", "r-2"]},
        {"code": "A002", "chunk_id": "cid2", "chunk_type": "unit", "page": 4,
         "section": "", "preview": "y"},            # 这条 chunk 还没有 pids
    ]
    monkeypatch.setattr(us, "chunk_inventory", lambda paper_id, path: inventory)
    monkeypatch.setattr(us, "_brief", lambda paper_id: "")
    monkeypatch.setattr(us, "_page_context_head", lambda path, paper_id: "")
    monkeypatch.setattr(us, "_provider",
                        lambda path: (SimpleNamespace(model_name="m"), "m"))
    monkeypatch.setattr(us, "_generate", lambda provider, prompt, **kw: SimpleNamespace(
        text='{"markdown": "见 [A001] 与 [A002]。", "refs": ["A001", "A002"]}',
        model="m", prompt_tokens=10, completion_tokens=20, raw={}))
    monkeypatch.setattr(us, "_save", lambda paper_id, path, payload: saved.update(payload))
    monkeypatch.setattr(us, "get_understanding", lambda paper_id, path=None: {})
    monkeypatch.setattr(us, "session_scope", lambda: _Ctx())

    us.build_understanding("p", "a", force=True)

    refs = json.loads(saved["refs_json"])
    assert refs["pids"] == {"A001": ["r-1", "r-2"]}, "只写有 pids 的那几条"
    assert refs["resolved"] == {"A001": "cid1", "A002": "cid2"}, "老键一个都不动"
    assert refs["codes"] == ["A001", "A002"] and refs["ok"] is True
    stored_inv = json.loads(saved["inventory"])
    assert stored_inv[0] == {"code": "A001", "chunk_id": "cid1", "chunk_type": "unit",
                            "page": 3}, "落库的 inventory 形状不变"


def test_ref_pids_by_code_skips_codes_without_pids() -> None:
    inventory = [{"code": "A001", "pids": ["r-1"]}, {"code": "A002", "pids": []},
                 {"code": "A003"}]
    assert us.ref_pids_by_code(inventory, {"A001": "c1", "A002": "c2", "A003": "c3",
                                           "A009": "c9"}) == {"A001": ["r-1"]}


# --------------------------------------------------------------------------
# remap apply：把 pids 写回 refs_json（幂等）
# --------------------------------------------------------------------------


def _remap_item(code: str, *, new_id: str | None, pids: list[str]) -> dict:
    return {"code": code, "old_chunk_id": "old:chunk", "new_chunk_id": new_id,
            "pids": list(pids), "chunk_type": "unit", "page": 1, "source": "resolved"}


def test_remap_apply_writes_pids_back_and_is_idempotent() -> None:
    from scripts import remap_understanding_refs as rm

    refs = {"codes": ["A001"], "resolved": {"A001": "old:chunk"},
            "invalid": [], "cross_road": [], "ok": True}
    inventory = [{"code": "A001", "chunk_id": "old:chunk", "chunk_type": "unit", "page": 1}]
    items = [_remap_item("A001", new_id="new:chunk", pids=["r-1", "r-2"])]

    once, inventory_after, changed = rm.remap_refs(refs, inventory, items)
    assert once["pids"] == {"A001": ["r-1", "r-2"]}
    assert once["resolved"] == {"A001": "new:chunk"}
    assert once["codes"] == ["A001"] and once["ok"] is True, "其它键原样带走"
    assert changed == ["A001"] and inventory_after[0]["chunk_id"] == "new:chunk"
    assert "pids" not in refs, "原始 refs 不就地改"

    again, inventory_twice, changed_twice = rm.remap_refs(once, inventory_after, items)
    assert changed_twice == [], "第二次跑没有任何字段要改（幂等）"
    assert again == once and inventory_twice == inventory_after


def test_remap_apply_leaves_unmapped_code_untouched() -> None:
    from scripts import remap_understanding_refs as rm

    refs = {"codes": ["A001"], "resolved": {"A001": "old:chunk"}, "ok": True}
    new_refs, _inv, changed = rm.remap_refs(
        refs, [{"code": "A001", "chunk_id": "old:chunk", "chunk_type": "unit", "page": 1}],
        [_remap_item("A001", new_id=None, pids=["r-1"])])
    assert changed == [] and new_refs == refs, "未映射：resolved / inventory / pids 一个字段都不碰"


# --------------------------------------------------------------------------
# 自检：全 ok 只进 [stat]，认不回才 warn（附 code 与 pid，最多 5 条）
# --------------------------------------------------------------------------


def test_audit_resolved_refs_are_clean_and_counted() -> None:
    from scripts.index_audit import check_ref_resolution, ref_resolution_stats

    row = _row("p003-04", 3, 4, "alpha beta gamma")
    refs = {"resolved": {"A001": "cid1"}, "pids": {"A001": ["r-1"]}}
    registry = _reg({"r-1": row})

    assert check_ref_resolution(refs, [], [row], registry) == [], "全 ok 不许产生发现"
    assert ref_resolution_stats([refs], [], [row], registry) == \
        {"refs": 1, "ok": 1, "bad": 0}


def test_audit_text_changed_or_gone_ref_is_warn_with_code_and_pid() -> None:
    from scripts.index_audit import check_ref_resolution

    old = _row("p003-04", 3, 4, "alpha beta gamma delta")
    new = {**old, "original": "完全换了一段话，长度也不一样了"}
    refs = {"resolved": {"A001": "cid1", "A002": "cid2"},
            "pids": {"A001": ["r-1"], "A002": ["r-gone", "r-2", "r-3", "r-4", "r-5", "r-6"]}}

    findings = check_ref_resolution(refs, [], [new], _reg({"r-1": old}))

    assert [f["level"] for f in findings] == ["warn"], "认不回只 warn（引用仍可按页跳）"
    assert findings[0]["check"] == "understanding_ref_resolve"
    detail = findings[0]["detail"]
    assert "A001/r-1:text-changed" in detail
    assert "A002/r-gone:gone" in detail
    assert detail.count("gone") >= 1
    samples = detail.count(":gone") + detail.count(":text-changed")
    assert samples == 5, "最多 5 条样例（7 条坏身份只列 5 条）"
    assert "…(+2)" in detail


def test_audit_ref_resolution_counts_persisted_pids() -> None:
    """解读自己存的 pids 优先于 chunk（重建后 chunk 已换，按 chunk 查只会是空）。"""
    from scripts.index_audit import ref_pids

    refs = {"resolved": {"A001": "旧:cid"}, "pids": {"A001": ["r-1"]}}
    assert ref_pids(refs, []) == [("A001", ["r-1"])]
    assert ref_pids({"resolved": {"A001": "cid1"}},
                    [{"id": "cid1", "metadata": {"pids": ["r-9"]}}]) == [("A001", ["r-9"])]
