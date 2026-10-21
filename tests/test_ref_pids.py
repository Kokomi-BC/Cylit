"""引用带上行身份（pid）：阶段 2b 只做**读时导出**，跳转行为零变化。

为什么只读时导出：``refs_json`` 的存储 schema 一个字都不改（``{codes, resolved, invalid,
cross_road, ok}``），老解读不用迁移；chunk 一重建索引，``metadata.pids`` 就有了，
引用跟着自动带上 —— 所以**任何一处都必须容忍 pids 缺失**，缺失时"不写这个键"，
而不是写空列表假装"这条证据没有身份"。

覆盖四段：

1. 解读引用（``understanding_service._chunk_anchors`` → ``split_live_refs`` → ``to_dict``）；
2. 对话引用（``project.search`` 的命中项 → ``_register_chunk`` → ``Citation``）；
3. ``Citation.to_dict`` 的序列化形状（空就不出现，保持老形状）；
4. 自检（``scripts/index_audit``）：pid 找不到只 warn、整篇都没有只统计。

全部离线：不连库、不跑检索/解读任务（DB 与检索都用构造数据替换）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from backend.agent.citations import Citation, RefRegistry
from backend.services.understanding import understanding_service as us


# --------------------------------------------------------------------------
# 1. 解读引用
# --------------------------------------------------------------------------


def _by_code() -> dict:
    return {"A001": {"chunk_type": "unit", "page": 3},
            "A002": {"chunk_type": "formula", "page": 7}}


def test_understanding_ref_carries_pids_from_chunk_metadata() -> None:
    """chunk 的 pids 一路带到 ref 项里（与 block_id / row_id 同样只是"带出来"）。"""
    live, _ = us.split_live_refs(
        {"A001": "p:a:unit:u1"}, _by_code(), exists=lambda ids: set(ids),
        anchors={"p:a:unit:u1": {"block_id": "b1", "row_id": "p003-04",
                                 "pids": ["r-1", "r-2"]}})
    assert live[0]["pids"] == ["r-1", "r-2"]
    assert live[0]["row_id"] == "p003-04", "落点字段一个都不许动（本阶段行为零变化）"


def test_understanding_ref_omits_pids_key_when_chunk_has_none() -> None:
    """存量 chunk 没有 pids（缺失 / 空列表）→ **不写这个键**，不是写空列表。"""
    live, _ = us.split_live_refs(
        {"A001": "p:a:unit:u1", "A002": "p:a:formula:f1"}, _by_code(),
        exists=lambda ids: set(ids),
        anchors={"p:a:unit:u1": {"row_id": "p003-04"},          # 老数据：没有这个字段
                 "p:a:formula:f1": {"row_id": "p007-01", "pids": []}})  # 有字段但空
    assert "pids" not in live[0]
    assert "pids" not in live[1]


class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self) -> list:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def query(self, *args, **kwargs):
        return _FakeQuery(self._rows)


class _SessionCtx:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeSession:
        return _FakeSession(self._rows)

    def __exit__(self, *args) -> bool:
        return False


def test_chunk_anchors_reads_pids_column(monkeypatch) -> None:
    """``_chunk_anchors`` 从 ``metadata_json`` 里取出 pids（还是那一次 in(...) 查询）。"""
    rows = [
        ("p:a:unit:u1", "b1", json.dumps({"row_ids": ["p003-04"],
                                          "pids": ["r-1", "r-2"]})),
        ("p:a:formula:f1", "row-p007-01", json.dumps({"row_ids": ["p007-01"]})),
    ]
    monkeypatch.setattr(us, "session_scope", lambda: _SessionCtx(rows))

    anchors = us._chunk_anchors(["p:a:unit:u1", "p:a:formula:f1"])
    assert anchors["p:a:unit:u1"]["pids"] == ["r-1", "r-2"]
    assert anchors["p:a:formula:f1"]["pids"] is None, "缺失就是 None（与'空列表'要分得开）"


def _row(markdown: str, resolved: dict, inventory: list) -> "us.Understanding":
    return us.Understanding(
        id="p:a", paper_id="p", path="a", markdown=markdown,
        refs_json=us.json_dumps({"codes": list(resolved), "resolved": resolved,
                                 "invalid": [], "cross_road": [], "ok": True}),
        inventory=us.json_dumps(inventory), refs_ok=True, model="m",
        prompt_tokens=0, completion_tokens=0, updated_at=None,
    )


def test_to_dict_refs_carry_pids_and_query_anchors_once(monkeypatch) -> None:
    """``to_dict`` 复用那一次 ``_chunk_anchors``：加了 pids 也不许多查一次库。"""
    calls: list[list[str]] = []

    def fake_anchors(ids: list[str]) -> dict:
        calls.append(list(ids))
        return {"cid1": {"row_id": "p003-04", "pids": ["r-1"]}}

    monkeypatch.setattr(us, "_chunk_anchors", fake_anchors)
    got = us.to_dict(_row("见 [A001]。", {"A001": "cid1"},
                          [{"code": "A001", "chunk_id": "cid1",
                            "chunk_type": "unit", "page": 3}]))

    assert got["refs_resolved"][0]["pids"] == ["r-1"]
    assert len(calls) == 1, "_chunk_anchors 一次就够（不许为了 pids 再查一次）"


# --------------------------------------------------------------------------
# 2. Citation 的形状与合并
# --------------------------------------------------------------------------


def test_citation_to_dict_omits_empty_pids() -> None:
    item = Citation(ref=1, paper_id="p", kind="chunk", page=3).to_dict()
    assert "pids" not in item, "空 pids 不出现：老形状（前端不用改）"
    assert item["label"] == "第 3 页" and item["page"] == 3


def test_citation_to_dict_includes_pids_when_present() -> None:
    item = Citation(ref=2, paper_id="p", kind="chunk", pids=["r-1", "r-2"]).to_dict()
    assert item["pids"] == ["r-1", "r-2"]
    assert "pids" not in Citation(ref=2, paper_id="p", kind="chunk").to_dict()


def test_registry_reuse_merges_pids_instead_of_clearing() -> None:
    """同一处证据复用编号：先到的 pids 不能被后到的命中抹掉（后到没带也算）。"""
    reg = RefRegistry("paper")
    first = reg.assign(key="chunk:paper:p003-04:x", kind="chunk",
                       pids=["r-1", "r-2"], ids=("c-en",))
    again = reg.assign(key="chunk:paper:p003-04:x", kind="chunk",
                       pids=["r-2", "r-3"], ids=("c-zh",))
    blank = reg.assign(key="chunk:paper:p003-04:x", kind="chunk", page=3)

    assert first.ref == again.ref == blank.ref == 1
    assert first.pids == ["r-1", "r-2", "r-3"], "并集、保序、去重"
    assert reg.ref_of_id("c-en") == reg.ref_of_id("c-zh") == 1


def test_register_chunk_passes_pids_into_registry() -> None:
    """``_register_chunk``（``paper_search`` 那条路）把命中里的 pids 传进 assign。"""
    from backend.agent.tools import _register_chunk

    ctx = SimpleNamespace(deps=SimpleNamespace(registry=RefRegistry("paper"),
                                               paper_id="paper"))
    cit = _register_chunk(ctx, {"chunk_id": "c1", "block_id": "p003-04", "page_no": 3,
                                "chunk_type": "unit", "content": "原文",
                                "translation": "译文", "pids": ["r-1", "r-2"]})
    assert cit.pids == ["r-1", "r-2"]

    plain = _register_chunk(ctx, {"chunk_id": "c2", "block_id": "p004-01", "page_no": 4,
                                  "chunk_type": "unit", "content": "原文"})
    assert plain.pids == [], "命中没带 pids（老索引）→ 空列表，to_dict 里不出现"


# --------------------------------------------------------------------------
# 3. 检索命中（project.search / kb_search）
# --------------------------------------------------------------------------


def _patch_project(monkeypatch, items: list[dict]):
    from backend.services.agent import project

    monkeypatch.setattr(project.paper_service, "get_paper",
                        lambda pid: {"id": pid, "page_count": 12})
    monkeypatch.setattr(project.retrieval, "search",
                        lambda *a, **kw: {"items": items, "retrieval": {}})
    # 中文孪生预览要回库查 —— 这里只测 pids 透传，换掉它（不连库）
    monkeypatch.setattr(project, "_paired_zh_previews", lambda its: {})
    return project


def test_project_search_hit_carries_pids(monkeypatch) -> None:
    """pids 就在检索结果条的 ``metadata`` 里 → **不需要**为此新增查询。"""
    project = _patch_project(monkeypatch, [{
        "chunk_id": "c1", "block_id": "p003-04", "page_no": 3, "section": "3.1",
        "chunk_type": "unit", "lang": "en", "content": "正文",
        "metadata": {"row_ids": ["p003-04"], "pids": ["r-1", "r-2"]},
    }])

    data = project.search("paper", "query")
    assert data["items"][0]["pids"] == ["r-1", "r-2"]
    assert data["items"][0]["page_no"] == 3


def test_project_search_hit_omits_pids_when_chunk_has_none(monkeypatch) -> None:
    project = _patch_project(monkeypatch, [{
        "chunk_id": "c1", "block_id": "p003-04", "page_no": 3,
        "chunk_type": "unit", "lang": "en", "content": "正文",
        "metadata": {"row_ids": ["p003-04"]},          # 存量 chunk：还没有 pids
    }, {
        "chunk_id": "c2", "page_no": 4, "chunk_type": "unit", "content": "没有 metadata",
    }])

    items = project.search("paper", "query")["items"]
    assert "pids" not in items[0] and "pids" not in items[1]


def test_project_kb_search_hit_carries_pids(monkeypatch) -> None:
    project = _patch_project(monkeypatch, [{
        "paper_id": "other", "chunk_id": "c9", "block_id": "p005-01", "page_no": 5,
        "chunk_type": "unit", "content": "正文",
        "metadata": {"pids": ["r-9"]},
    }])
    monkeypatch.setattr(project, "_paper_titles", lambda ids: {"other": "另一篇"})

    items = project.kb_search("query")["items"]
    assert items[0]["pids"] == ["r-9"] and items[0]["paper_id"] == "other"


# --------------------------------------------------------------------------
# 4. 自检：pid 找不到只 warn、整篇都没有只统计
# --------------------------------------------------------------------------


def _audit_chunk(cid: str, **meta) -> dict:
    return {"id": cid, "metadata": dict(meta)}


def _refs(**resolved: str) -> dict:
    return {"codes": list(resolved), "resolved": dict(resolved),
            "invalid": [], "cross_road": [], "ok": True}


def test_audit_unknown_ref_pid_is_warn() -> None:
    from scripts.index_audit import check_understanding_pids

    findings = check_understanding_pids(
        _refs(A001="c1"),
        [_audit_chunk("c1", row_ids=["p003-04"], pids=["r-1", "r-gone"])],
        {"rows": [{"pid": "r-1"}]})

    assert [f["level"] for f in findings] == ["warn"], "存量 chunk 还没 pids，不能升级成 error"
    assert findings[0]["check"] == "understanding_ref_pids"
    assert "r-gone" in findings[0]["detail"] and "A001" in findings[0]["detail"]


def test_audit_no_refs_with_pids_is_only_stat() -> None:
    """整篇引用都还没有 pids → 一条发现都不产生，只给统计。"""
    from scripts.index_audit import check_understanding_pids, understanding_pid_stats

    refs = _refs(A001="c1", A002="c2")
    chunks = [_audit_chunk("c1", row_ids=["p003-04"]), _audit_chunk("c2", row_ids=["p007-01"])]
    registry = {"rows": [{"pid": "r-1"}]}

    assert check_understanding_pids(refs, chunks, registry) == []
    stats = understanding_pid_stats([refs], chunks)
    assert stats == {"refs": 2, "with_pids": 0, "pids": 0}


def test_audit_ref_pids_all_known_is_clean_and_counted() -> None:
    from scripts.index_audit import check_understanding_pids, understanding_pid_stats

    refs = _refs(A001="c1")
    chunks = [_audit_chunk("c1", pids=["r-1", "r-2"])]
    registry = {"rows": [{"pid": "r-1"}, {"pid": "r-2"}]}

    assert check_understanding_pids(refs, chunks, registry) == []
    assert understanding_pid_stats([refs], chunks) == {"refs": 1, "with_pids": 1, "pids": 2}
