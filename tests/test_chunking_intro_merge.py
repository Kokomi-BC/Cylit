"""引言合并**只许合并到行序上紧邻的那个块**，否则会在正文里留下"洞"。

真实事故（turboquant 路 b，p018）：段首引言（行 205、206）本来要并进后面的块，
要紧邻的那个块（207..228）超出 hard 装不下，旧实现就跳去合并了更远的块（229..242），
于是造出一个行集合为「205,206,229..242」的块 —— 校验层报 order_between_chunks，
调用方整篇退回「只按目录层级」，语义锚点白算一次。
"""

from __future__ import annotations

from backend.services.knowledge import chunking as ck


def _row(index: int, text: str | None = None) -> dict:
    return {"id": f"p001-{index:02d}", "index": index, "page": 1, "type": "para",
            "level": 0, "number": "", "original": text if text is not None else "x" * 30,
            "translation": "", "semantic_break": False}


def _node(key: str, row_index: int, *, level: int = 1, parent: str | None = None) -> dict:
    return {"key": key, "number": key, "title": key, "title_zh": "", "level": level,
            "page": 1, "row_id": f"p001-{row_index:02d}", "children": [],
            "parent": parent, "path": key, "row_index": row_index}


def test_intro_is_not_merged_into_a_farther_chunk() -> None:
    """紧邻块装不下 → 引言单独成块；行序必须连续、无洞、无覆盖缺口。"""
    # 行序:  0 1 (引言节点 A) | 2..n1 属于节点 B（超大，装不下引言）| 之后属于节点 C
    rows = [_row(i) for i in range(0, 40)]
    a = _node("A", 0)
    b = _node("B", 2)
    c = _node("C", 30)
    tree = [{"key": "root", "number": "", "title": "", "title_zh": "", "level": 0,
             "page": 1, "row_id": "p001-00", "path": "", "row_index": None,
             "parent": None, "children": [a, b, c]}]
    built = ck.build(rows, tree, target=60, hard=200, floor=200, use_breaks=False)
    chunks = built["chunks"]
    seen: list[int] = []
    for chunk in chunks:
        indices = [r["index"] for r in chunk["rows"]]
        assert indices == sorted(indices), "块内行序必须递增"
        seen.extend(indices)
    assert seen == list(range(len(rows))), "行必须恰好覆盖一次且顺序不变"
    assert built["report"]["ok"], built["report"]["errors"]
    # 引言那两行单独成块（不许被并进 30 之后那个块）
    first = chunks[0]["rows"]
    assert [r["index"] for r in first] == [0, 1], "引言装不下就该单独成块"
