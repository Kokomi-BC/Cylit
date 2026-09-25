r"""量"语义锚点到底改变了什么" —— 回答"LLM 给了语义边界，为什么指标没变好"。

三个量都要，缺一个就会得出错结论：

1. **切点集合的对称差**（arm2 vs arm3 的 chunk 起始行）—— 口径是集合，不是块数差
   （初版写成 `len(starts_arm2) - len(共同块)`，把起点/终点混在一起，见 dev-log §17.63 的更正）；
2. **锚点被吞掉的比例** —— LLM 标了 N 条边界，最终只有 M 条成为切点，其余被 target/hard 合并掉；
3. **语义单元粒度**（每单元几行）+ 不同 target 下的趋势 —— 说明"锚点有没有机会起作用"。

用法::

    python scripts/kb_anchor_effect.py
"""
from __future__ import annotations

import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.knowledge import chunking
from backend.services.knowledge.knowledge_service import chunk_params

PAPERS = ["turboquant-online-vector-iclr-26", "attention-is-all-you-need"]


def spans(chunks):
    return [(c["rows"][0]["index"], c["rows"][-1]["index"]) for c in chunks]


def edges(chunks):
    """切点集合：每个 chunk 的起始行下标（等价于"块边界"）。"""
    return {c["rows"][0]["index"] for c in chunks}


for pid in PAPERS:
    rows, tree = chunking.load(pid)
    live = [r for r in rows if not r.get("skip_reason") and not chunking._is_structural(r)]
    brk = {r["index"] for r in rows if r.get("semantic_break")}
    head = {r["index"] for r in rows if r["type"] == "heading"}
    print("=" * 100)
    print(f"# {pid}：行 {len(rows)}（正文行 {len(live)}），语义锚点行 {len(brk)}，标题行 {len(head)}")

    # 锚点密度：单元有多细？
    cur = []
    units = []
    for r in live:
        if cur and (r["index"] in brk or r["section_key"] != cur[-1]["section_key"]):
            units.append(cur)
            cur = []
        cur.append(r)
    if cur:
        units.append(cur)
    sizes = [len(u) for u in units]
    print(f"  语义单元 {len(units)} 个：每单元行数 中位 {int(statistics.median(sizes))}"
          f" / p90 {sorted(sizes)[int(len(sizes) * 0.9)]} / max {max(sizes)}；"
          f"单行单元 {sum(1 for s in sizes if s == 1)} 个")

    for target in (1400, 900, 700):
        a2, _ = chunking.assemble(rows, tree, target=target, hard=2240, floor=200,
                                  use_breaks=False)
        a3, _ = chunking.assemble(rows, tree, target=target, hard=2240, floor=200,
                                  use_breaks=True)
        s2, s3 = spans(a2), spans(a3)
        same = len(set(s2) & set(s3))
        e2, e3 = edges(a2), edges(a3)
        print(f"  target={target:<5} arm2 {len(a2)} 块（中位 "
              f"{int(statistics.median([sum(len(r['original']) for r in c['rows']) for c in a2]))}）"
              f" / arm3 {len(a3)} 块（中位 "
              f"{int(statistics.median([sum(len(r['original']) for r in c['rows']) for c in a3]))}）；"
              f"完全相同的块 {same}；**切点不同 {len(e2 ^ e3)} 个**"
              f"（arm3 切在锚点行上 {len([e for e in e3 if e in brk])} / {len(e3)}）")
