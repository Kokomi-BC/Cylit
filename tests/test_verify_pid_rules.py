"""verify_pid_rules.py 的 11 条检查（全部离线：构造数据 + 注入的 rows/registry，不连库）。

重点钉住三件事：

1. **判定是纯函数**：A/C/D 直接喂 dict，B 组用 ``make_splitter`` 注入 ``rows`` /
   ``registry`` —— 走的仍是真实的 ``understanding_service.split_refs_by_pids``，
   只是不做任何 IO，所以故障注入验的就是读路径本身；
2. **每条检查的 FAIL 条件真的会 FAIL**（不是只会打印 PASS）：身份丢了 / 注册表对不上行 /
   pids 与 row_ids 不等长 / 打坏 chunk id 后未自愈 / 死 pid 标成 healed / 换了 pid 不跟着走 /
   注册表被改动 / 规划不幂等 / healed 项丢了旧字段，各有一条反例；
3. **退出码**：任一检查 FAIL → ``main()`` 返回 1（``_load_paper_ids`` / ``_load_context``
   被换成构造数据，不碰真实 ``data/`` 与库）。
"""

from __future__ import annotations

import pytest

from backend.services.paper import row_identity
from scripts import backfill_ref_pids as bf
from scripts import verify_pid_rules as vr

# --------------------------------------------------------------------------
# 构造数据：三行（两页）、三条 chunk、三条带行身份的引用
# --------------------------------------------------------------------------

MARKDOWN = "方法见 [A001]，动机见 [A002]，公式在 [A003]。"
CHUNK_UNIT_1 = "p:a:unit:p001-00-p001-00:en"
CHUNK_UNIT_2 = "p:a:unit:p001-01-p001-01:en"
CHUNK_FORMULA = "p:a:formula:p002-00:en"


def _rows() -> list[dict]:
    return [
        {"id": "p001-00", "page": 1, "index": 0, "original": "Alpha",
         "translation": "甲", "pid": "r-000000000000000a"},
        {"id": "p001-01", "page": 1, "index": 1, "original": "Beta",
         "translation": "乙", "pid": "r-000000000000000b"},
        {"id": "p002-00", "page": 2, "index": 0, "original": "Gamma",
         "translation": "丙", "pid": "r-000000000000000c"},
    ]


def _registry(rows: list[dict] | None = None, retired: list | None = None) -> dict:
    rows = _rows() if rows is None else rows
    return {"version": 1,
            "rows": [{"pid": r["pid"], "page": r["page"], "order": r["index"],
                      "fp": row_identity.fingerprint(r)} for r in rows],
            "retired": retired or []}


def _anchors(with_pids: bool = True) -> dict:
    """``_chunk_anchors`` 的形状；``with_pids=False`` 就是**当前库里**的老索引
    （chunk 还没带 metadata.pids，正是 D11 会红的那个状态）。"""
    out = {
        CHUNK_UNIT_1: {"block_id": "p001-00", "row_id": "p001-00", "latex": None,
                       "pids": ["r-000000000000000a"] if with_pids else None},
        CHUNK_UNIT_2: {"block_id": "p001-01", "row_id": "p001-01", "latex": None,
                       "pids": ["r-000000000000000b"] if with_pids else None},
        CHUNK_FORMULA: {"block_id": "row-p002-00", "row_id": "p002-00", "latex": "x = 1",
                        "pids": ["r-000000000000000c"] if with_pids else None},
    }
    return out


def _by_code() -> dict:
    return {
        "A001": {"code": "A001", "chunk_id": CHUNK_UNIT_1, "page": 1, "chunk_type": "unit"},
        "A002": {"code": "A002", "chunk_id": CHUNK_UNIT_2, "page": 1, "chunk_type": "unit"},
        "A003": {"code": "A003", "chunk_id": CHUNK_FORMULA, "page": 2, "chunk_type": "formula"},
    }


def _resolved() -> dict:
    return {"A001": CHUNK_UNIT_1, "A002": CHUNK_UNIT_2, "A003": CHUNK_FORMULA}


def _refs_pids(drop: str | None = None) -> dict:
    out = {"A001": ["r-000000000000000a"], "A002": ["r-000000000000000b"],
           "A003": ["r-000000000000000c"]}
    if drop:
        out.pop(drop, None)
    return out


