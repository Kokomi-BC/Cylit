"""两路全量重跑 + 时间线：触发 `paths=[a,b]` 并等到两路都结束，打印逐页时间线与用量。

这是 P4（对齐门）与"同一次调用 vs 两路并发"对照实验的执行工具：它只负责**跑**，
报告交给 `scripts/two_path_align_report.py`。

用法::

    python scripts/two_path_run.py                                   # 两篇演示论文，全部页
    python scripts/two_path_run.py --paper turboquant-online-vector-iclr-26
    python scripts/two_path_run.py --paper <id> --pages 3,4          # 只跑指定页
    python scripts/two_path_run.py --paper <id> --paths b --force --blocks-from-a
                                                                     # 块模式重译 B 路（A 定块）
    python scripts/two_path_run.py --check-only                      # 只看当前状态，不触发

为什么必须**一篇一篇跑**：两路各占一个 worker（`task_worker_count=3`），
同时跑两篇就是四个任务抢三个 worker，会有一条路排队等待 —— 那就不是"两路并发"了，
时间线也就不可比。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8710/api/v1"
PAPERS = ["turboquant-online-vector-iclr-26", "attention-is-all-you-need"]


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def run_paper(paper_id: str, pages: list[int] | None, *, force: bool,
              timeout: float, paths: list[str] | None = None,
              blocks_from_a: bool = False) -> dict:
    body = {"paths": paths or ["a", "b"], "mode": "retranslate" if force else "resume"}
    if blocks_from_a:
        body["blocks_from_a"] = True
    if pages:
        body["pages"] = pages
    res = call("POST", f"/papers/{paper_id}/translate", body)
    data = res["data"]
    ids = set(data["task_ids"])
    print(f"\n=== {paper_id}：{res.get('message')}  {sorted(ids)}")
    t0 = time.time()
    rows: list[dict] = []
    last_line = ""
    while time.time() - t0 < timeout:
        tasks = [t for t in call("GET", "/tasks?limit=50")["data"]["items"]
                 if t["id"] in ids]
        line = "  ｜ ".join(
            f"{t['type']:14} {t['status']:10} {t['done_items']}/{t['total_items']}"
            for t in sorted(tasks, key=lambda x: x["type"]))
        if line != last_line:
            print(f"  {time.time() - t0:6.1f}s  {line}", flush=True)
            last_line = line
        if len(tasks) == len(ids) and all(
                t["status"] in ("completed", "failed", "cancelled") for t in tasks):
            rows = tasks
            break
        time.sleep(5)

    print(f"  用时 {time.time() - t0:.1f}s")
    for t in sorted(rows, key=lambda x: x["type"]):
        stats = t.get("stats") or {}
        usage = stats.get("usage") or {}
        print(f"    {t['type']:14} {t['status']:10} "
              f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} "
              f"cache_hit={usage.get('cache_hit_tokens')} 用时={stats.get('elapsed_s')}s")
    return {"paper_id": paper_id, "tasks": rows, "seconds": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="append")
    ap.add_argument("--pages", default="", help="逗号分隔的页号（只跑这些页）")
    ap.add_argument("--paths", default="a,b", help="逗号分隔：a / b / fused（默认 a,b）")
    ap.add_argument("--blocks-from-a", action="store_true",
                    help="块模式（只对 B 路生效）：B 照 A 的块清单逐块译，见 BLOCK_TRANSLATE_SYSTEM")
    ap.add_argument("--force", action="store_true", help="忽略已有产物重跑")
    ap.add_argument("--timeout", type=float, default=3600.0, help="每篇论文的上限秒数")
    ap.add_argument("--check-only", action="store_true", help="只打印当前两路进度")
    ap.add_argument("--out", default="data/logs/snapshot/two-path-run.json")
    args = ap.parse_args()

    papers = args.paper or PAPERS
    pages = [int(p) for p in args.pages.split(",") if p.strip()] or None
    paths = [p.strip() for p in args.paths.split(",") if p.strip()] or ["a", "b"]

    if args.check_only:
        for pid in papers:
            r = call("GET", f"/papers/{pid}/reader")["data"]
            stats = r["stats"]
            print(f"{pid:40} pages_done={stats['pages_done']}/{stats['pages']} "
                  f"align={stats['align']}")
            shapes = sorted({p.get("source") for p in r["page_translations"]})
            print(f"    形态={shapes} 断点={len(r['page_translations'])} 页")
        return 0

    results = [run_paper(pid, pages, force=args.force, timeout=args.timeout,
                         paths=paths, blocks_from_a=args.blocks_from_a)
               for pid in papers]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n时间线 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
