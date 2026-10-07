"""两路（A 英文转录 / B 中文翻译）产物的**只读审计**：token、耗时、对齐质量、坐标落盘率。

用途：每次动"两路的同步契约"之前/之后跑一遍，用同一套数字说话（不给结论，只给数据）。

    python scripts/path_audit.py            # 全库
    python scripts/path_audit.py <paper_id> # 单篇

四组数字：

1. **单页成本**：A / B 各自的平均输入、输出 token 与 latency 中位数（判断"谁更便宜更快"）；
2. **对齐质量**：走**读模型**（`reader_service.page_products`，即两路现算合并的那份）统计
   `align`：`coords`（按解析器块号配对）/ `exact`（块数相同按序号）/ `structure`（difflib 兜底）
   / `blocks`（**块模式**：B 照 A 的块清单逐块译，第 i 项 ↔ 第 i 块），以及单侧行条数。
   ⚠️ 不看磁盘上的 `md/pages.json` —— 那可能是**融合模式**留下的旧产物，
   它的 `align` 是"一次调用出两侧"的天然对齐，与两路合并无关（踩过这个坑）。
3. **坐标契约**（P4b）：两路产物里 `src` 的落盘率（逐路 + 两侧同页都有），
   以及"项数一致"的页数（两侧块数相同 = 分块完全一致）；
4. **语义锚点**：两路各标了多少条边界（`breaks_a` / `breaks_b`，来自读模型）。
5. **块模式合规率**：B 产物里带 `blocks_from_a`（块模式产出）的页数，以及其中
   "块数与 A 的块清单**完全相等**"的页数 —— 相等才走 `pair_rows_by_blocks`，不等就退回旧兜底。
"""

from __future__ import annotations

import collections
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1] / "data" / "papers"


def _load(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("pages") or {}


def _agg(pages: dict) -> tuple[int, float, float, float]:
    vals = [v for v in pages.values() if not v.get("cached")]
    if not vals:
        return (0, 0.0, 0.0, 0.0)
    n = len(vals)
    return (n,
            sum(int(v.get("prompt_tokens") or 0) for v in vals) / n,
            sum(int(v.get("completion_tokens") or 0) for v in vals) / n,
            statistics.median([int(v.get("latency_ms") or 0) for v in vals]))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(ROOT.parents[1]))
    from backend.services.paper import reader_service
    from backend.services.translation.page_translate import split_blocks

    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    head = (f"{'paper':34} | {'A页':>3} {'A_in':>6} {'A_out':>6} {'A_ms':>6} | "
            f"{'B页':>3} {'B_in':>6} {'B_out':>6} {'B_ms':>6} | "
            f"{'coords':>6} {'exact':>5} {'struct':>6} {'blocks':>6} {'单侧':>4} | "
            f"{'src: A/B':>8} | {'锚A':>4} {'锚B':>4} | {'块模式':>6} {'合规':>5}")
    print(head)
    print("-" * len(head))
    tot = {"a": [0, 0.0, 0.0, []], "b": [0, 0.0, 0.0, []]}
    for d in sorted(ROOT.iterdir()):
        if wanted and d.name != wanted:
            continue
        md = d / "md"
        pa, pb = _load(md / "path_a.json"), _load(md / "path_b.json")
        if not (pa or pb):
            continue
        A, B = _agg(pa), _agg(pb)
        try:
            pages, shape = reader_service.page_products(d.name)
        except Exception:  # noqa: BLE001
            pages, shape = [], "?"
        align = collections.Counter(p.get("align") for p in pages)
        one = sum(int(p.get("one_sided") or 0) for p in pages)
        ba = sum(len(p.get("breaks_a") or []) for p in pages)
        bb = sum(len(p.get("breaks_b") or []) for p in pages)
        keys = sorted(set(pa) & set(pb), key=lambda k: int(k))
        rate_a = sum(1 for v in pa.values() if v.get("src")) / len(pa) if pa else 0
        rate_b = sum(1 for v in pb.values() if v.get("src")) / len(pb) if pb else 0
        # 块模式：B 是否照 A 的块清单逐块回（块数相等才叫合规 —— 不等会退回旧兜底）
        block_pages = [k for k in keys if pb[k].get("blocks_from_a")]
        block_ok = [k for k in block_pages
                    if len(pb[k].get("blocks") or [])
                    == len(split_blocks(str(pa[k].get("original_md") or "")))]

        def fmt(t):
            return (f"{t[0]:>3} {t[1]:>6.0f} {t[2]:>6.0f} {t[3]:>6.0f}") if t[0] else "  -      -      -      -"

        print(f"{d.name[:34]:34} | {fmt(A)} | {fmt(B)} | {align.get('coords', 0):>6} "
              f"{align.get('exact', 0):>5} {align.get('structure', 0):>6} "
              f"{align.get('blocks', 0):>6} {one:>4} | "
              f"{rate_a:>3.0%}/{rate_b:<3.0%} | {ba:>4} {bb:>4}   "
              f"[{shape} {len(pages)} 页] | {len(block_pages):>6} {len(block_ok):>5}")
        for key, t, pages_all in (("a", A, pa), ("b", B, pb)):
            if t[0]:
                tot[key][0] += t[0]
                tot[key][1] += t[1] * t[0]
                tot[key][2] += t[2] * t[0]
                tot[key][3].extend(int(v.get("latency_ms") or 0)
                                   for v in pages_all.values() if not v.get("cached"))
    for key, label in (("a", "A 转录"), ("b", "B 翻译")):
        n, pin, pout, lat = tot[key]
        if n:
            print(f"{label}：{n} 页，均输入 {pin / n:.0f} tok，均输出 {pout / n:.0f} tok，"
                  f"合计 {((pin + pout) / n):.0f} tok/页，latency 中位 {statistics.median(lat):.0f} ms")

    # 坐标契约（P4b）到底生效了几页：`coords_pair` 要求**两侧同一页都带 src**
    print("\n坐标契约（P4b）：`src` = 模型申报的「每个输出块覆盖了哪几个解析器块号」；"
          "只有**两侧同一页都有且校验通过**，配对才按块号走（`coords_pair`）")
    print(f"{'paper':34} {'共同页':>5} {'A有src':>7} {'B有src':>7} {'两侧都有':>7} "
          f"{'只A':>5} {'只B':>5} {'都没有':>6} {'项数一致':>7}")
    for d in sorted(ROOT.iterdir()):
        if wanted and d.name != wanted:
            continue
        md = d / "md"
        pa, pb = _load(md / "path_a.json"), _load(md / "path_b.json")
        keys = sorted(set(pa) & set(pb), key=lambda k: int(k))
        if not keys:
            continue
        both = [k for k in keys if pa[k].get("src") and pb[k].get("src")]
        only_a = [k for k in keys if pa[k].get("src") and not pb[k].get("src")]
        only_b = [k for k in keys if pb[k].get("src") and not pa[k].get("src")]
        none = [k for k in keys if not pa[k].get("src") and not pb[k].get("src")]
        same_len = [k for k in both if len(pa[k]["src"]) == len(pb[k]["src"])]
        print(f"{d.name[:34]:34} {len(keys):>5} {len(both) + len(only_a):>7} "
              f"{len(both) + len(only_b):>7} {len(both):>7} {len(only_a):>5} {len(only_b):>5} "
              f"{len(none):>6} {len(same_len):>7}")


if __name__ == "__main__":
    main()
