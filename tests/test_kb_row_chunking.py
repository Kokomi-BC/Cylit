"""行级分块的离线单测：TOC 归属 / 语义单元 / 确定性组装 / 校验层。

全部离线（不连数据库、不调模型）：`chunking` 只依赖 `reader_service` 的两个函数，
这里用 monkeypatch 把它们换成构造好的行与目录 —— 于是"切点在哪、有没有丢行"可以逐条钉住。

真实语料上的数字（两篇论文的块数 / 长度分布 / 检索回归）在 `docs/dev-log.md` 的 §17.63。
"""

from __future__ import annotations

import pytest

from backend.services.knowledge import chunking


# --------------------------------------------------------------------------
# 夹具：4 个小节 + 一个无子节的长节，行带/不带语义锚点
# --------------------------------------------------------------------------


def _row(i: int, page: int, text: str, *, type_: str = "para", zh: str = "",
         break_: bool = False, skip: str = "") -> dict:
    return {"id": f"p{page:03d}-{i:02d}", "page": page, "index": i, "type": type_,
            "level": 0, "number": "", "original": text, "translation": zh,
            "semantic_break": break_, "skip_reason": skip,
            # 夹具里"图注 / 表体 / 图"都是结构行（真实链路上图注还要过 IR 认证，见
            # `knowledge_service._caption_rows`；这一层只测分块）
            "structural": type_ in ("table", "figure", "caption")}


def _rows() -> list[dict]:
    return [
        _row(0, 1, "# 1 TITLE", type_="heading", zh="# 1 标题"),
        _row(1, 1, "First paragraph of the paper body, long enough to matter.", zh="第一段。"),
        _row(2, 1, "## 1.1 Alpha", type_="heading", zh="## 1.1 甲"),
        _row(3, 1, "Alpha introduces the problem statement of the paper.", zh="甲引入问题。"),
        _row(4, 1, "Alpha continues with the method description used later.", zh="甲继续方法。",
             break_=True),
        _row(5, 1, "## 1.2 Beta", type_="heading", zh="## 1.2 乙"),
        _row(6, 1, "Beta is short.", zh="乙很短。"),
        _row(7, 2, "## 1.3 Gamma", type_="heading", zh="## 1.3 丙"),
        _row(8, 2, "G" * 900, zh="丙" * 300, break_=True),
        _row(9, 2, "H" * 900, zh="丁" * 300),
        _row(10, 2, "**Table 1: Numbers.**", type_="caption", zh="**表 1：数字。**"),
        _row(11, 2, "| a | b |\n|---|---|\n| 1 | 2 |", type_="table", zh="| a | b |"),
        _row(12, 2, "See [12] for details of the reference list entry.", skip="ref_section"),
    ]


def _tree(rows: list[dict]) -> list[dict]:
    """手搓目录：1 → (1.1, 1.2, 1.3)，行下标与 `_rows()` 对齐。"""
    def node(key, title, row_id, children=None):
        item = {"key": key, "number": key, "title": title, "title_zh": title,
                "level": key.count(".") + 1, "page": 1, "row_id": row_id,
                "children": children or [], "parent": None}
        for child in item["children"]:
            child["parent"] = item
        return item

    alpha = node("1.1", "## 1.1 Alpha", "p001-02")
    beta = node("1.2", "## 1.2 Beta", "p001-05")
    gamma = node("1.3", "## 1.3 Gamma", "p002-07")
    root = node("1", "# 1 TITLE", "p001-00", [alpha, beta, gamma])
    index = {r["id"]: r["index"] for r in rows}
    for item in chunking._walk([root]):
        item["path"] = chunking._path_of(item)
        item["row_index"] = index.get(item["row_id"])
    return [root]


@pytest.fixture()
def paper(monkeypatch):
    rows = _rows()
    tree = _tree(rows)
    monkeypatch.setattr(chunking, "row_units", lambda pid, **kw: rows)
    monkeypatch.setattr(chunking, "section_tree", lambda pid, rws=None: tree)
    return rows, tree


def _assemble(rows, tree, **kw):
    params = {"target": 1200, "hard": 2000, "floor": 200, "use_breaks": True}
    params.update(kw)
    return chunking.assemble(rows, tree, **params)


