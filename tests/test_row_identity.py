"""行身份注册表（row_identity）—— 纯函数 + 文件读写。

钉住 docs/身份模型.md §2/§3/§4 的不变量：
- pid 生成一次、重建时继承、**永不重绑**；对不上的旧 pid 进 retired；
- 同一页同一批行连着 assign 两次，第二次**不写盘**；
- 注册表缺失/损坏不抛；写入原子、保留未知字段（forward-compat）。

文件读写全部落在 tmp_path（assign/load/save 的 root 参数），不写真实 data/。
"""

from __future__ import annotations

import json

import pytest

from backend.services.paper import row_identity as ri


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _rows(*texts: str) -> list[dict]:
    return [{"id": f"p015-{i:02d}", "original": t, "translation": ""}
            for i, t in enumerate(texts)]


def _registry(paper_id: str, root) -> dict:
    return ri.load(paper_id, root=root)


def _raw(paper_id: str, root):
    return ri.registry_path(paper_id, root=root).read_bytes()


# --------------------------------------------------------------------------
# ① 空注册表 → 全部新建且唯一
# --------------------------------------------------------------------------


def test_empty_registry_creates_unique_pids():
    rows = _rows("attention is all you need", "we use eight heads", "training on four gpus")
    plan = ri.plan_page(rows, [])

    assert len(plan["pids"]) == 3
    assert plan["reused"] == 0
    assert plan["retired"] == []
    assert len(set(plan["pids"])) == 3
    for pid in plan["pids"]:
        assert pid.startswith("r-") and len(pid) == 18
        assert all(c in "0123456789abcdef" for c in pid[2:])
    assert [c["order"] for c in plan["created"]] == [0, 1, 2]
    for entry in plan["created"]:
        assert set(entry) == {"pid", "order", "fp", "created_at"}
        assert entry["fp"] == ri.text_fingerprint(ri.row_text(rows[entry["order"]]))


# --------------------------------------------------------------------------
# ② 同文本再来一次 → pid 全等，且**未写盘**
# --------------------------------------------------------------------------


def test_same_rows_reuse_pids_without_writing(tmp_path, monkeypatch):
    rows = _rows("attention is all you need", "we use eight heads")
    first = ri.assign("p1", 15, rows, root=tmp_path)
    assert len(set(first)) == 2

    writes: list[str] = []
    real_save = ri.save
    monkeypatch.setattr(ri, "save", lambda *a, **kw: (writes.append("save"), real_save(*a, **kw))[1])

    before_bytes = _raw("p1", tmp_path)
    before_mtime = ri.registry_path("p1", root=tmp_path).stat().st_mtime_ns

    second = ri.assign("p1", 15, rows, root=tmp_path)

    assert second == first
    assert writes == [], "第二次不该写盘"
    assert _raw("p1", tmp_path) == before_bytes
    assert ri.registry_path("p1", root=tmp_path).stat().st_mtime_ns == before_mtime


# --------------------------------------------------------------------------
# ③ 中间插入一行 → 前 2 条不变，新行新建，后面的行**跟着原段落**走
# --------------------------------------------------------------------------


def test_insert_middle_row_keeps_following_pids():
    a, b, c = ("attention is all you need", "the model uses eight heads",
               "we train on four gpus for a week")
    p1, p2, p3 = ri.plan_page(_rows(a, b, c), [])["pids"]

    existing = [
        {"pid": p1, "order": 0, "fp": ri.fingerprint({"original": a})},
        {"pid": p2, "order": 1, "fp": ri.fingerprint({"original": b})},
        {"pid": p3, "order": 2, "fp": ri.fingerprint({"original": c})},
    ]
    plan = ri.plan_page(_rows(a, b, "a brand new sentence nobody wrote before", c), existing)

    assert plan["pids"][0] == p1
    assert plan["pids"][1] == p2
    assert plan["pids"][2] not in {p1, p2, p3}, "插入行必须是新 pid"
    assert plan["pids"][3] == p3, "原来的第 3 行要跟着段落走，不是顺移成第 4 行"
    assert plan["created"][0]["order"] == 2
    assert plan["retired"] == []
    assert plan["reused"] == 3, "三条旧行都续用，只有插入行是新的"