def _payload(*, resolved: dict | None = None, refs_pids: dict | None = None,
             anchors: dict | None = None, markdown: str = MARKDOWN) -> dict:
    """一路解读的检查入参（与 ``_load_paths`` 给的那个形状一致）。"""
    return {"path": "a", "markdown": markdown,
            "refs": {"resolved": resolved if resolved is not None else _resolved(),
                     "pids": refs_pids if refs_pids is not None else _refs_pids()},
            "inventory": list(_by_code().values()),
            "resolved": resolved if resolved is not None else _resolved(),
            "by_code": _by_code(),
            "anchors": anchors if anchors is not None else _anchors(),
            "refs_pids": refs_pids if refs_pids is not None else _refs_pids()}


def _chunks(with_pids: bool = True) -> list[dict]:
    meta = {CHUNK_UNIT_1: ["p001-00"], CHUNK_UNIT_2: ["p001-01"],
            CHUNK_FORMULA: ["p002-00"]}
    out = []
    for cid, row_ids in meta.items():
        metadata = {"row_ids": row_ids}
        if with_pids:
            metadata["pids"] = list((_anchors())[cid]["pids"])
        out.append({"id": cid, "chunk_type": "unit", "metadata": metadata})
    return out


def _ctx(*, anchors_pids: bool = True) -> dict:
    """``_load_context`` 的假版本（``main`` 的用例靠它，不碰库与 data/）。"""
    rows = _rows()
    payload = _payload(anchors=_anchors(anchors_pids))
    stat = {"mtime_ns": 111, "size": 222, "sha256": "deadbeef"}
    return {"shape": "paths", "rows": rows, "rows_again": [dict(r) for r in rows],
            "registry": _registry(rows), "registry_path": "unused",
            "registry_stat": stat, "registry_stat_again": dict(stat),
            "chunks": _chunks(anchors_pids),
            "chunk_pids": vr.chunk_pid_map(_chunks(anchors_pids)),
            "paths": [payload], "capture": None,
            "payloads": [{"path": "a", "refs": payload["refs"],
                          "inventory": payload["inventory"], "markdown": MARKDOWN}]}


@pytest.fixture()
def split():
    """读路径的注入点（rows / registry 直接给，不读 data/）。"""
    return vr.make_splitter("p", _rows(), _registry())


# --------------------------------------------------------------------------
# A. 不变量
# --------------------------------------------------------------------------


def test_a1_pid_coverage_ok_and_fail():
    assert vr.check_pid_coverage(_rows())["ok"] is True

    missing = _rows()
    missing[1]["pid"] = ""
    got = vr.check_pid_coverage(missing)
    assert got["ok"] is False and "没有 pid" in got["evidence"]

    dup = _rows()
    dup[2]["pid"] = dup[0]["pid"]
    got = vr.check_pid_coverage(dup)
    assert got["ok"] is False and "重复" in got["evidence"]

    assert vr.check_pid_coverage([])["ok"] is False       # 读不到行 = 无从校验


def test_a2_registry_one_to_one():
    rows = _rows()
    assert vr.check_registry_rows(rows, _registry(rows))["ok"] is True

    # 注册表条目落在不存在的 (页, 序位) 上 → slot 消失
    bad = _registry(rows)
    bad["rows"][2]["order"] = 9
    got = vr.check_registry_rows(rows, bad)
    assert got["ok"] is False and "找不到行" in got["evidence"]

    # retired 里的 pid 又出现在活行上 → 身份被重绑
    retired = [{"pid": rows[0]["pid"], "page": 1, "order": 0, "fp": "x",
                "retired_at": "2026-01-01T00:00:00", "reason": "gone"}]
    got = vr.check_registry_rows(rows, _registry(rows, retired=retired))
    assert got["ok"] is False and "已退休" in got["evidence"]


def test_a3_chunk_pids_alignment():
    rows = _rows()
    assert vr.check_chunk_pids(_chunks(), _registry(rows))["ok"] is True

    # 整个字段缺失（老索引）不算失败，但证据里要说清"无从检验"
    got = vr.check_chunk_pids(_chunks(with_pids=False), _registry(rows))
    assert got["ok"] is True and "没有该字段" in got["evidence"]

    # length: pids + pids_missing 必须等于 row_ids 条数
    bad = _chunks()
    bad[0]["metadata"]["pids"] = []
    bad[0]["metadata"]["row_ids"] = ["p001-00", "p001-01"]
    got = vr.check_chunk_pids(bad, _registry(rows))
    assert got["ok"] is False and "同序等长" in got["evidence"]

    # pids_missing 按口径计入后可以过
    ok = _chunks()
    ok[0]["metadata"]["pids"] = []
    ok[0]["metadata"]["row_ids"] = ["p001-00", "p001-01"]
    ok[0]["metadata"]["pids_missing"] = 2
    assert vr.check_chunk_pids(ok, _registry(rows))["ok"] is True

    # pid 不在注册表里
    unknown = _chunks()
    unknown[0]["metadata"]["pids"] = ["r-0000000000000fff"]
    got = vr.check_chunk_pids(unknown, _registry(rows))
    assert got["ok"] is False and "找不到" in got["evidence"]


