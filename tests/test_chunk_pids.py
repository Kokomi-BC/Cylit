"""chunk 记住行身份（pid）：metadata.pids 的组装 + pid 解析（阶段 2a，见 docs/身份模型.md §5）。

两层都在这里：

1. **组装**（``knowledge_service._attach_row_pids``）：`pids` 与 `row_ids` 同序等长，
   缺 pid 的行跳过并记 `pids_missing`；`chunk.id` / `row_ids` / 排序 / 正文一律不动。
   pid 只从页产物取（``reader_service.page_translations`` = ``page_products(paper_id)[0]``），
   **不重算**；这里用 monkeypatch 把页产物换掉，于是 pid 的来源可以逐条钉住。
2. **解析**（``row_identity.resolve_pids``）：``ok`` / ``text-changed`` / ``gone`` 三态，
   传 rows 与不传 rows 的差别（不传就查不到行 id、指纹也不校验）。

全部离线：不连库、不跑解析/翻译/建索引，注册表写在 tmp_path。
"""

from __future__ import annotations

import json

import pytest


# --------------------------------------------------------------------------
# 组装：pids 与 row_ids 同序等长
# --------------------------------------------------------------------------


def test_attach_row_pids_same_order_and_length() -> None:
    from backend.services.knowledge import knowledge_service as ks

    chunks = [{"id": "demo:a:text:p001-00-p001-01", "chunk_type": "text",
               "metadata": {"row_ids": ["p001-00", "p001-01"]}}]
    ks._attach_row_pids(chunks, {"p001-00": "r-1", "p001-01": "r-2"})

    meta = chunks[0]["metadata"]
    assert meta["pids"] == ["r-1", "r-2"], "顺序跟着 row_ids，不按字典序"
    assert len(meta["pids"]) == len(meta["row_ids"])
    assert "pids_missing" not in meta, "一条不缺就不写这个键"


def test_attach_row_pids_skips_rows_without_pid() -> None:
    from backend.services.knowledge import knowledge_service as ks

    chunks = [{"id": "c1", "chunk_type": "text",
               "metadata": {"row_ids": ["p001-00", "p001-01", "p001-02"]}}]
    ks._attach_row_pids(chunks, {"p001-00": "r-1", "p001-02": "r-3"})

    meta = chunks[0]["metadata"]
    assert meta["pids"] == ["r-1", "r-3"], "缺 pid 的那一行跳过，不填假值"
    assert meta["pids_missing"] == 1
    assert meta["row_ids"] == ["p001-00", "p001-01", "p001-02"], "row_ids 原样保留"


def test_attach_row_pids_only_adds_fields() -> None:
    from backend.services.knowledge import knowledge_service as ks

    chunks = [
        {"id": "a", "chunk_type": "text", "order_global": 5, "page_no": 1,
         "metadata": {"row_ids": ["p001-00"], "pages": [1], "road": "a"}},
        {"id": "b", "chunk_type": "unit", "order_global": 6, "page_no": 1,
         "metadata": {"row_ids": []}},
    ]

    def skeleton(items: list[dict]) -> str:
        return json.dumps([{k: v for k, v in c.items() if k != "metadata"}
                           for c in items], sort_keys=True)

    before = skeleton(chunks)
    ks._attach_row_pids(chunks, {"p001-00": "r-1"})
    assert skeleton(chunks) == before, "chunk 自己的字段（id / 类型 / 顺序）一个字都不动"
    assert [c["id"] for c in chunks] == ["a", "b"]
    assert chunks[0]["metadata"]["row_ids"] == ["p001-00"]
    assert chunks[0]["metadata"]["pages"] == [1] and chunks[0]["metadata"]["road"] == "a"
    assert chunks[1]["metadata"]["pids"] == [], "没有行就没有 pid，但键要在"
    assert "pids_missing" not in chunks[1]["metadata"]


