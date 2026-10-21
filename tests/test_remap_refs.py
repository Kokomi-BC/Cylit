"""重映射脚本（scripts/remap_understanding_refs.py）的纯函数 + 两段式离线段。

全部离线：不连库（session 用构造对象）、不跑解析/翻译/建索引、不往真实 data/ 写
（capture 的落点用 tmp_path 当"数据根"）。重点钉住四件事：

1. 选择规则的**优先级**：集合完全相等 > 交集最大 > road 匹配优先 > chunk_type >
   page > id 字典序，且同样输入必须选出同一条（确定性）。其中 road 只在 ①② 打平时
   起作用 —— **集合相等优先于更大交集**这条最高优先级没被改动（专门有一条用例钉它）；
2. **没有交集就不许映射**：旧 chunk id / inventory 一个字都不改，只进未映射清单；
   pids 为空同理（没有身份就没有桥，不猜）；
3. capture 往返：写出的 ref_pids.json 读回来结构一致、重复 capture 幂等；
   code 集合是 resolved ∪ inventory（后者是"正文写了短码、收尾 JSON 漏登记"的那类），
   每条带 source 供审计，apply 后一律成为正式 resolved；
4. dry-run 与 apply 的写库边界：dry-run 一个字段都不赋值，apply 只写
   refs_json / inventory 两列，codes / invalid / cross_road / ok 原样不动。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import remap_understanding_refs as rm


# --------------------------------------------------------------------------
# 构造数据
# --------------------------------------------------------------------------


def _cand(chunk_id: str, pids: list[str], *, chunk_type: str = "unit", page: int = 1,
          road: str = "") -> dict:
    return {"chunk_id": chunk_id, "pids": list(pids),
            "chunk_type": chunk_type, "page": page, "road": road}


def _ref(pids: list[str], *, chunk_id: str = "old:chunk", chunk_type: str = "unit",
         page: int = 1) -> dict:
    return {"chunk_id": chunk_id, "pids": list(pids),
            "chunk_type": chunk_type, "page": page}


def _doc(codes: dict[str, dict], *, path: str = "a") -> dict:
    return {"version": 1, "paper_id": "p", "captured_at": "2026-01-01T00:00:00",
            "paths": {path: {"codes": codes}}}


def _refs_json(resolved: dict[str, str]) -> str:
    return json.dumps({"codes": list(resolved), "resolved": resolved,
                       "invalid": ["A009"], "cross_road": ["A008"], "ok": True},
                      ensure_ascii=False)


def _inventory_json(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False)


def _pages() -> list[dict]:
    """页产物（pid 的唯一来源）：两行，各带 pid。"""
    return [{"page": 1, "rows": [{"id": "p001-00", "pid": "r-1"},
                                 {"id": "p001-01", "pid": "r-2"}]}]


class _Query:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *args, **kwargs) -> "_Query":
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """按"要查哪个模型"分流的假 session：understanding 给记录，其余给 chunk 行。"""

    def __init__(self, understanding: list, chunk_rows: list) -> None:
        self.understanding = understanding
        self.chunk_rows = chunk_rows
        self.commits = 0

    def query(self, *columns):
        from backend.repositories.mysql.models import Understanding

        if columns and columns[0] is Understanding:
            return _Query(self.understanding)
        return _Query(self.chunk_rows)

    def commit(self) -> None:            # 真 session_scope 才会提交；这里只记账
        self.commits += 1


class _SessionCtx:
    def __init__(self, sess: _FakeSession) -> None:
        self.sess = sess

    def __enter__(self) -> _FakeSession:
        return self.sess

    def __exit__(self, *args) -> bool:
        return False


class _Row:
    """understanding 记录的替身：**记录每一次字段赋值**，dry-run 就靠它验。"""

    def __init__(self, path: str, refs: dict, inventory: list) -> None:
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "refs_json", _refs_json(refs))
        object.__setattr__(self, "inventory", _inventory_json(inventory))
        object.__setattr__(self, "writes", [])

    def __setattr__(self, name: str, value) -> None:
        object.__setattr__(self, name, value)
        if name in ("refs_json", "inventory"):
            self.writes.append(name)


# --------------------------------------------------------------------------
# 1. 选择规则：优先级与确定性
# --------------------------------------------------------------------------


def test_exact_set_equality_wins_over_superset_with_same_overlap() -> None:
    """①集合完全相等优先：哪怕另一条交集同大小、且 chunk_type / 页都对得上。"""
    candidates = [
        _cand("c-superset", ["r-1", "r-2", "r-3"], chunk_type="unit", page=1),
        _cand("c-exact", ["r-1", "r-2"], chunk_type="formula", page=9),
    ]
    got = rm.choose_chunk(["r-1", "r-2"], candidates, chunk_type="unit", page=1)

    assert got is not None
    assert got["chunk_id"] == "c-exact", "集合相等优先于'交集一样大但多出别的行'"
    assert got["mode"] == "equal"


def test_max_overlap_selected() -> None:
    """②交集最大：1 条重合 vs 2 条重合 → 选后者。"""
    candidates = [_cand("c-one", ["r-9", "r-1"]), _cand("c-two", ["r-2", "r-3"])]
    got = rm.choose_chunk(["r-1", "r-2", "r-3"], candidates)

    assert got is not None and got["chunk_id"] == "c-two"
    assert got["mode"] == "overlap"


def test_road_filter_picks_same_road_for_identical_pids() -> None:
    """A/B 两路 unit 的 pids 完全相同 → a 路 code 选 a 路块、b 路 code 选 b 路块。

    这是"交集/类型/页都一样"的极端并列：没有 road 这一档就只能靠 id 字典序，
    两边都会落到 :a: 那条（同一段行、但 b 路读到英文块）。
    """
    candidates = [_cand("p:b:unit:u1", ["r-1", "r-2"], road="b"),
                  _cand("p:a:unit:u1", ["r-1", "r-2"], road="a")]

    got_a = rm.choose_chunk(["r-1", "r-2"], candidates, road="a")
    got_b = rm.choose_chunk(["r-1", "r-2"], candidates, road="b")

    assert got_a is not None and got_b is not None
    assert got_a["chunk_id"] == "p:a:unit:u1" and got_a["mode"] == "equal"
    assert got_b["chunk_id"] == "p:b:unit:u1" and got_b["mode"] == "equal"
    # 共用块（图/表/公式）两路都算匹配，仍然选它
    shared = [_cand("p:formula:f1", ["r-1", "r-2"], chunk_type="formula", road="shared")]
    assert rm.choose_chunk(["r-1", "r-2"], shared, road="b")["chunk_id"] == "p:formula:f1"


def test_candidates_without_road_still_map() -> None:
    """老索引的候选**没有 road 这个键**：照样能选中，只是排在"road 已知"的后面。

    注意与「road 是空串」区分：空串是**共用块（图表公式）**，算本路（见
    test_shared_road_as_empty_string_keeps_formula_precision）；键根本不存在才是"老索引、未知"。
    """
    plain = rm.choose_chunk(["r-1", "r-2"], [_cand("c-old", ["r-1", "r-2"])], road="a")
    assert plain is not None and plain["chunk_id"] == "c-old"

    no_road_key = {"chunk_id": "c-old-no-road", "pids": ["r-1", "r-2"],
                   "chunk_type": "unit", "page": 1}        # ← 故意不带 road 键
    mixed = [_cand("p:a:unit:u1", ["r-1", "r-2"], road="a"), no_road_key]
    got = rm.choose_chunk(["r-1", "r-2"], mixed, road="a")
    order_flipped = rm.choose_chunk(["r-1", "r-2"], list(reversed(mixed)), road="a")
    assert got["chunk_id"] == order_flipped["chunk_id"] == "p:a:unit:u1"


def test_road_does_not_outrank_exact_set_equality() -> None:
    """③road 只在 ①② 打平时生效：集合相等的那条**永远**优先于"road 对但交集更小"。"""
    candidates = [_cand("p:a:unit:exact", ["r-1", "r-2"], road="a"),
                  _cand("p:a:unit:partial", ["r-1"], road="a")]
    got = rm.choose_chunk(["r-1", "r-2"], candidates, road="a")
    assert got is not None and got["chunk_id"] == "p:a:unit:exact"

    # 反过来：唯一"集合相等"的那条 road 不对，也只能选它（不能因为 road 去挑交集更小的）
    only_exact_other_road = [_cand("p:b:unit:exact", ["r-1", "r-2"], road="b"),
                            _cand("p:a:unit:partial", ["r-1"], road="a")]
    got2 = rm.choose_chunk(["r-1", "r-2"], only_exact_other_road, road="a")
    assert got2 is not None and got2["chunk_id"] == "p:b:unit:exact"


def test_tie_break_chunk_type() -> None:
    """②a 交集相同时，chunk_type 与旧 chunk 一致的那条优先。"""
    candidates = [_cand("c-formula", ["r-1", "r-2"], chunk_type="formula"),
                  _cand("c-unit", ["r-1", "r-2"], chunk_type="unit")]
    got = rm.choose_chunk(["r-1", "r-2", "r-3"], candidates,
                          chunk_type="unit", page=1)

    assert got is not None and got["chunk_id"] == "c-unit"
    assert got["mode"] == "overlap", "差一条不算集合相等，仍然按交集映射"


def test_tie_break_page() -> None:
    """②b 交集与类型都相同时，页码与旧 chunk 一致的那条优先。"""
    candidates = [_cand("c-p7", ["r-1"], page=7), _cand("c-p3", ["r-1"], page=3)]
    got = rm.choose_chunk(["r-1", "r-2"], candidates, chunk_type="unit", page=3)

    assert got is not None and got["chunk_id"] == "c-p3"


def test_tie_break_chunk_id_is_deterministic() -> None:
    """②c 其余全并列 → chunk id 字典序；两次调用 + 打乱候选顺序，结果一样。"""
    candidates = [_cand("c-zzz", ["r-1"]), _cand("c-aaa", ["r-1"]), _cand("c-mmm", ["r-1"])]
    first = rm.choose_chunk(["r-1", "r-2"], candidates)
    again = rm.choose_chunk(["r-1", "r-2"], list(reversed(candidates)))

    assert first is not None and again is not None
    assert first["chunk_id"] == again["chunk_id"] == "c-aaa"


# --------------------------------------------------------------------------
# 2. 未映射：不许动旧值
# --------------------------------------------------------------------------


def test_no_overlap_is_unmapped_and_keeps_old_value() -> None:
    """③一条都不沾边：不映射，resolved / inventory 一个字都不改。"""
    plan = rm.plan_path({"A001": _ref(["r-99"])}, [_cand("c1", ["r-1"])])
    item = plan["items"][0]

    assert item["new_chunk_id"] is None and item["mode"] == "none"
    assert plan["unmapped"] == ["A001"] and plan["mapped"] == 0

    refs = {"codes": ["A001"], "resolved": {"A001": "old:chunk"},
            "invalid": [], "cross_road": [], "ok": True}
    inventory = [{"code": "A001", "chunk_id": "old:chunk", "chunk_type": "unit", "page": 2}]
    new_refs, new_inventory, changed = rm.remap_refs(refs, inventory, plan["items"])

    assert changed == []
    assert new_refs["resolved"] == {"A001": "old:chunk"}
    assert new_inventory == inventory


def test_empty_pids_in_ref_pids_is_unmapped() -> None:
    """ref_pids 里 pids 为空（capture 时就没取到身份）→ 未映射，绝不瞎配一条。"""
    candidates = [_cand("c1", ["r-1"]), _cand("c2", ["r-2"])]
    assert rm.choose_chunk([], candidates) is None

    plan = rm.plan_path({"A001": _ref([])}, candidates)
    assert plan["items"][0]["new_chunk_id"] is None
    assert plan["unmapped"] == ["A001"]


# --------------------------------------------------------------------------
# 3. capture：往返 + 幂等 + 不编造 pid
# --------------------------------------------------------------------------


def test_capture_roundtrip_writes_and_reads_same_structure(tmp_path) -> None:
    """capture 写文件 → 读回来 → 结构与内存里的一致（含 version / paper_id / 路）。"""
    understanding = [{"path": "a",
                      "refs_json": _refs_json({"A001": "p:a:unit:u1", "A002": "gone"}),
                      "inventory": _inventory_json([
                          {"code": "A001", "chunk_id": "p:a:unit:u1",
                           "chunk_type": "unit", "page": 1},
                          {"code": "A002", "chunk_id": "gone",
                           "chunk_type": "text", "page": 5}])}]
    chunk_rows = [("p:a:unit:u1", "p001-00", 1, "unit",
                   json.dumps({"row_ids": ["p001-00", "p001-01"]}))]
    sess = _FakeSession(understanding, chunk_rows)

    got = rm.capture("p", root=tmp_path, pages=_pages(),
                     session_factory=lambda: _SessionCtx(sess),
                     captured_at="2026-01-01T00:00:00")

    path = tmp_path / "data" / "papers" / "p" / "md" / "ref_pids.json"
    assert path == got["path"] and path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(got["doc"], ensure_ascii=False))
    assert on_disk["version"] == rm.VERSION
    assert on_disk["paper_id"] == "p" and on_disk["captured_at"] == "2026-01-01T00:00:00"
    assert on_disk["paths"]["a"]["codes"]["A001"] == {
        "chunk_id": "p:a:unit:u1", "pids": ["r-1", "r-2"], "page": 1,
        "chunk_type": "unit", "source": "resolved"}
    # 旧 chunk 已经不在库里：pids 空，page / chunk_type 退回 inventory，不编造
    assert on_disk["paths"]["a"]["codes"]["A002"] == {
        "chunk_id": "gone", "pids": [], "page": 5, "chunk_type": "text",
        "source": "resolved"}

    # 幂等：再 capture 一次，文件内容一个字节都不变（覆盖同名文件，不追加）
    before = path.read_bytes()
    rm.capture("p", root=tmp_path, pages=_pages(),
               session_factory=lambda: _SessionCtx(_FakeSession(understanding, chunk_rows)),
               captured_at="2026-01-01T00:00:00")
    assert path.read_bytes() == before


def test_capture_includes_inventory_only_code_with_pids(tmp_path) -> None:
    """resolved 漏登记、只有 inventory 有的 code 也要 capture（线上 A116 就是这形态）。"""
    understanding = [{"path": "a",
                      "refs_json": _refs_json({"A001": "p:a:unit:u1"}),
                      # 正文真引用了 A116 —— 这才是"resolved 漏登记"的定义；
                      # inventory 里**没被引用过**的短码不该被写进 resolved（见下一条用例）。
                      "markdown": "作者把方法归纳成一个困境 [A001] …… 而这一点 [A116]。",
                      "inventory": _inventory_json([
                          {"code": "A001", "chunk_id": "p:a:unit:u1",
                           "chunk_type": "unit", "page": 1},
                          {"code": "A116", "chunk_id": "p:a:unit:u116",
                           "chunk_type": "unit", "page": 3}])}]
    chunk_rows = [
        ("p:a:unit:u1", "p001-00", 1, "unit",
         json.dumps({"row_ids": ["p001-00", "p001-01"]})),
        ("p:a:unit:u116", "p001-01", 3, "unit",
         json.dumps({"row_ids": ["p001-01"]})),
    ]
    sess = _FakeSession(understanding, chunk_rows)

    got = rm.capture("p", root=tmp_path, pages=_pages(),
                     session_factory=lambda: _SessionCtx(sess),
                     captured_at="2026-01-01T00:00:00")
    codes = got["doc"]["paths"]["a"]["codes"]

    assert set(codes) == {"A001", "A116"}, "resolved ∪ inventory，一个都不许漏"
    assert codes["A116"] == {"chunk_id": "p:a:unit:u116", "pids": ["r-2"],
                             "page": 3, "chunk_type": "unit", "source": "inventory"}
    assert codes["A001"]["source"] == "resolved"
    assert got["from_inventory"] == 1 and got["with_pids"] == 2


def test_resolved_wins_over_inventory_for_same_code(tmp_path) -> None:
    """同名 code 两处都有：chunk_id 以 resolved 为准（inventory 只补页/类型）。"""
    understanding = [{"path": "a",
                      "refs_json": _refs_json({"A001": "p:a:unit:u1"}),
                      "inventory": _inventory_json([
                          {"code": "A001", "chunk_id": "p:a:unit:OTHER",
                           "chunk_type": "unit", "page": 7}])}]
    chunk_rows = [("p:a:unit:u1", "p001-00", 1, "unit",
                   json.dumps({"row_ids": ["p001-00"]}))]
    got = rm.capture("p", root=tmp_path, pages=_pages(),
                     session_factory=lambda: _SessionCtx(_FakeSession(understanding,
                                                                     chunk_rows)),
                     captured_at="2026-01-01T00:00:00")
    entry = got["doc"]["paths"]["a"]["codes"]["A001"]

    assert entry["chunk_id"] == "p:a:unit:u1" and entry["source"] == "resolved"
    assert entry["page"] == 1, "旧 chunk 表在库里就以它为准（inventory 的 7 不覆盖）"
    assert got["total_codes"] == 1, "同名不重复记"


def test_pids_for_chunk_uses_block_id_only_when_row_ids_yield_nothing() -> None:
    """公式块 row_ids 为空时退回 row_id / block_id（row-<行 id>），仍是"真取到 pid"。"""
    pid_of = {"p003-04": "r-7", "p001-00": "r-1"}
    assert rm.pids_for_chunk({"row_ids": ["p003-04"], "block_id": "p003-04"},
                             pid_of) == ["r-7"]
    assert rm.pids_for_chunk({"row_ids": [], "row_id": "p003-04",
                              "block_id": "row-p003-04"}, pid_of) == ["r-7"]
    # row_ids 全取不到 → 才看 block_id（正文 / 单元的 block_id 就是首行 id）
    assert rm.pids_for_chunk({"row_ids": ["p004-00"], "block_id": "p001-00"},
                             pid_of) == ["r-1"]
    # 一处都不在 pid 表里就是空：不编造、不拿"看起来像行 id"的块 id 硬凑
    assert rm.pids_for_chunk({"row_ids": ["p004-00"], "block_id": "p004-b01"},
                             pid_of) == []
    assert rm.pids_for_chunk({"row_ids": [], "block_id": "blk-9"}, pid_of) == []
    assert rm.pids_for_chunk(None, pid_of) == []


# --------------------------------------------------------------------------
# 4. apply：写库边界 + inventory 跟着新 chunk 走
# --------------------------------------------------------------------------


def _apply_setup(tmp_path):
    """一个可映射（集合相等）+ 一个未映射的中间文件 + 假库。"""
    doc = _doc({
        "A001": _ref(["r-1", "r-2"], chunk_id="p:a:unit:old1"),
        "A002": _ref(["r-9"], chunk_id="p:a:unit:dead"),
    })
    rm.write_ref_doc("p", doc, root=tmp_path)
    rows = [_Row("a", {"A001": "p:a:unit:old1", "A002": "p:a:unit:dead"},
                 [{"code": "A001", "chunk_id": "p:a:unit:old1",
                   "chunk_type": "unit", "page": 1},
                  {"code": "A002", "chunk_id": "p:a:unit:dead",
                   "chunk_type": "unit", "page": 2}])]
    chunk_rows = [
        ("p:a:unit:new1", "unit", 4,
         json.dumps({"row_ids": ["p001-00", "p001-01"], "pids": ["r-1", "r-2"]})),
        ("p:a:unit:new2", "unit", 5, json.dumps({"pids": ["r-3"]})),
    ]
    sess = _FakeSession(rows, chunk_rows)
    return doc, rows, sess


def test_apply_dry_run_writes_nothing(tmp_path) -> None:
    """dry-run：字段一个都不赋值、不提交，但"会改几条"照样算出来。"""
    _, rows, sess = _apply_setup(tmp_path)
    before_refs = rows[0].refs_json
    before_inv = rows[0].inventory

    plan = rm.apply("p", dry_run=True, root=tmp_path,
                    session_factory=lambda: _SessionCtx(sess))

    assert plan["result"]["written"] == 1 and plan["result"]["codes"] == 1
    assert plan["result"]["dry_run"] is True
    assert rows[0].writes == [], "dry-run 不许给任何字段赋值"
    assert rows[0].refs_json == before_refs and rows[0].inventory == before_inv
    assert sess.commits == 0


def test_apply_writes_only_refs_and_inventory(tmp_path) -> None:
    """apply：只写 refs_json / inventory 两列，且 inventory 的 page / type 跟着新 chunk。"""
    _, rows, sess = _apply_setup(tmp_path)

    plan = rm.apply("p", dry_run=False, root=tmp_path,
                    session_factory=lambda: _SessionCtx(sess))

    assert plan["result"]["written"] == 1
    assert set(rows[0].writes) == {"refs_json", "inventory"}, "其他列一律不许碰"
    refs = json.loads(rows[0].refs_json)
    assert refs["resolved"]["A001"] == "p:a:unit:new1"
    assert refs["resolved"]["A002"] == "p:a:unit:dead", "未映射保留旧 chunk id"
    by_code = {it["code"]: it for it in json.loads(rows[0].inventory)}
    assert by_code["A001"]["chunk_id"] == "p:a:unit:new1"
    assert by_code["A001"]["page"] == 4, "page 必须跟着新 chunk（否则跳到新块显示旧页）"
    assert by_code["A002"]["page"] == 2 and by_code["A002"]["chunk_id"] == "p:a:unit:dead"


def test_apply_maps_inventory_only_code_into_resolved(tmp_path) -> None:
    """inventory-only 的 code：apply 后出现在 resolved 里，并映射到新 chunk。"""
    doc = _doc({"A116": {"chunk_id": "p:a:unit:old116", "pids": ["r-2"],
                         "page": 3, "chunk_type": "unit", "source": "inventory"}})
    rm.write_ref_doc("p", doc, root=tmp_path)
    # 库里 resolved 只有 A001（A116 是漏登记的），inventory 里两枚都在
    rows = [_Row("a", {"A001": "p:a:unit:old1"},
                 [{"code": "A001", "chunk_id": "p:a:unit:old1",
                   "chunk_type": "unit", "page": 1},
                  {"code": "A116", "chunk_id": "p:a:unit:old116",
                   "chunk_type": "unit", "page": 3}])]
    chunk_rows = [("p:a:unit:new116", "unit", 3, json.dumps({"pids": ["r-2"]}))]
    sess = _FakeSession(rows, chunk_rows)

    plan = rm.apply("p", dry_run=False, root=tmp_path,
                    session_factory=lambda: _SessionCtx(sess))

    refs = json.loads(rows[0].refs_json)
    assert refs["resolved"]["A116"] == "p:a:unit:new116", "漏登记的 code 写回后成为正式 resolved"
    assert refs["resolved"]["A001"] == "p:a:unit:old1", "不在本中间文件里的 code 一律不动"
    by_code = {it["code"]: it for it in json.loads(rows[0].inventory)}
    assert by_code["A116"]["chunk_id"] == "p:a:unit:new116"
    assert plan["result"]["written"] == 1 and plan["result"]["codes"] == 1


def test_apply_keeps_other_ref_keys() -> None:
    """codes / invalid / cross_road / ok 原样不动，只换 resolved（未映射的也不动）。"""
    items = rm.plan_path({"A001": _ref(["r-1"])}, [_cand("c1", ["r-1"])])["items"]
    refs = {"codes": ["A001"], "resolved": {"A001": "old", "A002": "dead"},
            "invalid": ["A005"], "cross_road": ["A006"], "ok": False}

    new_refs, _, changed = rm.remap_refs(refs, [], items)

    assert new_refs["codes"] == ["A001"]
    assert new_refs["invalid"] == ["A005"] and new_refs["cross_road"] == ["A006"]
    assert new_refs["ok"] is False
    assert new_refs["resolved"] == {"A001": "c1", "A002": "dead"}
    assert changed == ["A001"]


def test_inventory_follows_new_chunk_type_and_page() -> None:
    """inventory 决定引用显示的类型/页码：映射到新块后必须一起改。"""
    items = rm.plan_path({"A001": _ref(["r-1"], chunk_type="text", page=3)},
                         [_cand("c-formula", ["r-1"], chunk_type="formula", page=8)]
                         )["items"]
    inventory = [{"code": "A001", "chunk_id": "old", "chunk_type": "text", "page": 3}]

    _, new_inventory, changed = rm.remap_refs({"resolved": {"A001": "old"}}, inventory, items)

    assert changed == ["A001"]
    assert new_inventory[0] == {"code": "A001", "chunk_id": "c-formula",
                                "chunk_type": "formula", "page": 8}


def test_apply_plan_counts_and_summary_line(tmp_path) -> None:
    """汇总格式固定：mapped N/M（集合相等 X / 交集 Y / 未映射 Z）。"""
    _, _, sess = _apply_setup(tmp_path)
    plan = rm.apply("p", dry_run=True, root=tmp_path,
                    session_factory=lambda: _SessionCtx(sess))

    assert (plan["total"], plan["mapped"], plan["equal"], plan["overlap"]) == (2, 1, 1, 0)
    assert plan["unmapped"] == [{"path": "a", "code": "A002"}]
    assert rm.summary_line(plan) == "mapped 1/2（集合相等 1 / 交集 0 / 未映射 1）"


def test_apply_without_capture_file_is_an_error(tmp_path) -> None:
    """没跑过 capture 就 apply → 明确报错（不静默当"零条未映射"）。"""
    try:
        rm.apply("p", root=tmp_path, session_factory=lambda: _SessionCtx(_FakeSession([], [])))
    except FileNotFoundError as exc:
        assert "capture" in str(exc)
    else:                                  # pragma: no cover - 失败时更好看
        raise AssertionError("缺少 ref_pids.json 时必须抛 FileNotFoundError")


def test_capture_skips_inventory_code_never_cited(tmp_path) -> None:
    """inventory 是全篇材料（213 条）：**没被正文引用过**的短码绝不能写成 resolved。

    否则重建一次就把"resolved = 引用了且能解析"的语义改掉（引用表凭空多出一百多条）。
    """
    understanding = [{"path": "a",
                      "refs_json": _refs_json({"A001": "p:a:unit:u1"}),
                      "markdown": "只引用了 [A001]。",
                      "inventory": _inventory_json([
                          {"code": "A001", "chunk_id": "p:a:unit:u1",
                           "chunk_type": "unit", "page": 1},
                          {"code": "A999", "chunk_id": "p:a:unit:u999",
                           "chunk_type": "unit", "page": 9}])}]
    chunk_rows = [("p:a:unit:u1", "p001-00", 1, "unit",
                   json.dumps({"row_ids": ["p001-00"]})),
                  ("p:a:unit:u999", "p009-00", 9, "unit",
                   json.dumps({"row_ids": ["p009-00"]}))]
    sess = _FakeSession(understanding, chunk_rows)
    got = rm.capture("p", root=tmp_path, pages=_pages(),
                     session_factory=lambda: _SessionCtx(sess),
                     captured_at="2026-01-01T00:00:00")
    codes = got["doc"]["paths"]["a"]["codes"]
    assert set(codes) == {"A001"}, "没被引用的 inventory 短码不许进 resolved"


def test_shared_road_as_empty_string_keeps_formula_precision() -> None:
    """真实坑：共用块的 metadata.road 常常是空串，不能当"另一路"。

    公式块与它所在的行共用同一批 pids ⇒ 集合相等时两条候选打平；若把空串 road 判成不匹配，
    公式块会被同行的 unit 块挤掉，引用就从"精确到这条公式"退回"精确到这一行"（线上 A045）。
    """
    pids = ["r-1"]
    unit = {"chunk_id": "p:a:unit:p004-04-p004-04:en", "chunk_type": "unit",
            "page": 4, "road": "a", "pids": pids}
    formula = {"chunk_id": "p:formula:f004-0041:any", "chunk_type": "formula",
               "page": 4, "road": "", "pids": pids}
    picked = rm.choose_chunk(pids, [unit, formula], chunk_type="formula", page=4, road="a")
    assert picked["chunk_id"] == "p:formula:f004-0041:any", "共用块（road 空串）要按本路算，chunk_type 才能决胜"