# --------------------------------------------------------------------------
# 1. 归属：行归到"最深且在它之前"的节点
# --------------------------------------------------------------------------


def test_rows_are_assigned_to_deepest_preceding_node(paper):
    rows, tree = paper
    chunking._assign_sections(rows, tree)
    assert rows[0]["section_path"] == "# 1 TITLE"              # 标题行归自己
    assert rows[1]["section_path"] == "# 1 TITLE"
    assert rows[3]["section_path"] == "# 1 TITLE / ## 1.1 Alpha"
    assert rows[6]["section_path"] == "# 1 TITLE / ## 1.2 Beta"
    assert rows[8]["section_path"] == "# 1 TITLE / ## 1.3 Gamma"
    assert rows[9]["section_key"] == "1.3"


def test_toc_boundary_is_never_crossed(paper):
    rows, tree = paper
    chunks, _ = _assemble(rows, tree)
    for chunk in chunks:
        paths = {r["section_path"] for r in chunk["rows"]}
        assert chunking._paths_comparable(paths), paths


# --------------------------------------------------------------------------
# 2. 语义单元：带锚点 / 不带锚点
# --------------------------------------------------------------------------


def test_semantic_units_split_at_anchor_and_section(paper):
    rows, tree = paper
    chunking._assign_sections(rows, tree)
    units = chunking.semantic_units(rows, use_breaks=True)
    starts = [u["rows"][0]["id"] for u in units]
    # 1.1 内部被锚点（p001-04）切开；1.2 → 1.3 是 TOC 边界；表体/图注不成单元
    assert starts == ["p001-00", "p001-02", "p001-04",
                      "p001-05", "p002-07", "p002-08"]
    assert all(not any(chunking._is_structural(r) for r in u["rows"]) for u in units)


def test_units_ignore_breaks_when_disabled(paper):
    rows, tree = paper
    chunking._assign_sections(rows, tree)
    units = chunking.semantic_units(rows, use_breaks=False)
    assert [u["rows"][0]["id"] for u in units] == [
        "p001-00", "p001-02", "p001-05", "p002-07"]


# --------------------------------------------------------------------------
# 3. 组装：整棵 / 引言并入 / 超长切分
# --------------------------------------------------------------------------


def test_small_subtree_becomes_one_chunk(paper):
    rows, tree = paper
    chunks, stats = _assemble(rows, tree)
    beta = [c for c in chunks if c["rows"][0]["id"] == "p001-05"]
    assert len(beta) == 1
    assert [r["id"] for r in beta[0]["rows"]] == ["p001-05", "p001-06"]
    # 1.1 与 1.2 的子树都能装进 target → 各自整棵成一块
    assert stats["whole_subtree"] == 2


def test_own_chunk_plus_children(paper):
    rows, tree = paper
    # floor 调到比根节点引言还小 → 引言不再并入首个子块，而是自己成一块
    chunks, stats = _assemble(rows, tree, floor=10)
    assert stats["intro_merged"] == 0
    first = chunks[0]
    assert [r["id"] for r in first["rows"]] == ["p001-00", "p001-01"]
    assert first["node"]["key"] == "1"


def test_long_leaf_is_split_at_unit_boundaries(paper):
    """超 hard 的块按**单元**边界切（单元是分块层的原子，见 docs/语义单元索引-设计.md §1）。

    900 字那两行各自超过默认 `unit_max_chars=700`，所以各成一条单元；hard=1000 时
    "标题行 + 第一条 900"（915）还塞得下 → 贪心合并后封口，第二条另起一块。
    **单元不可拆**是刻意的：宁可块长偏离 target，也不要让检索单位被切成两半。
    """
    rows, tree = paper
    chunks, stats = _assemble(rows, tree)
    gamma = [c for c in chunks if c["rows"][0]["section_key"] == "1.3"]
    assert len(gamma) == 1                      # 1800 < hard=2000 → 整块
    chunks, stats = _assemble(rows, tree, hard=1000, target=600)
    gamma = [c for c in chunks if c["rows"][0]["section_key"] == "1.3"]
    assert [[r["id"] for r in c["rows"]] for c in gamma] == \
        [["p002-07", "p002-08"], ["p002-09"]]
    assert stats["unit_split"] >= 1