def test_attach_row_pids_formula_chunk_uses_anchor_row() -> None:
    from backend.services.knowledge import knowledge_service as ks

    chunks = [
        {"id": "demo:formula:f001", "chunk_type": "formula",
         "metadata": {"row_ids": ["p001-02"], "latex": "a = b"}},
        {"id": "demo:formula:f002", "chunk_type": "formula",
         "metadata": {"row_ids": []}},
    ]
    ks._attach_row_pids(chunks, {"p001-02": "r-9"})

    assert chunks[0]["metadata"]["pids"] == ["r-9"], "公式块锚在一行上，pids 就是那条行"
    assert chunks[1]["metadata"]["pids"] == []


# --------------------------------------------------------------------------
# pid 的来源：只读页产物（不重算）
# --------------------------------------------------------------------------


def test_pid_map_comes_from_page_products(monkeypatch) -> None:
    from backend.services.knowledge import knowledge_service as ks

    pages = [
        {"page": 1, "rows": [{"id": "p001-00", "pid": "r-1"},
                             {"id": "p001-01"},                   # 这一行还没 pid
                             {"id": "p001-02", "pid": "   "}]},   # 空白不算
        {"page": 2, "rows": [{"id": "p002-00", "pid": "r-2"}]},
    ]
    monkeypatch.setattr(ks.reader_service, "page_translations", lambda pid: pages)

    assert ks._pid_by_row_id("demo") == {"p001-00": "r-1", "p002-00": "r-2"}


def test_pid_map_empty_when_page_products_fail(monkeypatch) -> None:
    from backend.services.knowledge import knowledge_service as ks

    def boom(paper_id):
        raise RuntimeError("产物损坏")

    monkeypatch.setattr(ks.reader_service, "page_translations", boom)
    assert ks._pid_by_row_id("demo") == {}, "身份这一层永远不许把建索引弄挂"


# --------------------------------------------------------------------------
# 解析：ok / text-changed / gone
# --------------------------------------------------------------------------


@pytest.fixture()
def ident(tmp_path, monkeypatch):
    """行身份模块，注册表落在 tmp_path（``paper_paths`` 指过去，不写真实 data/）。"""
    from backend.services.paper import paper_service, row_identity

    md = tmp_path / "md"
    monkeypatch.setattr(paper_service, "paper_paths", lambda pid: {"md": md})
    return row_identity


def test_resolve_pids_ok(ident) -> None:
    row = {"id": "p001-01", "page": 1, "index": 1, "original": "alpha beta gamma"}
    ident.save("demo", {"version": 1, "retired": [],
                        "rows": [{"pid": "r-1", "page": 1, "order": 1,
                                  "fp": ident.fingerprint(row)}]})

    got = ident.resolve_pids("demo", ["r-1"], rows=[row])
    assert got["r-1"] == {"page": 1, "order": 1, "row_id": "p001-01",
                          "fp_match": True, "state": "ok"}


def test_resolve_pids_text_changed(ident) -> None:
    old = {"id": "p001-01", "page": 1, "index": 1, "original": "alpha beta gamma delta"}
    new = {**old, "original": "完全换了一段话，长度也不一样了"}
    ident.save("demo", {"version": 1, "retired": [],
                        "rows": [{"pid": "r-1", "page": 1, "order": 1,
                                  "fp": ident.fingerprint(old)}]})

    got = ident.resolve_pids("demo", ["r-1"], rows=[new])
    assert got["r-1"]["state"] == "text-changed", "位子还在、文本变了"
    assert got["r-1"]["fp_match"] is False
    assert got["r-1"]["row_id"] == "p001-01", "行还能定位到，只是内容不是那一版"


def test_resolve_pids_gone_for_unknown_and_retired(ident) -> None:
    ident.save("demo", {"version": 1,
                        "rows": [{"pid": "r-live", "page": 1, "order": 0, "fp": "x"}],
                        "retired": [{"pid": "r-old", "page": 2, "order": 3, "fp": "y",
                                     "reason": "text-changed"}]})

    got = ident.resolve_pids("demo", ["r-old", "r-never"], rows=[])
    assert got["r-old"]["state"] == "gone"
    assert got["r-old"]["page"] == 2, "退休条目里还记得它原来在哪一页"
    assert got["r-never"]["state"] == "gone" and got["r-never"]["page"] is None
    assert got["r-never"]["row_id"] == ""


