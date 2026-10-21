"""对照：把「断点当块的硬边界」（break_walls）开/关，块会差多少 —— 只算不写库。

为什么要有这个脚本：改造前实测「同一份文本只切 use_breaks 开/关，四组块集合一字不差」，
说明锚点只切细了单元、块边界没动。这个脚本按路分别量四件事：

1. 块数 / 长度分布（p50 / mean / max）—— 硬边界会不会把块切碎；
2. 跨断点的块数（块内部含本路断点行）—— 硬边界下必须是 0，否则改造没生效；
3. 纯标题块数（只由标题行组成且短于 floor）—— 标题被断点孤立出来的代价；
4. 校验结论（validate）—— 硬边界不许把「覆盖 / 顺序 / 不跨节」弄坏。

用法::

    python scripts/kb_break_walls_compare.py            # 库里所有论文
    python scripts/kb_break_walls_compare.py <paper_id>...
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(ROOT))

from backend.repositories.mysql.models import Paper  # noqa: E402
from backend.repositories.mysql.session import session_scope  # noqa: E402
from backend.services.knowledge import chunking  # noqa: E402
from backend.services.knowledge import knowledge_service as ks  # noqa: E402

ROADS = ("a", "b")


def _papers(argv: list[str]) -> list[tuple[str, str]]:
    with session_scope() as sess:
        query = sess.query(Paper)
        if argv:
            query = query.filter(Paper.id.in_(argv))
        return [(p.id, p.authors or "") for p in query.order_by(Paper.id.asc()).all()]


def _size(chunk: dict, road: str) -> int:
    return len("\n\n".join(chunking.road_text(r, road) for r in chunk["rows"]))


def _crossing(chunks: list[dict], road: str) -> int:
    """块内部（首行之外）含本路断点行的块数 —— 硬边界下应为 0。"""
    return sum(1 for c in chunks
               if any(chunking.is_break(r, road) for r in c["rows"][1:]))


def _heading_only(chunks: list[dict], road: str, floor: int) -> int:
    """只由标题行组成、且短于 floor 的块（断点把标题孤立出来的代价）。"""
    return sum(1 for c in chunks
               if all(r.get("type") == "heading" for r in c["rows"])
               and _size(c, road) < floor)


def _stats(chunks: list[dict], road: str) -> str:
    sizes = sorted(_size(c, road) for c in chunks) or [0]
    return (f"n={len(sizes)} p50={statistics.median(sizes):.0f} "
            f"mean={statistics.mean(sizes):.0f} max={max(sizes)}")


def main(argv: list[str]) -> int:
    target, floor, hard = ks.chunk_params()
    use_breaks = ks.using_breaks()
    print(f"chunk_params=({target}, {floor}, {hard})  use_breaks={use_breaks}")
    for paper_id, authors in _papers(argv):
        rows, tree = chunking.load(paper_id, authors=authors)
        if not rows:
            print(f"\n== {paper_id}: 没有可用行，跳过")
            continue
        print(f"\n== {paper_id}（行 {len(rows)}，目录节点 {len(chunking._walk(tree))}）")
        for road in ROADS:
            breaks = sum(1 for r in rows if chunking.is_break(r, road))
            print(f"  -- 路 {road}：本路断点 {breaks} 行")
            for walls in (False, True):
                built = chunking.build(rows, tree, target=target, hard=hard, floor=floor,
                                       use_breaks=use_breaks, break_walls=walls, road=road)
                chunks = built["chunks"]
                report = built["report"]
                verdict = "ok" if report["ok"] else str(report["errors"][:2])
                print(f"     walls={str(walls):5} {_stats(chunks, road)}"
                      f"  跨断点 {_crossing(chunks, road)}"
                      f"  纯标题块 {_heading_only(chunks, road, floor)}"
                      f"  校验 {verdict}"
                      f"  断点切分 {built['stats']['break_cuts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