def test_intro_is_merged_into_first_child_when_too_short(paper):
    rows, tree = paper
    # 把根节点的标题+引言压到 floor 以下 → 应并进首个子块的头部
    rows[0]["original"] = "# 1 T"
    rows[1]["original"] = "short."
    tree[0]["row_index"] = 0
    chunks, stats = _assemble(rows, tree, floor=200)
    assert stats["intro_merged"] == 1
    merged = [c for c in chunks if c["node"] and c["node"]["key"] == "1"]
    assert not merged, "根节点自己不该再单独成块"
    assert [r["id"] for r in chunks[0]["rows"]][:2] == ["p001-00", "p001-01"]


def test_structure_rows_never_enter_text_chunks(paper):
    rows, tree = paper
    chunks, _ = _assemble(rows, tree)
    for chunk in chunks:
        for row in chunk["rows"]:
            assert not chunking._is_structural(row)
            assert not row.get("skip_reason")


# --------------------------------------------------------------------------
# 4. 校验层：不变量逐条可查
# --------------------------------------------------------------------------


def test_validate_accepts_good_assembly(paper):
    rows, tree = paper
    chunks, _ = _assemble(rows, tree)
    report = chunking.validate(chunks, rows, hard=2000)
    assert report["ok"] and report["covered"] == report["rows"] == 10


def test_validate_catches_dropped_row_and_bad_order(paper):
    rows, tree = paper
    chunks, _ = _assemble(rows, tree)
    broken = [dict(c) for c in chunks]
    broken[1] = {**broken[1], "rows": broken[1]["rows"][1:]}       # 丢一行
    report = chunking.validate(broken, rows, hard=2000)
    assert not report["ok"]
    assert any(e["kind"] == "coverage" for e in report["errors"])

    swapped = [dict(c) for c in chunks]
    swapped[0], swapped[1] = {**swapped[1]}, {**swapped[0]}
    report = chunking.validate(swapped, rows, hard=2000)
    assert any(e["kind"] == "order_between_chunks" for e in report["errors"])


def test_validate_rejects_cross_section_mix(paper):
    rows, tree = paper
    chunking._assign_sections(rows, tree)
    mixed = [{"rows": [rows[3], rows[6]], "node": tree[0]}]
    report = chunking.validate(mixed, rows, hard=2000)
    assert any(e["kind"] == "cross_section" for e in report["errors"])
    # 祖孙关系（同一棵子树里）不算跨节
    ancestor = [{"rows": [rows[1], rows[3]], "node": tree[0]}]
    assert not any(e["kind"] == "cross_section"
                   for e in chunking.validate(ancestor, rows, hard=2000)["errors"])


def test_validate_flags_oversize_and_structure_in_text(paper):
    rows, tree = paper
    report = chunking.validate([{"rows": [rows[8], rows[9]], "node": tree[0]}],
                               rows, hard=1200)
    assert any(e["kind"] == "over_hard" for e in report["errors"])
    report = chunking.validate([{"rows": [rows[0], rows[11]], "node": tree[0]}],
                               rows, hard=2000)
    assert any(e["kind"] == "structure_in_text" for e in report["errors"])


def test_validate_flags_skipped_row_in_text(paper):
    rows, tree = paper
    report = chunking.validate([{"rows": [rows[0], rows[12]], "node": tree[0]}],
                               rows, hard=2000)
    assert any(e["kind"] == "skipped_row_in_text" for e in report["errors"])


# --------------------------------------------------------------------------
# 5. 降级：没有目录时只按长度切（内容与顺序不变）
# --------------------------------------------------------------------------


def test_no_outline_degrades_to_flat_packing(paper):
    rows, _ = paper
    chunking._assign_sections(rows, [])
    chunks, _ = _assemble(rows, [], target=1000, hard=1500)
    report = chunking.validate(chunks, rows, hard=1500)
    assert report["ok"]
    assert [r["id"] for c in chunks for r in c["rows"]] == \
        [r["id"] for r in rows if not r.get("skip_reason") and not chunking._is_structural(r)]


# --------------------------------------------------------------------------
# 6. 参数与双语行展开（从已删除的旧路径测试里保留下来、并按新路径改写）
# --------------------------------------------------------------------------