def test_resolve_pids_gone_when_slot_missing(ident) -> None:
    row = {"id": "p001-00", "page": 1, "index": 0, "original": "alpha"}
    ident.save("demo", {"version": 1, "retired": [],
                        "rows": [{"pid": "r-1", "page": 5, "order": 2,
                                  "fp": ident.fingerprint(row)}]})

    got = ident.resolve_pids("demo", ["r-1"], rows=[row])
    assert got["r-1"]["state"] == "gone", "注册表有这一条，但当前页没有那个 order"
    assert got["r-1"]["row_id"] == ""


def test_resolve_pids_without_rows_only_reads_registry(ident) -> None:
    row = {"id": "p001-01", "page": 1, "index": 1, "original": "alpha beta"}
    ident.save("demo", {"version": 1, "retired": [],
                        "rows": [{"pid": "r-1", "page": 1, "order": 1,
                                  "fp": ident.fingerprint(row)}]})

    plain = ident.resolve_pids("demo", ["r-1"])
    full = ident.resolve_pids("demo", ["r-1"], rows=[row])

    assert plain["r-1"]["row_id"] == "" and full["r-1"]["row_id"] == "p001-01"
    assert plain["r-1"]["fp_match"] is None, "没传 rows 就没有指纹可比 → 不猜"
    assert full["r-1"]["fp_match"] is True
    assert plain["r-1"]["state"] == full["r-1"]["state"] == "ok"
    assert (plain["r-1"]["page"], plain["r-1"]["order"]) == (1, 1)



# --------------------------------------------------------------------------
# 端到端：`_base_chunks` 把 pid 挂到正文块与公式块上
# （离线替身与 tests/test_kb_row_chunking.py 的 e2e 夹具同一套，只是行带 pid）
# --------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, paper):
        self._paper = paper

    def get(self, model, key):
        return self._paper

    def query(self, *a, **kw):
        return self

    def filter(self, *a, **kw):
        return self

    def order_by(self, *a, **kw):
        return self

    def all(self):
        return []


class _Ctx:
    def __init__(self, sess):
        self._sess = sess

    def __enter__(self):
        return self._sess

    def __exit__(self, *a):
        return False


class _FakeIR:
    def __init__(self, path):
        pass

    def formulas(self):
        return [{"block_id": "f001", "page": 1}]

    def figures(self):
        return []

    def tables(self):
        return []


def _pages(with_pids: bool) -> list[dict]:
    """页产物：标题行 + 正文行 + 公式行（``with_pids`` 关掉就是“改动前”的样子）。"""
    rows = [
        {"id": "p001-00", "page": 1, "index": 0, "type": "heading", "level": 1,
         "number": "1", "original": "# 1 Method", "translation": "# 1 方法"},
        {"id": "p001-01", "page": 1, "index": 1, "type": "para",
         "original": "We study online vector quantization for streaming data and show bounds.",
         "translation": "我们研究流式数据的在线向量量化，并给出界。", "semantic_break": True},
        {"id": "p001-02", "page": 1, "index": 2, "type": "para",
         "original": "$$D_{\\text{mse}} := \\mathbb{E}\\|x - Q(x)\\|^2$$",
         "translation": "$$D_{\\text{mse}} := \\mathbb{E}\\|x - Q(x)\\|^2$$"},
    ]
    if with_pids:
        for i, row in enumerate(rows):
            row["pid"] = f"r-{i + 1:016d}"
    return [{"page": 1, "rows": rows}]


