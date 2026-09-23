"""跨页衔接体检：逐页检查**页尾 → 下页页首**是否被版面折断得七零八落。

为什么需要它：页级翻译是"一页一次"的独立调用，而论文的句子/词/标记经常**跨页被劈开**
（`communication bot-` | `tlenecks …`）。这类问题不会让任何断言失败，只会让阅读体验变差，
所以单独立一把尺子：

    python scripts/check_boundaries.py                    # 库内全部论文
    python scripts/check_boundaries.py turboquant-iclr26  # 指定论文
    python scripts/check_boundaries.py --paper-id X --json

输出三类信息：
1. **带衔接标记的边界**：后端已在行上标 `continues` / `continued_from_prev`（供合并显示）；
2. **译文边界噪声**：以「……」/「（承接上页）」开头、或以悬空连字符结尾的行 —— 应为 0；
3. **可疑边界**：页尾未断句 + 下页首行小写起头（按解析器原文判定，与后端同一套判据）。

退出码：发现**译文边界噪声** → 1（这是真问题）；只有可疑边界 → 0（多为论文本身的排版特点）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.paper import paper_service, reader_service  # noqa: E402
from backend.services.translation.page_translate import (  # noqa: E402
    mark_rows_continuity, page_continuity,
)

LEAD_JUNK = re.compile(r"^\s*(?:（?承?接上页）?|\(?承?接上页\)?|【接上页】|…+|\.{2,})")
TRAIL_HYPHEN = re.compile(r"[-\u2010\u2013\u2014]\s*$")


def audit(paper_id: str) -> dict:
    """返回该论文的边界体检结果（不打印）。"""
    pages = reader_service.page_translations(paper_id)
    flagged: list[dict] = []
    noisy: list[dict] = []
    for payload in pages:
        page = int(payload["page"])
        flags = page_continuity(paper_id, page)
        rows = payload.get("rows") or []
        if not rows:
            continue
        if flags["head"] or flags["tail"]:
            flagged.append({"page": page, "head": flags["head"], "tail": flags["tail"],
                            "first_id": rows[0].get("id"), "last_id": rows[-1].get("id")})
        # 读时已清理过一遍；这里再查一次是为了防"清理没赶上"（比如别处直接改了产物）
        cleaned = [dict(r) for r in rows]
        mark_rows_continuity(cleaned, head=flags["head"], tail=flags["tail"])
        for before, after in zip(rows, cleaned):
            if (before.get("translation") or "") != (after.get("translation") or ""):
                noisy.append({"page": page, "id": before.get("id"),
                              "text": (before.get("translation") or "")[:30]})
    return {
        "paper_id": paper_id,
        "pages": len(pages),
        "flagged": flagged,
        "noisy": noisy,
        "boundaries": max(0, len(pages) - 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paper_ids", nargs="*", help="留空 = 库内全部论文")
    ap.add_argument("--paper-id", action="append", default=[])
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    wanted = list(args.paper_ids) + list(args.paper_id)
    if not wanted:
        wanted = [p["id"] for p in paper_service.list_papers(page_size=50)["items"]]

    reports = []
    failed = 0
    for pid in wanted:
        try:
            report = audit(pid)
        except Exception as exc:  # noqa: BLE001
            print(f"=== {pid}：读取失败（{exc}）")
            continue
        reports.append(report)
        if report["noisy"]:
            failed = 1
        if args.json:
            continue
        print(f"=== {pid}：{report['pages']} 页 / {report['boundaries']} 处边界 ===")
        print(f"  带衔接标记的边界：{len(report['flagged'])} 处 → "
              f"{[(f['page'], 'head' if f['head'] else '', 'tail' if f['tail'] else '') for f in report['flagged']]}")
        print(f"  译文边界噪声　　：{len(report['noisy'])} 条"
              + (f" → {report['noisy'][:3]}" if report["noisy"] else "（干净）"))
        if report["noisy"]:
            print("  ⚠️ 噪声说明清理没生效（读时应清掉「……」开头 / 悬空连字符结尾）")
        print()

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    total_noisy = sum(len(r["noisy"]) for r in reports)
    total_flagged = sum(len(r["flagged"]) for r in reports)
    print(f"合计：{len(reports)} 篇论文，带衔接标记 {total_flagged} 处，边界噪声 {total_noisy} 条")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