def test_a4_ref_coverage():
    rows, registry = _rows(), _registry()
    chunk_pids = vr.chunk_pid_map(_chunks())
    assert vr.check_ref_coverage(rows, registry, chunk_pids, [_payload()])["ok"] is True

    # refs_json.pids 缺一条、且那条 chunk 也没带 pids → 覆盖率不足 100%
    payload = _payload(refs_pids=_refs_pids(drop="A001"),
                       anchors=_anchors(with_pids=False))
    got = vr.check_ref_coverage(rows, registry, {}, [payload])
    assert got["ok"] is False and "覆盖 2/3" in got["evidence"]

    # 有身份但认不回原行（注册表里没有这个 pid）
    got = vr.check_ref_coverage(
        rows, _registry(rows[:-1]), chunk_pids,
        [_payload(refs_pids={"A001": ["r-000000000000000a"],
                             "A002": ["r-000000000000000b"],
                             "A003": ["r-0000000000000fff"]})])
    assert got["ok"] is False and "认不回原行" in got["evidence"]


# --------------------------------------------------------------------------
# B. 故障注入（走真实的 split_refs_by_pids，只是没有 IO）
# --------------------------------------------------------------------------


def test_b5_heal_single_chunk_id(split):
    got = vr.check_heal_single(split, [_payload()])
    assert got["ok"] is True and "3 条" in got["evidence"]

    # 注册表少了 A003 的身份 → 那一条打坏 chunk id 后只能按旧 id 落地（未自愈）
    blind = vr.make_splitter("p", _rows(), _registry(_rows()[:-1]))
    got = vr.check_heal_single(blind, [_payload()])
    assert got["ok"] is False and "未自愈" in got["evidence"]

    # 一条带身份的引用都没有 = 无从证明
    got = vr.check_heal_single(split, [_payload(refs_pids={}, anchors=_anchors(False))])
    assert got["ok"] is False and "样本 0" in got["evidence"]


def test_b6_heal_all_chunk_ids(split):
    got = vr.check_heal_all(split, [_payload()])
    assert got["ok"] is True and "100%" in got["evidence"]

    blind = vr.make_splitter("p", _rows(), _registry(_rows()[:-1]))
    got = vr.check_heal_all(blind, [_payload()])
    assert got["ok"] is False and "2/3 条带身份引用仍精确" in got["evidence"]

    got = vr.check_heal_all(split, [_payload(refs_pids={}, anchors=_anchors(False))])
    assert got["ok"] is False and "样本 0" in got["evidence"]


def test_b7_dead_pid_degrades_without_jumping(split):
    got = vr.check_dead_pid(split, [_payload()])
    assert got["ok"] is True and "无一标 healed" in got["evidence"]

    def lying_split(payload, refs_pids):
        # 死 pid 也敢标 healed 并给出一个行 = "乱跳"，必须被抓到
        return ([{"code": "A001", "chunk_id": "", "page": 9, "row_id": "p009-09",
                  "healed": True}], [])

    got = vr.check_dead_pid(lying_split, [_payload()])
    assert got["ok"] is False and "healed" in got["evidence"]


def test_b8_foreign_pid_follows_target_row(split):
    got = vr.check_foreign_pid(split, _rows(), _registry(), [_payload()])
    assert got["ok"] is True
    assert "p001-00 → p001-01" in got["evidence"]      # 两个行 id 都写进证据
    assert "→ p001-01" in got["evidence"]

    def ignoring_split(payload, refs_pids):
        # 忽略传入的 pids，永远落回自己那一行 = 不是"跟着 pid 走"
        return ([{"code": code, "chunk_id": CHUNK_UNIT_1, "page": 1,
                  "row_id": "p001-00", "healed": True}
                 for code in ("A001", "A002", "A003")], [])

    got = vr.check_foreign_pid(ignoring_split, _rows(), _registry(), [_payload()])
    assert got["ok"] is False and "没跟着 pid 走" in got["evidence"]