def _install(monkeypatch, *, with_pids: bool):
    """一台离线建块机：页产物 / 目录 / IR / 公式条目全换成构造好的数据。"""
    from types import SimpleNamespace

    from backend.services.knowledge import knowledge_service as ks
    from backend.services.paper import figure_regions

    pages = _pages(with_pids)
    outline = {
        "tree": [{"block_id": "p001-00", "page": 1, "level": 1, "number": "1",
                  "text": "# 1 Method", "text_zh": "# 1 方法", "children": []}],
        "items": [], "count": 1}

    monkeypatch.setattr(ks, "chunk_params", lambda: (1400, 200, 2240))
    monkeypatch.setattr(ks, "using_breaks", lambda: True)
    monkeypatch.setattr(ks, "unit_index_enabled", lambda: True)
    monkeypatch.setattr(ks, "session_scope",
                        lambda: _Ctx(_FakeSession(SimpleNamespace(id="demo", authors=""))))
    monkeypatch.setattr(ks.reader_service, "page_translations", lambda pid: pages)
    monkeypatch.setattr(ks.reader_service, "build_outline", lambda pid: outline)
    monkeypatch.setattr(ks.reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(ks.reader_service, "row_id_of_caption",
                        lambda pid, page, number: None)
    monkeypatch.setattr(ks.paper_service, "paper_paths",
                        lambda pid: {"ir": "/tmp/none.sqlite"})
    monkeypatch.setattr(figure_regions, "all_regions", lambda pid: {})
    monkeypatch.setattr(ks, "_block_row_ids", lambda pid, rows_: {})
    monkeypatch.setattr(ks.formula_source, "kb_formula_entries",
                        lambda pid, formulas, rows_: [{
                            "id": "f001", "formula_block_id": "f001",
                            "latex": "D_{\\text{mse}} := \\mathbb{E}\\|x - Q(x)\\|^2",
                            "page": 1, "row_id": "p001-02", "display": True,
                            "anchor_type": "page_row", "anchor_precision": "row",
                            "source_item_ids": ["f001"]}])
    import backend.parsing.ir.ir_reader as ir_reader

    monkeypatch.setattr(ir_reader, "IRReader", _FakeIR)
    return ks


def test_base_chunks_text_and_formula_carry_pids(monkeypatch) -> None:
    ks = _install(monkeypatch, with_pids=True)
    base, _ = ks._base_chunks("demo")

    pid_of = {r["id"]: r["pid"] for r in _pages(True)[0]["rows"]}
    for chunk in base:
        meta = chunk["metadata"]
        assert meta["pids"] == [pid_of[rid] for rid in meta["row_ids"]], (
            f"{chunk['id']} 的 pids 与 row_ids 不同序等长：{meta}")
        assert "pids_missing" not in meta

    texts = [c for c in base if c["chunk_type"] == "text"]
    assert texts and all(c["metadata"].get("pids") for c in texts)
    covered = {pid for c in texts for pid in c["metadata"]["pids"]}
    assert covered == set(pid_of.values()), "正文块把三条行都记住了"

    formula = [c for c in base if c["chunk_type"] == "formula"][0]
    assert formula["metadata"]["row_ids"] == ["p001-02"]
    assert formula["metadata"]["pids"] == ["r-0000000000000003"]


def test_base_chunks_identical_to_a_run_without_pids(monkeypatch) -> None:
    """关掉 pids 的那一版就是“改动前”的行为：逐字段比，只许多出 pids 这个键。"""
    ks = _install(monkeypatch, with_pids=True)
    hot, hot_summary = ks._base_chunks("demo")
    monkeypatch.setattr(ks, "_attach_row_pids", lambda chunks, pid_of: None)
    cold, cold_summary = ks._base_chunks("demo")

    def strip(chunk: dict) -> dict:
        meta = {k: v for k, v in (chunk.get("metadata") or {}).items()
                if k not in ("pids", "pids_missing")}
        return {**{k: v for k, v in chunk.items() if k != "metadata"}, "metadata": meta}

    assert [strip(c) for c in hot] == cold, "id / row_ids / 顺序 / 正文一个字都不许动"
    assert all("pids" not in (c.get("metadata") or {}) for c in cold)
    assert all(c["metadata"]["pids"] for c in hot if c["metadata"]["row_ids"])
    assert hot_summary == cold_summary

