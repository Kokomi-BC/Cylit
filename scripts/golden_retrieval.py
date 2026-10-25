"""page 级 golden set 的**验收**侧：跑一遍，看这些查询该看到的页有没有被检索给到。

指标（每个 case 一行）：
- `recall@k` = |期望页 ∩ 前 k 条落到的页| / |期望页| —— 该看到的页漏了几页；
- `noise`   = 前 k 条里落在期望页之外的页数 —— 名额被不相关页占了几个。

为什么两个都要：只报 recall 时，"全部返回 20 条覆盖一切"也能拿满分；只报 noise 时，
"只返回 1 条精确命中"看着最干净，但该看的页一个没给。

用法（主后端要跑着；token 默认读 data/secrets/agent.token）：

    python scripts/golden_retrieval.py --base https://fakenewsdect...  # 或 http://127.0.0.1:8710
    python scripts/golden_retrieval.py --k 10 --verbose

退出码：任一条 recall < JSON 里的 `min_recall` → 1（可以直接挂进 CI 或发布前跑一遍）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "golden" / "retrieval_pages.json"


def _search(base: str, token: str, paper_id: str, query: str, mode: str, k: int) -> dict:
    body = json.dumps({"query": query, "top_k": k, "mode": mode}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/agent/papers/" + paper_id + "/search",
        data=body,
        headers={"Content-Type": "application/json", "X-Internal-Token": token})
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.load(resp)
    return payload.get("data") or {}


def check(spec: dict, base: str, token: str, *, k: int, verbose: bool) -> int:
    paper_id = spec["paper_id"]
    floor = float(spec.get("min_recall") or 0.0)
    rows: list[dict] = []
    failed = 0
    for case in spec["cases"]:
        expect = {int(p) for p in case.get("expect_pages") or []}
        data = _search(base, token, paper_id, case["query"], case.get("mode") or "text", k)
        items = list(data.get("items") or [])
        got = [int(item.get("page_no") or 0) for item in items]
        hit = {p for p in got if p in expect}
        recall = len(hit) / len(expect) if expect else 1.0
        noise = len([p for p in got if p not in expect])
        # 块级：某页某节**应有几块**（定理/引理链这类小节天然有 3~4 块）。
        # 页级 recall 抓不到这件事：p16 只要回来一块，页就算命中了。
        groups: list[dict] = []
        for want in case.get("expect_groups") or []:
            page = int(want.get("page") or 0)
            prefix = str(want.get("section_prefix") or "")
            need = int(want.get("expect") or 0)
            # 按「位置」数，不按行数：同一段的中英两行（`…:en` / `…:zh`）算**一个位置**，
            # 否则 p16 只回来定义一行也被数成 2 块，分母与分子一起虚高（外面指出过这一点）。
            # 位置键 = 页 + 这条 chunk 覆盖的**起始行**（`p016-08-p016-16` → `p016-08`）。
            seen = set()
            for item in items:
                if int(item.get("page_no") or 0) != page:
                    continue
                if not str(item.get("section") or "").startswith(prefix):
                    continue
                parts = str(item.get("chunk_id") or "").split(":")
                span = parts[3] if len(parts) > 3 else ""
                first = "-".join(span.split("-")[:2])   # `p016-08-p016-14` → `p016-08`
                seen.add(first or str(item.get("chunk_id") or ""))
            groups.append({"page": page, "section_prefix": prefix, "expect": need,
                           "got": len(seen)})
        rows.append({"query": case["query"], "expect": sorted(expect), "got": got,
                     "recall": recall, "noise": noise, "groups": groups,
                     # 目标值只打印、不判失败：现状未达标的护栏先别变红，
                     # 否则所有人学会"看见红就调低门槛"。
                     "target_blocks": case.get("target_block_recall"),
                     "returned": int(data.get("returned") or 0)})
        if recall + 1e-9 < floor:
            failed += 1
        for group in groups:
            floor_blocks = case.get("min_block_recall")
            if floor_blocks is None or not group["expect"]:
                continue
            if group["got"] / group["expect"] + 1e-9 < float(floor_blocks):
                failed += 1
    print("%-26s %-6s %-8s %-6s %s" % ("query", "ret", "recall", "noise", "页（前 k 条）"))
    for row in rows:
        mark = "" if row["recall"] + 1e-9 >= floor else "  ← 低于 %.2f" % floor
        print("%-26s %-6d %-8.2f %-6d %s%s"
              % (row["query"], row["returned"], row["recall"], row["noise"],
                 row["got"], mark))
        for group in row["groups"]:
            print("    %s p%d %s：应有 %d 块，回来 %d 块%s"
                  % (row["query"], group["page"], group["section_prefix"],
                     group["expect"], group["got"],
                     "（目标 %.2f）" % row.get("target_blocks", 0)
                     if row.get("target_blocks") else ""))
        if verbose:
            print("    期望页：%s  命中：%s" % (row["expect"], sorted(set(row["got"]) & set(row["expect"]))))
    total = sum(r["recall"] for r in rows) / len(rows) if rows else 0.0
    print("\n%d 条：平均 recall %.2f，低于下限 %d 条（min_recall=%.2f，k=%d）"
          % (len(rows), total, failed, floor, k))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑 page 级 golden set（recall@k / noise）")
    ap.add_argument("--base", default="http://127.0.0.1:8710/api/v1",
                    help="主后端地址，**含 API 前缀**（/api/v1）")
    ap.add_argument("--token-file", default=str(ROOT / "data" / "secrets" / "agent.token"))
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--k", type=int, default=0, help="只看前 k 条；默认取 JSON 里的 k")
    ap.add_argument("--verbose", action="store_true", help="打印期望页与命中页")
    args = ap.parse_args(argv)
    spec = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    k = args.k or int(spec.get("k") or 10)
    return check(spec, args.base, token, k=k, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
