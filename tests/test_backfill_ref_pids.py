"""backfill_ref_pids.py 的取值顺序与写库边界（全部离线，不连库、不跑解析）。

重点钉住四件事：

1. **三级取值顺序**：capture 产物（md/ref_pids.json）→ refs_json.resolved 那条 chunk 的
   metadata.pids → inventory 里 chunk_id 那条 chunk 的 metadata.pids → 跳过（不编造）。
   本地三篇论文的 chunk 表**还没有 metadata.pids**（它们没重建过索引），只靠 capture
   这一级 —— 所以"capture 有、chunk 没有"这件事单独钉一条；
2. 回填范围 = **正文里出现的短码**：没被引用的 code 一个都不写（不把 inventory 全灌进去）；
3. 写库边界：只给 refs_json 一列赋值、只碰它的 pids 一个键（其它键与 inventory 原样），
   --dry-run 一个字段都不赋值（writes == []）；
4. 幂等：第二次运行一个 code 都不改（已有 pids 不重写）。

落点一律用 ``tmp_path`` 当"数据根"（--root 只给测试），不往真实 data/ 写。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import backfill_ref_pids as bf


# --------------------------------------------------------------------------
# 构造数据（假 session + 记录赋值的高亮行）
# --------------------------------------------------------------------------


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

    def query(self, *columns):
        from backend.repositories.mysql.models import Understanding

        if columns and columns[0] is Understanding:
            return _Query(self.understanding)
        return _Query(self.chunk_rows)


class _SessionCtx:
    def __init__(self, sess: _FakeSession) -> None:
        self.sess = sess

    def __enter__(self) -> _FakeSession:
        return self.sess

    def __exit__(self, *args) -> bool:
        return False


class _Record:
    """understanding 记录的替身：**记录每一次字段赋值** —— dry-run 就靠它验。"""

    def __init__(self, path: str, refs: dict, inventory: list,
                 markdown: str = "") -> None:
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "refs_json", json.dumps(refs, ensure_ascii=False))
        object.__setattr__(self, "inventory", json.dumps(inventory, ensure_ascii=False))
        object.__setattr__(self, "markdown", markdown)
        object.__setattr__(self, "writes", [])

    def __setattr__(self, name: str, value) -> None:
        object.__setattr__(self, name, value)
        if name in ("refs_json", "inventory"):
            self.writes.append(name)

    def refs(self) -> dict:
        return json.loads(self.refs_json)


def _row(path: str = "a", *, resolved: dict | None = None, markdown: str = "",
         inventory: list | None = None, pids: dict | None = None) -> _Record:
    refs: dict = {"codes": list((resolved or {})), "resolved": dict(resolved or {}),
                  "invalid": ["A099"], "cross_road": [], "ok": True}
    if pids is not None:
        refs["pids"] = dict(pids)
    return _Record(path, refs, inventory or [], markdown)


def _chunk(cid: str, pids: list | None = None) -> tuple:
    """chunk 表的一行（``load_chunk_pids`` 只查 id + metadata_json）。"""
    meta: dict = {"row_ids": [f"{cid}-row"]}
    if pids is not None:
        meta["pids"] = list(pids)
    return (cid, json.dumps(meta, ensure_ascii=False))


def _write_capture(root, paths_codes: dict, *, paper_id: str = "p") -> Path:
    """在 tmp_path 造一份 capture 产物（与 remap capture 的形状一致）。"""
    md = Path(root) / "data" / "papers" / paper_id / "md"
    md.mkdir(parents=True, exist_ok=True)
    path = md / "ref_pids.json"
    path.write_text(json.dumps({"version": 1, "paper_id": paper_id,
                                "captured_at": "2026-01-01T00:00:00",
                                "paths": paths_codes}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _capture(codes: dict) -> dict:
    """ref_pids.json 里"路 a"的形状。"""
    return {"a": {"codes": codes}}


def _run(rows: list, chunk_rows: list, *, root, dry_run: bool = False) -> dict:
    sess = _FakeSession(rows, chunk_rows)
    return bf.backfill("p", dry_run=dry_run, root=root, session_factory=lambda: _SessionCtx(sess))


# --------------------------------------------------------------------------
# 1. 三级取值顺序
# --------------------------------------------------------------------------


def test_capture_pids_fill_when_chunks_have_no_pids(tmp_path):
    """本地论文的形态：chunk 表**没有** metadata.pids（没重建过索引），capture 产物里有。

    没有这一级的话，这类论文就只剩"跳过"—— 而它们本来就**不该**为了补 pids 去重建索引。
    """
    _write_capture(tmp_path, _capture({"A001": {"chunk_id": "c1", "pids": ["r-1", "r-2"]}}))
    row = _row(resolved={"A001": "c1"}, markdown="方法见 [A001]。")

    plan = _run([row], [_chunk("c1")], root=tmp_path)

    assert row.refs()["pids"] == {"A001": ["r-1", "r-2"]}
    assert plan["by_source"] == {"capture": 1, "chunk": 0, "inventory": 0}
    assert plan["result"] == {"written": 1, "codes": 1, "dry_run": False}


def test_falls_back_to_resolved_chunk_pids(tmp_path):
    """capture 产物缺这条 code（或没有产物）→ 退到 refs_json.resolved 那条 chunk 的 pids。"""
    _write_capture(tmp_path, _capture({}))
    row = _row(resolved={"A001": "c1"}, markdown="见 [A001]。")

    plan = _run([row], [_chunk("c1", ["r-9"])], root=tmp_path)

    assert row.refs()["pids"] == {"A001": ["r-9"]}
    assert plan["by_source"]["chunk"] == 1


def test_inventory_chunk_used_when_resolved_chunk_gone(tmp_path):
    """resolved 指向的 chunk 已经不在（旧 id）→ 用 inventory 里该 code 的 chunk_id 现查。"""
    row = _row(resolved={"A002": "c-old"}, markdown="见 [A002]。",
               inventory=[{"code": "A002", "chunk_id": "c-new", "chunk_type": "unit",
                           "page": 3}])

    plan = _run([row], [_chunk("c-new", ["r-7"])], root=tmp_path)

    assert row.refs()["pids"] == {"A002": ["r-7"]}
    assert plan["by_source"]["inventory"] == 1


def test_skip_when_nowhere_and_counted(tmp_path):
    """三级都取不到 → 跳过并计数（不编造 pids，也不写这个键）。"""
    row = _row(resolved={"A003": "c-x"}, markdown="见 [A003] 与 [A004]。")

    plan = _run([row], [], root=tmp_path)

    assert "pids" not in row.refs(), "取不到就不写这个键（与读路径同口径）"
    assert plan["fillable"] == 0 and plan["result"]["written"] == 0
    assert [it["code"] for it in plan["skipped"]] == ["A003", "A004"]


def test_broken_capture_file_is_ignored(tmp_path):
    """capture 产物损坏 → 当"没有这一级"，继续往下回落到 chunk 表（不报错）。"""
    md = Path(tmp_path) / "data" / "papers" / "p" / "md"
    md.mkdir(parents=True, exist_ok=True)
    (md / "ref_pids.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
    row = _row(resolved={"A001": "c1"}, markdown="见 [A001]。")

    plan = _run([row], [_chunk("c1", ["r-1"])], root=tmp_path)

    assert plan["capture_used"] is False
    assert row.refs()["pids"] == {"A001": ["r-1"]}


# --------------------------------------------------------------------------
# 2. 范围：只补正文里出现的短码
# --------------------------------------------------------------------------


def test_codes_not_cited_are_not_written(tmp_path):
    """正文里没出现的 code 一个都不写（inventory 是全篇材料，不是引用清单）。"""
    row = _row(resolved={"A001": "c1", "A002": "c2"}, markdown="只引用了 [A001]。",
               inventory=[{"code": "A002", "chunk_id": "c2"}])

    _run([row], [_chunk("c1", ["r-1"]), _chunk("c2", ["r-2"])], root=tmp_path)

    assert row.refs()["pids"] == {"A001": ["r-1"]}


def test_already_stored_pids_are_not_rewritten(tmp_path):
    """refs_json.pids 里已经有这条 → 算"已有"，不重写（读路径正在用的那份不覆盖）。"""
    _write_capture(tmp_path, _capture({"A001": {"chunk_id": "c1", "pids": ["r-new"]}}))
    row = _row(resolved={"A001": "c1"}, markdown="见 [A001]。", pids={"A001": ["r-old"]})

    plan = _run([row], [_chunk("c1", ["r-old"])], root=tmp_path)

    assert row.refs()["pids"] == {"A001": ["r-old"]} and row.writes == []
    assert plan["existing"] == 1 and plan["fillable"] == 0


# --------------------------------------------------------------------------
# 3. 写库边界
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    """--dry-run 一个字段都不赋值（连"改了但没提交"都不允许），但计数照算。"""
    _write_capture(tmp_path, _capture({"A001": {"chunk_id": "c1", "pids": ["r-1"]}}))
    row = _row(resolved={"A001": "c1"}, markdown="见 [A001]。")

    plan = _run([row], [_chunk("c1")], root=tmp_path, dry_run=True)

    assert row.writes == [], "dry-run 不许碰任何列"
    assert plan["result"] == {"written": 1, "codes": 1, "dry_run": True}


def test_only_refs_json_column_and_only_pids_key(tmp_path):
    """只写 refs_json 一列；inventory 一列与 refs 的其它键**一个都不动**。"""
    _write_capture(tmp_path, _capture({"A001": {"chunk_id": "c1", "pids": ["r-1"]}}))
    row = _row(resolved={"A001": "c1"}, markdown="见 [A001]。",
               inventory=[{"code": "A001", "chunk_id": "c1"}])
    before_refs, before_inventory = row.refs(), row.inventory

    _run([row], [_chunk("c1")], root=tmp_path)

    assert row.writes == ["refs_json"]
    assert row.inventory == before_inventory
    after = row.refs()
    assert set(after) == set(before_refs) | {"pids"}
    for key in ("codes", "resolved", "invalid", "cross_road", "ok"):
        assert after[key] == before_refs[key], f"{key} 被改动了"


def test_idempotent_second_run_changes_nothing(tmp_path):
    """第二次运行一个 code 都不改（已有 pids 不重写）—— 幂等。"""
    _write_capture(tmp_path, _capture({"A001": {"chunk_id": "c1", "pids": ["r-1"]}}))
    row = _row(resolved={"A001": "c1"}, markdown="见 [A001]。")

    first = _run([row], [_chunk("c1")], root=tmp_path)
    assert first["result"]["codes"] == 1
    row.writes.clear()

    second = _run([row], [_chunk("c1")], root=tmp_path)

    assert second["result"] == {"written": 0, "codes": 0, "dry_run": False}
    assert row.writes == []
    assert second["existing"] == 1


# --------------------------------------------------------------------------
# 4. 汇总与退出码
# --------------------------------------------------------------------------


def test_summary_splits_the_four_counts(tmp_path):
    """汇总里"来自 capture / chunk / inventory / 跳过"四个数分开（一眼看出论文是哪一类）。"""
    _write_capture(tmp_path, _capture({"A001": {"chunk_id": "c1", "pids": ["r-1"]}}))
    row_a = _row("a", resolved={"A001": "c1"}, markdown="见 [A001]。")
    row_b = _row("b", resolved={"A002": "c2", "A003": "c-gone"}, markdown="[A002] [A003]",
                 inventory=[{"code": "A003", "chunk_id": "c3"}])
    plan = bf.plan_backfill(
        "p",
        [{"path": r.path, "refs": r.refs(), "inventory": json.loads(r.inventory),
          "markdown": r.markdown} for r in (row_a, row_b)],
        {"c1": ["r-1"], "c2": ["r-2"], "c3": ["r-3"]},
        json.loads(Path(tmp_path / "data/papers/p/md/ref_pids.json").read_text("utf-8")))

    line = bf.summary_line(plan)

    assert "可补 3" in line and "跳过 0" in line
    assert "来自 capture 1" in line
    assert "来自 chunk 1" in line
    assert "来自 inventory 1" in line


def test_main_returns_zero_even_with_skips(monkeypatch, capsys, tmp_path):
    """有跳过（缺口）→ 退出码仍是 0，但汇总里要看得见"跳过"。"""
    row = _row(resolved={"A003": "c-x"}, markdown="见 [A003]。")
    sess = _FakeSession([row], [])
    monkeypatch.setattr(bf, "_session_scope", lambda: (lambda: _SessionCtx(sess)))

    code = bf.main(["p", "--root", str(tmp_path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "跳过 1" in out and "A003" in out


def test_main_returns_two_when_paper_missing(monkeypatch, capsys, tmp_path):
    """读不到论文（没有 understanding 记录）→ 退出码 2。"""
    sess = _FakeSession([], [])
    monkeypatch.setattr(bf, "_session_scope", lambda: (lambda: _SessionCtx(sess)))

    code = bf.main(["no-such-paper", "--root", str(tmp_path)])

    assert code == 2
    assert "读不到论文" in capsys.readouterr().out
