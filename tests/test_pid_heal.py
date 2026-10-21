"""退休身份的就地愈合（`row_identity.heal_pid` + 读路径 `_heal_pids`）。

重译 / 重排会把"这一行的文字"切成不同的行，旧 pid 于是退休（reason=gone），引用只能退到
跳整页（实测 turboquant 补完附录后 104 条里有 11 条掉到这一步）。退休记录留着指纹与页号，
所以后继是确定性地找得回来的 —— 这里钉住"找得回 / 不硬猜"四个边界。
"""

from __future__ import annotations

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
