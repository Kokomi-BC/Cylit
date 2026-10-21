"""断点当**块的硬边界**（``break_walls``）：默认口径的不变量 + 与旧合并口径的差异。

旧口径（``break_walls=False``）里，一个 TOC 子树只要装得进 target 就整棵成一块 —— 模型给的
断点虽然把**单元**切细了，随后又被合并回去。实测真实语料（turboquant，路 a）「只切
``use_breaks`` 开/关，块集合一字不差、跨断点块 27 个」，所以"语义分块"当时只是口号。

默认口径（``break_walls=True``）加一条硬约束：**块的边界必须是本路断点** —— 任何一条 chunk
内部（首行之外）都不许出现断点行。没有断点时（模型不给锚点 / ``use_breaks=False``）行为与
改造前**逐字一致**，这是它敢默认打开的前提。
"""

from __future__ import annotations

from backend.services.knowledge import chunking

from tests.test_kb_row_chunking import _rows, _tree  # noqa: E402


def _paper(monkeypatch, rows: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    rows = rows if rows is not None else _rows()
    tree = _tree(rows)
    monkeypatch.setattr(chunking, "row_units", lambda pid, **kw: rows)
    monkeypatch.setattr(chunking, "section_tree", lambda pid, rws=None: tree)
    return rows, tree


def _assemble(rows, tree, **kw):
    params = {"target": 1200, "hard": 2000, "floor": 200, "use_breaks": True}
    params.update(kw)
    return chunking.assemble(rows, tree, **params)


def _layout(chunks) -> list[list[str]]:
    return [[r["id"] for r in c["rows"]] for c in chunks]


def _crossing(chunks) -> int:
    return sum(1 for c in chunks if any(chunking.is_break(r) for r in c["rows"][1:]))


# --------------------------------------------------------------------------
# 1. 默认口径：断点就是块的首行
# --------------------------------------------------------------------------


def test_default_mode_never_crosses_a_break(monkeypatch):
    rows, tree = _paper(monkeypatch)
    chunks, stats = _assemble(rows, tree)          # 不传 break_walls → 默认开
    assert _crossing(chunks) == 0
    assert stats["break_cuts"] == 2               # p001-04、p002-08 各切一刀
    # 1.1 的整棵子树（p001-02..p001-04）装得进 target，但内部有断点 → 不许整棵成一块
    assert _layout(chunks) == [
        ["p001-00", "p001-01", "p001-02", "p001-03"],
        ["p001-04"],
        ["p001-05", "p001-06"],
        ["p002-07"],
        ["p002-08", "p002-09"],
    ]


def test_legacy_mode_still_merges_across_a_break(monkeypatch):
    """对照组：关掉 walls 才允许跨断点 —— 上面那条断言因此是有牙的。"""
    rows, tree = _paper(monkeypatch)
    chunks, stats = _assemble(rows, tree, break_walls=False)
    assert stats["break_cuts"] == 0
    assert _crossing(chunks) == 2
    assert len(chunks) == 3


def test_intro_is_not_merged_when_the_child_chunk_starts_at_a_break(monkeypatch):
    """引言并入首个子块**不许把断点挤到块内部**：宁可引言自己成一块。"""
    rows = _rows()
    rows[2]["semantic_break"] = True              # 1.1 的首行就是断点
    rows, tree = _paper(monkeypatch, rows)
    chunks, stats = _assemble(rows, tree)
    assert stats["intro_merged"] == 0 and stats["intro_alone"] == 1
    assert ["p001-00", "p001-01"] in _layout(chunks)
    assert _crossing(chunks) == 0


# --------------------------------------------------------------------------
# 2. 没有断点时逐字不变（默认打开的前提）
# --------------------------------------------------------------------------


def test_paper_without_breaks_is_byte_identical(monkeypatch):
    rows = _rows()
    for row in rows:
        row["semantic_break"] = False
    _paper(monkeypatch, rows)
    rows, tree = rows, _tree(rows)
    walls_on, _ = _assemble(rows, tree)
    walls_off, _ = _assemble(rows, tree, break_walls=False)
    assert _layout(walls_on) == _layout(walls_off)


def test_use_breaks_false_ignores_walls(monkeypatch):
    """``use_breaks=False`` 是对照臂：墙上开关不许改写它的划分。"""
    rows, tree = _paper(monkeypatch)
    on, _ = _assemble(rows, tree, use_breaks=False)
    off, _ = _assemble(rows, tree, use_breaks=False, break_walls=False)
    assert _layout(on) == _layout(off)


# --------------------------------------------------------------------------
# 3. 按路：只有本路标的断点才算墙
# --------------------------------------------------------------------------


def test_only_this_road_s_break_is_a_wall(monkeypatch):
    rows = _rows()
    rows[4]["semantic_break_side"] = "original"   # p001-04 是 A 路标的
    _paper(monkeypatch, rows)
    tree = _tree(rows)
    road_a = chunking.build(rows, tree, target=1200, hard=2000, floor=200, road="a")
    road_b = chunking.build(rows, tree, target=1200, hard=2000, floor=200, road="b")
    assert len(road_a["chunks"]) > len(road_b["chunks"])
    assert sum(1 for c in road_a["chunks"]
               if any(chunking.is_break(r, "a") for r in c["rows"][1:])) == 0


# --------------------------------------------------------------------------
# 4. 校验层：硬边界不许把覆盖 / 顺序 / 不跨节弄坏
# --------------------------------------------------------------------------


def test_validate_passes_in_default_mode(monkeypatch):
    rows, tree = _paper(monkeypatch)
    built = chunking.build(rows, tree, target=1200, hard=2000, floor=200)
    assert built["report"]["ok"], built["report"]["errors"]
    assert built["stats"]["chunks"] == len(built["chunks"])