def test_chunk_params_reads_settings_and_clamps(monkeypatch) -> None:
    from backend.services.knowledge import knowledge_service as ks

    # 键名是**裸名**（`retrieval.setting` 负责补 `kb.` 前缀），见 tests/test_kb_settings_keys.py
    values = {"chunk_target_chars": 1600, "chunk_min_chars": 10, "chunk_hard_chars": 100}
    monkeypatch.setattr(ks, "_setting_int", lambda key, default: values.get(key, default))
    target, floor, hard = ks.chunk_params()
    assert target == 1600
    assert floor == ks.RESIDUE_CHARS, "min 不得低于残渣门槛"
    assert hard == 1600, "hard 不得小于 target"


def test_chunk_params_defaults(monkeypatch) -> None:
    from backend.services.knowledge import knowledge_service as ks

    monkeypatch.setattr(ks, "_setting_int", lambda key, default: default)
    assert ks.chunk_params() == (ks.TARGET_CHUNK_CHARS, ks.MIN_CHUNK_CHARS,
                                 ks.HARD_CHUNK_CHARS)
    assert (ks.TARGET_CHUNK_CHARS, ks.MIN_CHUNK_CHARS, ks.HARD_CHUNK_CHARS) == (1400, 200, 2240)


def _base_chunk(chunk_type: str, content: str, zh: str | None) -> dict:
    from backend.services.knowledge import knowledge_service as ks

    row = {"id": "p001-00", "page": 1, "order": 0}
    chunk = ks._make_chunk("demo", chunk_type, content, row, row, section="§1",
                           section_path="§1", metadata={"row_ids": ["p001-00"]})
    chunk["_zh"] = zh
    return chunk


def test_variants_rows_emit_zh_en_and_single_any() -> None:
    from backend.services.knowledge import knowledge_service as ks

    base = [
        _base_chunk("text", "A paragraph with enough characters to pass the residue gate.",
                    "一段足够长的中文译文，用来通过中文侧的门槛。"),
        _base_chunk("formula", "公式 f001：$$a = b$$", None),
    ]
    rows = ks._chunk_variants_rows(base, ks.LANGS)
    langs = [r["lang"] for r in rows]
    assert langs.count("any") == 1 and langs.count("zh") == 1 and langs.count("en") == 1
    zh = [r for r in rows if r["lang"] == "zh"][0]
    en = [r for r in rows if r["lang"] == "en"][0]
    assert zh["id"].endswith(":zh") and en["id"].endswith(":en")
    assert zh["metadata"]["pair_id"] == en["metadata"]["pair_id"] == "demo:text:p001-00"
    formula = [r for r in rows if r["lang"] == "any"][0]
    assert "pair_id" not in formula["metadata"]


def test_variants_rows_skip_zh_when_untranslated() -> None:
    from backend.services.knowledge import knowledge_service as ks

    rows = ks._chunk_variants_rows(
        [_base_chunk("text", "Only the English side exists for this chunk here.", None)],
        ks.LANGS)
    assert {r["lang"] for r in rows} == {"en"}


def test_variants_rows_drop_residue_english_text() -> None:
    from backend.services.knowledge import knowledge_service as ks

    rows = ks._chunk_variants_rows([_base_chunk("text", "too short", "太短")], ks.LANGS)
    assert rows == [], "英文正文低于残渣门槛 → 整块不出行"


def test_variants_rows_keep_figure_label_prefix() -> None:
    from backend.services.knowledge import knowledge_service as ks

    rows = ks._chunk_variants_rows(
        [_base_chunk("figure", "插图 3：**Figure 3: Needle-In-A-Haystack recall.**", "插图 3：图注")],
        ks.LANGS)
    en = [r for r in rows if r["lang"] == "en"][0]
    assert en["content"].startswith("插图 3："), "定位前缀不能被清洗掉"
    assert en["chunk_type"] == "figure"


# --------------------------------------------------------------------------
# 7. 索引层端到端（`build_chunks_for_paper`）：把"行 → chunk → 双语行"整条串起来
#
# 这一段是从已删除的旧路径测试文件里**改写移植**过来的：旧路径用解析块当夹具，
# 这里改成页级行 + 目录 + IR（正是新路径的三个输入），断言的都是与分块策略无关的契约。
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
        return [{"block_id": "t001", "page": 2, "caption_block_id": "tb-caption",
                 "image_path": "/tmp/t001.png", "caption": "Table 1: Demo."}]


