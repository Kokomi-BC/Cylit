"""退休身份的就地愈合（`row_identity.heal_pid` + 读路径 `_heal_pids`）。

重译 / 重排会把"这一行的文字"切成不同的行，旧 pid 于是退休（reason=gone），引用只能退到
跳整页（实测 turboquant 补完附录后 104 条里有 11 条掉到这一步）。退休记录留着指纹与页号，
所以后继是确定性地找得回来的 —— 这里钉住"找得回 / 不硬猜"四个边界。
"""

from __future__ import annotations

import pytest

from backend.services.paper import row_identity
from backend.services.understanding.understanding_service import _heal_pids


def _reg(live=(), retired=()) -> dict:
    return {"rows": list(live), "retired": list(retired)}


def _fp(text: str) -> str:
    return row_identity.fingerprint({"original": text})


OLD = "Thus we aim to design quantizers that for any distribution minimize distortion"


def _row(pid: str, page: int, text: str, rid: str = "") -> dict:
    return {"pid": pid, "id": rid or f"p{page:03d}-00", "page": page, "original": text}


def test_heal_pid_finds_the_successor_on_the_same_page():
    old = {"pid": "r-old", "page": 3, "fp": _fp(OLD), "reason": "gone"}
    new = _row("r-new", 3, OLD + " rate and inner product estimation", "p003-01")
    hit = row_identity.heal_pid(_reg([new], [old]), "r-old", [new])
    assert hit is not None
    assert hit["pid"] == "r-new" and hit["row_id"] == "p003-01" and hit["page"] == 3
    assert hit["similarity"] >= row_identity.MATCH_THRESHOLD


def test_heal_pid_refuses_when_the_text_changed_too_much():
    old = {"pid": "r-old", "page": 3, "fp": _fp(OLD), "reason": "gone"}
    new = _row("r-new", 3, "**Table 2: BLEU scores on newstest2014.**")
    assert row_identity.heal_pid(_reg([new], [old]), "r-old", [new]) is None


def test_heal_pid_never_replaces_a_live_identity():
    alive = _row("r-live", 3, OLD)
    assert row_identity.heal_pid(_reg([alive]), "r-live", [alive]) is None


def test_heal_pid_only_looks_within_the_same_page():
    """跨页同文多半是页眉 / 水印：拿它当后继会把引用指到别页去。"""
    old = {"pid": "r-old", "page": 3, "fp": _fp(OLD), "reason": "gone"}
    other = _row("r-other", 4, OLD)
    assert row_identity.heal_pid(_reg([other], [old]), "r-old", [other]) is None


def test_heal_pid_needs_a_registered_fingerprint():
    assert row_identity.heal_pid(_reg(), "r-unknown", [_row("r-new", 3, OLD)]) is None


def _assign_page(tmp_path, rows: list[dict]) -> dict:
    """跑一次真实的身份分配（写 tmp 的注册表，不碰 data/）。"""
    row_identity.assign("p1", 3, rows, root=tmp_path)
    return row_identity.load("p1", root=tmp_path)


def test_retirement_records_the_slot_and_its_successor(tmp_path):
    """重切之后必须留下"这条身份原来在第几行、那里现在是谁"。"""
    v1 = [{"id": "p003-00", "original": OLD},
          {"id": "p003-01", "original": "Second paragraph of the original split."}]
    v2 = [{"id": "p003-00", "original": OLD},
          {"id": "p003-01", "original": "Second paragraph of the original split, cut later."},
          {"id": "p003-02", "original": "A brand new row that did not exist before."}]
    first = _assign_page(tmp_path, v1)
    old_pid = first["rows"][1]["pid"]
    _assign_page(tmp_path, v2)
    reg = row_identity.load("p1", root=tmp_path)
    retired = {e["pid"]: e for e in reg["retired"]}
    if old_pid in retired:                        # 相似度过阈值时会被认领，不算退休
        entry = retired[old_pid]
        assert entry["order"] == 1
        assert entry["successor"], "退休条目必须记下槽位上的新行"
        assert isinstance(entry["successor_similarity"], float)


def test_heal_pid_follows_a_trusted_successor():
    """映射可信（过 0.5）时按表跳，哪怕与任何活行的相似度都低。"""
    rows = [_row("r-new", 3, "Completely different wording appears here now.", "p003-01")]
    retired = [{"pid": "r-old", "page": 3, "order": 1, "fp": _fp(OLD),
                "successor": "r-new", "successor_similarity": 0.62, "reason": "gone"}]
    hit = row_identity.heal_pid(_reg(rows, retired), "r-old", rows)
    assert hit and hit["pid"] == "r-new" and hit["row_id"] == "p003-01"


def test_heal_pid_ignores_an_untrusted_successor():
    """相似度只有 0.4 的"槽位新行"不是后继 —— 顺着它绑就是编造精确度。"""
    rows = [_row("r-new", 3, "Completely different wording appears here now.", "p003-01")]
    retired = [{"pid": "r-old", "page": 3, "order": 1, "fp": _fp(OLD),
                "successor": "r-new", "successor_similarity": 0.40, "reason": "gone"}]
    assert row_identity.heal_pid(_reg(rows, retired), "r-old", rows) is None


def test_partial_pids_are_pruned_so_the_rest_stays_row_level():
    """一条引用带多个 pid 时，失效的那几个只该被剪掉，不该把整条拖进 stale。"""
    from backend.services.understanding import understanding_service as us

    rows = [{"pid": "r-live", "id": "p003-01", "page": 3, "original": "A surviving row."}]
    registry = {"rows": [{"pid": "r-live", "page": 3, "order": 0,
                         "fp": _fp("A surviving row.")}],
                "retired": [{"pid": "r-gone", "page": 3, "fp": _fp("A vanished row.")}]}
    pids = ["r-live", "r-gone"]
    verdict = us._resolve_registry(registry, pids, rows)
    assert [verdict[p]["state"] for p in pids] == ["ok", "gone"]
    kept = [p for p in pids if verdict[p]["state"] == "ok"]
    keeps = us._resolve_registry(registry, kept, rows)
    assert kept == ["r-live"] and keeps["r-live"]["row_id"] == "p003-01"


def test_heal_pids_is_all_or_nothing():
    """认不回来的 pid 只要还剩一个，整条引用就整体放弃（半恢复 = 看着精确、其实指错行）。"""
    old = {"pid": "r-old", "page": 3, "fp": _fp(OLD), "reason": "gone"}
    new = _row("r-new", 3, OLD + " rate", "p003-01")
    pids, healed = _heal_pids(_reg([new], [old]), ["r-old", "r-lost"], [new],
                              ["gone", "gone"])
    assert healed == {} and pids == ["r-old", "r-lost"]
    pids, healed = _heal_pids(_reg([new], [old]), ["r-old"], [new], ["gone"])
    assert healed and pids == ["r-new"]


def test_heal_pids_keeps_the_live_half_untouched():
    """`ok` 的 pid 原样留着：它本来就不该被替代，也不该挡住另一半愈合。"""
    alive = _row("r-live", 3, "Something else entirely.")
    old = {"pid": "r-old", "page": 3, "fp": _fp(OLD), "reason": "gone"}
    new = _row("r-new", 3, OLD + " rate", "p003-01")
    pids, healed = _heal_pids(_reg([alive, new], [old]), ["r-live", "r-old"],
                              [alive, new], ["ok", "gone"])
    assert pids == ["r-live", "r-new"] and set(healed) == {"r-old"}
