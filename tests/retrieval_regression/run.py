"""固定检索回归集：跑一遍 10 个问题，记录"检得准不准"的可比指标。

为什么要有它：chunk 分布刚经历过大改（turboquant+attention 391 → 266 行、公式原子行去重、
附录进库）。**行数变少不等于检得更准** —— 要看固定问题上的命中情况。这份集子就是量尺：
每题记录 rank / section / 是否命中 formula atomic / 是否命中 semantic chunk / 公式上下文 /
是否命中 appendix，并给出 hit@k 与 MRR。

用法::

    # 详细报告（JSON + Markdown 表）
    python -m tests.retrieval_regression.run --out docs/retrieval-regression/after.json

    # 换成本地目录的索引（做改造前/后对比时指到临时 PR_DATA_DIR）
    python -m tests.retrieval_regression.run --env-note "before(391 rows)"

回归靠 `tests/test_retrieval_regression.py`（e2e）自动跑；本模块只负责**量化**与出报告，
不做断言，方便拿两次报告直接对比。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
QUESTIONS_PATH = HERE / "questions.json"


def load_spec(path: Path | str | None = None) -> dict:
    return json.loads(Path(path or QUESTIONS_PATH).read_text(encoding="utf-8"))


def _hit_matches(item: dict, spec: dict) -> bool:
    """这一条命中是否算"问到点子上了"：小节名**或**正文里出现期望的子串。"""
    blob = " ".join(str(x) for x in (
        item.get("section") or "", (item.get("metadata") or {}).get("section_path") or "",
        item.get("content") or "")).lower()
    for key in spec.get("expect_any_section") or []:
        if str(key).lower() in blob:
            return True
    for key in spec.get("expect_any_content") or []:
        if str(key).lower() in blob:
            return True
    return False


def _content_matches(item: dict, spec: dict) -> bool:
    """**只看正文**的命中判据。

    为什么单独量一道：小节标题（`### B.2QJL: …`）在改造前后都在库里（标题块从来没被
    改写成 ref），所以"命中 B.2 这个小节名"在改造前也能成立 —— 但**附录正文**（Lemma、证明、
    微分熵）改造前整个不在索引里。只有按正文判据量，才能看出"能检索到内容"和
    "只能检索到标题"的区别。
    """
    content = str(item.get("content") or "").lower()
    return any(str(k).lower() in content for k in (spec.get("expect_any_content") or []))


def run_question(spec: dict, *, paper_id: str, top_k: int) -> dict:
    """跑一个问题（走的是 KB 检索同一条链路：向量 + 词法 → RRF → 重排）。

    每个问题跑两遍：
    1. **混合检索**（默认，跟知识库页搜索一致）→ 看"语义上找没找到"；
    2. `want_formula` 的问题再跑一遍 **只看公式原子块**（`chunk_types=["formula"]`）→
       看"公式专项检索"是否把该公式翻出来。为什么要分开量：公式原子块正文只有 LaTeX，
       在混合检索里天然拼不过整段正文（实测只 1/6 挤进 top-6），但那不是它存在的理由 ——
       它的用途是"专门找公式"。
    """
    from backend.services.knowledge import retrieval

    result = retrieval.search(spec["q"], mode="similar", scope="paper",
                              paper_id=paper_id, top_k=top_k)
    items = result.get("items") or []
    hits = []
    for i, item in enumerate(items, 1):
        meta = item.get("metadata") or {}
        hits.append({
            "rank": i,
            "chunk_id": item.get("chunk_id"),
            "chunk_type": item.get("chunk_type"),
            "lang": item.get("lang") or "en",
            "page": item.get("page_no"),
            "section": item.get("section"),
            "section_path": meta.get("section_path"),
            "section_kind": meta.get("section_kind") or "body",
            "formula_ids": meta.get("formula_ids") or [],
            "text_chunk_id": meta.get("text_chunk_id"),
            "matched": _hit_matches(item, spec),
            "content_matched": _content_matches(item, spec),
            "preview": (str(item.get("content") or "").strip()
                        .replace("\n", " ")[:160]),
        })
    first = next((h["rank"] for h in hits if h["matched"]), None)
    first_content = next((h["rank"] for h in hits if h["content_matched"]), None)

    formula_rank = None
    formula_top = []
    if spec.get("want_formula"):
        fres = retrieval.search(spec["q"], mode="similar", scope="paper",
                                paper_id=paper_id, chunk_types=["formula"], top_k=3)
        for i, item in enumerate(fres.get("items") or [], 1):
            matched = _hit_matches(item, spec)
            formula_top.append({
                "rank": i, "chunk_id": item.get("chunk_id"),
                "lang": item.get("lang") or "en",
                "matched": matched,
                "preview": str(item.get("content") or "").strip()[:120],
            })
            if matched and formula_rank is None:
                formula_rank = i

    return {
        "id": spec["id"], "q": spec["q"], "note": spec.get("note", ""),
        "paper_id": paper_id,
        "want_formula": bool(spec.get("want_formula")),
        "want_appendix": bool(spec.get("want_appendix")),
        "first_match_rank": first,
        "content_first_match_rank": first_content,
        "matched": first is not None,
        "content_matched": first_content is not None,
        "formula_atomic": any(h["chunk_type"] == "formula" for h in hits),
        "semantic": any(h["chunk_type"] == "text" for h in hits),
        "formula_context": any(h["chunk_type"] == "text" and h["formula_ids"] for h in hits),
        "appendix": any(h["section_kind"] == "appendix" for h in hits),
        "formula_first_match_rank": formula_rank,
        "formula_top": formula_top,
        "hits": hits,
        "retrieval": result.get("retrieval") or {},
    }


def evaluate(spec: dict | None = None, *, top_k: int | None = None,
             paper_id: str | None = None) -> dict:
    spec = spec or load_spec()
    pid = paper_id or spec["paper_id"]
    k = int(top_k or spec.get("top_k") or 6)
    # 单题可用 `paper_id` 覆盖题库级默认值：题库扩到两篇论文之后，一份 spec 就能跑完
    # （改造前只有 turboquant 一篇，所以历史 10 题没有这个字段）。
    rows = [run_question(s, paper_id=(paper_id or s.get("paper_id") or pid), top_k=k)
            for s in spec["questions"]]
    n = len(rows) or 1
    mrr = sum(1.0 / r["first_match_rank"] for r in rows if r["first_match_rank"]) / n
    want_f = [r for r in rows if r["want_formula"]]
    want_a = [r for r in rows if r["want_appendix"]]
    return {
        "paper_id": pid, "top_k": k, "questions": len(rows),
        "hit_at_k": sum(1 for r in rows if r["matched"]) / n,
        "content_hit_at_k": sum(1 for r in rows if r["content_matched"]) / n,
        "mrr": round(mrr, 4),
        "content_mrr": round(
            sum(1.0 / r["content_first_match_rank"]
                for r in rows if r["content_first_match_rank"]) / n, 4),
        "formula_coverage": (sum(1 for r in want_f if r["formula_atomic"]) / len(want_f)
                             if want_f else None),
        "formula_hit_rate": (sum(1 for r in want_f if r["formula_first_match_rank"]) / len(want_f)
                             if want_f else None),
        "appendix_coverage": (sum(1 for r in want_a if r["appendix"]) / len(want_a)
                              if want_a else None),
        "formula_context_coverage": sum(1 for r in rows if r["formula_context"]) / n,
        "rows": rows,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def to_markdown(report: dict, *, env_note: str = "") -> str:
    papers = sorted({r.get("paper_id") or report["paper_id"] for r in report["rows"]})
    out = [f"### 检索回归 {env_note}".rstrip(),
           "",
           f"- 论文：{'、'.join(f'`{p}`' for p in papers)} · top_k={report['top_k']} · "
           f"题数 {report['questions']}",
           f"- hit@{report['top_k']}：**{_pct(report['hit_at_k'])}**（正文命中 "
           f"**{_pct(report['content_hit_at_k'])}**）· MRR **{report['mrr']}**"
           f"（正文 {report['content_mrr']}） · "
           f"公式专项命中 {_pct(report['formula_hit_rate'])} · "
           f"附录覆盖 {_pct(report['appendix_coverage'])} · "
           f"语义块含公式 {_pct(report['formula_context_coverage'])}",
           "",
           "| # | 论文 | 问题 | 首次命中 rank | 正文命中 rank | 公式专项 rank | 公式上下文 | appendix | 命中小节 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        sec = next((h["section"] for h in r["hits"] if h["matched"]), "—")
        fz = ("—" if not r["want_formula"]
              else (r.get("formula_first_match_rank") or "✗"))
        pid = (r.get("paper_id") or "").replace("turboquant-online-vector-iclr-26", "turbo") \
                                       .replace("attention-is-all-you-need", "attn")
        out.append(
            f"| {r['id']} | {pid} | {r['q']} | {r['first_match_rank'] or '✗'} | "
            f"{r.get('content_first_match_rank') or '✗'} | {fz} | "
            f"{'✓' if r['formula_context'] else '·'} | {'✓' if r['appendix'] else '·'} | "
            f"{(sec or '—')[:34]} |")
    return "\n".join(out) + "\n"


def compare(before: dict, after: dict) -> str:
    """两份报告逐题对比（改造前 vs 改造后）。"""
    out = ["| # | 问题 | 正文命中 rank 前→后 | 公式专项 前→后 | appendix 前→后 |",
           "|---|---|---|---|---|"]
    b = {r["id"]: r for r in before["rows"]}
    gained = lost = better = worse = 0
    for a in after["rows"]:
        row_b = b.get(a["id"], {})
        rb = row_b.get("content_first_match_rank")
        ra = a.get("content_first_match_rank")
        if ra and not rb:
            gained += 1
        elif rb and not ra:
            lost += 1
        elif rb and ra and ra < rb:
            better += 1
        elif rb and ra and ra > rb:
            worse += 1
        want_f = bool(a.get("want_formula") or row_b.get("want_formula"))
        fz = ("—" if not want_f else
              f"{row_b.get('formula_first_match_rank') or '✗'}→"
              f"{a.get('formula_first_match_rank') or '✗'}")
        appx = f"{'✓' if row_b.get('appendix') else '·'}→{'✓' if a['appendix'] else '·'}"
        out.append(f"| {a['id']} | {a['q']} | {rb or '✗'} → {ra or '✗'} | {fz} | {appx} |")
    out += ["",
            f"- **正文命中** hit@k：{before['content_hit_at_k']:.0%} → "
            f"**{after['content_hit_at_k']:.0%}**；MRR：{before['content_mrr']} → "
            f"**{after['content_mrr']}**",
            f"- 逐题变化：新增命中 **{gained}**、名次更好 {better}、名次更差 {worse}、丢失 {lost}",
            f"- 公式专项命中：{_pct(before.get('formula_hit_rate'))} → "
            f"**{_pct(after.get('formula_hit_rate'))}**；"
            f"语义块含公式：{_pct(before.get('formula_context_coverage'))} → "
            f"**{_pct(after.get('formula_context_coverage'))}**；"
            f"appendix chunk 覆盖：{_pct(before.get('appendix_coverage'))} → "
            f"**{_pct(after.get('appendix_coverage'))}**"]
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="固定检索回归集")
    ap.add_argument("--out", help="报告 JSON 路径")
    ap.add_argument("--md", help="Markdown 表路径（默认与 JSON 同名 .md）")
    ap.add_argument("--env-note", default="", help="报告标题里的环境说明")
    ap.add_argument("--questions", default=None, help="题库 JSON（默认随包）")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="不跑检索，直接对比两份已有报告")
    ap.add_argument("--compare-out", help="对比表写到哪里（Markdown）")
    args = ap.parse_args()

    if args.compare:
        before = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        after = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        table = compare(before, after)
        print(table)
        if args.compare_out:
            out = Path(args.compare_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(table, encoding="utf-8")
            print(f"对比表已写入 {out}")
        return

    report = evaluate(load_spec(args.questions), top_k=args.top_k)
    md = to_markdown(report, env_note=args.env_note)
    print(md)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path = Path(args.md) if args.md else out.with_suffix(".md")
        md_path.write_text(md, encoding="utf-8")
        print(f"报告已写入 {out} / {md_path}")


if __name__ == "__main__":
    main()
