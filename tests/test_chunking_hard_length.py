"""hard 的两把尺子必须一致：装配按"各行长度之和"，校验按"真文本（行间空行连接）"。

实测（turboquant 路 b，p017-00）：17 行的块装配算出 2215 ≤ hard(2240)、校验按真文本算出
2247 > hard —— 差的就是 16 个 "\n\n"。于是"装配通过、复核超标"，调用方先把锚点全退掉、
再报一次 ERROR（同一篇里那两条 ERROR 就是这么来的）。
"""

from __future__ import annotations

from backend.services.knowledge import chunking as ck

HARD = 2240
TARGET = 1400


def _row(index: int, size: int) -> dict:
    return {"id": f"p001-{index:02d}", "index": index, "page": 1, "type": "para",
            "level": 0, "number": "", "original": "x" * size, "translation": "",
            "semantic_break": False}


def _tree(row_index: int, key: str = "1") -> list[dict]:
    return [{"key": key, "number": key, "title": key, "title_zh": "", "level": 1,
             "page": 1, "row_id": f"p001-{row_index:02d}", "children": [], "parent": None,
             "path": key, "row_index": row_index}]


def test_chunk_text_with_separators_never_exceeds_hard() -> None:
    # 17 行 × 130 = 2210（≤ 2240），加 16 个空行 = 2242（> 2240）：正是那个 7 字超标的形状
    rows = [_row(i, 130) for i in range(17)]
    built = ck.build(rows, _tree(0), target=TARGET, hard=HARD, floor=200, use_breaks=False)
    over = [c for c in built["chunks"]
            if len("\n\n".join(c["rows"][i]["original"] for i in range(len(c["rows"])))) > HARD]
    assert over == [], "按真文本量，任何块都不许超过 hard"
    kinds = [e["kind"] for e in built["report"]["errors"]]
    assert "over_hard" not in kinds, built["report"]["errors"]
    assert built["report"]["ok"], built["report"]["errors"]


def test_assembled_chunks_still_cover_every_row() -> None:
    rows = [_row(i, 130) for i in range(17)]
    built = ck.build(rows, _tree(0), target=TARGET, hard=HARD, floor=200, use_breaks=False)
    seen = sorted(r["index"] for c in built["chunks"] for r in c["rows"])
    assert seen == list(range(17))
