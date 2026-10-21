"""目录树对不上正文行时，**正文块不能整层为空**。

真实故障形态：页级产物里没有 level 1-4 的 heading 行 → build_outline 退回
_outline_from_index（解析块 id）→ 那些 id 与行 id 不同源，node.row_index 全是 None
→ _own_rows 一律返回 []，pack_node 的「not subtree → 只递归子节点」把整篇静默跳完，
chunks 为空而调用方只打一行 ERROR 就入库（审计 2026-12-04 的 P0）。
"""

from __future__ import annotations

from backend.services.knowledge import chunking as ck


def _rows(n: int = 6) -> list[dict]:
    return [{"id": f"p001-0{i}", "index": i, "page": 1, "type": "para", "level": 0,
             "number": "", "original": "这一行有一些正文，" + "内容" * 40,
             "translation": "", "semantic_break": False} for i in range(n)]


def _node(row_index: int | None) -> dict:
    return {"key": "1", "number": "1", "title": "Intro", "title_zh": "", "level": 1,
            "page": 1, "row_id": "blk-1", "children": [], "parent": None, "path": "1",
            "row_index": row_index}


def test_tree_that_matches_no_row_still_produces_chunks() -> None:
    rows = _rows()
    chunks, _stats = ck.assemble(rows, [_node(None)], target=200, hard=600, floor=50)
    assert chunks, "目录对不上任何行时必须退回合成节点，不能整篇没有正文块"
    covered = sorted(r["index"] for ch in chunks for r in ch["rows"])
    assert covered == list(range(len(rows))), "降级路径也要把每一行都覆盖到"


def test_tree_that_matches_rows_is_kept() -> None:
    rows = _rows()
    chunks, _stats = ck.assemble(rows, [_node(0)], target=200, hard=600, floor=50)
    assert chunks
    covered = sorted(r["index"] for ch in chunks for r in ch["rows"])
    assert covered == list(range(len(rows)))


def test_locates_rows_detects_dead_tree() -> None:
    rows = _rows(3)
    assert ck._locates_rows([_node(None)], rows) is False
    assert ck._locates_rows([_node(0)], rows) is True
    assert ck._locates_rows([_node(99)], rows) is False, "行下标越界也算认不出行"


def test_synthetic_tree_covers_whole_paper() -> None:
    rows = _rows(3)
    tree = ck._synthetic_tree(rows)
    assert len(tree) == 1 and tree[0]["row_index"] == 0 and tree[0]["level"] == 1