@pytest.fixture()
def e2e(monkeypatch):
    from types import SimpleNamespace

    from backend.services.knowledge import knowledge_service as ks
    from backend.services.paper import figure_regions

    rows = [
        # 前沿元数据（作者邮箱）→ 应被丢掉
        {"id": "p001-00", "page": 1, "type": "para",
         "original": "Jane Doe, Example University, jane@example.edu", "translation": ""},
        {"id": "p001-01", "page": 1, "type": "heading", "level": 2,
         "original": "# Abstract", "translation": "# 摘要", "number": ""},
        {"id": "p001-02", "page": 1, "type": "para",
         "original": "We study online vector quantization for streaming data and show bounds.",
         "translation": "我们研究流式数据的在线向量量化，并给出界。", "semantic_break": True},
        {"id": "p001-03", "page": 1, "type": "heading", "level": 2,
         "original": "# 1 Method", "translation": "# 1 方法", "number": "1"},
        {"id": "p001-04", "page": 1, "type": "para",
         "original": "The quantizer minimizes the distortion over all codebooks here.",
         "translation": "量化器在所有码本上最小化失真。"},
        # 公式行：正文里原样带 `$$…$$`，同时另有一条 canonical 原子条目
        {"id": "p001-05", "page": 1, "type": "formula",
         "original": "$$D_{\\text{mse}} := \\mathbb{E}\\|x - Q(x)\\|^2$$",
         "translation": "$$D_{\\text{mse}} := \\mathbb{E}\\|x - Q(x)\\|^2$$"},
        # 表：图注 + 表体（两句都要进同一条 table chunk）
        {"id": "p002-00", "page": 2, "type": "caption",
         "original": "**Table 1: Demo results.**", "translation": "**表 1：示例结果。**"},
        {"id": "p002-01", "page": 2, "type": "table",
         "original": "| Method | Score |\n|---|---|\n| Full Cache | 45.0 |",
         "translation": "| 方法 | 分数 |\n|---|---|\n| Full Cache | 45.0 |"},
    ]
    pages = [{"page": 1, "rows": [r for r in rows if r["page"] == 1]},
             {"page": 2, "rows": [r for r in rows if r["page"] == 2]}]
    outline = {"items": [{"block_id": "p001-01", "page": 1, "level": 1, "number": "",
                          "text": "# Abstract", "text_zh": "# 摘要", "children": [],
                          "id": "p001-01"},
                         {"block_id": "p001-03", "page": 1, "level": 1, "number": "1",
                          "text": "# 1 Method", "text_zh": "# 1 方法", "children": [],
                          "id": "p001-04"}],
               "tree": [{"block_id": "p001-01", "page": 1, "level": 1, "number": "",
                         "text": "# Abstract", "text_zh": "# 摘要", "children": []},
                        {"block_id": "p001-03", "page": 1, "level": 1, "number": "1",
                         "text": "# 1 Method", "text_zh": "# 1 方法", "children": []}],
               "count": 2}

    monkeypatch.setattr(ks, "chunk_params", lambda: (1400, 200, 2240))
    monkeypatch.setattr(ks, "session_scope",
                        lambda: _Ctx(_FakeSession(SimpleNamespace(id="demo", authors="Jane Doe"))))
    monkeypatch.setattr(ks.reader_service, "page_translations", lambda pid: pages)
    monkeypatch.setattr(ks.reader_service, "build_outline", lambda pid: outline)
    monkeypatch.setattr(ks.reader_service, "load_markdown_map", lambda pid: {})
    monkeypatch.setattr(ks.reader_service, "row_id_of_caption",
                        lambda pid, page, number: "p002-00" if str(number) == "1" else None)
    monkeypatch.setattr(ks.paper_service, "paper_paths", lambda pid: {"ir": "/tmp/none.sqlite"})
    monkeypatch.setattr(figure_regions, "all_regions",
                        lambda pid: {2: [{"page": 2, "kind": "table", "number": "1",
                                          "caption": "Table 1: Demo results.",
                                          "caption_block_id": "tb-caption"}]})
    monkeypatch.setattr(ks, "_block_row_ids", lambda pid, rows_: {})
    monkeypatch.setattr(ks.formula_source, "kb_formula_entries",
                        lambda pid, formulas, rows_: [{
                            "id": "f001", "formula_block_id": "f001", "latex": r"D_{\text{mse}} := \mathbb{E}\|x - Q(x)\|^2",
                            "page": 1, "row_id": "p001-05", "display": True,
                            "context": "MSE", "context_zh": "均方", "formula_number": "1",
                            "anchor_type": "page_row", "anchor_precision": "row",
                            "source_item_ids": ["f001"]}])
    import backend.parsing.ir.ir_reader as ir_reader

    monkeypatch.setattr(ir_reader, "IRReader", _FakeIR)
    return ks