# --------------------------------------------------------------------------
# C. 确定性 / 幂等
# --------------------------------------------------------------------------


def test_c9_read_stability(tmp_path):
    path = tmp_path / "row_ids.json"
    path.write_text('{"version": 1}', encoding="utf-8")
    first = vr.file_stat(path)
    first_map = vr.pid_map(_rows())
    assert vr.check_read_stability(first_map, dict(first_map), first,
                                   vr.file_stat(path))["ok"] is True

    # 行 → pid 抖动
    second_map = dict(first_map)
    second_map["p001-00"] = "r-00000000000000ff"
    got = vr.check_read_stability(first_map, second_map, first, vr.file_stat(path))
    assert got["ok"] is False and "pid 变了" in got["evidence"]

    # 注册表被改写（字节数与 mtime 都变了）
    path.write_text('{"version": 1, "rows": []}', encoding="utf-8")
    got = vr.check_read_stability(first_map, dict(first_map), first, vr.file_stat(path))
    assert got["ok"] is False and "注册表文件被改动" in got["evidence"]


def test_c10_backfill_dryrun_is_idempotent(monkeypatch):
    payloads = [{"path": "a", "markdown": MARKDOWN,
                 "refs": {"resolved": _resolved(),
                          "pids": _refs_pids(drop="A001")},
                 "inventory": list(_by_code().values())}]
    capture = {"paths": {"a": {"codes": {"A001": {"pids": ["r-000000000000000a"]}}}}}

    got = vr.check_backfill_idempotent("p", payloads, {}, capture)
    assert got["ok"] is True and "第一次可补 1 条" in got["evidence"]
    # 断言"第二次为 0"是这条检查的核心：证据里必须出现
    assert "第二次可补 0 条" in got["evidence"]

    # 规划器不幂等（第二次还在补）必须被发现
    monkeypatch.setattr(bf, "plan_backfill",
                        lambda *a, **k: {"paths": {"a": {"items": [
                            {"code": "A001", "pids": [], "source": "",
                             "action": "fill"}]}},
                            "fillable": 1, "existing": 0, "skipped": []})
    got = vr.check_backfill_idempotent("p", payloads, {}, capture)
    assert got["ok"] is False and "不幂等" in got["evidence"]


# --------------------------------------------------------------------------
# D. 与改造前行为等价
# --------------------------------------------------------------------------


def test_d11_legacy_equivalence(split):
    # chunk 带 metadata.pids（重建索引后的形态）：healed 项把旧字段全部保留
    got = vr.check_legacy_equivalence(split, [_payload()])
    assert got["ok"] is True and "对比 3 条" in got["evidence"]
    assert "原样保留" in got["evidence"]

    # chunk 还没带 pids（当前库的形态）：chunk_id / block_id 被清空 = 不等价，
    # 证据里要同时写清"丢了哪些字段"与"新增字段只有 pids"
    got = vr.check_legacy_equivalence(split, [_payload(anchors=_anchors(False))])
    assert got["ok"] is False
    assert "chunk_id" in got["evidence"] and "没保留旧逻辑的字段" in got["evidence"]
    assert "新增字段 ['pids']" in got["evidence"]


# --------------------------------------------------------------------------
# 装配与退出码
# --------------------------------------------------------------------------


def test_verify_paper_runs_all_eleven_checks():
    checks = vr.verify_paper("p", _ctx())
    assert [c["check"] for c in checks] == list(vr.CHECK_NAMES)
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


def test_main_returns_1_when_a_check_fails(monkeypatch, capsys):
    from scripts import verify_pid_rules as module

    monkeypatch.setattr(module, "_load_paper_ids", lambda: ["p"])
    monkeypatch.setattr(module, "_load_context", lambda paper_id: _ctx())
    assert module.main([]) == 0                    # 全 PASS → 0

    # 老索引形态（chunk 无 metadata.pids）→ D11 FAIL → 退出码 1
    monkeypatch.setattr(module, "_load_context",
                        lambda paper_id: _ctx(anchors_pids=False))
    assert module.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] D11" in out and "[PASS] A1" in out
    assert "FAIL=1" in out


def test_main_json_payload_and_help(monkeypatch, capsys):
    from scripts import verify_pid_rules as module

    monkeypatch.setattr(module, "_load_paper_ids", lambda: ["p"])
    monkeypatch.setattr(module, "_load_context", lambda paper_id: _ctx())
    assert module.main(["--json"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["summary"]["checks"] == len(module.CHECK_NAMES)
    assert payload["summary"]["fail"] == 0

    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