# --------------------------------------------------------------------------
# ④ 改一个字的同一行 → 仍继承原 pid（相似度过阈值）
# --------------------------------------------------------------------------


def test_one_char_edit_inherits_pid(tmp_path):
    rows = _rows("attention is all you need", "we use eight heads")
    pids = ri.assign("p1", 15, rows, root=tmp_path)

    edited = _rows("attention is all you nees", "we use eight heads")  # 末字 y → s
    assert ri.similarity(ri.fingerprint(rows[0]), ri.fingerprint(edited[0])) >= ri.MATCH_THRESHOLD
    again = ri.assign("p1", 15, edited, root=tmp_path)

    assert again == pids
    reg = _registry("p1", tmp_path)
    assert [e["pid"] for e in reg["rows"]] == pids
    assert reg["retired"] == []
    assert reg["rows"][0]["fp"] == ri.fingerprint(edited[0]), "继承后指纹要跟着新文本更新"


# --------------------------------------------------------------------------
# ⑤ 删掉一行 → 该 pid 进 retired，reason = gone
# --------------------------------------------------------------------------


def test_deleted_row_is_retired_as_gone(tmp_path):
    rows = _rows("attention is all you need", "we use eight heads", "training on four gpus")
    pids = ri.assign("p1", 15, rows, root=tmp_path)

    kept = ri.assign("p1", 15, [rows[0], rows[2]], root=tmp_path)

    assert kept == [pids[0], pids[2]]
    reg = _registry("p1", tmp_path)
    assert [e["pid"] for e in reg["rows"]] == kept
    assert len(reg["retired"]) == 1
    gone = reg["retired"][0]
    assert gone["pid"] == pids[1]
    assert gone["page"] == 15 and gone["reason"] == "gone"
    assert set(gone) == {"pid", "page", "fp", "retired_at", "reason"}


# --------------------------------------------------------------------------
# ⑤b 同一槽位文本大改（相似度过低）→ retired reason = text-changed
# --------------------------------------------------------------------------


def test_heavily_edited_row_is_retired_as_text_changed():
    old = "x" * 48
    new = "x" * 20 + "y" * 28
    pid = ri.plan_page(_rows(old), [])["pids"][0]
    existing = [{"pid": pid, "order": 0, "fp": ri.text_fingerprint(old)}]

    sim = ri.similarity(ri.text_fingerprint(old), ri.text_fingerprint(new))
    assert ri.CHANGED_THRESHOLD <= sim < ri.MATCH_THRESHOLD
    plan = ri.plan_page(_rows(new), existing)
    assert plan["retired"] == [pid]
    assert plan["pids"][0] != pid, "低相似度不认领，必须新建"


# --------------------------------------------------------------------------
# ⑥ 整页换内容 → 全部新建、旧 pid 全退休
# --------------------------------------------------------------------------


def test_full_page_replacement_creates_all_and_retires_all(tmp_path):
    old = _rows("attention is all you need", "we use eight heads", "training on four gpus")
    old_pids = ri.assign("p1", 15, old, root=tmp_path)

    new = _rows("论文的中文摘要在下面", "第二段讲实验设置", "第三段讲结论")
    new_pids = ri.assign("p1", 15, new, root=tmp_path)

    assert len(set(new_pids)) == 3
    assert not set(new_pids) & set(old_pids)
    reg = _registry("p1", tmp_path)
    assert [e["pid"] for e in reg["rows"]] == new_pids
    assert sorted(e["pid"] for e in reg["retired"]) == sorted(old_pids)
    assert {e["reason"] for e in reg["retired"]} == {"gone"}


# --------------------------------------------------------------------------
# ⑦ 新旧无关但同长度 → 不认领（绝不把旧 pid 重绑到另一段）
# --------------------------------------------------------------------------