def test_build_chunks_end_to_end(e2e) -> None:
    ks = e2e
    base = ks._base_chunks("demo")
    by_type: dict[str, list[dict]] = {}
    for chunk in base:
        by_type.setdefault(chunk["chunk_type"], []).append(chunk)

    # 1) 前沿元数据不进库；正文两侧同行
    texts = by_type["text"]
    assert texts, "至少要有正文块"
    assert all("jane@example.edu" not in c["content"] for c in base)
    abstract = next(c for c in texts if "Abstract" in c["content"])
    assert "We study online vector quantization" in abstract["content"]
    assert abstract["_zh"] and "我们研究流式数据的在线向量量化" in abstract["_zh"]
    assert abstract["metadata"]["section_path"] == "# Abstract"
    assert abstract["section"] == "# Abstract"

    # 2) 公式：正文里有定界符，另有原子 chunk 且**双向关联**；关联挂在**单元**上
    #    （单元是检索单位，比正文 chunk 细 —— 见 docs/语义单元索引-设计.md §5）
    method = next(c for c in texts if "1 Method" in c["content"])
    assert "$$D_{\\text{mse}}" in method["content"]
    units = by_type["unit"]
    assert units, "要建出语义单元"
    holder = next(u for u in units if "f001" in (u["metadata"].get("formula_ids") or []))
    assert holder["metadata"]["formula_ids"] == ["f001"]
    assert holder["metadata"]["parent_chunk_id"] == method["id"], "单元的父块就是它所在正文块"
    formula = by_type["formula"][0]
    assert formula["id"] == "demo:formula:f001"
    assert formula["metadata"]["text_chunk_id"] == holder["id"], "公式指回**单元**（更精确）"
    assert formula["metadata"]["section_path"] == "# 1 Method"

    # 2b) 单元层：覆盖同一批正文行（不漏不重），每条都有父块与 ±1 邻居指针（首尾为空）
    chunk_rows = [rid for c in texts for rid in c["metadata"]["row_ids"]]
    unit_rows = [rid for u in units for rid in u["metadata"]["row_ids"]]
    assert sorted(unit_rows) == sorted(chunk_rows), "单元与正文块必须覆盖同一批行"
    assert len(unit_rows) == len(set(unit_rows)), "单元之间不重叠"
    by_parent: dict[str, list[dict]] = {}
    for unit in units:
        by_parent.setdefault(unit["metadata"]["parent_chunk_id"], []).append(unit)
    for group in by_parent.values():
        assert group[0]["metadata"]["prev_unit_id"] is None
        assert group[-1]["metadata"]["next_unit_id"] is None
        for left, right in zip(group, group[1:]):
            assert left["metadata"]["next_unit_id"] == right["id"]
            assert right["metadata"]["prev_unit_id"] == left["id"]

    # 3) 表：图注 + 表体在同一条 table chunk 里，且两侧都有
    table = by_type["table"][0]
    assert "Table 1: Demo results." in table["content"]
    assert "| Full Cache | 45.0 |" in table["content"]
    assert table["_zh"] and "表 1：示例结果。" in table["_zh"]
    assert table["metadata"]["row_ids"] == ["p002-00", "p002-01"]
    assert table["metadata"]["section_kind"] == "body"

    # 4) 没有任何正文块超过 hard
    assert all(len(c["content"]) <= 2240 for c in base)