def test_unrelated_same_length_text_is_not_claimed():
    old = "a" * 40
    new = "b" * 40
    pid = ri.plan_page(_rows(old), [])["pids"][0]
    existing = [{"pid": pid, "order": 0, "fp": ri.text_fingerprint(old)}]

    plan = ri.plan_page(_rows(new), existing)

    assert ri.similarity(ri.text_fingerprint(old), ri.text_fingerprint(new)) < ri.MATCH_THRESHOLD
    assert plan["pids"][0] != pid
    assert plan["reused"] == 0
    assert plan["retired"] == [pid]


# --------------------------------------------------------------------------
# ⑧ 文件损坏 / 缺失 → load 返回空注册表，不抛
# --------------------------------------------------------------------------


def test_load_tolerates_missing_and_corrupt_file(tmp_path):
    assert _registry("ghost", tmp_path) == {"version": 1, "rows": [], "retired": []}

    path = ri.registry_path("p1", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_bytes(b"{not json at all")
    assert _registry("p1", tmp_path) == {"version": 1, "rows": [], "retired": []}

    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")  # 顶层不是 dict
    assert _registry("p1", tmp_path) == {"version": 1, "rows": [], "retired": []}

    path.write_text(json.dumps({"version": 1, "rows": "oops"}), encoding="utf-8")
    reg = _registry("p1", tmp_path)
    assert reg["rows"] == [] and reg["retired"] == []

    # 损坏之后的 assign 照样能干活（现建现用）
    pids = ri.assign("p1", 15, _rows("hello world"), root=tmp_path)
    assert len(pids) == 1 and pids[0].startswith("r-")


def test_registry_path_uses_root_for_tests(tmp_path):
    assert ri.registry_path("p1", root=tmp_path) == tmp_path / "p1" / "md" / "row_ids.json"
    assert ri.registry_path("p1").name == "row_ids.json"
    assert ri.registry_path("p1").parent.name == "md"


# --------------------------------------------------------------------------
# ⑨ save → load 往返一致，且未知字段原样保留
# --------------------------------------------------------------------------


def test_save_load_roundtrip_preserves_unknown_fields(tmp_path):
    reg = {
        "version": 1,
        "x_meta": {"writer": "stage-1a", "note": "未知字段要留着"},
        "rows": [
            {"pid": "r-0000000000000001", "page": 15, "order": 0,
             "fp": "abc", "created_at": "2026-01-01T00:00:00", "source": "legacy"},
        ],
        "retired": [
            {"pid": "r-0000000000000002", "page": 15, "fp": "def",
             "retired_at": "2026-01-01T00:00:00", "reason": "gone", "why": "手工退休"},
        ],
    }
    ri.save("p1", reg, root=tmp_path)
    loaded = ri.load("p1", root=tmp_path)

    assert loaded == reg
    ri.save("p1", loaded, root=tmp_path)
    assert ri.load("p1", root=tmp_path) == reg


# --------------------------------------------------------------------------
# 附加：注册表行只留四个字段（+created_at），页与页互不干扰
# --------------------------------------------------------------------------


def test_registry_rows_keep_contract_fields_and_pages_are_independent(tmp_path):
    ri.assign("p1", 15, _rows("page fifteen row one", "page fifteen row two"), root=tmp_path)
    pids_16 = ri.assign("p1", 16, _rows("page sixteen row one"), root=tmp_path)
    again_16 = ri.assign("p1", 16, _rows("page sixteen row one"), root=tmp_path)

    assert again_16 == pids_16
    reg = _registry("p1", tmp_path)
    assert len(reg["rows"]) == 3
    for entry in reg["rows"]:
        assert set(entry) <= {"pid", "page", "order", "fp", "created_at"}
        assert {'pid', 'page', 'order', 'fp'} <= set(entry)
    assert ri.assign("p1", 15, _rows("page fifteen row one", "page fifteen row two"),
                     root=tmp_path) == [e["pid"] for e in reg["rows"] if e["page"] == 15]
